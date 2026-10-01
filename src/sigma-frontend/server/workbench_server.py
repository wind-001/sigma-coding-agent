"""sigma 工作台桥接服务(最小执行环)。

为 ``src/sigma-frontend``(React 工作台)提供 sigma 真实数据的 HTTP 子集:
端点与前端 ``httpClient.ts`` 的契约一一对应(sigma-frontend/docs/sigma-backend-api-design.md),
另加 ``GET /tasks/{id}/timeline``(观测层 --timeline 的数据面)。

定位与边界(为什么不是 sigma-server)
    σ-server(FastAPI + SSE + 审批环)是**待拍板**的 M1/M2/M3。本服务是
    其中"能以现有能力落地"的最小执行环,stdlib ``http.server`` 实现,
    零新依赖、零核心改动:

    - **读**:会话 JSONL / 观测时间线 / 技能 / 记忆 / 内置工具——与 CLI 同源;
    - **写**:`POST /tasks`(草稿,元数据仅内存)+ `POST /tasks/{id}/messages`
      (**同步跑一轮**:``run_task`` 驱动 InteractiveSession,会话落真实 JSONL,
      断点续跑、影子快照、trace 与 CLI 完全同一条代码)。

    仍未接入(前端对应控件标"待接入"):审批交互确认(**工具调用自动放行**
    ——L1 路径沙箱与 L2 影子快照照常在岗)、打断 / steering、流式增量、
    自动化调度、插件装卸。会话事实永远只有一份(磁盘 JSONL),
    工作台重启只丢未执行的草稿。

    三道边界:不执行扩展代码(插件清单不含 ``extensions/*.py``——装载即执行);
    执行受 L1/L2 约束(写不越工作区、写批次前自动快照);只绑 127.0.0.1。

用法
    ./.venv/Scripts/python.exe src/sigma-frontend/server/workbench_server.py
    可选:--port 8301 --sessions-dir <dir> --workspace <dir> --dist <dir>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from sigma import __version__
from sigma.agent.messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma.config.resident_caps import CAPS, MEASURED, RESIDENT_BUDGET_TOKENS, caps_sum
from sigma.config.settings import resolve_api_key
from sigma.memory.file_store import memory_dir_for, scan_memory
from sigma.observability.timeline import build_timeline
from sigma.providers.anthropic.provider import AnthropicProvider
from sigma.providers.base import BaseProvider
from sigma.providers.messages import AssistantMessage, TextBlock, ToolCallBlock, UserMessage
from sigma.providers.openai.provider import OpenAICompatProvider
from sigma.providers.registry import builtin_providers
from sigma.providers.stamps import now as _now_stamp
from sigma.sdk import run_task
from sigma.sessions.sessions import (
    TRACE_SUFFIX,
    new_session_id,
    session_path,
    session_previews,
)
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

# ---------------------------------------------------------------------------
# 执行环(σ-server M2 的最小切片):创建任务 → 驱动 InteractiveSession → 回放
#
# 任务元数据**只在内存**(``_TASKS``);一旦发出第一条消息,执行经
# ``run_task`` 落进真实的会话 JSONL(~/.sigma/sessions/<id>.jsonl)——
# 会话事实永远只有一份,与 CLI 同源,工作台重启后草稿消失而已执行的会话
# 全部还在磁盘上。审批(交互确认)/打断/流式是 M2 的其余部分,仍未接入:
# 本切片的工具调用**自动放行**(approval=None),L1 路径沙箱与 L2 影子
# 快照照常在岗。
# ---------------------------------------------------------------------------

_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()
#: 正在执行的会话 id(同一会话不允许并发轮——会话树不是并发安全的)。
_RUNNING: set[str] = set()
#: 测试注入点:提供假 provider(否则按 sigma 配置真构造)。
_PROVIDER_FACTORY: Callable[[], BaseProvider] | None = None


def _default_preset() -> str:
    """默认厂商预设。cli.main 是这个常量的所有者,惰性导入(装配成本一次性)。"""
    from sigma.cli.main import DEFAULT_PRESET  # noqa: PLC0415 - 见 docstring

    return DEFAULT_PRESET


def _execution_params() -> tuple[str, str, str]:
    """解析执行三要素 ``(base_url, api_key, model)``。

    与 CLI 同一条解析链(env SIGMA_BASE_URL / SIGMA_MODEL / SIGMA_PRESET
    > 厂商预设默认值;密钥:命令行 > 环境变量 > 用户级 .env > 项目 .env)。
    没配 key 抛 ``LookupError``,由端点转成 400 的指路文案。
    """
    api_key, _source = resolve_api_key()
    if not api_key:
        raise LookupError(
            "未配置模型密钥:设环境变量 SIGMA_API_KEY,或写进 ~/.sigma/.env(推荐)"
            "——与 sigma CLI 用的是同一份配置。"
        )
    preset = os.environ.get("SIGMA_PRESET", _default_preset())
    spec = builtin_providers().resolve(preset)
    base_url = os.environ.get("SIGMA_BASE_URL") or spec.base_url
    model = os.environ.get("SIGMA_MODEL") or spec.default_model
    return base_url, api_key, model


def _make_provider(base_url: str, api_key: str) -> BaseProvider:
    """按预设的**线协议**分派 provider 实现类(与 cli._make_provider 同判据)。"""
    if _PROVIDER_FACTORY is not None:
        return _PROVIDER_FACTORY()
    preset = os.environ.get("SIGMA_PRESET", _default_preset())
    spec = builtin_providers().resolve(preset)
    if spec.protocol == "anthropic":
        return AnthropicProvider(base_url=base_url, api_key=api_key, provider_name=preset)
    return OpenAICompatProvider(base_url=base_url, api_key=api_key, provider_name=preset)


def _run_turn(task_id: str, text: str, workspace: Path, sessions_root: Path) -> str:
    """跑一轮:新建或续跑会话,返回最终状态(completed/error/stopped)。

    会话树:**有文件就续**(断点续跑,增量持久化免费),没有就建带 store
    的新树(一下笔就落盘)。L2 影子快照按 CLI 同一条规则装配
    (家目录工作区自动禁)。trace 默认开 → 时间线端点拿到真实指标。
    """
    base_url, api_key, model = _execution_params()
    provider = _make_provider(base_url, api_key)
    path = session_path(sessions_root, task_id)
    if path.is_file():
        tree = SessionTree.from_store(JsonlStore(sessions_root, task_id))
    else:
        tree = SessionTree(store=JsonlStore(sessions_root, task_id))
    from sigma.cli.main import (  # noqa: PLC0415 - cli 是这些工具函数的所有者
        checkpoint_disabled_reason as _checkpoint_disabled,
    )
    from sigma.cli.main import shadow_git_dir_for as _shadow_dir  # noqa: PLC0415

    disabled = _checkpoint_disabled(workspace, no_checkpoint_flag=False)
    shadow_dir = None if disabled else _shadow_dir(workspace)
    try:
        result = asyncio.run(
            run_task(
                text,
                provider=provider,
                workspace_root=workspace,
                model=model,
                session_id=task_id,
                tree=tree,
                shadow_git_dir=shadow_dir,
                max_rounds=20,
            )
        )
    finally:
        closer = getattr(provider, "aclose", None)
        if closer is not None:
            asyncio.run(closer())
    return str(result.status)


def _create_task(body: dict[str, Any]) -> dict[str, Any]:
    """创建草稿任务(前端 Composer/侧栏「新建会话」)。元数据仅内存。"""
    record: dict[str, Any] = {
        "id": new_session_id(),
        "title": str(body.get("title") or "").strip(),
        "description": str(body.get("description") or "").strip(),
        "access": str(body.get("access") or ""),
        "model": str(body.get("model") or ""),
        "effort": str(body.get("effort") or ""),
        "status": "draft",
        "createdAt": _now_stamp(),
    }
    with _TASKS_LOCK:
        _TASKS[record["id"]] = record
    return _draft_payload(record)


def _draft_payload(record: dict[str, Any]) -> dict[str, Any]:
    created = str(record.get("createdAt") or "")
    return {
        "id": record["id"],
        "projectId": PROJECT_ID,
        "title": str(record.get("title") or "") or "(新会话)",
        "description": str(record.get("description") or ""),
        "status": "draft",
        "access": str(record.get("access") or ""),
        "model": str(record.get("model") or ""),
        "effort": str(record.get("effort") or ""),
        "createdAt": created,
        "updatedAt": created,
        "events": [],
        "messageCount": 0,
        "sizeBytes": 0,
        "modifiedEpoch": 0,
    }


def _post_message(
    sessions_root: Path, workspace: Path, task_id: str, text: str
) -> tuple[int, dict[str, Any]]:
    """发一条消息并**同步**跑一轮(执行完才返回最终任务载荷)。

    并发守卫:同一会话同时只允许一轮(``409``);未知任务 ``404``;
    未配 key ``400``(指路文案)。执行失败不放大成 HTTP 500——会话文件里
    已经落了的事实照常回放,任务标 failed。
    """
    text = text.strip()
    if text == "":
        return 400, {"detail": "消息不能为空"}
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
        if record is None and not session_path(sessions_root, task_id).is_file():
            return 404, {"detail": f"任务 {task_id} 不存在(草稿随工作台重启消失,已执行的会话在磁盘上)"}
        if task_id in _RUNNING:
            return 409, {"detail": "该会话正在执行中,请等当前轮完成再发下一条"}
        _RUNNING.add(task_id)
        if record is not None:
            record["status"] = "running"
    try:
        try:
            _run_turn(task_id, text, workspace, sessions_root)
        except LookupError as exc:
            with _TASKS_LOCK:
                if record is not None:
                    record["status"] = "draft"  # 没跑起来,退回草稿
            return 400, {"detail": str(exc)}
        except Exception as exc:
            with _TASKS_LOCK:
                if record is not None:
                    record["status"] = "failed"
                    record["lastError"] = f"{type(exc).__name__}: {exc}"
        payload = _merged_task_payload(sessions_root, task_id)
        return 200, payload
    finally:
        with _TASKS_LOCK:
            _RUNNING.discard(task_id)


def _merged_task_payload(sessions_root: Path, task_id: str) -> dict[str, Any]:
    """执行后的任务载荷:磁盘会话重建(事实),草稿记录补充 access/effort(意图)。"""
    with _TASKS_LOCK:
        record = dict(_TASKS[task_id]) if task_id in _TASKS else None
    payload: dict[str, Any] | None = None
    if session_path(sessions_root, task_id).is_file():
        try:
            payload = _task_payload(
                sessions_root, task_id, first_user_text=None, message_count=0, modified=0
            )
        except Exception:
            payload = None  # 坏数据降级:退回草稿形状
    if payload is None:
        assert record is not None  # 执行过必有文件或草稿记录二者之一
        payload = _draft_payload(record)
        if record.get("lastError"):
            payload["events"] = [
                {"id": "e0", "kind": "note", "text": f"✗ 执行失败:{record['lastError']}", "at": _now_stamp()}
            ]
    elif record is not None:
        payload["access"] = str(record.get("access") or "")
        payload["effort"] = str(record.get("effort") or "")
    return payload


def _all_task_payloads(sessions_root: Path) -> list[dict[str, Any]]:
    """草稿(内存)+ 已执行会话(磁盘)合并;同 id 磁盘版本优先(事实更全)。"""
    disk = {payload["id"]: payload for payload in _list_task_payloads(sessions_root)}
    with _TASKS_LOCK:
        records = {rid: dict(record) for rid, record in _TASKS.items()}
    merged: dict[str, dict[str, Any]] = {}
    for rid, record in records.items():
        if rid in disk:
            payload = disk[rid]
            payload["access"] = str(record.get("access") or "") or payload["access"]
            payload["effort"] = str(record.get("effort") or "") or payload["effort"]
        else:
            payload = _draft_payload(record)
        if rid in _RUNNING:
            payload = {**payload, "status": "running"}
        merged[rid] = payload
    for rid, payload in disk.items():
        merged.setdefault(rid, payload)
    return sorted(merged.values(), key=lambda p: str(p.get("updatedAt") or ""), reverse=True)


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

    kind 映射:用户/助手消息 → ``message``(+ ``role`` 供气泡分边);
    assistant 声明的工具调用 → ``tool_call``;
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
                    "role": "user",
                    "text": _clip(str(message.message.content), 4000),
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
                        "role": "assistant",
                        "text": text,
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
                            "role": "tool",
                            "tool": block.name,
                            "text": _clip(f"{block.name}({args})", 220),
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
        """SPA 静态资源;未命中路径回退 index.html(vite 构建的单页)。

        两个防"重建后白屏"的细节(实测踩出来的):
        1. **带文件后缀且不存在的路径直接 404**,绝不回退——vite 的
           ``index-<hash>.js`` 每次构建换名,浏览器缓存的旧 index.html 会
           指向已删除的 hash;把 HTML 当 JS 返回(200 + text/html)的后果是
           模块解析炸掉、整页白屏,比 404 难排查得多。
        2. **index.html 发 no-cache**,hash 资源发 immutable——旧页面刷新后
           必拿新 index.html,新 index.html 必指向在场的资源。
        """
        dist = self.dist_dir
        if dist is None or not dist.is_dir():
            self._send_json(
                503,
                {"detail": "前端未构建:先在 src/sigma-frontend 下 npm install && npm run build,"
                "或用 npm run dev 走 vite(5173)直连本服务。"},
            )
            return
        clean = rel.lstrip("/")
        candidate = (dist / clean).resolve()
        is_asset = "/assets/" in f"/{clean}" or (
            Path(clean).suffix != "" and clean != "index.html"
        )
        if not candidate.is_file() or not candidate.is_relative_to(dist.resolve()):
            if is_asset:
                self._send_json(404, {"detail": f"资源不存在(前端已重新构建?):{clean}"})
                return
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
        # hash 资源内容不可变,可永久缓存;入口页必须每次回源拿新 hash 引用。
        cache = "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable"
        self.send_header("Cache-Control", cache)
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
        # /tasks(草稿+磁盘合并;过滤参数前端本地做——单项目、量级小)
        if parts == ["tasks"]:
            self._send_json(200, _all_task_payloads(root))
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
        # /budget:常驻区预算表(D4 v3,"表即常量")——工作台"上下文构成"看板的数据源。
        if parts == ["budget"]:
            self._send_json(
                200,
                {
                    "residentBudgetTokens": RESIDENT_BUDGET_TOKENS,
                    "capsSum": caps_sum(),
                    "caps": CAPS,
                    "measured": MEASURED,
                },
            )
            return
        # /memory:契约之外的附加数据面(工作台"记忆"区)。
        if parts == ["memory"]:
            self._send_json(200, _memory_payload(self.workspace))
            return
        self._send_json(404, {"detail": f"未知端点:{'/'.join(parts)}"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        clean = self.path.split("?", 1)[0]
        parts = [p for p in clean.split("/") if p]
        # 创建草稿任务(Composer / 侧栏「新建会话」)。
        if parts == ["api", "v1", "tasks"]:
            self._send_json(200, _create_task(self._read_json_body()))
            return
        # 发消息并同步跑一轮(执行完返回最终载荷;长轮询——UI 侧乐观显示 running)。
        if len(parts) == 5 and parts[:3] == ["api", "v1", "tasks"] and parts[4] == "messages":
            body = self._read_json_body()
            code, payload = _post_message(
                self.sessions_root, self.workspace, parts[3], str(body.get("text", ""))
            )
            self._send_json(code, payload)
            return
        self._not_implemented(_what_from_path(self.path), "M2")

    def _read_json_body(self) -> dict[str, Any]:
        """读 JSON 请求体;空/坏体按 {} 处理(各端点自己校验必填)。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

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
