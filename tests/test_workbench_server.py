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
from sigma.events.lifecycle import TextChunk, ThinkingChunk, ToolEnd, ToolStart

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
    默认 preset 排内置组第一;apiKey 绝不回传(只有 hasKey 布尔)。"""
    from sigma.cli.main import DEFAULT_PRESET
    from sigma.providers.registry import builtin_providers

    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    code, saved = _post(base, "/api/v1/models/save", {
        "name": "GLM-5.3-Flash",
        "protocol": "openai-compat",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
        "apiKey": "sk-test-123",
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
    assert "apiKey" not in mine and "sk-test-123" not in json.dumps(models)


def test_model_save_rejects_duplicate_name(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """name 是执行链的解析键:重名(含改名撞名)一律 400;更新自身留空
    apiKey = 保留原值(密钥不回传,表单无从带回)。"""
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    payload = {
        "name": "my-model", "protocol": "openai-compat",
        "baseUrl": "https://x/v1", "apiKey": "k", "modelId": "m-1",
        "efforts": [], "effortStyle": "reasoning_effort",
    }
    code, first = _post(base, "/api/v1/models/save", payload)
    assert code == 200
    code, clash = _post(base, "/api/v1/models/save", {**payload, "modelId": "m-2"})
    assert code == 400 and "已存在" in clash["detail"]
    code, updated = _post(base, "/api/v1/models/save", {**payload, "id": first["id"], "apiKey": ""})
    assert code == 200
    stored = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))["models"][0]
    assert stored["modelId"] == "m-1" and stored["apiKey"] == "k"


def test_model_remove_roundtrip(
    http_server: tuple[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    base, _root = http_server
    _code, saved = _post(base, "/api/v1/models/save", {
        "name": "gone", "protocol": "openai-compat",
        "baseUrl": "https://x/v1", "apiKey": "", "modelId": "m",
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
    """自定义条目整体接管 base_url/model/协议/key;档位按条目 effortStyle
    随附;内置 preset 仍按名解析(reasoning_effort 风格)。"""
    for var in ("SIGMA_PRESET", "SIGMA_MODEL", "SIGMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(SERVER, "_MODELS_REGISTRY_PATH", tmp_path / "models.json")
    (tmp_path / "models.json").write_text(json.dumps({
        "models": [{
            "id": "mdl-x", "name": "GLM-5.3-Flash", "protocol": "openai-compat",
            "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "apiKey": "zk-own-key",
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
    assert api_key == "zk-own-key"  # 条目自带 key,不碰环境链
    assert extra == {"thinking": {"type": "开启"}}

    _url, _k, model, protocol, extra = SERVER._execution_params("zhipu", "high")
    assert protocol == "openai-compat" and extra == {"reasoning_effort": "high"}
    assert model == "glm-4-flash"


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
    assert [piece["k"] for piece in pieces] == ["text", "thinking", "tool_start", "tool_end"]
    assert pieces[0] == {"k": "text", "t": "我先看一下目录。"}
    assert pieces[2]["name"] == "bash" and "pwd" in pieces[2]["args"]
    assert pieces[3]["ok"] is True and pieces[3]["preview"] == "D:\\"
    # 换新缓冲区(新一轮开始)后,事件写进**新**缓冲——跨轮不串
    pieces2: list[dict[str, Any]] = []
    monkeypatch.setattr(
        SERVER,
        "_DELTAS",
        {"t1": {"pieces": pieces2, "lock": threading.Lock(), "done": False, "error": None}},
    )
    collector.on_event(TextChunk(text="第二轮"))
    assert len(pieces) == 4 and [p["t"] for p in pieces2] == ["第二轮"]
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
