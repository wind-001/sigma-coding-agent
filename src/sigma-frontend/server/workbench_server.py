"""sigma 工作台桥接服务(只读)。

为 ``src/sigma-frontend``(React 工作台)提供**真实 sigma 数据**的 HTTP 只读子集:
端点与前端 ``httpClient.ts`` 的契约一一对应(sigma-frontend/docs/sigma-backend-api-design.md),
另加一个 ``GET /tasks/{id}/timeline``(观测层 --timeline 的数据面,前端渲染运行时间线)。

定位与边界(为什么不是 sigma-server)
    sigma-frontend/docs 里那份 σ-server 详规(FastAPI + SSE + 执行)是**待拍板未实现**的
    M1/M2/M3;本服务不抢它的活——只用 stdlib ``http.server`` 把 sigma **现有**的数据面
    (会话 JSONL / 观测时间线 / 技能 / 记忆 / 内置工具)以契约形状读出来,零新依赖、
    零核心改动、零写盘。执行类端点(start/messages/interrupt/steer/approvals/SSE)
    统一返回 501 + 指路文案——前端据此把对应控件标成"待接入"。

    前端任务(前端 Task)= 一个 sigma 会话(JSONL)。对话不二次存储:
    列表用 ``session_previews``(前 4 KB 一瞥),详情/回放整树解析——
    与 CLI ``/sessions`` 和 ``--timeline`` 同源,没有第二份事实。

只读的三道边界
    1. 不执行扩展代码:插件列表只含内置工具与技能(markdown 扫描),
       ``extensions/*.py`` 的装载即执行,只读服务不做;
    2. 不写任何文件:PATCH/DELETE/POST 一律 501(会话 JSONL 是审计事实,只读);
    3. 只绑 127.0.0.1:本机单用户工具,不做鉴权(σ-server 的 token 方案见其详规)。

用法
    ./.venv/Scripts/python.exe src/sigma-frontend/server/workbench_server.py
    可选:--port 8301 --sessions-dir <dir> --workspace <dir> --dist <dir>
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from sigma import __version__
from sigma.agent.messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma.memory.file_store import memory_dir_for, scan_memory
from sigma.observability.timeline import build_timeline
from sigma.providers.messages import AssistantMessage, TextBlock, ToolCallBlock, UserMessage
from sigma.sessions.sessions import TRACE_SUFFIX, session_path, session_previews
from sigma.sessions.store import JsonlStore
from sigma.sessions.tree import SessionTree
from sigma.skills.scanner import SKILLS_DIRNAME, discover_skills
from sigma.tools.registry import ToolRegistry

#: 单项目固定 id——sigma 是单工作区 harness,工作台把它呈现为一个项目。
PROJECT_ID = "proj-sigma"
#: 任务列表一次最多解析多少个会话(按修改时间取最近)。整树解析有读盘代价。
TASKS_LIMIT = 100
#: 详情回放事件上限(老会话可能有几百条)。
EVENTS_LIMIT = 300


def _clip(text: str, width: int) -> str:
    """给模型的不是给人看的——回放文本截断只影响展示。"""
    return text if len(text) <= width else text[: width - 1] + "…"


def _load_tree(sessions_root: Path, session_id: str) -> SessionTree | None:
    """整树读入一个会话;文件不存在返回 None(对应契约的 ``Task | null``)。"""
    path = session_path(sessions_root, session_id)
    if not path.is_file():
        return None
    return SessionTree.from_store(JsonlStore(sessions_root, session_id))


def _trace_path(sessions_root: Path, session_id: str) -> Path | None:
    """trace 与会话文件同目录、同名不同后缀(P5-批次1);不存在的返回 None。"""
    path = session_path(sessions_root, session_id)
    trace = path.with_name(path.name + TRACE_SUFFIX)
    return trace if trace.is_file() else None


def _derive_status(
    tree: SessionTree, timeline_status: str
) -> tuple[str, str]:
    """推导前端任务状态。返回 ``(status, 状态说明)``。

    判据按可信度排序:
    1. trace 里有最后一轮的落点状态(P5-批次1 的 TurnResult.status):
       ``completed`` → 已完成;``error``/``stopped`` → 失败(错误/被打断);
    2. 没有 trace(观测层关闭的会话)→ 看**悬空工具调用**:
       末尾有 assistant 声明了工具调用但没有对应结果 = 中断(可断点续跑)→ failed;
    3. 其余 = 正常收尾 → completed。
    """
    if timeline_status == "completed":
        return "completed", "最后一轮正常收尾(trace)"
    if timeline_status in ("error", "stopped"):
        return "failed", f"最后一轮 {timeline_status}(trace)"
    unanswered: set[str] = set()
    for message in tree.history():
        if isinstance(message, LlmMessageWrapper) and isinstance(
            message.message, AssistantMessage
        ):
            for block in message.message.content:
                if isinstance(block, ToolCallBlock):
                    unanswered.add(block.id)
        elif isinstance(message, ToolResultAgentMessage):
            unanswered.discard(message.tool_call_id)
    if unanswered:
        return "failed", f"{len(unanswered)} 个工具调用无结果(中断,可断点续跑)"
    return "completed", "正常收尾"


def _assistant_text(content: list[Any]) -> str:
    """assistant 消息里的文本块拼接(工具调用块不算文本)。"""
    parts = [block.text for block in content if isinstance(block, TextBlock)]
    return "".join(parts)


def _events_from_history(history: list[Any]) -> list[dict[str, str]]:
    """把会话树压成前端 ``TaskEvent[]``(只读回放,截断在展示层语义内)。

    kind 映射:用户/助手消息 → ``message``;assistant 声明的工具调用 → ``tool_call``;
    工具结果错误 → ``note``(✗ 前缀);取**最后** EVENTS_LIMIT 条——
    新会话看全,老会话看尾巴,与"工作台是回放不是审计导出"的定位一致。
    """
    events: list[dict[str, str]] = []
    for message in history:
        at = message.timestamp
        if isinstance(message, LlmMessageWrapper) and isinstance(message.message, UserMessage):
            events.append(
                {
                    "id": f"e{len(events)}",
                    "kind": "message",
                    "text": _clip(f"用户:{message.message.content}", 220),
                    "at": at,
                }
            )
        elif isinstance(message, LlmMessageWrapper) and isinstance(
            message.message, AssistantMessage
        ):
            text = _assistant_text(message.message.content).strip()
            if text:
                events.append(
                    {
                        "id": f"e{len(events)}",
                        "kind": "message",
                        "text": _clip(f"助手:{text}", 300),
                        "at": at,
                    }
                )
            for block in message.message.content:
                if isinstance(block, ToolCallBlock):
                    args = json.dumps(block.arguments, ensure_ascii=False)
                    events.append(
                        {
                            "id": f"e{len(events)}",
                            "kind": "tool_call",
                            "text": _clip(f"调用 {block.name}({args})", 220),
                            "at": at,
                        }
                    )
        elif isinstance(message, ToolResultAgentMessage):
            if message.is_error:
                events.append(
                    {
                        "id": f"e{len(events)}",
                        "kind": "note",
                        "text": _clip(f"✗ {message.tool_name} 失败", 160),
                        "at": at,
                    }
                )
    # id 在截断后重排——key 唯一即可,不必与会话节点 id 对应。
    return [{**event, "id": f"e{index}"} for index, event in enumerate(events[-EVENTS_LIMIT:])]


def _task_payload(
    sessions_root: Path,
    session_id: str,
    *,
    first_user_text: str | None,
    message_count: int,
    modified: float,
) -> dict[str, Any] | None:
    """会话 → 前端 ``Task`` 契约形状。文件不存在(并发被清)返回 None。"""
    tree = _load_tree(sessions_root, session_id)
    if tree is None:
        return None
    history = tree.history()
    timeline = build_timeline(
        session_id,
        session_path(sessions_root, session_id),
        _trace_path(sessions_root, session_id),
    )
    status, _status_note = _derive_status(tree, timeline.status)
    # 描述 = 历史里第一条用户消息**原文**(带换行);preview 的 first_user_text
    # 是列表预览的归一化形态,只做兜底——标题"取首行"必须基于原文才成立。
    description = first_user_text or ""
    for message in history:
        if isinstance(message, LlmMessageWrapper) and isinstance(message.message, UserMessage):
            content = message.message.content
            description = content if isinstance(content, str) else str(content)
            break
    model = timeline.rounds[-1].model if timeline.rounds else ""
    events = _events_from_history(history)
    created = events[0]["at"] if events else ""
    updated = history[-1].timestamp if history else ""
    return {
        "id": session_id,
        "projectId": PROJECT_ID,
        "title": _clip(description.split("\n")[0], 40) or "(空会话)",
        "description": _clip(description, 2000),
        "status": status,
        # access/effort 是"下一次执行"的参数,只读回放没有这个语义,留空;
        # model 是事实(来自最后一轮 provider 返回),如实给。
        "access": "",
        "model": model,
        "effort": "",
        "createdAt": created,
        "updatedAt": updated,
        "events": events,
        # 契约之外附加:列表计数用真实消息数(预览的 count 只数前 4 KB)。
        "messageCount": len(history) or message_count,
        "sizeBytes": session_path(sessions_root, session_id).stat().st_size if history else 0,
        "modifiedEpoch": modified,
    }


def _list_task_payloads(sessions_root: Path) -> list[dict[str, Any]]:
    """最近 TASKS_LIMIT 个会话的任务载荷(按修改时间,最新在前)。

    单个会话解析失败(如 v1.4 时间戳格式变更前的旧记录:整数 timestamp
    过不了 pydantic 校验)→ **跳过该会话**,不拖死整个列表——
    与 timeline 模块声明的查看器策略同一条:"坏数据降级展示,不抛给用户"。
    """
    previews = session_previews(sessions_root, limit=TASKS_LIMIT)
    tasks: list[dict[str, Any]] = []
    for preview in previews:
        try:
            payload = _task_payload(
                sessions_root,
                preview.id,
                first_user_text=preview.first_user_text,
                message_count=preview.message_count,
                modified=preview.modified,
            )
        except Exception:
            # 只读查看器对坏数据的降级:跳过。宁可列表少一条,不可整页 500。
            continue
        if payload is not None:
            tasks.append(payload)
    return tasks


def _timeline_payload(sessions_root: Path, session_id: str) -> dict[str, Any] | None:
    """观测层时间线 → JSON(--timeline 的数据面,渲染归前端)。坏会话降级为 null。"""
    if not session_path(sessions_root, session_id).is_file():
        return None
    try:
        report = build_timeline(
            session_id,
            session_path(sessions_root, session_id),
            _trace_path(sessions_root, session_id),
        )
    except Exception:
        return None
    cache_rate = report.cache_rate
    return {
        "sessionId": report.session_id,
        "status": report.status,
        "hasTrace": report.has_trace,
        "totalPrompt": report.total_prompt,
        "totalCompletion": report.total_completion,
        "totalCached": report.total_cached,
        "cacheRate": round(cache_rate, 4) if cache_rate is not None else None,
        "toolErrors": report.tool_errors,
        "truncated": report.truncated,
        "injected": report.injected,
        "wallSeconds": report.wall_seconds,
        "wallApprox": report.wall_approx,
        "approvals": [
            {"name": view.name, "allowed": view.allowed, "reason": view.reason}
            for view in report.approvals
        ],
        "rounds": [
            {
                "index": view.index,
                "model": view.model,
                "promptTokens": view.prompt_tokens,
                "completionTokens": view.completion_tokens,
                "cachedTokens": view.cached_tokens,
                "latencyMs": view.latency_ms,
                "latencyApprox": view.latency_approx,
                "ttftMs": view.ttft_ms,
                "tools": [
                    {"name": tool.name, "ok": tool.ok, "durationMs": tool.duration_ms}
                    for tool in view.tools
                ],
            }
            for view in report.rounds
        ],
    }


def _plugins_payload(workspace: Path) -> list[dict[str, Any]]:
    """插件市场 = 内置工具(builtin)+ 技能(markdown 扫描)。

    扩展工具(**extensions/*.py**)刻意不列:装载即执行模块级代码,
    只读服务不执行任何用户代码——见模块 docstring 的三道边界。
    """
    registry: ToolRegistry = _builtin_registry()
    plugins: list[dict[str, Any]] = [
        {
            "id": f"tool:{definition.name}",
            "name": definition.name,
            "description": definition.description,
            "version": "core",
            "installed": True,
            "builtin": True,
        }
        for definition in registry.definitions()
    ]
    scan = discover_skills(workspace / "extensions" / SKILLS_DIRNAME)
    for skill in scan.skills:
        plugins.append(
            {
                "id": f"skill:{skill.name}",
                "name": skill.name,
                "description": skill.description,
                "version": "skill",
                "installed": True,
                "builtin": False,
            }
        )
    return plugins


def _builtin_registry() -> ToolRegistry:
    """内置工具表(只读清单用)。惰性导入:压测/离线场景不连任何外部服务。"""
    from sigma.sdk import default_registry  # noqa: PLC0415 - 避免模块导入期的装配成本

    return default_registry()


def _memory_payload(workspace: Path) -> list[dict[str, str]]:
    """跨会话记忆索引(P5-批次3)。条目只有 slug + 首行标题(快照语义,会话内冻结)。"""
    scan = scan_memory(memory_dir_for(workspace))
    return [{"slug": entry.slug, "title": entry.title} for entry in scan.entries]


def _project_payload(workspace: Path) -> dict[str, Any]:
    """当前工作区 → 前端 ``Project``。分支从 .git/HEAD 读,读不到就是 main。"""
    branch = "main"
    head = workspace / ".git" / "HEAD"
    if head.is_file():
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref: refs/heads/"):
            branch = text.removeprefix("ref: refs/heads/")
    return {
        "id": PROJECT_ID,
        "name": workspace.name or "sigma",
        "repoPath": str(workspace),
        "branch": branch,
        "createdAt": "",
    }


class WorkbenchHandler(BaseHTTPRequestHandler):
    """只读契约子集 + 统一 501。路由表在 ``do_GET``;写动词全部进 ``_not_implemented``。"""

    #: 由 make_server 注入(Handler 是按类实例化的,参数走类属性)。
    sessions_root: Path = Path.home() / ".sigma" / "sessions"
    workspace: Path = Path.cwd()
    dist_dir: Path | None = None

    # ------------------------------------------------------------------
    # 输出辅助
    # ------------------------------------------------------------------

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _not_implemented(self, what: str, milestone: str) -> None:
        """执行类端点的统一回执:501 + 指路,前端 toast 直接展示 detail。"""
        self._send_json(
            501,
            {
                "detail": f"{what}待接入(σ-server {milestone}:执行/审批/事件流,"
                "见 sigma-frontend/docs/sigma-backend-api-design.md)。当前工作台为只读回放。"
            },
        )

    def _static(self, rel: str) -> None:
        """SPA 静态资源;未命中路径回退 index.html(vite 构建的单页)。"""
        dist = self.dist_dir
        if dist is None or not dist.is_dir():
            self._send_json(
                503,
                {"detail": "前端未构建:先在 src/sigma-frontend 下 npm install && npm run build,"
                "或用 npm run dev 走 vite(5173)直连本服务。"},
            )
            return
        candidate = (dist / rel.lstrip("/")).resolve()
        if not candidate.is_file() or not candidate.is_relative_to(dist.resolve()):
            candidate = dist / "index.html"
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(candidate.suffix, "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        """安静模式:工作台轮询会让默认日志刷屏,只打非常规状态。"""
        pass

    # ------------------------------------------------------------------
    # 动词
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - 基类命名
        # URL 按 "/" 切分,不用 Path.parts——Windows 上 Path("/a").parts 的
        # 首元素是 "\\",按平台漂移;URL 语义只认正斜杠。
        clean = self.path.split("?", 1)[0]
        parts = [p for p in clean.split("/") if p]
        # API 路由:/api/v1/...
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "v1":
            self._route_api(parts[2:])
            return
        # 静态 SPA(其余一切路径回 index.html,前端自己路由)。
        self._static(clean.lstrip("/"))

    def _route_api(self, parts: list[str]) -> None:
        root = self.sessions_root
        # /system/ping
        if parts == ["system", "ping"]:
            self._send_json(
                200,
                {"ok": True, "mode": "http", "version": __version__, "workspace": str(self.workspace)},
            )
            return
        # /projects
        if parts == ["projects"]:
            self._send_json(200, [_project_payload(self.workspace)])
            return
        # /tasks(过滤参数本服务不实现——数据面单项目、量级小,前端本地过滤已够)
        if parts == ["tasks"]:
            self._send_json(200, _list_task_payloads(root))
            return
        # /tasks/{id}[/timeline]
        if len(parts) == 2 and parts[0] == "tasks":
            payload = _task_payload_for_id(root, parts[1])
            self._send_json(200, payload)  # 找不到 → null(契约:getTask 返回 Task | null)
            return
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "timeline":
            self._send_json(200, _timeline_payload(root, parts[1]))
            return
        # /automations、/plugins、/models
        if parts == ["automations"]:
            self._send_json(200, [])
            return
        if parts == ["plugins"]:
            self._send_json(200, _plugins_payload(self.workspace))
            return
        if parts == ["models"]:
            self._send_json(200, [{"id": "current", "name": "当前配置模型", "efforts": ["—"]}])
            return
        # /memory:契约之外的附加数据面(工作台"记忆"区)。
        if parts == ["memory"]:
            self._send_json(200, _memory_payload(self.workspace))
            return
        self._send_json(404, {"detail": f"未知端点:{'/'.join(parts)}"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        self._not_implemented(_what_from_path(self.path), "M2")

    def do_PATCH(self) -> None:  # noqa: N802 - 基类命名
        self._not_implemented(_what_from_path(self.path), "M2")

    def do_DELETE(self) -> None:  # noqa: N802 - 基类命名
        self._not_implemented("删除任务(会话 JSONL 是审计事实,工作台只读)", "—")

    def do_PUT(self) -> None:  # noqa: N802 - 基类命名
        self._not_implemented(_what_from_path(self.path), "M2")


def _what_from_path(path: str) -> str:
    """501 回执里的动词宾语(只区分大类,不再细分)。"""
    if "/automations" in path:
        return "自动化调度(σ 未实现调度器)"
    if "/plugins" in path:
        return "插件装卸(M3:接技能/工具装卸)"
    if "/projects" in path:
        return "注册工作区"
    return "任务执行(创建/推进/打断/审批)"


def _task_payload_for_id(sessions_root: Path, session_id: str) -> dict[str, Any] | None:
    """按 id 取单任务;安全网:先过一遍列表映射,避免路径段被拼进文件路径。"""
    for payload in _list_task_payloads(sessions_root):
        if payload["id"] == session_id:
            return payload
    return None


def make_server(
    *,
    host: str,
    port: int,
    sessions_root: Path,
    workspace: Path,
    dist_dir: Path | None,
) -> ThreadingHTTPServer:
    """装配:参数走类属性(Handler 实例由基类按请求构造,不走构造参数)。"""

    class _Handler(WorkbenchHandler):
        pass

    _Handler.sessions_root = sessions_root
    _Handler.workspace = workspace
    _Handler.dist_dir = dist_dir
    return ThreadingHTTPServer((host, port), _Handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sigma 工作台桥接服务(只读)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8301)
    parser.add_argument("--sessions-dir", default=None, help="会话目录,默认 ~/.sigma/sessions")
    parser.add_argument("--workspace", default=None, help="工作区根,默认当前目录")
    parser.add_argument(
        "--dist", default=None, help="前端构建产物目录,默认 src/sigma-frontend/dist"
    )
    args = parser.parse_args(argv)

    here = Path(__file__).resolve()
    sessions_root = (
        Path(args.sessions_dir) if args.sessions_dir else Path.home() / ".sigma" / "sessions"
    )
    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    dist_dir = Path(args.dist) if args.dist else here.parent.parent / "dist"
    server = make_server(
        host=args.host,
        port=args.port,
        sessions_root=sessions_root,
        workspace=workspace,
        dist_dir=dist_dir,
    )
    print(f"sigma 工作台桥接(只读):http://{args.host}:{args.port}")
    print(f"  会话目录  {sessions_root}")
    print(f"  工作区    {workspace}")
    print(f"  前端产物  {dist_dir}{'(存在)' if dist_dir.is_dir() else '(未构建)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
