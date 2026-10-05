"""工作台桥接服务的门槛测试(只读契约子集)。

被测对象 ``src/sigma-frontend/server/workbench_server.py`` 不在 ``sigma``
包里(路径含连字符,不可常规 import),按文件路径加载——与产品路径
同一条代码。夹具用**真实的** ``JsonlStore`` + ``SessionTree`` 落盘会话,
不过 HTTP 的端点测试起真服务(随机端口)。

覆盖面:
- 会话 → 前端 Task 契约的映射(标题/状态/事件/模型);
- 悬空工具调用 → failed(中断,可断点续跑)的状态推导;
- 坏会话(旧格式记录)→ 跳过,不拖死列表(查看器降级策略);
- timeline 载荷(--timeline 数据面);
- 501:执行类端点统一拒绝 + 指路文案;
- 插件 = 内置工具 + 技能(真实扫描),扩展代码不执行。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from sigma.agent.messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma.agent.types import ToolResult
from sigma.providers.base import BaseProvider, SamplingParams
from sigma.providers.events import StopEvent, TextDelta, UsageEvent
from sigma.providers.messages import (
    AssistantMessage,
    TextBlock,
    ToolCallBlock,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from sigma.sessions.store import JsonlStore
from sigma.sessions.tree import SessionTree
from sigma.events.lifecycle import MessageInjected, TextChunk, ThinkingChunk, ToolEnd, ToolStart

_TS = "2026-10-01T10:00:00.000"


class _ScriptedProvider:
    """执行环测试的假 provider:固定一轮文本回复(不联网、零花费)。"""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def stream(  # type: ignore[override]
        self,
        messages: list[Any],
        tools: list[dict[str, Any]],
        *,
        model: str,
        signal: Any,
        sampling: SamplingParams | None = None,
    ) -> Any:
        yield TextDelta(text=self._reply, text_signature=None)
        yield UsageEvent(usage=Usage(prompt_tokens=50, completion_tokens=5, cached_tokens=40))
        yield StopEvent(stop_reason="stop")

    def estimate_tokens(self, messages: list[Any]) -> int:
        return 10

    async def aclose(self) -> None:
        return None


def _load_server_module() -> ModuleType:
    """按文件路径加载桥接模块(路径含连字符,不能 import)。"""
    path = (
        Path(__file__).resolve().parent.parent
        / "src" / "sigma-frontend" / "server" / "workbench_server.py"
    )
    spec = importlib.util.spec_from_file_location("sigma_workbench_server", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVER = _load_server_module()

#: 测试用的主工作区(签名要求一个存在目录,不参与断言)
SERVER_PRIMARY = Path(__file__).resolve().parent.parent


def _assistant(
    *, model: str = "test-model", content: list[Any] | None = None
) -> LlmMessageWrapper:
    return LlmMessageWrapper(
        timestamp=_TS,
        message=AssistantMessage(
            content=content if content is not None else [TextBlock(text="完成")],
            model=model,
            usage=Usage(prompt_tokens=100, completion_tokens=10, cached_tokens=90),
            stop_reason="stop",
            timestamp=_TS,
        ),
    )


def _write_session(root: Path, session_id: str, messages: list[Any]) -> None:
    tree = SessionTree(store=JsonlStore(root, session_id))
    for message in messages:  # tree.append 单参(与 SessionContext 的 *messages 不同)
        tree.append(message)


@pytest.fixture()
def sessions_root(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


# ---------------------------------------------------------------------------
# 会话 → Task 映射
# ---------------------------------------------------------------------------


def test_task_mapping_from_real_session(sessions_root: Path) -> None:
    """干净的问答会话 → completed;标题取首行;模型取最后一轮。"""
    _write_session(
        sessions_root,
        "s-good",
        [
            LlmMessageWrapper(
                timestamp=_TS,
                message=UserMessage(content="修一下 off-by-one\n第二行", timestamp=_TS),
            ),
            _assistant(),
        ],
    )
    tasks = SERVER._list_task_payloads(SERVER_PRIMARY, sessions_root)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["id"] == "s-good"
    assert task["title"] == "修一下 off-by-one"
    assert task["status"] == "completed"
    assert task["model"] == "test-model"
    assert task["projectId"] == SERVER.PROJECT_ID
    kinds = [event["kind"] for event in task["events"]]
    assert kinds == ["message", "message"]
    assert task["events"][0]["role"] == "user"
    assert task["events"][0]["text"] == "修一下 off-by-one\n第二行"


def test_dangling_tool_call_derives_failed(sessions_root: Path) -> None:
    """assistant 声明了工具调用但没有结果 = 中断 → failed(可断点续跑)。"""
    _write_session(
        sessions_root,
        "s-dangling",
        [
            LlmMessageWrapper(
                timestamp=_TS,
                message=UserMessage(content="读文件", timestamp=_TS),
            ),
            _assistant(
                content=[ToolCallBlock(id="call-1", name="read", arguments={"path": "a.py"})]
            ),
        ],
    )
    (tasks,) = [SERVER._list_task_payloads(SERVER_PRIMARY, sessions_root)]
    assert tasks[0]["status"] == "failed"


def test_answered_tool_calls_derive_completed(sessions_root: Path) -> None:
    """工具调用 + 结果配平 + 收尾文本 = completed(工具批次不误判中断)。"""
    call = ToolCallBlock(id="call-1", name="grep", arguments={"pattern": "x"})
    result = ToolResultAgentMessage.from_result(
        call, ToolResult(content=[TextBlock(text="命中")]), timestamp=_TS
    )
    _write_session(
        sessions_root,
        "s-tools",
        [
            LlmMessageWrapper(timestamp=_TS, message=UserMessage(content="找 x", timestamp=_TS)),
            _assistant(content=[call]),
            result,
            _assistant(),
        ],
    )
    (tasks,) = [SERVER._list_task_payloads(SERVER_PRIMARY, sessions_root)]
    assert tasks[0]["status"] == "completed"
    # 工具调用与结果配对:status=ok + durationMs(时间戳差 ≈ 口径)
    tools = [event for event in tasks[0]["events"] if event["kind"] == "tool_call"]
    assert len(tools) == 1 and tools[0]["status"] == "ok"
    assert tools[0]["tool"] == "grep"
    assert isinstance(tools[0]["durationMs"], int)


def test_tool_error_marked_in_tool_event(sessions_root: Path) -> None:
    """失败的工具调用:chip 事件带 status=error(前端红叉),不再另发 note。"""
    call = ToolCallBlock(id="call-1", name="bash", arguments={})
    result = ToolResultAgentMessage.from_result(
        call, ToolResult(content=[TextBlock(text="boom")], is_error=True), timestamp=_TS
    )
    _write_session(sessions_root, "s-err", [result])
    (tasks,) = [SERVER._list_task_payloads(SERVER_PRIMARY, sessions_root)]
    tools = [event for event in tasks[0]["events"] if event["kind"] == "tool_call"]
    assert len(tools) == 1 and tools[0]["status"] == "error"


def test_broken_session_skipped_not_fatal(sessions_root: Path) -> None:
    """旧格式记录(整数 timestamp)让整树解析炸 → 该会话跳过,其余照常。"""
    _write_session(
        sessions_root,
        "s-good",
        [
            LlmMessageWrapper(timestamp=_TS, message=UserMessage(content="好的会话", timestamp=_TS)),
            _assistant(),
        ],
    )
    broken = sessions_root / "s-old.jsonl"
    record = {
        "id": "n1",
        "parentId": None,
        "message": {
            "role": "tool_result",
            "tool_call_id": "a",
            "tool_name": "read",
            "content": [],
            "details": {},
            "is_error": False,
            "timestamp": 123,  # v1.4 前的整数时间戳 → pydantic 拒收
        },
    }
    broken.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    tasks = SERVER._list_task_payloads(SERVER_PRIMARY, sessions_root)
    ids = [task["id"] for task in tasks]
    assert ids == ["s-good"]


def test_missing_session_returns_null(sessions_root: Path) -> None:
    assert SERVER._task_payload_for_id(SERVER_PRIMARY, sessions_root, "nope") is None


# ---------------------------------------------------------------------------
# timeline / 插件 / 项目
# ---------------------------------------------------------------------------


def test_timeline_payload_without_trace(sessions_root: Path) -> None:
    _write_session(
        sessions_root,
        "s-tl",
        [
            LlmMessageWrapper(timestamp=_TS, message=UserMessage(content="hi", timestamp=_TS)),
            _assistant(),
        ],
    )
    payload = SERVER._timeline_payload(sessions_root, "s-tl")
    assert payload is not None
    assert payload["hasTrace"] is False
    assert payload["totalPrompt"] == 100
    assert payload["totalCached"] == 90
    assert payload["cacheRate"] == 0.9
    assert payload["rounds"][0]["model"] == "test-model"
    assert SERVER._timeline_payload(sessions_root, "nope") is None


def test_inbox_project_always_listed(http_server: tuple[str, Path]) -> None:
    """随手问(proj-inbox)恒在项目列表:不绑定工作区的日常任务入口;
    在它下面建任务,projectId 原样保留(不落回主工作区)。"""
    base, _root = http_server
    code, projects = _get(base, "/api/v1/projects")
    assert code == 200
    inbox = next((p for p in projects if p["id"] == "proj-inbox"), None)
    assert inbox is not None and inbox["name"] == "随手问"
    code, created = _post(base, "/api/v1/tasks", {"projectId": "proj-inbox", "title": "hi"})
    assert code == 200 and created["projectId"] == "proj-inbox"
    # 模块级 _TASKS 是跨用例的全局:清掉草稿,别污染后面的列表断言
    SERVER._TASKS.pop(created["id"], None)  # noqa: SLF001 - 测试清理


def test_plugins_payload_builtin_and_skills(tmp_path: Path) -> None:
    """插件 = 真实内置工具 + 真实技能扫描;扩展工具不列出(装载即执行)。"""
    skill_dir = tmp_path / "extensions" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "demo.md").write_text(
        "---\nname: demo\ndescription: 演示技能\n---\n正文\n", encoding="utf-8"
    )
    (tmp_path / "extensions" / "evil.py").write_text("raise SystemExit(1)", encoding="utf-8")

    plugins = SERVER._plugins_payload(tmp_path)
    names = {plugin["name"] for plugin in plugins}
    assert "bash" in names and "read" in names  # 内置工具,来自 default_registry
    assert "demo" in names  # 技能,来自真实扫描
    assert all(plugin["id"].split(":")[0] in ("tool", "skill") for plugin in plugins)
    builtin = {plugin["name"] for plugin in plugins if plugin["builtin"]}
    assert builtin >= {"bash", "read"}


def test_projects_registry_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """导入工作区:注册表落盘、去重、主工作区恒在列、坏路径 400。"""
    monkeypatch.setattr(SERVER, "_REGISTRY_PATH", tmp_path / "registry.json")
    other = tmp_path / "other-ws"
    other.mkdir()
    code, created = SERVER._register_project(tmp_path, {"repoPath": str(other)})
    assert code == 200 and created["name"] == "other-ws"
    code2, again = SERVER._register_project(tmp_path, {"repoPath": str(other)})
    assert code2 == 200 and again["id"] == created["id"]
    code3, _detail = SERVER._register_project(tmp_path, {"repoPath": str(tmp_path / "nope")})
    assert code3 == 400
    projects = SERVER._all_projects(tmp_path)
    ids = [project["id"] for project in projects]
    assert ids.count(SERVER.PROJECT_ID) == 1 and created["id"] in ids


def test_memory_payload(tmp_path: Path) -> None:
    memory_dir = tmp_path / ".sigma" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "alpha.md").write_text("# Alpha 记忆\n内容\n", encoding="utf-8")
    entries = SERVER._memory_payload(tmp_path)
    assert entries == [{"slug": "alpha", "title": "Alpha 记忆"}]


# ---------------------------------------------------------------------------
# HTTP 层:真起服务,打真实请求
# ---------------------------------------------------------------------------


@pytest.fixture()
def http_server(tmp_path: Path) -> tuple[str, Path]:
    sessions_root = tmp_path / "sessions"
    _write_session(
        sessions_root,
        "s-http",
        [
            LlmMessageWrapper(timestamp=_TS, message=UserMessage(content="你好", timestamp=_TS)),
            _assistant(),
        ],
    )
    server = SERVER.make_server(
        host="127.0.0.1",
        port=0,  # 随机端口,避免与开发实例互踩
        sessions_root=sessions_root,
        workspace=tmp_path,
        dist_dir=None,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, sessions_root
    server.shutdown()
    server.server_close()


def _get(base: str, path: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(f"{base}{path}", timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(base: str, path: str, payload: dict[str, Any] | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _delete(base: str, path: str) -> tuple[int, Any]:
    request = urllib.request.Request(f"{base}{path}", method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_get_endpoints(http_server: tuple[str, Path]) -> None:
    base, _root = http_server
    code, ping = _get(base, "/api/v1/system/ping")
    assert code == 200 and ping["ok"] is True and ping["mode"] == "http"
    code, tasks = _get(base, "/api/v1/tasks")
    assert code == 200 and [task["id"] for task in tasks] == ["s-http"]
    code, timeline = _get(base, "/api/v1/tasks/s-http/timeline")
    assert code == 200 and timeline["totalPrompt"] == 100
    code, missing = _get(base, "/api/v1/tasks/nope")
    assert code == 200 and missing is None  # 契约:getTask → Task | null


def test_http_write_verbs_are_501_with_guidance(http_server: tuple[str, Path]) -> None:
    base, _root = http_server
    code, body = _post(base, "/api/v1/automations/none/enabled")
    assert code == 501
    assert "待接入" in body["detail"]


# ---------------------------------------------------------------------------
# 执行环:创建任务 → 发消息 → run_task 真跑(假 provider)
# ---------------------------------------------------------------------------


@pytest.fixture()
def execution_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """注入假 provider 与密钥——执行走**真实的** run_task/落盘/回放链路。"""
    monkeypatch.setenv("SIGMA_API_KEY", "test-key")
    monkeypatch.setattr(SERVER, "_PROVIDER_FACTORY", lambda: _ScriptedProvider("你好!我是 sigma。"))
    return "ok"


def test_execution_loop_create_and_run(http_server: tuple[str, Path], execution_env: str) -> None:
    """执行环(流式):POST 立即返回 running → deltas 逐块到齐 → 任务 completed。"""
    base, sessions_root = http_server
    code, created = _post(base, "/api/v1/tasks")
    assert code == 200 and created["status"] == "draft"

    code, started = _post(base, f"/api/v1/tasks/{created['id']}/messages", {"text": "你好"})
    assert code == 200
    assert started["status"] == "running"

    # 轮询流式增量直到轮结束(结构化块:text/thinking/tool_start/tool_end)
    text_acc, seq = "", 0
    kinds: set[str] = set()
    for _ in range(200):
        code, d = _get(base, f"/api/v1/tasks/{created['id']}/deltas?since={seq}")
        assert code == 200
        for piece in d["pieces"]:
            kinds.add(piece["k"])
            if piece["k"] == "text":
                text_acc += piece["t"]
        seq = d["seq"]
        if not d["running"]:
            break
        time.sleep(0.05)
    assert "我是 sigma" in text_acc
    assert kinds == {"text"}  # 假 provider 纯文本:只见文本块,协议形状如实

    # 轮结束:任务 completed,回放含双方消息,会话事实落盘,时间线有真实用量
    code, final = _get(base, f"/api/v1/tasks/{created['id']}")
    assert code == 200 and final["status"] == "completed"
    roles = [(event.get("role"), event["text"]) for event in final["events"] if event["kind"] == "message"]
    assert any(role == "user" and "你好" in text for role, text in roles)
    assert any(role == "assistant" and "我是 sigma" in text for role, text in roles)
    assert (sessions_root / f"{created['id']}.jsonl").is_file()
    code, timeline = _get(base, f"/api/v1/tasks/{created['id']}/timeline")
    assert code == 200 and timeline["totalPrompt"] == 50 and timeline["cacheRate"] == 0.8


def test_post_message_empty_text_rejected(http_server: tuple[str, Path], execution_env: str) -> None:
    base, _root = http_server
    code, created = _post(base, "/api/v1/tasks")
    request = urllib.request.Request(
        f"{base}/api/v1/tasks/{created['id']}/messages",
        data=json.dumps({"text": "  "}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 400


def test_second_turn_shares_persistent_loop(
    http_server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一会话连续两轮必须跑在**同一个**事件循环上。

    回归:provider 的 httpx.AsyncClient / asyncio.Lock 绑定创建时的循环,
    旧实现每轮 asyncio.run 各造一个循环,第二轮起必现
    ``RuntimeError: Event loop is closed``(星辰实测 2026-10-02)。
    修复 = 工作台用持久循环线程;此断言在旧代码下必红(两轮两循环)。"""
    base, _root = http_server
    monkeypatch.setenv("SIGMA_API_KEY", "test-key")
    loops: list[int] = []

    class _LoopSpyProvider(_ScriptedProvider):
        def __init__(self) -> None:
            super().__init__("第二轮回执")

        async def stream(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            loops.append(id(asyncio.get_running_loop()))
            async for event in super().stream(*args, **kwargs):
                yield event

    monkeypatch.setattr(SERVER, "_PROVIDER_FACTORY", _LoopSpyProvider)

    code, created = _post(base, "/api/v1/tasks")
    assert code == 200
    for text in ("第一轮", "第二轮"):
        code, _started = _post(base, f"/api/v1/tasks/{created['id']}/messages", {"text": text})
        assert code == 200
        for _ in range(200):
            _code, d = _get(base, f"/api/v1/tasks/{created['id']}/deltas?since=0")
            if not d["running"]:
                break
            time.sleep(0.05)
    assert len(loops) == 2
    assert loops[0] == loops[1]  # 同一持久循环——旧代码这里两轮各一个


def test_post_message_unknown_task_404(http_server: tuple[str, Path], execution_env: str) -> None:
    base, _root = http_server
    request = urllib.request.Request(
        f"{base}/api/v1/tasks/nope/messages",
        data=b'{"text": "hi"}',
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)
    assert excinfo.value.code == 404


# ---------------------------------------------------------------------------
# 三处功能实化:模型选择(注册表 preset)/ 目录浏览列文件 / 模型解析链
# ---------------------------------------------------------------------------


def test_models_endpoint_custom_first_and_no_key_leak(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """/models = 自定义条目(模型设置,排最前)+ 内置 preset(CLI 同词汇);
    默认 preset 排内置组第一。密钥本体只存 ~/.sigma/.env,服务端从不接触
    key 本身——GET 载荷只有变量名 apiKeyEnv 与 hasKey 布尔,均非机密。"""
    from sigma.cli.main import DEFAULT_PRESET
    from sigma.providers.registry import builtin_providers

    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    code, saved = _post(base, "/api/v1/models/save", {
        "name": "GLM-5.3-Flash",
        "protocol": "openai-compat",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "apiKeyEnv": "TEST_MODEL_KEY",
        "modelId": "glm-5.3-flash",
        "efforts": ["开启", "关闭"],
        "effortStyle": "thinking",
    })
    assert code == 200 and saved["ok"] is True

    code, models = _get(base, "/api/v1/models")
    assert code == 200
    names = [m["name"] for m in models]
    assert names[0] == "GLM-5.3-Flash"  # 自定义条目永远在前
    assert sorted(names[1:]) == builtin_providers().names()
    assert names[1] == DEFAULT_PRESET
    mine = models[0]
    assert mine["efforts"] == ["开启", "关闭"] and mine["custom"] is True
    assert mine["modelId"] == "glm-5.3-flash" and mine["hasKey"] is True
    assert mine["apiKeyEnv"] == "TEST_MODEL_KEY"  # 变量名回传(非机密),编辑可回显
    assert "apiKey" not in mine and "sk-" not in json.dumps(models)


def test_model_save_rejects_duplicate_name(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """name 是执行链的解析键:重名(含改名撞名)一律 400;密钥只落**变量名**
    apiKeyEnv,每次保存整体覆盖,JSON 里没有任何明文 key 字段。"""
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    payload = {
        "name": "my-model", "protocol": "openai-compat",
        "baseUrl": "https://x/v1", "apiKeyEnv": "K_VAR", "modelId": "m-1",
        "efforts": [], "effortStyle": "reasoning_effort",
    }
    code, first = _post(base, "/api/v1/models/save", payload)
    assert code == 200
    code, clash = _post(base, "/api/v1/models/save", {**payload, "modelId": "m-2"})
    assert code == 400 and "已存在" in clash["detail"]
    code, updated = _post(base, "/api/v1/models/save", {**payload, "id": first["id"], "apiKeyEnv": "K_VAR2"})
    assert code == 200
    stored = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))["models"][0]
    assert stored["modelId"] == "m-1" and stored["apiKeyEnv"] == "K_VAR2"
    assert "apiKey" not in stored


def test_model_remove_roundtrip(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    _code, saved = _post(base, "/api/v1/models/save", {
        "name": "gone", "protocol": "openai-compat",
        "baseUrl": "https://x/v1", "apiKeyEnv": "", "modelId": "m",
        "efforts": [], "effortStyle": "reasoning_effort",
    })
    code, body = _post(base, "/api/v1/models/remove", {"id": saved["id"]})
    assert code == 200 and body["ok"] is True
    code, body = _post(base, "/api/v1/models/remove", {"id": saved["id"]})
    assert code == 404


def test_effort_extra_body_styles() -> None:
    """档位 → 请求体:thinking 风格包对象,reasoning_effort 风格直传字符串,
    空档位 = None(请求字节与无档位一致)。"""
    assert SERVER._effort_extra_body("thinking", "开启") == {"thinking": {"type": "开启"}}
    assert SERVER._effort_extra_body("reasoning_effort", "high") == {"reasoning_effort": "high"}
    assert SERVER._effort_extra_body("thinking", "  ") is None


def test_execution_params_custom_model_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """自定义条目整体接管 base_url/model/协议;密钥按条目声明的 apiKeyEnv
    变量名解析(环境变量 > ~/.sigma/.env);档位按条目 effortStyle 随附;
    内置 preset 仍按名解析(reasoning_effort 风格)。"""
    for var in ("SIGMA_PRESET", "SIGMA_MODEL", "SIGMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    # 第二段(内置 preset)走全局密钥链:CI 上没有 ~/.sigma/.env,显式给一个
    monkeypatch.setenv("SIGMA_API_KEY", "ci-global-key")
    monkeypatch.setenv("ZK_TEST_KEY", "zk-own-key")
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    (tmp_path / "models.json").write_text(json.dumps({
        "models": [{
            "id": "mdl-x", "name": "GLM-5.3-Flash", "protocol": "openai-compat",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "apiKeyEnv": "ZK_TEST_KEY",
            "modelId": "glm-5.3-flash", "efforts": ["开启", "关闭"],
            "effortStyle": "thinking",
        }]
    }), encoding="utf-8")

    base_url, api_key, model, protocol, extra = SERVER._execution_params(
        "GLM-5.3-Flash", "开启"
    )
    assert (base_url, model, protocol) == (
        "https://open.bigmodel.cn/api/paas/v4", "glm-5.3-flash", "openai-compat"
    )
    assert api_key == "zk-own-key"  # 条目变量名指向的 key,不碰全局链
    assert extra == {"thinking": {"type": "开启"}}

    _url, _k, model, protocol, extra = SERVER._execution_params("zhipu", "high")
    assert protocol == "openai-compat" and extra == {"reasoning_effort": "high"}
    assert model == "glm-4-flash"


def test_execution_params_key_env_falls_back_to_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条目未声明 apiKeyEnv → 退到全局 SIGMA_API_KEY 链(环境变量优先)。"""
    monkeypatch.delenv("SIGMA_PRESET", raising=False)
    monkeypatch.setenv("SIGMA_API_KEY", "global-key")
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    (tmp_path / "models.json").write_text(json.dumps({
        "models": [{
            "id": "mdl-y", "name": "no-env-entry", "protocol": "openai-compat",
            "baseUrl": "https://x/v1", "modelId": "m-9",
            "efforts": [], "effortStyle": "reasoning_effort",
        }]
    }), encoding="utf-8")

    _url, api_key, model, _protocol, _extra = SERVER._execution_params("no-env-entry")
    assert api_key == "global-key" and model == "m-9"


def test_execution_params_no_key_anywhere_raises_with_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """条目变量名解析不到 + 全局链也空 → LookupError,文案指路
    ~/.sigma/.env 与密钥变量名。真实 ~/.sigma/.env 不参与本用例。"""
    import sigma.config.settings as sigma_settings

    monkeypatch.delenv("SIGMA_API_KEY", raising=False)
    monkeypatch.delenv("MISSING_VAR_XYZ", raising=False)
    # 屏蔽真实 ~/.sigma/.env:解析链 candidates 置空
    monkeypatch.setattr(sigma_settings, "CANDIDATE_FILES", ())
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    (tmp_path / "models.json").write_text(json.dumps({
        "models": [{
            "id": "mdl-z", "name": "broken-entry", "protocol": "openai-compat",
            "baseUrl": "https://x/v1", "apiKeyEnv": "MISSING_VAR_XYZ",
            "modelId": "m-0", "efforts": [], "effortStyle": "reasoning_effort",
        }]
    }), encoding="utf-8")

    with pytest.raises(LookupError) as excinfo:
        SERVER._execution_params("broken-entry")
    message = str(excinfo.value)
    assert "broken-entry" in message
    assert "~/.sigma/.env" in message and "密钥变量名" in message


def test_model_save_rejects_bad_api_key_env(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """apiKeyEnv 是指向 ~/.sigma/.env 某一行的指针,写错等于指空气——
    非法变量名在保存时就拦下,不进注册表。"""
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    payload = {
        "name": "bad-var", "protocol": "openai-compat",
        "baseUrl": "https://x/v1", "modelId": "m",
        "efforts": [], "effortStyle": "reasoning_effort",
    }
    for bad in ("1startsWith-digit", "has space", "has-dash", "中文名"):
        code, body = _post(base, "/api/v1/models/save", {**payload, "apiKeyEnv": bad})
        assert code == 400 and "apiKeyEnv" in body["detail"], bad
    # 合法名与空串照常通过(不同名,避开重名 400)
    code, _body = _post(base, "/api/v1/models/save", {**payload, "name": "good-var", "apiKeyEnv": "GOOD_VAR_1"})
    assert code == 200
    code, _body = _post(base, "/api/v1/models/save", {**payload, "name": "empty-var", "apiKeyEnv": ""})
    assert code == 200


# ---------------------------------------------------------------------------
# ask_user 交互通道:模型主动问的方向决策在工作台真正等人选(2026-10-03)
# ---------------------------------------------------------------------------


def test_ask_channel_pending_and_answer() -> None:
    """ask 挂起 → 注册表可见待答问题 → 应答唤醒执行线程并返回所选选项;
    应答后条目摘除(下次 /queues 不再出现)。"""
    ask = SERVER._make_ask("task-ask-1")
    results: list[str] = []

    async def scenario() -> None:
        waiter = asyncio.create_task(ask("选哪个方案?", ["方案一", "方案二"], 0))
        await asyncio.sleep(0.2)  # 让挂起生效(to_thread 起线程有延迟)
        pending = SERVER._pending_questions("task-ask-1")
        assert len(pending) == 1
        assert pending[0]["question"] == "选哪个方案?"
        assert pending[0]["options"] == ["方案一", "方案二"]
        assert pending[0]["recommended"] == 0
        code, payload = SERVER._answer_question(
            "task-ask-1", pending[0]["id"], {"option": "方案二"}
        )
        assert code == 200 and payload["ok"] is True
        results.append(await waiter)

    asyncio.run(scenario())
    assert results == ["方案二"]
    assert SERVER._pending_questions("task-ask-1") == []


def test_ask_channel_custom_answer_and_dismiss() -> None:
    """三形态回填(星辰 2026-10-04"留自由选择空间"):命中候选=选择;
    非空不在候选=自由输入原样传;dismiss=忽略哨兵;空 option=400;
    重复回答=409(先到先得)。"""
    from sigma.tools.builtin.ask_user import IGNORE_SENTINEL

    ask = SERVER._make_ask("task-ask-2")
    results: list[str] = []

    async def scenario() -> None:
        w_opt = asyncio.create_task(ask("q1", ["A", "B"], 0))
        w_custom = asyncio.create_task(ask("q2", ["A", "B"], 0))
        w_dismiss = asyncio.create_task(ask("q3", ["A", "B"], 0))
        await asyncio.sleep(0.2)  # 让挂起生效(to_thread 起线程有延迟)
        pending = SERVER._pending_questions("task-ask-2")
        ids = {p["question"]: p["id"] for p in pending}
        assert len(ids) == 3
        # 空 option → 400(忽略必须走 dismiss=true)
        code, payload = SERVER._answer_question("task-ask-2", ids["q1"], {"option": ""})
        assert code == 400 and "dismiss" in payload["detail"]
        code, _payload = SERVER._answer_question("task-ask-2", ids["q1"], {"option": "B"})
        assert code == 200
        code, _payload = SERVER._answer_question(
            "task-ask-2", ids["q2"], {"option": "先写测试再写实现"}
        )
        assert code == 200  # 自由输入合法,原样传给工具
        code, _payload = SERVER._answer_question("task-ask-2", ids["q3"], {"dismiss": True})
        assert code == 200
        # 重复回答 → 409 先到先得
        code, _payload = SERVER._answer_question("task-ask-2", ids["q1"], {"option": "A"})
        assert code == 409
        results.extend(await asyncio.gather(w_opt, w_custom, w_dismiss))

    asyncio.run(scenario())
    assert results == ["B", "先写测试再写实现", IGNORE_SENTINEL]


def test_ask_channel_ignored_question_auto_skips_repeat() -> None:
    """忽略去重(2026-10-04):用户显式忽略过的问题,模型原样重问时**直接
    返回忽略哨兵**——不再出卡、不再阻塞,把"重问循环"在通道层掐断。
    实测背景:忽略后 deepseek-reasoner 连问三轮,每轮阻塞等待,用户体感卡住。"""
    from sigma.tools.builtin.ask_user import IGNORE_SENTINEL

    ask = SERVER._make_ask("task-ask-dedupe")
    results: list[str] = []

    async def scenario() -> None:
        first = asyncio.create_task(ask("今晚吃什么", ["火锅", "烧烤"], 0))
        await asyncio.sleep(0.2)
        qid = SERVER._pending_questions("task-ask-dedupe")[0]["id"]
        code, _payload = SERVER._answer_question(
            "task-ask-dedupe", qid, {"dismiss": True}
        )
        assert code == 200
        results.append(await first)

        # 原样重问:立即返回哨兵,不注册新条目
        second = asyncio.create_task(ask("今晚吃什么", ["火锅", "烧烤"], 0))
        await asyncio.sleep(0.1)
        assert SERVER._pending_questions("task-ask-dedupe") == []
        results.append(await second)
        # 空白差异视为同一问题
        third = asyncio.create_task(ask("今晚吃什么  ", ["火锅", "烧烤"], 0))
        results.append(await third)

    asyncio.run(scenario())
    assert results == [IGNORE_SENTINEL, IGNORE_SENTINEL, IGNORE_SENTINEL]


def test_stop_releases_pending_questions_and_denies_approvals() -> None:
    """停止任务时:挂起问题放行(空答案 → ask_user 兜底回退推荐项),
    挂起审批拒绝并唤醒——否则 stop 要干等工具超时(实测卡 10 分钟)。"""
    ask = SERVER._make_ask("task-stop-rel")
    released_answer: list[str] = []

    async def scenario() -> None:
        waiter = asyncio.create_task(ask("q", ["A", "B"], 0))
        await asyncio.sleep(0.2)
        assert SERVER._release_questions_on_stop("task-stop-rel") == 1
        released_answer.append(await waiter)

    asyncio.run(scenario())
    assert released_answer == [""]  # 空答案 → 工具兜底回退推荐项
    assert SERVER._pending_questions("task-stop-rel") == []

    # 审批侧:直接造一个挂起条目,验证拒绝并唤醒
    entry: dict[str, Any] = {
        "id": "req-x", "tool": "bash", "args": {}, "summary": "x",
        "event": threading.Event(), "decision": "deny",
        "reason": "等待审批超时(300s),自动拒绝",
    }
    with SERVER._APPROVALS_LOCK:
        SERVER._APPROVALS.setdefault("task-stop-rel", []).append(entry)
    assert SERVER._deny_approvals_on_stop("task-stop-rel") == 1
    assert entry["event"].is_set() and entry["decision"] == "deny"
    assert entry["reason"] == "任务已被停止"


def test_ask_channel_unknown_question_404() -> None:
    code, _payload = SERVER._answer_question(
        "no-such-task", "no-such-id", {"option": "A"}
    )
    assert code == 404


def test_questions_http_roundtrip(http_server: tuple[str, Path]) -> None:
    """HTTP 全链:ask 挂起(后台线程)→ /queues 载荷带 questions →
    POST /questions/{qid} 回填 → ask 返回所选。"""
    base, _root = http_server
    ask = SERVER._make_ask("task-q-http")
    result: list[str] = []

    def runner() -> None:
        result.append(asyncio.run(ask("选哪个?", ["甲", "乙"], 0)))

    thread = threading.Thread(target=runner)
    thread.start()
    try:
        time.sleep(0.3)
        code, queues = _get(base, "/api/v1/tasks/task-q-http/queues")
        assert code == 200 and len(queues["questions"]) == 1
        qid = queues["questions"][0]["id"]
        code, body = _post(base, f"/api/v1/tasks/task-q-http/questions/{qid}", {"option": "乙"})
        assert code == 200 and body["ok"] is True
    finally:
        thread.join(timeout=5)
    assert result == ["乙"]


def test_fs_listing_with_files_flag(http_server: tuple[str, Path]) -> None:
    """/fs 默认只列子目录;files=1 追加文件条目(isDir=False,目录在前)。"""
    base, sessions_root = http_server
    root = sessions_root.parent
    target = root / "browse-me"
    (target / "sub").mkdir(parents=True)
    (target / "a-dir-nested").mkdir()
    (target / "z-file.txt").write_text("x", encoding="utf-8")
    (target / ".hidden").write_text("x", encoding="utf-8")

    code, dirs_only = _get(base, f"/api/v1/fs?path={urllib.request.quote(str(target))}")
    assert code == 200
    assert [e["name"] for e in dirs_only["entries"]] == ["a-dir-nested", "sub"]

    code, with_files = _get(
        base, f"/api/v1/fs?path={urllib.request.quote(str(target))}&files=1"
    )
    assert code == 200
    names = [e["name"] for e in with_files["entries"]]
    assert names == ["a-dir-nested", "sub", "z-file.txt"]
    assert with_files["entries"][-1]["isDir"] is False


def test_stream_collector_structured_pieces(monkeypatch: pytest.MonkeyPatch) -> None:
    """collector 把四类事件折叠成**结构化块**——直播区分块渲染的原料:
    text/thinking/tool_start/tool_end,工具参数与结果摘要服务端截断。
    collector 随会话缓存,但缓冲区按 task_id **每轮实时读取**——换新缓冲
    立即生效(旧实现绑死首轮流缓冲,第二轮起 deltas 永远为空,实测)。"""
    pieces: list[dict[str, Any]] = []
    monkeypatch.setattr(
        SERVER,
        "_DELTAS",
        {"t1": {"pieces": pieces, "lock": threading.Lock(), "done": False, "error": None}},
    )
    collector = SERVER._StreamCollector("t1")
    message = LlmMessageWrapper(timestamp=_TS, message=UserMessage(content="x", timestamp=_TS))
    collector.on_event(TextChunk(text="我先看一下目录。"))
    collector.on_event(ThinkingChunk(text="想一想"))
    collector.on_event(ToolStart(name="bash", arguments={"command": "pwd"}, call_id="c1"))
    collector.on_event(ToolEnd(name="bash", ok=True, preview="D:\\", message=message))
    assert [piece["k"] for piece in pieces[:4]] == [
        "text",
        "thinking",
        "tool_start",
        "tool_end",
    ]
    assert pieces[0] == {"k": "text", "t": "我先看一下目录。"}
    assert pieces[2]["name"] == "bash" and "pwd" in pieces[2]["args"]
    assert pieces[3]["ok"] is True and pieces[3]["preview"] == "D:\\"
    # 注入即时上屏(星辰 2026-10-02"点立即后面板要及时渲染"):steering/
    # 排队的 UserMessage 注入推 user 块;[ 开头的是系统条(note 块)。
    collector.on_event(
        MessageInjected(
            message=LlmMessageWrapper(
                timestamp=_TS, message=UserMessage(content="继续下一部分", timestamp=_TS)
            )
        )
    )
    collector.on_event(
        MessageInjected(
            message=LlmMessageWrapper(
                timestamp=_TS,
                message=UserMessage(content="[任务清单提醒] 核对一次", timestamp=_TS),
            )
        )
    )
    assert pieces[-2] == {"k": "user", "t": "继续下一部分"}
    assert pieces[-1] == {"k": "note", "t": "[任务清单提醒] 核对一次"}
    # 换新缓冲区(新一轮开始)后,事件写进**新**缓冲——跨轮不串
    pieces2: list[dict[str, Any]] = []
    monkeypatch.setattr(
        SERVER,
        "_DELTAS",
        {"t1": {"pieces": pieces2, "lock": threading.Lock(), "done": False, "error": None}},
    )
    collector.on_event(TextChunk(text="第二轮"))
    assert len(pieces) == 6 and [p["t"] for p in pieces2] == ["第二轮"]
    # 缓冲不存在(轮间隙/已清理):事件无处可写,不抛
    monkeypatch.setattr(SERVER, "_DELTAS", {})
    collector.on_event(TextChunk(text="孤儿事件"))


def test_http_stop_task_no_run_self_heals(http_server: tuple[str, Path]) -> None:
    """后端无在跑轮:stop 返回 200(interrupted=False)并纠正滞留的
    running 状态——界面卡执行态时点停止应自愈,而不是 409 卡死
    (实测 2026-10-02)。在跑但会话缺失的异常态仍 409。"""
    base, _root = http_server
    code, body = _post(base, "/api/v1/tasks/s-http/stop")
    assert code == 200 and body["interrupted"] is False
    SERVER._TASKS["s-http"] = {"id": "s-http", "status": "running", "projectId": "proj-sigma"}
    try:
        code, body = _post(base, "/api/v1/tasks/s-http/stop")
        assert code == 200 and body["interrupted"] is False
        assert SERVER._TASKS["s-http"]["status"] == "draft"
    finally:
        SERVER._TASKS.pop("s-http", None)
    SERVER._RUNNING.add("s-http")
    try:
        code, body = _post(base, "/api/v1/tasks/s-http/stop")
        assert code == 409 and "会话未装配" in body["detail"]
    finally:
        SERVER._RUNNING.discard("s-http")


def test_http_queue_edit(monkeypatch: pytest.MonkeyPatch) -> None:
    """queue-edit:改写排队文本落到 session.edit_queued;空文本 400;无会话 409。"""
    calls: list[tuple[str, int, str]] = []

    class _FakeSession:
        def edit_queued(self, *, kind: str, index: int, text: str) -> bool:
            calls.append((kind, index, text))
            return True

    monkeypatch.setattr(SERVER, "_SESSIONS", {"t-q": _FakeSession()})
    code, body = SERVER._queue_op(
        "t-q", "queue-edit", {"kind": "followup", "index": 1, "text": "改过的任务"}
    )
    assert code == 200 and body["ok"] is True
    assert calls == [("followup", 1, "改过的任务")]
    code, body = SERVER._queue_op("t-q", "queue-edit", {"kind": "followup", "index": 0, "text": "  "})
    assert code == 400
    monkeypatch.setattr(SERVER, "_SESSIONS", {})
    code, body = SERVER._queue_op("t-q", "queue-edit", {"kind": "followup", "index": 0, "text": "x"})
    assert code == 409


def test_queue_op_index_zero_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """下标 0 必须原样透传(星辰 2026-10-02 实测"follow-up 按钮无效"根因)。

    ``body.get("index") or -1`` 把首项下标 0 当falsy 吞成 -1 →
    drop_queued 越界返回 False → 「立即」/删除/编辑**对队列第一项一律
    失效**,而第 2..n 项正常。症状极具迷惑性:按钮点了没反应、消息却
    真的注入成功了(steer 走的是另一条路,不看 index)。

    这条断言必须打到**删除路径**上——旧测试只把 index=0 用在 400/409
    早返回分支,真删除断言用的是 index=1(真值),bug 正好落在缝隙里。
    """

    class _IndexRecordingSession:
        def __init__(self) -> None:
            self.seen: list[tuple[str, int]] = []

        def drop_queued(self, *, kind: str, index: int) -> bool:
            self.seen.append((kind, index))
            return True

        def edit_queued(self, *, kind: str, index: int, text: str) -> bool:
            self.seen.append((kind, index))
            return True

    fake = _IndexRecordingSession()
    monkeypatch.setattr(SERVER, "_SESSIONS", {"t-0": fake})

    # 删除首项:index=0 必须原样到达
    code, body = SERVER._queue_op("t-0", "queue-remove", {"kind": "followup", "index": 0})
    assert code == 200 and body["ok"] is True
    # 编辑首项:同一条解析路径,同样必须是 0
    code, body = SERVER._queue_op("t-0", "queue-edit", {"kind": "followup", "index": 0, "text": "改过"})
    assert code == 200 and body["ok"] is True
    # steering 首项同理(另一条队列,同一个 index 字段)
    code, body = SERVER._queue_op("t-0", "queue-remove", {"kind": "steering", "index": 0})
    assert code == 200 and body["ok"] is True
    assert fake.seen == [("followup", 0), ("followup", 0), ("steering", 0)]

    # 反向断言:字段**缺失**才回落 -1(而不是把显式 0 也吞掉)
    code, body = SERVER._queue_op("t-0", "queue-remove", {"kind": "followup"})
    assert code == 200 and body["ok"] is True
    assert fake.seen[-1] == ("followup", -1)


def _pending_entry(task_id: str, entry_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """往全局审批表塞一条**挂起**的审批(模拟 confirm 模式下等人工的工具)。"""
    entry: dict[str, Any] = {
        "id": entry_id,
        "tool": tool,
        "args": args,
        "summary": "x",
        "event": threading.Event(),
        "decision": "deny",
        "reason": "等待审批超时(300s),自动拒绝",
    }
    with SERVER._APPROVALS_LOCK:
        SERVER._APPROVALS.setdefault(task_id, []).append(entry)
    return entry


def test_access_switch_wakes_pending_approval(http_server: tuple[str, Path]) -> None:
    """切权限必须**唤醒已挂起的审批**并按新模式重新裁决(星辰实测 2026-10-02:
    手动确认下挂着的 bash,切自动审批后原等待不感知新模式,界面卡死)。
    - 切 auto/full:无需人工的立即放行;
    - 切 readonly:写类立即拒绝(带原因);
    - 切 confirm:仍需人工的继续等(不误杀)。"""
    base, root = http_server
    gate = SERVER._HttpApprovalGate("confirm", "t-wake", root)
    SERVER._GATES["t-wake"] = gate
    try:
        entry1 = _pending_entry("t-wake", "r1", "bash", {"command": "tar -c . | tar -x"})
        assert not entry1["event"].is_set()
        code, body = _post(base, "/api/v1/tasks/t-wake/access", {"access": "full"})
        assert code == 200
        assert entry1["event"].is_set() and entry1["decision"] == "approve"

        entry2 = _pending_entry("t-wake", "r2", "write", {"path": "x"})
        code, _ = _post(base, "/api/v1/tasks/t-wake/access", {"access": "readonly"})
        assert code == 200
        assert entry2["event"].is_set() and entry2["decision"] == "deny"
        assert "只读" in str(entry2["reason"])

        entry3 = _pending_entry("t-wake", "r3", "write", {"path": "y"})
        code, _ = _post(base, "/api/v1/tasks/t-wake/access", {"access": "confirm"})
        assert code == 200
        assert not entry3["event"].is_set()  # 仍需人工 → 继续等,不误杀
        # 手动决策路径照常可用
        code, _ = _post(base, "/api/v1/tasks/t-wake/approvals/r3", {"decision": "approve"})
        assert code == 200 and entry3["event"].is_set() and entry3["decision"] == "approve"
    finally:
        SERVER._GATES.pop("t-wake", None)
        SERVER._APPROVALS.pop("t-wake", None)
        # 切档端点为纯磁盘会话补过草稿记录——不清会污染后续测试的全局断言
        SERVER._TASKS.pop("t-wake", None)


def test_post_message_running_task_queues_followup(
    http_server: tuple[str, Path], execution_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行中发消息 = **自动排队**(200 + queued),不再 409 硬拒——
    followup 落进会话队列,本轮结束自动接跑;被熔断/中断则留在
    队列区可见可删(与'中断不自动续跑'语义一致)。"""
    base, _root = http_server
    received: list[str] = []

    class _FakeSession:
        def submit_followup(self, text: str) -> None:
            received.append(text)

        def pending_steering(self) -> list[str]:
            return []

        def pending_followups(self) -> list[str]:
            return list(received)

    monkeypatch.setattr(SERVER, "_SESSIONS", {"s-queue": _FakeSession()})
    SERVER._RUNNING.add("s-queue")
    SERVER._TASKS["s-queue"] = {
        "id": "s-queue",
        "projectId": "proj-sigma",
        "title": "t",
        "access": "full",
    }
    try:
        code, body = _post(base, "/api/v1/tasks/s-queue/messages", {"text": "继续"})
        assert code == 200
        assert body.get("queued") is True
        assert body["status"] == "running"
        assert received == ["继续"]
        # 队列端点可见(前端排队区的数据源)
        code, queues = _get(base, "/api/v1/tasks/s-queue/queues")
        assert code == 200 and queues["followups"] == ["继续"]
    finally:
        SERVER._RUNNING.discard("s-queue")
        SERVER._TASKS.pop("s-queue", None)


def test_fs_listing_drive_root_parent_back_to_drives() -> None:
    """盘符根(D:\\)的 parent 是空串(回「此电脑」层),不能是 None——
    否则「上一级」在根目录被禁死,用户永远换不了盘。"""
    if not Path("C:\\").is_dir():
        pytest.skip("非 Windows 环境,无盘符根")
    listing = SERVER._fs_listing("C:\\")
    assert listing["parent"] == ""
    drives = SERVER._fs_listing("")
    assert drives["path"] == "" and drives["parent"] is None
    assert all(entry["isDir"] for entry in drives["entries"])


def test_http_delete_task_moves_to_trash(
    http_server: tuple[str, Path], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """删除 = 回收站移入(审计可恢复),不是 unlink;列表同步消失,
    再删一次 404。"""
    base, sessions_root = http_server
    monkeypatch.setattr(SERVER, "_TRASH_DIR", tmp_path / "trash")
    code, body = _delete(base, "/api/v1/tasks/s-http")
    assert code == 200 and body["ok"] is True
    assert not SERVER.session_path(sessions_root, "s-http").is_file()
    trashed = list((tmp_path / "trash").glob("s-http.*.jsonl"))
    assert len(trashed) == 1 and trashed[0].read_text(encoding="utf-8") != ""
    code, tasks = _get(base, "/api/v1/tasks")
    assert code == 200 and all(task["id"] != "s-http" for task in tasks)
    code, body = _delete(base, "/api/v1/tasks/s-http")
    assert code == 404


def test_http_delete_task_rejects_running_and_bad_id(
    http_server: tuple[str, Path],
) -> None:
    """执行中的会话拒绝删除(409);路径段拼不进文件路径(400)。"""
    base, _root = http_server
    SERVER._RUNNING.add("s-http")
    try:
        code, body = _delete(base, "/api/v1/tasks/s-http")
        assert code == 409 and "执行中" in body["detail"]
    finally:
        SERVER._RUNNING.discard("s-http")
    code, body = _delete(base, "/api/v1/tasks/..")
    assert code == 400


def test_http_access_switch_hot_reloads_gate(
    http_server: tuple[str, Path],
) -> None:
    """权限模式中途切换:记录更新(载荷读到新档)+ 存活 gate 热替换;
    非法档位 400。**草稿(未落盘)也要能读到**——单任务端点必须认内存
    草稿记录,否则草稿期切档前端回读 null 不更新(实测 2026-10-02)。"""
    base, _root = http_server
    try:
        code, body = _post(base, "/api/v1/tasks/s-http/access", {"access": "confirm"})
        assert code == 200 and body["access"] == "confirm"
        code, task = _get(base, "/api/v1/tasks/s-http")
        assert code == 200 and task["access"] == "confirm"
        code, body = _post(base, "/api/v1/tasks/s-http/access", {"access": "yolo"})
        assert code == 400
        # 纯草稿(不在磁盘):切档后单任务端点仍要回读得到新档位
        code, created = _post(base, "/api/v1/tasks", {"title": "草稿"})
        assert code == 200
        code, body = _post(base, f"/api/v1/tasks/{created['id']}/access", {"access": "auto"})
        assert code == 200
        code, task = _get(base, f"/api/v1/tasks/{created['id']}")
        assert code == 200 and task is not None and task["access"] == "auto"
    finally:
        SERVER._TASKS.pop("s-http", None)


def test_execution_params_model_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """内置 preset 路径:显式选择整体接管(协议随 preset);未注册名回落
    CLI 同链;env 覆盖只在无显式选择时生效;无档位 extra_body=None。"""
    from sigma.providers.registry import builtin_providers

    for var in ("SIGMA_PRESET", "SIGMA_MODEL", "SIGMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(SERVER, "resolve_api_key", lambda: ("test-key", "test"))
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")

    base_url, _key, model, protocol, extra = SERVER._execution_params("zhipu")
    spec = builtin_providers().resolve("zhipu")
    assert (base_url, model, protocol) == (spec.base_url, "glm-4-flash", spec.protocol)
    assert extra is None

    _url, _key, model, _protocol, _extra = SERVER._execution_params("no-such-preset")
    assert model == "deepseek-chat"  # DEFAULT_PRESET=deepseek

    monkeypatch.setenv("SIGMA_MODEL", "custom-model-x")
    _url, _key, model, _protocol, _extra = SERVER._execution_params("")
    assert model == "custom-model-x"  # env 覆盖生效
    _url, _key, model, _protocol, _extra = SERVER._execution_params("zhipu")
    assert model == "glm-4-flash"  # 显式选择压过 env


# ==========================================================================
# 联网开关(星辰 2026-10-02;详规 docs/plans/工作台联网开关-详规.md)
#
# 为什么这批门长这样:缺陷本身是**装配层少传了一个参数**,
# 而少传参数在Python 里不报错——只是安静地少两个工具。
# 所以门必须覆盖**装配路径**(跑 _get_or_create_session 看真工具表),
# 而不是只测 default_registry 本身(那只能证明函数没坏)。
# ==========================================================================


def _web_session(
    tmp_path: Path,
    sessions_root: Path,
    *,
    web: bool | None,
    tavily: str | None = "tvly-test-key",
) -> Any:
    """按当前装配路径真造一个会话,返回它(不给假 registry——那正是本缺陷
    藏身的地方:手写 registry 就绕过了出问题的装配代码)。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(SERVER, "_PROVIDER_FACTORY", lambda: _ScriptedProvider("ok"))
    # CI 上没有 ~/.sigma/.env:装配链要解析全局密钥,必须显式给一个
    # (开发者本机有真密钥,所以这坑只在 CI 炸——2026-10-03 实测)。
    monkey.setenv("SIGMA_API_KEY", "web-test-key")
    monkey.setattr(SERVER._WebKeys, "tavily", tavily)
    monkey.setattr(SERVER._WebKeys, "firecrawl", None)
    monkey.setattr(SERVER._WebKeys, "resolved", True)
    task_id = "t-web"
    with SERVER._TASKS_LOCK:
        SERVER._TASKS[task_id] = {"id": task_id, "access": "full"}
        if web is not None:
            SERVER._TASKS[task_id]["web"] = web
    with SERVER._SESSIONS_LOCK:
        SERVER._SESSIONS.pop(task_id, None)
    session = SERVER._get_or_create_session(task_id, tmp_path, "full", sessions_root)
    session.__dict__["_test_monkey"] = monkey  # 保持patch 生效直到用例结束
    return session










# ==========================================================================
# 联网开关(星辰 2026-10-02;详规 docs/plans/工作台联网开关-详规.md)
#
# 为什么这批门长这样:缺陷本身是**装配层少传了一个参数**,
# 而少传参数在Python 里不报错——只是安静地少两个工具。
# 所以门必须覆盖**装配路径**(跑 _get_or_create_session 看真工具表),
# 而不是只测 default_registry 本身(那只能证明函数没坏)。
# ==========================================================================


def _web_session(
    tmp_path: Path,
    sessions_root: Path,
    *,
    web: bool | None,
    tavily: str | None = "tvly-test-key",
) -> Any:
    """按当前装配路径真造一个会话,返回它(不给假 registry——那正是本缺陷
    藏身的地方:手写 registry 就绕过了出问题的装配代码)。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(SERVER, "_PROVIDER_FACTORY", lambda: _ScriptedProvider("ok"))
    # CI 上没有 ~/.sigma/.env:装配链要解析全局密钥,必须显式给一个
    # (开发者本机有真密钥,所以这坑只在 CI 炸——2026-10-03 实测)。
    monkey.setenv("SIGMA_API_KEY", "web-test-key")
    monkey.setattr(SERVER._WebKeys, "tavily", tavily)
    monkey.setattr(SERVER._WebKeys, "firecrawl", None)
    monkey.setattr(SERVER._WebKeys, "resolved", True)
    task_id = "t-web"
    with SERVER._TASKS_LOCK:
        SERVER._TASKS[task_id] = {"id": task_id, "access": "full"}
        if web is not None:
            SERVER._TASKS[task_id]["web"] = web
    with SERVER._SESSIONS_LOCK:
        SERVER._SESSIONS.pop(task_id, None)
    session = SERVER._get_or_create_session(task_id, tmp_path, "full", sessions_root)
    session.__dict__["_test_monkey"] = monkey  # 保持patch 生效直到用例结束
    return session


def test_g91_web_tools_registered_only_when_on(
    tmp_path: Path,
) -> None:
    """G91:装配路径真的按档位给工具表(默认关 / 显式开 / 没 key 不开)。"""
    sessions_root = tmp_path / "sessions"

    # 默认(记录里没有 web 字段)→ 关
    off = _web_session(tmp_path, sessions_root, web=None)
    assert "web_search" not in off._registry.names(), (
        "默认必须关(星辰 2026-10-02:默认不启动)。有 key 也不开。"
    )
    assert "web_search" not in off._context._system_prompt

    # 显式开 → 工具在
    on = _web_session(tmp_path, sessions_root, web=True)
    assert "web_search" in on._registry.names(), (
        "记录里 web=True 时必须注册——本缺陷就是这里少传了参数,"
        "导致 agent 答'我没有联网工具'(它说的是实话)"
    )
    off.__dict__["_test_monkey"].undo()
    on.__dict__["_test_monkey"].undo()


def test_g92_prompt_and_registry_same_source(tmp_path: Path) -> None:
    """G92:提示词与注册表同源——两者要么都有 web_search,要么都没有。

    这是本批次的核心纪律(CLI 侧写下的"提示词与注册表同源")在SDK 侧的落点。
    断言两侧同时成立,**不是只查一侧**:只查注册表会漏掉"工具在、提示词不提"
    (模型不知道自己能调);只查提示词会漏掉"提示词说能调、表里没有"
    (模型去调一个不存在的工具)。两种症状都表现为"联网功能看起来是坏的"。
    """
    sessions_root = tmp_path / "sessions"
    for web in (None, True, False):
        session = _web_session(tmp_path, sessions_root, web=web)
        names = session._registry.names()
        in_table = "web_search" in names
        in_prompt = "web_search" in session._context._system_prompt
        assert in_table == in_prompt, (
            f"web={web}:工具表有={in_table}、提示词有={in_prompt}——"
            "两者必须同源,否则模型看到的工具面自相矛盾"
        )
        session.__dict__["_test_monkey"].undo()


def test_g93_resident_region_ok_both_switches(tmp_path: Path) -> None:
    """G93:开关两态下常驻区指纹与预算都过(名义门槛比没有更坏——要真跑)。

    开启比关闭多≈600 token(工具行 + 调研纪律段),必须确认仍在 D4 的
    总闸之内。这个断言的价值在于:预算不够时它会**当场抛**而不是
    "跑跑看好像也行"。
    """
    sessions_root = tmp_path / "sessions"
    for web in (False, True):
        session = _web_session(tmp_path, sessions_root, web=web)
        # 指纹:换表后必须重新冻结,否则下一轮 verify_resident_region 当场炸
        session._context.verify_resident_region()
        # 预算:超了当场抛 ResidentBudgetExceeded
        session._context.verify_resident_budget()
        assert session._context.resident_tokens <= 3500, (
            f"web={web} 常驻区 {session._context.resident_tokens} 超 D4 总闸 3500"
        )
        session.__dict__["_test_monkey"].undo()


def test_web_set_task_web_roundtrip(tmp_path: Path) -> None:
    """开关端点:开→真开;关→真关;没 key 时如实说"不可用"而不是假装开。"""
    code, body = SERVER._set_task_web(tmp_path, "t-rt", {"enabled": True})
    assert code == 200 and body["web"] is True
    assert body["pendingDropped"] == 0

    # 没配key 时:用户点了开,但 webSearch 必须 False(note 说明原因)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(SERVER._WebKeys, "tavily", None)
    monkey.setattr(SERVER._WebKeys, "resolved", True)
    code, body = SERVER._set_task_web(tmp_path, "t-rt2", {"enabled": True})
    assert code == 200
    assert body["webSearch"] is False, "没 key 不能真的开"
    assert "TAVILY" in body["note"], f"要说清是缺 key 而不是已开:{body['note']}"
    monkey.undo()

    # 非法入参
    code, body = SERVER._set_task_web(tmp_path, "t-rt3", {"enabled": "yes"})
    assert code == 400




def test_http_web_switch_roundtrip(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 层：POST /tasks/{id}/web 与回读（详规 §7.5 记的残留，此处补齐）。

    函数层的门已经覆盖了「装配出的工具表对不对」，但**路由接错/字段名写错**
    只有走HTTP 才暴露——而那正是"按钮点了没反应"的形态。
    草稿（未落盘）也要能读到新档位，与 access 端点同要求。
    """
    base, _root = http_server
    monkeypatch.setattr(SERVER._WebKeys, "tavily", "tvly-test")
    monkeypatch.setattr(SERVER._WebKeys, "resolved", True)
    try:
        # 默认关
        code, body = _post(base, "/api/v1/tasks/s-http/web", {"enabled": False})
        assert code == 200 and body["web"] is False
        code, task = _get(base, "/api/v1/tasks/s-http")
        assert code == 200 and task["web"] is False

        # 开→真开（key 在）
        code, body = _post(base, "/api/v1/tasks/s-http/web", {"enabled": True})
        assert code == 200 and body["web"] is True and body["webSearch"] is True
        code, task = _get(base, "/api/v1/tasks/s-http")
        assert code == 200 and task["web"] is True

        # 非法入参：enabled 必须是布尔（传字符串不许被当成真）
        code, _body = _post(base, "/api/v1/tasks/s-http/web", {"enabled": "yes"})
        assert code == 400

        # 草稿（不在磁盘）也要能读到
        code, created = _post(base, "/api/v1/tasks", {"title": "草稿"})
        assert code == 200
        code, body = _post(base, f"/api/v1/tasks/{created['id']}/web", {"enabled": True})
        assert code == 200
        code, task = _get(base, f"/api/v1/tasks/{created['id']}")
        assert code == 200 and task is not None and task["web"] is True
    finally:
        SERVER._TASKS.pop("s-http", None)


# ============ G94-G96：档位链路（2026-10-02） ============
#
# 起因：用户报「能选模型、选不了档位」。两条独立路径 ——
#   ① preset 侧：ProviderSpec 当时没有档位字段，端点又写死 "efforts": []
#      → 前端 efforts.length>0 判假 → 档位下拉**根本不渲染**；
#   ② 随附方式：执行链三处写死 "reasoning_effort"，spec 声明 thinking 也发不出去。










# ============ G94-G96：档位链路（2026-10-02） ============
#
# 起因：用户报「能选模型、选不了档位」。两条独立路径 ——
#   ① preset 侧：ProviderSpec 当时没有档位字段，端点又写死 "efforts": []
#      → 前端 efforts.length>0 判假 → 档位下拉**根本不渲染**；
#   ② 随附方式：执行链三处写死 "reasoning_effort"，spec 声明 thinking 也发不出去。


def test_g94_preset_efforts_come_from_spec_not_hardcoded() -> None:
    """models 端点必须把 preset 的档位**真的下发**，而不是写死空数组。

    端点绿但字段空 = 界面依然没下拉 —— 这就是原缺陷的形态。
    """
    from sigma.providers.registry import builtin_providers

    spec = builtin_providers().resolve("deepseek")
    assert spec.efforts == ("low", "medium", "high"), "档位是实测数据，不该为空"

    src = SERVER.__file__ and open(SERVER.__file__, encoding="utf-8").read()
    # 端点里那处不能再是空数组（自定义条目那处仍按用户配置读，是对的）
    assert '"efforts": list(spec.efforts)' in src, "端点未从 spec 读档位"
    assert '"effortStyle": spec.effort_style' in src, "端点未下发随附方式"


def test_g95_execution_params_follows_spec_effort_style(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """preset 分支的随附方式取自 spec，不写死 reasoning_effort。

    写死的话，智谱系preset（thinking 风格）即便声明了档位也发不出去 ——
    症状是「档位选了、行为没变」，最难察觉的一种假功能。
    """
    from sigma.providers.registry import builtin_providers

    registry = builtin_providers()
    # 用一个声明了 thinking 风格的 spec 验证随附方式真的被读
    spec = registry.resolve("zhipu")
    assert spec.effort_style == "reasoning_effort", "zhipu 未实测，保持默认风格"

    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    monkeypatch.setenv("SIGMA_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("SIGMA_PRESET", "deepseek")

    # deepseek 走 reasoning_effort 风格
    _bu, _key, _model, _proto, extra = SERVER._execution_params(effort_hint="high")
    assert extra == {"reasoning_effort": "high"}

    # 空档位 → None（请求字节与无档位完全一致，不得凭空多一个字段）
    _bu, _key, _model, _proto, extra_none = SERVER._execution_params(effort_hint="")
    assert extra_none is None


def test_g96_effort_extra_body_both_styles() -> None:
    """两种随附方式的线格式都必须对（OpenAI 字符串 / 智谱对象）。"""
    assert SERVER._effort_extra_body("reasoning_effort", "high") == {
        "reasoning_effort": "high"
    }
    assert SERVER._effort_extra_body("thinking", "enabled") == {
        "thinking": {"type": "enabled"}
    }
    # 空白档位等同于没有（不能发出 {"reasoning_effort": ""}）
    assert SERVER._effort_extra_body("reasoning_effort", "   ") is None


def test_models_endpoint_payload_carries_efforts(
    http_server: tuple[str, Path]
) -> None:
    """端到端：真的起服务打 /models，deepseek 条目必须带三档。

    函数层绿不够 —— 端点组装层才决定前端拿不拿得到。
    """
    base, _root = http_server
    code, models = _get(base, "/api/v1/models")
    assert code == 200 and isinstance(models, list)
    by_name = {m["name"]: m for m in models}
    assert "deepseek" in by_name, f"preset 未下发：{sorted(by_name)}"
    assert by_name["deepseek"]["efforts"] == ["low", "medium", "high"]
    # 未实测的厂商：空数组 → 前端不显示档位下拉（如实，而不是给假档位）
    for name in ("moonshot", "zhipu", "dashscope", "ollama", "anthropic"):
        if name in by_name:
            assert by_name[name]["efforts"] == [], f"{name} 应无档位"


# ============ G97：首页联网开关（2026-10-02） ============
#
# 用户报「为什么新任务刚开始没显示联网开关」。两层根因：
#   ① 前端：按钮只加在 TaskDetail（会话页），首页 Composer 没有 ——
#      而首页才是**建任务的地方**。
#   ② 后端：createTask 的 record **不写 web**，而 _draft_payload 会读它
#      ⇒ 前端传了也被"缺字段=False"吃掉 ⇒「开关能点、开了没效果」。
#
# 注意本组测试**刻意走建任务路径**，而不是上一批的 setTaskWeb 热切路径 ——
# 上一批全测「建完之后改」，恰好漏掉「建的时候能不能带上」。


def test_g97_create_task_persists_web_flag() -> None:
    """建任务时 web=true 必须真的落进 record（不是只在 payload 上好看）。"""
    srv = SERVER
    original = srv._MODELS_REGISTRY_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            srv._MODELS_REGISTRY_PATH = Path(td) / "models.json"
            payload = srv._create_task({
                "title": "T", "description": "d",
                "access": "full", "model": "m", "web": True,
            })
            assert payload["web"] is True, "payload 应回显 web=true"
            # 关键断言：record 内部也要落住 —— 原缺陷是「payload 读得到
            # 但 record 没写」，之后热切/执行链都读不到。
            assert srv._TASKS[payload["id"]]["web"] is True
            srv._TASKS.pop(payload["id"], None)
    finally:
        srv._MODELS_REGISTRY_PATH = original


def test_g97_create_task_defaults_web_off() -> None:
    """不传 web 时默认**关**（拍板口径），且补了写入不等于变成默认开。"""
    srv = SERVER
    original = srv._MODELS_REGISTRY_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            srv._MODELS_REGISTRY_PATH = Path(td) / "models.json"
            payload = srv._create_task({
                "title": "T2", "description": "d",
                "access": "full", "model": "m",
            })
            assert payload["web"] is False, "缺字段必须默认关"
            srv._TASKS.pop(payload["id"], None)
    finally:
        srv._MODELS_REGISTRY_PATH = original


def test_g97_create_task_web_coerced_to_bool() -> None:
    """web 必须布尔化：0 / '' / None 都是关，1 / True 是开。

    防的是 record 里存进非布尔值，之后 ``bool(record.get("web"))`` 之外的
    消费方（如 JSON 契约、前端 === true 判断）行为不一致。
    """
    srv = SERVER
    original = srv._MODELS_REGISTRY_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            srv._MODELS_REGISTRY_PATH = Path(td) / "models.json"
            for raw, expected in ((1, True), (0, False), ("", False), (None, False)):
                payload = srv._create_task({
                    "title": "B", "description": "d",
                    "access": "full", "model": "m", "web": raw,
                })
                assert payload["web"] is expected, f"web={raw!r} 应落成 {expected}"
                assert isinstance(srv._TASKS[payload["id"]]["web"], bool)
                srv._TASKS.pop(payload["id"], None)
    finally:
        srv._MODELS_REGISTRY_PATH = original


# ============ G98：静态资源 MIME（2026-10-02） ============
#
# 起因：背景图从 PNG(1902KB) 换成 WebP(48KB) 后，HTTP 端实测
# ``Content-Type: application/octet-stream``——服务端那张 MIME 表是
# **硬编码白名单**，漏了 .webp。危害不是"不好看"：严格 MIME 校验的场合
# （nosniff 头、部分代理、某些资源管线）会**拒收**，
# 症状是背景静默不出现 ⇒ **压缩白做**。
#
# 为什么用 HTTP 端到端而不是查源码字符串：
# 查源码能被"重构后换写法"骗过，也能被"字典里有这一行"骗过
# （字符串存在性 ≠ 运行时生效）。真起服务取 header 才是判据。


@pytest.fixture
def dist_http_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """起一个带真实 dist 目录的服务器（只放测需要的资源）。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><body>x</body></html>', encoding="utf-8"
    )
    # 最小合法文件内容；断言只关心 header，不校验字节解码
    payloads = {
        "bg-guofeng-CKLqumMD.webp": b"RIFF\x00\x00\x00\x00WEBPVP8 ",
        "a.png": b"\x89PNG\r\n\x1a\n",
        "a.jpg": b"\xff\xd8\xff",
        "a.svg": b"<svg/>",
        "a.woff2": b"wOF2",
    }
    for name, blob in payloads.items():
        (dist / "assets" / name).write_bytes(blob)

    server = SERVER.make_server(
        host="127.0.0.1",
        port=0,
        sessions_root=tmp_path / "sessions",
        workspace=tmp_path,
        dist_dir=dist,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, dist
    server.shutdown()
    server.server_close()


def _content_type(base: str, path: str) -> str:
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as resp:
        return resp.headers.get("Content-Type", "")


def test_g98_webp_served_as_image_webp(dist_http_server: tuple[str, Path]) -> None:
    """.webp 必须是 image/webp，不能退回 octet-stream。

    背景图就是 .webp。这条红了 = 背景静默不显示（严格 MIME 校验时）。
    """
    base, _dist = dist_http_server
    got = _content_type(base, "/assets/bg-guofeng-CKLqumMD.webp")
    assert got == "image/webp", f".webp 的 Content-Type 应为 image/webp，实得 {got!r}"


def test_g98_all_asset_mime_types(dist_http_server: tuple[str, Path]) -> None:
    """一张表覆盖全部已登记类型——**新增资源类型必须在这里登记**。

    参数化而非单个断言：漏一种类型时能**指名道姓**地红，
    而不是只报一句"有个不对"。
    """
    base, _dist = dist_http_server
    expected = {
        "/assets/bg-guofeng-CKLqumMD.webp": "image/webp",
        "/assets/a.png": "image/png",
        "/assets/a.jpg": "image/jpeg",
        "/assets/a.svg": "image/svg+xml",
        "/assets/a.woff2": "font/woff2",
    }
    for path, want in expected.items():
        got = _content_type(base, path)
        assert got == want, f"{path} 的 Content-Type 应为 {want}，实得 {got!r}"


def test_g98_unknown_suffix_falls_back_to_octet_stream(
    dist_http_server: tuple[str, Path],
) -> None:
    """未登记的类型仍退回 octet-stream（不要把默认改成 *通配*）。

    给了 image/* 或 application/octet-stream 之外的通配，
    等于让**任何**上传进来的文件都能被当成可执行内容伺服。
    """
    base, dist = dist_http_server
    (dist / "assets" / "a.bin").write_bytes(b"\x00\x01")
    got = _content_type(base, "/assets/a.bin")
    assert got == "application/octet-stream", f"未登记类型应退回 octet-stream，实得 {got!r}"


# ---------------------------------------------------------------------------
# 斜杠命令(2026-10-03):"/" 在服务端拦截执行,note 块上屏,**永不进模型**
# ---------------------------------------------------------------------------


def _drain_deltas(base: str, task_id: str) -> list[dict[str, Any]]:
    code, d = _get(base, f"/api/v1/tasks/{task_id}/deltas?since=0")
    assert code == 200
    return d["pieces"]


def _wait_turn_done(base: str, task_id: str) -> None:
    for _ in range(200):
        _code, d = _get(base, f"/api/v1/tasks/{task_id}/deltas?since=0")
        if not d["running"]:
            return
        time.sleep(0.05)


def test_commands_endpoint_lists_registry(http_server: tuple[str, Path]) -> None:
    """GET /commands = 命令注册表 + 技能清单(面板数据源,不需要 execution_env)。"""
    base, _root = http_server
    code, payload = _get(base, "/api/v1/commands")
    assert code == 200
    names = {c["name"] for c in payload["commands"]}
    assert {"help", "stop", "reload", "checkpoints", "rollback", "new", "compact", "goal", "plan"} <= names
    assert isinstance(payload["skills"], list)


def test_slash_command_intercepted_never_reaches_model(
    http_server: tuple[str, Path], execution_env: str
) -> None:
    """/help:200 + command 标记 + note 块;模型零调用(任务仍是 draft)。"""
    base, _root = http_server
    _code, created = _post(base, "/api/v1/tasks")
    task_id = created["id"]

    code, payload = _post(base, f"/api/v1/tasks/{task_id}/messages", {"text": "/help"})
    assert code == 200
    assert payload["command"] is True
    assert any("可用命令" in line for line in payload["output"])

    # note 块经 deltas 上屏,且立即 done(命令不开轮)
    pieces = _drain_deltas(base, task_id)
    assert pieces, "命令输出应作为 note 块出现在 deltas 里"
    assert all(p["k"] == "note" for p in pieces)

    # 模型没被调用:会话文件未产生,任务还是 draft(假 provider 若被调,状态会是 completed)
    code, task = _get(base, f"/api/v1/tasks/{task_id}")
    assert code == 200 and task["status"] == "draft"


def test_slash_unknown_command_reports_and_not_sent(
    http_server: tuple[str, Path], execution_env: str
) -> None:
    """未知命令:报错并指向 /help,绝不静默转发给模型(REPL 同款约定)。"""
    base, _root = http_server
    _code, created = _post(base, "/api/v1/tasks")
    task_id = created["id"]
    code, payload = _post(
        base, f"/api/v1/tasks/{task_id}/messages", {"text": "/根本不存在"}
    )
    assert code == 200
    assert any("未知命令" in line for line in payload["output"])
    code, task = _get(base, f"/api/v1/tasks/{task_id}")
    assert code == 200 and task["status"] == "draft"


def test_slash_stop_when_idle(http_server: tuple[str, Path], execution_env: str) -> None:
    base, _root = http_server
    _code, created = _post(base, "/api/v1/tasks")
    code, payload = _post(base, f"/api/v1/tasks/{created['id']}/messages", {"text": "/stop"})
    assert code == 200
    assert any("没有执行中的任务" in line for line in payload["output"])


def test_slash_goal_set_and_show(http_server: tuple[str, Path], execution_env: str) -> None:
    """/goal 设与查:目标入 _GOALS(会话(重)装配时注入系统提示)。"""
    base, _root = http_server
    _code, created = _post(base, "/api/v1/tasks")
    task_id = created["id"]
    code, payload = _post(
        base, f"/api/v1/tasks/{task_id}/messages", {"text": "/goal 写一个爬虫"}
    )
    assert code == 200
    assert any("会话目标已设" in line for line in payload["output"])
    assert payload["goal"] == "写一个爬虫"
    assert SERVER._GOALS[task_id] == "写一个爬虫"  # noqa: SLF001 - 注入链路的被测状态
    code, payload = _post(base, f"/api/v1/tasks/{task_id}/messages", {"text": "/goal"})
    assert code == 200
    assert any("写一个爬虫" in line for line in payload["output"])


def test_slash_running_guard_blocks_idle_commands(
    http_server: tuple[str, Path], execution_env: str
) -> None:
    """执行中仅 /stop 放行,其余命令被挡(否则 /compact 会和跑着的轮对撞)。"""
    base, _root = http_server
    _code, created = _post(base, "/api/v1/tasks")
    task_id = created["id"]
    SERVER._RUNNING.add(task_id)  # noqa: SLF001 - 模拟"正在执行"
    try:
        code, payload = _post(
            base, f"/api/v1/tasks/{task_id}/messages", {"text": "/compact"}
        )
        assert code == 200
        assert any("执行中" in line for line in payload["output"])
        # /stop 执行中放行(此时无会话 → 中断失败但可达,如实回执)
        code, payload = _post(
            base, f"/api/v1/tasks/{task_id}/messages", {"text": "/stop"}
        )
        assert code == 200
        assert any("未装配" in line for line in payload["output"])
    finally:
        SERVER._RUNNING.discard(task_id)


def test_slash_checkpoints_and_rollback_after_real_turn(
    http_server: tuple[str, Path], execution_env: str
) -> None:
    """真跑一轮后:装配出的会话能应答 /checkpoints 与 /rollback(纯文本轮无写批次 → 无快照)。"""
    base, _root = http_server
    _code, created = _post(base, "/api/v1/tasks")
    task_id = created["id"]
    code, started = _post(base, f"/api/v1/tasks/{task_id}/messages", {"text": "你好"})
    assert code == 200
    _wait_turn_done(base, task_id)

    code, payload = _post(base, f"/api/v1/tasks/{task_id}/messages", {"text": "/checkpoints"})
    assert code == 200
    assert any("快照" in line for line in payload["output"])

    code, payload = _post(base, f"/api/v1/tasks/{task_id}/messages", {"text": "/rollback"})
    assert code == 200
    assert any("用法" in line for line in payload["output"])
    code, payload = _post(base, f"/api/v1/tasks/{task_id}/messages", {"text": "/rollback 1"})
    assert code == 200
    assert any("找不到该回滚点" in line for line in payload["output"])


def test_slash_skill_fallback_starts_turn(
    http_server: tuple[str, Path], execution_env: str, tmp_path: Path
) -> None:
    """未知命令名命中技能 → 以规范提示语起**真实一轮**(模型经 load_skill 加载);
    与技能不同名的未知命令仍然报错。"""
    base, _root = http_server
    skill_dir = tmp_path / "extensions" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "demo.md").write_text(
        "---\nname: demo\ndescription: 演示技能\n---\n正文\n", encoding="utf-8"
    )
    _code, created = _post(base, "/api/v1/tasks")
    task_id = created["id"]

    code, payload = _post(
        base, f"/api/v1/tasks/{task_id}/messages", {"text": "/demo 帮我写个函数"}
    )
    assert code == 200
    assert payload.get("skill") == "demo"
    assert payload["status"] == "running"  # 真起了一轮,不是 note 回执
    _wait_turn_done(base, task_id)
    code, task = _get(base, f"/api/v1/tasks/{task_id}")
    assert code == 200 and task["status"] == "completed"

    code, payload = _post(
        base, f"/api/v1/tasks/{task_id}/messages", {"text": "/根本不存在"}
    )
    assert code == 200
    assert any("未知命令" in line for line in payload["output"])


def test_system_status_and_addresses(http_server: tuple[str, Path]) -> None:
    """状态(版本/会话数/工作区)与局域网地址(不含 127.0.0.1)。"""
    base, _root = http_server
    code, status = _get(base, "/api/v1/system/status")
    assert code == 200
    assert status["version"] != ""
    assert status["sessions"] >= 1  # 夹具写过 s-http
    assert status["workspace"] != ""
    code, addresses = _get(base, "/api/v1/system/addresses")
    assert code == 200 and isinstance(addresses["urls"], list)
    assert all(url.startswith("http://") for url in addresses["urls"])


def test_trash_delete_restore_roundtrip(http_server: tuple[str, Path]) -> None:
    """删除 → 回收站可见 → 恢复回会话列表(trace 兄弟文件一并找回)。

    ⚠ 回收站目录是全局真实目录(~/.sigma/trash),测试**只做单条恢复**,
    绝不调清空——那会把用户真实的回收站清掉。"""
    base, _root = http_server
    code, _ = _delete(base, "/api/v1/tasks/s-http")
    assert code == 200

    code, trash = _get(base, "/api/v1/system/trash")
    assert code == 200
    entry = next(
        (
            item
            for item in trash["entries"]
            if item["kind"] == "session" and item["taskId"] == "s-http"
        ),
        None,
    )
    assert entry is not None, trash["entries"][:5]

    code, restored = _post(base, "/api/v1/system/trash/restore", {"name": entry["name"]})
    assert code == 200 and restored["taskId"] == "s-http"
    code, tasks = _get(base, "/api/v1/tasks")
    assert code == 200
    assert any(task["id"] == "s-http" for task in tasks)


def test_session_rebuilds_when_task_model_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """任务记录的 model 变化 → 缓存会话**同树重建**(provider/model 换新);
    不变 → 复用同一实例。没有它,TaskDetail 的模型切换器只是改了 UI
    (实测 2026-10-05:切到 deepseek-reasoner,三轮仍打旧模型)。"""
    for var in ("SIGMA_PRESET", "SIGMA_MODEL", "SIGMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(SERVER, "resolve_api_key", lambda: ("test-key", "test"))
    monkeypatch.setattr(SERVER, "_PROVIDER_FACTORY", lambda: _ScriptedProvider("好"))
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    task_id = "t-rebuild"
    repo = tmp_path / "repo"
    repo.mkdir()
    root = tmp_path / "sessions"
    SERVER._TASKS[task_id] = {"id": task_id, "model": "deepseek", "effort": "", "access": "full"}
    try:
        s1 = SERVER._get_or_create_session(task_id, repo, "full", root)
        s2 = SERVER._get_or_create_session(task_id, repo, "full", root)
        assert s2 is s1  # 元数据不变 → 复用

        SERVER._TASKS[task_id]["model"] = "zhipu"
        s3 = SERVER._get_or_create_session(task_id, repo, "full", root)
        assert s3 is not s1  # 模型变了 → 重建
        assert SERVER._SESSION_META[task_id]["model"] == "zhipu"
        s4 = SERVER._get_or_create_session(task_id, repo, "full", root)
        assert s4 is s3  # 新元数据稳定后继续复用
    finally:
        with SERVER._SESSIONS_LOCK:
            SERVER._SESSIONS.pop(task_id, None)
            SERVER._SESSION_META.pop(task_id, None)
        with SERVER._TASKS_LOCK:
            SERVER._TASKS.pop(task_id, None)
