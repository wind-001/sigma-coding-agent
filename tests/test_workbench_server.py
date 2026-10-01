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

    # 轮询流式增量直到轮结束
    text_acc, seq = "", 0
    for _ in range(200):
        code, d = _get(base, f"/api/v1/tasks/{created['id']}/deltas?since={seq}")
        assert code == 200
        text_acc += d["text"]
        seq = d["seq"]
        if not d["running"]:
            break
        time.sleep(0.05)
    assert "我是 sigma" in text_acc

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
