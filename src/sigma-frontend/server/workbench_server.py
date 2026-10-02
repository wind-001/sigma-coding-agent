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
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs

from sigma import __version__
from sigma.agent.messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma.config.settings import resolve_api_key
from sigma.events.lifecycle import (
    ApprovalDecision,
    HookEvent,
    TextChunk,
    ThinkingChunk,
    ToolEnd,
    ToolStart,
)
from sigma.hooks.base import ApprovalHook, BaseHook
from sigma.memory.file_store import memory_dir_for, scan_memory
from sigma.observability.timeline import build_timeline
from sigma.prompts.system_prompt import SYSTEM_PROMPT
from sigma.providers.anthropic.provider import AnthropicProvider
from sigma.providers.base import BaseProvider
from sigma.providers.messages import (
    AssistantMessage,
    ThinkingBlock,
    TextBlock,
    ToolCallBlock,
    UserMessage,
)
from sigma.providers.openai.provider import OpenAICompatProvider
from sigma.providers.registry import builtin_providers
from sigma.providers.stamps import now as _now_stamp
from sigma.sdk import InteractiveSession
from sigma.security.approval import analyze_call
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
#: 每个执行中会话的流式增量缓冲:{"pieces": list[str], "lock", "done", "error"}。
#: pieces 由 TextChunk 钩子订阅者逐块追加("逐块透传,不聚合"——聚合了就没流式了)。
_DELTAS: dict[str, dict[str, Any]] = {}
_DELTAS_LOCK = threading.Lock()
#: 持久会话(每任务一个):steering/follow-up 队列与断点续跑都要求
#: **同一个 InteractiveSession 跨轮复用**——run_task 每次新建会话做不到。
_SESSIONS: dict[str, InteractiveSession] = {}
_SESSIONS_LOCK = threading.Lock()
#: 审批闸注册表:approve() 逐调用读 mode,工作台切权限档时热替换(见
#: _set_task_access)——无需重建会话,下一声工具调用立即生效。
_GATES: dict[str, _HttpApprovalGate] = {}  # noqa: F821 - 类定义在下面(from __future__ annotations)
_GATES_LOCK = threading.Lock()
#: 待审批项:task_id → [{id, tool, summary, event, decision, reason}]。
_APPROVALS: dict[str, list[dict[str, Any]]] = {}
_APPROVALS_LOCK = threading.Lock()
#: 测试注入点:提供假 provider(否则按 sigma 配置真构造)。
_PROVIDER_FACTORY: Callable[[], BaseProvider] | None = None

#: 工作区项目注册表(用户级,仓库外):二级工作区 + 会话→项目归属索引。
#: sigma 的会话目录是全局扁平的(~/.sigma/sessions),会话本身不记工作区——
#: 归属由工作台在执行时记下,导入前的历史会话归入主工作区(如实说明)。
_REGISTRY_PATH = Path.home() / ".sigma" / "workbench-projects.json"

#: 删除会话的回收站(移入而非 unlink:审计事实可恢复)。测试可替换。
_TRASH_DIR = Path.home() / ".sigma" / "trash"

#: 计划模式追加的系统提示词段(与 L3 审批的写类拒绝配合:
#: 模型先出计划,用户看完切回其他模式再执行)。
_PLAN_INSTRUCTION = (
    "【计划模式】先输出完整计划(要改哪些文件、每一步做什么、怎么验证),"
    "**不要调用 write/edit/bash 执行任何修改**;等用户切换模式后再动手。"
)

#: 各权限模式的提示行(前端下拉与这里同词)。
ACCESS_MODES: dict[str, str] = {
    "full": "完全访问",
    "auto": "自动编辑",
    "confirm": "变更前确认",
    "plan": "计划模式",
    "readonly": "只读",
}

#: 写类工具(readonly/plan 模式拒绝的对象)。
_WRITE_TOOLS = frozenset({"write", "edit", "bash", "multi_agent"})


def _registry_read() -> dict[str, Any]:
    if _REGISTRY_PATH.is_file():
        try:
            data = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}  # 注册表坏了按空处理(它只是索引,不是审计事实)
    return {}


def _registry_write(reg: dict[str, Any]) -> None:
    _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REGISTRY_PATH.write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 模型配置注册表(工作台"模型设置"):用户自定义 base_url/api_key/model_id
# 的条目,与内置 preset 并列进 /models;执行链按名字解析。apiKey 落盘在
# ~/.sigma/workbench-models.json——与 ~/.sigma/.env 同一信任域(本机明文),
# 且**绝不回传前端**(GET 载荷只有 hasKey 布尔)。
# ---------------------------------------------------------------------------

_MODELS_REGISTRY_PATH = Path.home() / ".sigma" / "workbench-models.json"

#: 档位随附方式:OpenAI 风格 reasoning_effort 字符串 / 智谱风格 thinking 对象。
#: 取值集合刻意封闭——线格式是协议事实,不该让用户自由发挥。
EFFORT_STYLES: tuple[str, ...] = ("reasoning_effort", "thinking")

_PROTOCOLS: tuple[str, ...] = ("openai-compat", "anthropic")


def _models_registry_read() -> dict[str, Any]:
    try:
        data = json.loads(_MODELS_REGISTRY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # 同项目注册表:配置坏了按空处理,不让工作台起不来


def _models_registry_write(reg: dict[str, Any]) -> None:
    _MODELS_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODELS_REGISTRY_PATH.write_text(
        json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _effort_extra_body(style: str, effort: str) -> dict[str, Any] | None:
    """档位值 → 请求体附加字段。空档位 = None(请求字节与无档位完全一致)。"""
    effort = effort.strip()
    if effort == "":
        return None
    if style == "thinking":
        return {"thinking": {"type": effort}}
    return {"reasoning_effort": effort}


def _save_model_entry(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """新增/更新模型条目。``name`` 是执行链的解析键,必须唯一;
    ``apiKey`` 留空 = 保留原值(密钥不回传前端,表单无从带回)。"""
    name = str(body.get("name") or "").strip()
    base_url = str(body.get("baseUrl") or "").strip()
    model_id = str(body.get("modelId") or "").strip()
    protocol = str(body.get("protocol") or "openai-compat").strip()
    style = str(body.get("effortStyle") or "reasoning_effort").strip()
    efforts_raw = body.get("efforts")
    if name == "" or base_url == "" or model_id == "":
        return 400, {"detail": "name / baseUrl / modelId 均必填"}
    if protocol not in _PROTOCOLS:
        return 400, {"detail": f"protocol 只支持 {'/'.join(_PROTOCOLS)}"}
    if style not in EFFORT_STYLES:
        return 400, {"detail": f"effortStyle 只支持 {'/'.join(EFFORT_STYLES)}"}
    efforts: list[str] = (
        [str(e).strip() for e in efforts_raw if str(e).strip()]
        if isinstance(efforts_raw, list)
        else []
    )
    reg = _models_registry_read()
    models: list[dict[str, Any]] = list(reg.get("models", []))
    entry_id = str(body.get("id") or "").strip()
    target: dict[str, Any] | None = next(
        (e for e in models if e.get("id") == entry_id), None
    )
    if target is None:
        target = {"id": "mdl-" + uuid.uuid4().hex[:8]}
        models.append(target)
    # name 是执行链的解析键:撞到**别的**条目(含改名撞名)一律拒绝。
    if any(e.get("name") == name and e is not target for e in models):
        return 400, {"detail": f"模型名 {name!r} 已存在(name 是解析键,不可重复)"}
    target.update(
        {
            "name": name,
            "protocol": protocol,
            "baseUrl": base_url,
            "modelId": model_id,
            "efforts": efforts,
            "effortStyle": style,
            "createdAt": target.get("createdAt", _now_stamp()),
        }
    )
    api_key = str(body.get("apiKey") or "")
    if api_key != "" or "apiKey" not in target:
        target["apiKey"] = api_key  # 新条目落空串;更新留空 = 保留原值
    reg["models"] = models
    _models_registry_write(reg)
    return 200, {"ok": True, "id": target["id"]}


def _remove_model_entry(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    entry_id = str(body.get("id") or "").strip()
    reg = _models_registry_read()
    models: list[dict[str, Any]] = list(reg.get("models", []))
    kept = [e for e in models if e.get("id") != entry_id]
    if len(kept) == len(models):
        return 404, {"detail": f"模型条目 {entry_id} 不存在"}
    reg["models"] = kept
    _models_registry_write(reg)
    return 200, {"ok": True}


def _all_projects(primary: Path) -> list[dict[str, Any]]:
    """主工作区(服务启动时的 workspace)+ 用户导入的二级工作区。"""
    reg = _registry_read()
    projects: list[dict[str, Any]] = [
        {
            "id": PROJECT_ID,
            "name": primary.name or "sigma",
            "repoPath": str(primary),
            "branch": "",
            "createdAt": "",
        }
    ]
    for entry in reg.get("projects", []):
        if entry.get("repoPath") == str(primary):
            continue  # 主工作区已在列
        projects.append(
            {
                "id": entry.get("id", ""),
                "name": entry.get("name", ""),
                "repoPath": entry.get("repoPath", ""),
                "branch": "",
                "createdAt": entry.get("createdAt", ""),
            }
        )
    return projects


def _active_project_id(primary: Path) -> str:
    """当前激活的工作区(切换语义):注册表记录,回退主工作区。"""
    reg = _registry_read()
    active = str(reg.get("active") or PROJECT_ID)
    known = {p.get("id") for p in _all_projects(primary)}
    return active if active in known else PROJECT_ID


def _activate_project(primary: Path, project_id: str) -> bool:
    if _project_by_id(primary, project_id) is None:
        return False
    reg = _registry_read()
    reg["active"] = project_id
    _registry_write(reg)
    return True


def _remove_project(primary: Path, project_id: str) -> tuple[bool, str]:
    """移除导入的工作区(主工作区不可移):其会话索引一并清除,
    这些会话回落主工作区(回放仍可用)。"""
    if project_id == PROJECT_ID:
        return False, "主工作区不可移除"
    reg = _registry_read()
    projects: list[dict[str, Any]] = [
        p for p in reg.get("projects", []) if p.get("id") != project_id
    ]
    if len(projects) == len(reg.get("projects", [])):
        return False, "工作区不存在"
    reg["projects"] = projects
    sessions: dict[str, str] = {
        sid: pid
        for sid, pid in reg.get("sessions", {}).items()
        if pid != project_id
    }
    reg["sessions"] = sessions
    if reg.get("active") == project_id:
        reg["active"] = PROJECT_ID
    _registry_write(reg)
    return True, ""


def _fs_listing(raw: str, include_files: bool = False) -> dict[str, Any]:
    """目录浏览:默认只列**子目录**(选工作区用);``include_files`` 追加
    文件条目(引用文件路径用)。两类都不读任何文件内容。
    目录在前文件在后,同组按名排序;隐藏项(``.``/``$`` 前缀)两版都跳过。"""
    if raw.strip() in ("", "drives", "/"):
        drives = [
            f"{letter}:\\"
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ"
            if Path(f"{letter}:\\").exists()
        ]
        return {
            "path": "",
            "parent": None,
            "entries": [{"name": d, "path": d, "isDir": True} for d in drives],
        }
    path = Path(raw)
    if not path.is_dir():
        return {"path": raw, "parent": None, "entries": [], "error": "目录不存在"}
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(path.iterdir(), key=lambda c: c.name.lower())
        for child in children:
            if child.name.startswith((".", "$")):
                continue
            if child.is_dir():
                entries.append({"name": child.name, "path": str(child), "isDir": True})
            elif include_files and child.is_file():
                entries.append({"name": child.name, "path": str(child), "isDir": False})
    except (PermissionError, OSError) as exc:
        return {"path": str(path), "parent": None, "entries": [], "error": f"无法读取:{exc}"}
    # 盘符根(D:\)的 path.parent == path,若同样给 None,「上一级」在根目录
    # 被禁死、永远回不到盘符列表换盘——故盘符根的 parent 给空串
    # (空 path 在本函数入口即"此电脑"层)。POSIX "/" 走不到这(入口已拦)。
    if path.parent != path:
        parent: str | None = str(path.parent)
    elif path.drive:
        parent = ""
    else:
        parent = None
    return {"path": str(path), "parent": parent, "entries": entries}


def _register_project(primary: Path, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """导入工作区:校验目录存在,去重后写入注册表。"""
    repo = str(body.get("repoPath") or "").strip().strip('"')
    if repo == "":
        return 400, {"detail": "repoPath 必填(工作区绝对路径)"}
    path = Path(repo)
    if not path.is_dir():
        return 400, {"detail": f"目录不存在:{repo}"}
    reg = _registry_read()
    projects: list[dict[str, Any]] = list(reg.get("projects", []))
    for entry in projects:
        if entry.get("repoPath") == str(path):
            return 200, {"id": entry["id"], "name": entry["name"], "repoPath": str(path), "branch": "", "createdAt": entry.get("createdAt", "")}
    project_id = "proj-" + uuid.uuid4().hex[:8]
    entry = {
        "id": project_id,
        "name": str(body.get("name") or "").strip() or path.name,
        "repoPath": str(path),
        "createdAt": _now_stamp(),
    }
    projects.append(entry)
    reg["projects"] = projects
    _registry_write(reg)
    return 200, {"id": project_id, "name": entry["name"], "repoPath": str(path), "branch": "", "createdAt": entry["createdAt"]}


def _project_by_id(primary: Path, project_id: str) -> dict[str, Any] | None:
    for project in _all_projects(primary):
        if project["id"] == project_id:
            return project
    return None


def _session_project_id(primary: Path, session_id: str) -> str:
    """会话归属:执行时记入注册表索引;无记录的历史会话归主工作区(如实)。"""
    reg = _registry_read()
    index: dict[str, str] = reg.get("sessions", {})
    return index.get(session_id, PROJECT_ID)


def _index_session(session_id: str, project_id: str) -> None:
    reg = _registry_read()
    index: dict[str, str] = dict(reg.get("sessions", {}))
    index[session_id] = project_id
    reg["sessions"] = index
    _registry_write(reg)


class _HttpApprovalGate(ApprovalHook):
    """HTTP 审批环(M2 切片):待确认项入列表,前端决策唤醒(线程 Event)。

    模式映射(与前端 ACCESS_OPTIONS 同词,以 sigma 现有能力为核心):
    - **full 完全访问**:全部放行(现状行为);
    - **auto 自动编辑**:危险 bash(analyze_call 命中)才确认,编辑自动;
    - **confirm 变更前确认**:写类工具与危险 bash 都确认,只读放行;
    - **plan 计划模式**:写类一律拒(提示先出计划),配合系统提示词计划段;
    - **readonly 只读**:写类一律拒。
    审批等待用 ``asyncio.to_thread(event.wait)``——不阻塞执行循环。
    """

    name = "workbench-approval"

    def __init__(self, mode: str, task_id: str, workspace: Path) -> None:
        self._mode = mode if mode in ACCESS_MODES else "full"
        self._task_id = task_id
        self._workspace = workspace

    def set_mode(self, mode: str) -> None:
        """中途换挡(工作台权限自由切换):approve() 每次调用都读 self._mode,
        改完下一声工具调用立即生效——无需重建会话。线程安全靠 GIL 的
        属性赋值原子性(模式字符串无中间态)。"""
        self._mode = mode if mode in ACCESS_MODES else "full"

    def _ask(self, name: str, arguments: dict[str, Any]) -> ApprovalDecision:
        request_id = uuid.uuid4().hex[:8]
        entry: dict[str, Any] = {
            "id": request_id,
            "tool": name,
            "summary": _clip(json.dumps(arguments, ensure_ascii=False), 220),
            "event": threading.Event(),
            "decision": "deny",
            "reason": "等待审批超时(300s),自动拒绝",
        }
        with _APPROVALS_LOCK:
            _APPROVALS.setdefault(self._task_id, []).append(entry)
        got = entry["event"].wait(300)
        with _APPROVALS_LOCK:
            pending = _APPROVALS.get(self._task_id, [])
            if entry in pending:
                pending.remove(entry)
        if not got:
            return ApprovalDecision(allowed=False, reason=entry["reason"])
        if entry["decision"] == "approve":
            return ApprovalDecision(allowed=True)
        return ApprovalDecision(
            allowed=False,
            reason=str(entry.get("reason") or "已在工作台拒绝"),
        )

    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        mode = self._mode
        if mode == "full":
            return ApprovalDecision(allowed=True)
        if mode == "readonly":
            if name in _WRITE_TOOLS:
                return ApprovalDecision(
                    allowed=False, reason="只读模式:写类工具被拒(工作台)"
                )
            return ApprovalDecision(allowed=True)
        if mode == "plan":
            if name in _WRITE_TOOLS:
                return ApprovalDecision(
                    allowed=False,
                    reason="计划模式:先给出计划,不要执行修改;切回其他模式后再执行",
                )
            return ApprovalDecision(allowed=True)
        findings = analyze_call(name, arguments, self._workspace)
        if mode == "auto":
            if findings:
                return await asyncio.to_thread(self._ask, name, arguments)
            return ApprovalDecision(allowed=True)
        # confirm:写类工具与危险调用都要确认
        if name in _WRITE_TOOLS or findings:
            return await asyncio.to_thread(self._ask, name, arguments)
        return ApprovalDecision(allowed=True)


def _decide_approval(task_id: str, request_id: str, decision: str) -> bool:
    """前端审批决策:找到待确认项,写入结论并唤醒执行线程。"""
    with _APPROVALS_LOCK:
        for entry in _APPROVALS.get(task_id, []):
            if entry["id"] == request_id:
                entry["decision"] = "approve" if decision == "approve" else "deny"
                if decision != "approve":
                    entry["reason"] = "已在工作台拒绝"
                entry["event"].set()
                return True
    return False


def _pending_approvals(task_id: str) -> list[dict[str, str]]:
    with _APPROVALS_LOCK:
        pending = list(_APPROVALS.get(task_id, []))
    return [
        {"id": e["id"], "tool": e["tool"], "summary": e["summary"]} for e in pending
    ]


def _set_task_access(
    primary: Path, task_id: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """中途切换权限模式(human-in-the-loop,星辰 2026-10-02):记录更新 +
    存活会话审批闸热替换——approve() 逐调用读 mode,下一声工具调用立即生效。
    会话尚未执行过时只改记录,下次执行按新档装配(plan 模式的系统提示词段
    在会话创建时注入,存活会话中途切 plan 只有闸行为、无提示词引导——如实)。"""
    mode = str(body.get("access") or "").strip()
    if mode not in ACCESS_MODES:
        return 400, {"detail": f"access 只支持 {'/'.join(ACCESS_MODES)}"}
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
        if record is None:
            # 纯磁盘会话(本进程没跑过):补一条最小记录,让载荷与后续
            # 执行都读到新档位。归属按注册表如实回填。
            record = {
                "id": task_id,
                "projectId": _session_project_id(primary, task_id),
                "title": "",
                "access": mode,
            }
            _TASKS[task_id] = record
        else:
            record["access"] = mode
    with _GATES_LOCK:
        gate = _GATES.get(task_id)
    if gate is not None:
        gate.set_mode(mode)
    return 200, {"ok": True, "access": mode}


def _delete_task(sessions_root: Path, task_id: str) -> tuple[int, dict[str, Any]]:
    """删除会话(审计安全版):JSONL 与 trace **移入回收站**(_TRASH_DIR,
    不直接 unlink——删除可恢复,审计事实仍在盘上);内存草稿记录、流式缓冲、
    缓存会话与注册表归属索引一并清除。执行中的会话拒绝删除。"""
    if task_id in _RUNNING:
        return 409, {"detail": "会话执行中,等本轮结束再删"}
    if (
        task_id == ""
        or len(task_id) > 80
        or ".." in task_id
        or set(task_id) - set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    ):
        return 400, {"detail": "非法任务 id"}
    with _TASKS_LOCK:
        record = _TASKS.pop(task_id, None)
    moved = False
    src = session_path(sessions_root, task_id)
    trace = src.with_name(src.name + TRACE_SUFFIX)
    if src.is_file() or trace.is_file():
        trash = _TRASH_DIR
        trash.mkdir(parents=True, exist_ok=True)
        stamp = _now_stamp().replace(":", "").replace(" ", "-")
        if src.is_file():
            src.replace(trash / f"{task_id}.{stamp}.jsonl")
            moved = True
        if trace.is_file():
            trace.replace(trash / f"{task_id}.{stamp}{TRACE_SUFFIX}")
    with _DELTAS_LOCK:
        _DELTAS.pop(task_id, None)
    with _APPROVALS_LOCK:
        _APPROVALS.pop(task_id, None)
    with _SESSIONS_LOCK:
        _SESSIONS.pop(task_id, None)
    with _GATES_LOCK:
        _GATES.pop(task_id, None)
    reg = _registry_read()
    index = reg.get("sessions", {})
    if task_id in index:
        reg["sessions"] = {k: v for k, v in index.items() if k != task_id}
        _registry_write(reg)
    if record is None and not moved:
        return 404, {"detail": "任务不存在"}
    return 200, {"ok": True}


def _session_of(task_id: str) -> InteractiveSession | None:
    with _SESSIONS_LOCK:
        return _SESSIONS.get(task_id)


def _queue_op(task_id: str, op: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """队列操作(图一语义):steer=「立即」打断注入;queue=默认排队;
    queue-remove=删除排队项;queue-edit=改写排队文本(星辰 2026-10-02)。
    会话尚未创建(没执行过)时无队列可操作。"""
    text = str(body.get("text") or "").strip()
    if op in ("steer", "queue", "queue-edit") and text == "":
        return 400, {"detail": "内容不能为空"}
    session = _session_of(task_id)
    if session is None:
        return 409, {"detail": "会话尚未开始执行(发第一条消息后再使用队列)"}
    if op == "steer":
        session.submit_steering(text)
        return 200, {"ok": True, "steered": True}
    if op == "queue":
        session.submit_followup(text)
        return 200, {"ok": True, "queued": True}
    kind = str(body.get("kind") or "followup")
    index = int(body.get("index") or -1)
    if op == "queue-edit":
        edited = session.edit_queued(
            kind="steering" if kind == "steering" else "followup", index=index, text=text
        )
        return 200, {"ok": edited}
    removed = session.drop_queued(
        kind="steering" if kind == "steering" else "followup", index=index
    )
    return 200, {"ok": removed}


class _StreamCollector(BaseHook):
    """流式订阅者:把模型过程**增量**追加进本会话缓冲,**结构化分块**。

    collector 随会话缓存（每会话一个），但缓冲区**按 task_id 实时读取**
    （``_DELTAS[task_id]``）——每一轮 ``_post_message`` 都会换新缓冲区，
    若把缓冲区绑死在 collector 上，第二轮起事件会写进上一轮的旧缓冲区，
    deltas 永远为空，前端只能在轮结束后一次性拉全量（实测 2026-10-02）。

    块形状(前端直播区按块渲染,与回放气泡流同一套视觉,星辰 2026-10-02
    "参考成熟 agent:文本块/思考块/工具调用块分开"):
    - ``{"k": "text", "t": ...}``        模型正文增量(逐块透传,不聚合);
    - ``{"k": "thinking", "t": ...}``    思考增量(当前 provider 不产生,订阅
      零成本留通路——一旦上游有了直播区自动出现思考块);
    - ``{"k": "tool_start", "name": ..., "args": ...}``   工具即将执行;
    - ``{"k": "tool_end", "name": ..., "ok": ..., "preview": ...}`` 结果落定。
    这是 sigma 文档写明的唯一扩展面("钩子是唯一扩展面")——流式零核心改动。
    """

    name = "workbench-stream"

    def __init__(self, task_id: str) -> None:
        self._task_id = task_id

    def events(self) -> tuple[type[HookEvent], ...]:
        return (TextChunk, ThinkingChunk, ToolStart, ToolEnd)

    def on_event(self, event: HookEvent) -> None:
        piece: dict[str, Any] | None = None
        if isinstance(event, TextChunk):
            piece = {"k": "text", "t": event.text}
        elif isinstance(event, ThinkingChunk):
            piece = {"k": "thinking", "t": event.text}
        elif isinstance(event, ToolStart):
            piece = {
                "k": "tool_start",
                "name": event.name,
                "args": _clip(json.dumps(event.arguments, ensure_ascii=False), 220),
            }
        elif isinstance(event, ToolEnd):
            piece = {
                "k": "tool_end",
                "name": event.name,
                "ok": bool(event.ok),
                "preview": _clip(event.preview, 220),
            }
        if piece is None:
            return
        with _DELTAS_LOCK:
            buffer = _DELTAS.get(self._task_id)
        if buffer is None:
            return  # 轮间隙/缓冲已清:事件无处可写,如实丢弃
        with buffer["lock"]:
            buffer["pieces"].append(piece)


def _default_preset() -> str:
    """默认厂商预设。cli.main 是这个常量的所有者,惰性导入(装配成本一次性)。"""
    from sigma.cli.main import DEFAULT_PRESET  # noqa: PLC0415 - 见 docstring

    return DEFAULT_PRESET


def _execution_params(
    model_hint: str = "", effort_hint: str = ""
) -> tuple[str, str, str, str, dict[str, Any] | None]:
    """解析执行五要素 ``(base_url, api_key, model, protocol, extra_body)``。

    模型解析链(按优先级):
    1. **用户自定义条目**(模型设置里配的,name 精确匹配)——base_url/
       model_id/协议/key 全按条目,apiKey 留空才走 resolve_api_key 链;
       档位按条目声明的 ``effortStyle`` 随附;
    2. **内置 preset**(与 CLI ``--preset`` 同一套注册表词汇)——显式选择
       整体接管 preset/base_url/model;
    3. 都没有 → 与 CLI 同链(env SIGMA_BASE_URL / SIGMA_MODEL / SIGMA_PRESET
       > 厂商预设默认值;密钥:命令行 > 环境变量 > 用户级 .env > 项目 .env)。

    没配 key(且条目没带 key)抛 ``LookupError``,由端点转成 400 指路文案。
    """
    registry = builtin_providers()
    hint = model_hint.strip()
    if hint != "":
        entry = next(
            (e for e in _models_registry_read().get("models", []) if e.get("name") == hint),
            None,
        )
        if entry is not None:
            api_key = str(entry.get("apiKey") or "")
            if api_key == "":
                chained, _source = resolve_api_key()
                api_key = chained or ""
                if api_key == "":
                    raise LookupError(
                        f"模型「{hint}」未配置密钥:在模型设置里填 apiKey,"
                        "或走 SIGMA_API_KEY / ~/.sigma/.env 链。"
                    )
            style = str(entry.get("effortStyle") or "reasoning_effort")
            return (
                str(entry.get("baseUrl") or ""),
                api_key,
                str(entry.get("modelId") or ""),
                str(entry.get("protocol") or "openai-compat"),
                _effort_extra_body(style, effort_hint),
            )
        if hint in registry:
            spec = registry.resolve(hint)
            chained, _source = resolve_api_key()
            api_key = chained or ""
            if not api_key:
                raise LookupError(
                    "未配置模型密钥:设环境变量 SIGMA_API_KEY,或写进 ~/.sigma/.env(推荐)"
                    "——与 sigma CLI 用的是同一份配置。"
                )
            return (
                spec.base_url,
                api_key,
                spec.default_model,
                spec.protocol,
                _effort_extra_body("reasoning_effort", effort_hint),
            )
    chained, _source = resolve_api_key()
    api_key = chained or ""
    if not api_key:
        raise LookupError(
            "未配置模型密钥:设环境变量 SIGMA_API_KEY,或写进 ~/.sigma/.env(推荐)"
            "——与 sigma CLI 用的是同一份配置。"
        )
    preset = os.environ.get("SIGMA_PRESET", _default_preset())
    spec = registry.resolve(preset)
    base_url = os.environ.get("SIGMA_BASE_URL") or spec.base_url
    model = os.environ.get("SIGMA_MODEL") or spec.default_model
    return (
        base_url,
        api_key,
        model,
        spec.protocol,
        _effort_extra_body("reasoning_effort", effort_hint),
    )


def _make_provider(base_url: str, api_key: str, protocol: str) -> BaseProvider:
    """按**线协议**分派 provider 实现类(与 cli._make_provider 同判据)。"""
    if _PROVIDER_FACTORY is not None:
        return _PROVIDER_FACTORY()
    if protocol == "anthropic":
        return AnthropicProvider(base_url=base_url, api_key=api_key, provider_name=protocol)
    return OpenAICompatProvider(base_url=base_url, api_key=api_key, provider_name=protocol)


def _get_or_create_session(
    task_id: str,
    repo: Path,
    access: str,
    sessions_root: Path,
) -> InteractiveSession:
    """取(或建)任务的**持久会话**:steering/follow-up 队列与断点续跑
    都要求同一个 InteractiveSession 跨轮复用——这是 run_task 做不到的,
    所以工作台直接走 SDK 装配(与 CLI 同一条构造路径、同一批默认值)。
    流式 collector 随会话建一次(缓冲区按 task_id 每轮实时读,见
    _StreamCollector)。"""
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(task_id)
    if session is not None:
        return session
    # 模型/档位:任务记录里显式选的优先(自定义条目 > 内置 preset > CLI 同链);
    # 历史/磁盘会话无记录 → 与 CLI 同链。会话级冻结(见 InteractiveSession)。
    record = _TASKS.get(task_id) or {}
    base_url, api_key, model, protocol, extra_body = _execution_params(
        str(record.get("model") or ""), str(record.get("effort") or "")
    )
    provider = _make_provider(base_url, api_key, protocol)
    path = session_path(sessions_root, task_id)
    if path.is_file():
        tree = SessionTree.from_store(JsonlStore(sessions_root, task_id))
    else:
        tree = SessionTree(store=JsonlStore(sessions_root, task_id))
    from sigma.cli.main import (  # noqa: PLC0415 - cli 是工具函数的所有者
        checkpoint_disabled_reason as _checkpoint_disabled,
    )
    from sigma.cli.main import shadow_git_dir_for as _shadow_dir  # noqa: PLC0415

    disabled = _checkpoint_disabled(repo, no_checkpoint_flag=False)
    shadow_dir = None if disabled else _shadow_dir(repo)
    access = access if access in ACCESS_MODES else "full"
    # 审批闸**恒挂载**(full 本身就是秒放行路径,行为不变):中途切档时
    # 从 _GATES 拿到同一个 gate 热替换模式即可,不用重建会话。
    gate = _HttpApprovalGate(access, task_id, repo)
    with _GATES_LOCK:
        _GATES[task_id] = gate
    approval: ApprovalHook = gate
    system_prompt = SYSTEM_PROMPT + (
        ("\n\n" + _PLAN_INSTRUCTION) if access == "plan" else ""
    )
    session = InteractiveSession(
        provider=provider,
        workspace_root=repo,
        model=model,
        system_prompt=system_prompt,
        max_rounds=20,
        extra_body=extra_body,
        tree=tree,
        session_id=task_id,
        shadow_git_dir=shadow_dir,
        enable_checkpoint=shadow_dir is not None,
        enable_trace=True,
        approval=approval,
        extra_hooks=[_StreamCollector(task_id)],
    )
    with _SESSIONS_LOCK:
        _SESSIONS[task_id] = session
    return session


def _project_of(task_id: str, record: dict[str, Any] | None, primary: Path) -> dict[str, Any]:
    """任务的工作区:草稿记录里记的 projectId → 注册表;执行时也写归属索引。"""
    project_id = (record or {}).get("projectId") or _session_project_id(primary, task_id)
    return _project_by_id(primary, project_id) or {
        "id": PROJECT_ID,
        "name": primary.name or "sigma",
        "repoPath": str(primary),
    }


def _create_task(body: dict[str, Any]) -> dict[str, Any]:
    """创建草稿任务(前端 Composer/侧栏「新建会话」)。元数据仅内存。"""
    record: dict[str, Any] = {
        "id": new_session_id(),
        "projectId": str(body.get("projectId") or PROJECT_ID),
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
        "projectId": str(record.get("projectId") or PROJECT_ID),
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
    primary: Path, sessions_root: Path, task_id: str, text: str
) -> tuple[int, dict[str, Any]]:
    """发一条消息:**立即返回 running**,一轮在后台线程执行(前端流式轮询)。

    并发守卫:同一会话同时只允许一轮(``409``);未知任务 ``404``;
    未配 key ``400``(指路文案,启动线程前先验,快速失败)。
    会话→项目归属索引在本入口写入(工作台执行过的会话才知道归属)。
    """
    text = text.strip()
    if text == "":
        return 400, {"detail": "消息不能为空"}
    try:
        _execution_params()  # 快速失败:没配 key 不起线程
    except LookupError as exc:
        return 400, {"detail": str(exc)}
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
        if record is None and not session_path(sessions_root, task_id).is_file():
            return 404, {"detail": f"任务 {task_id} 不存在(草稿随工作台重启消失,已执行的会话在磁盘上)"}
        if task_id in _RUNNING:
            return 409, {"detail": "该会话正在执行中,输入会自动排队(或点「立即」打断注入)"}
        _RUNNING.add(task_id)
        if record is not None:
            record["status"] = "running"
    project = _project_of(task_id, record, primary)
    _index_session(task_id, str(project["id"]))
    buffer: dict[str, Any] = {
        "pieces": [],
        "lock": threading.Lock(),
        "done": False,
        "error": None,
    }
    with _DELTAS_LOCK:
        _DELTAS[task_id] = buffer
    threading.Thread(
        target=_turn_thread,
        args=(task_id, text, project, sessions_root, buffer, primary),
        daemon=True,
        name=f"wb-turn-{task_id}",
    ).start()
    payload = _merged_task_payload(primary, sessions_root, task_id)
    payload["status"] = "running"
    return 200, payload


#: 持久事件循环(独立线程,**永不关闭**):InteractiveSession 的 httpx
#: AsyncClient 与 asyncio.Lock 都绑定**创建时**的运行循环——之前每轮
#: ``asyncio.run`` 各造一个循环,第二轮起客户端拿着已关闭循环的连接池,
#: 必现 ``RuntimeError: Event loop is closed``(星辰实测 2026-10-02)。
#: CLI 是整会话共用一个循环;工作台每轮一循环是自创用法,不是 SDK 的锅。
_TURN_LOOP: asyncio.AbstractEventLoop | None = None
_TURN_LOOP_LOCK = threading.Lock()


def _run_on_persistent_loop(coro: Any) -> Any:
    """把一轮协程提交到持久循环并阻塞到完成(替代 asyncio.run)。"""
    global _TURN_LOOP
    with _TURN_LOOP_LOCK:
        if _TURN_LOOP is None or _TURN_LOOP.is_closed():
            _TURN_LOOP = asyncio.new_event_loop()
            threading.Thread(
                target=_TURN_LOOP.run_forever, daemon=True, name="wb-turn-loop"
            ).start()
    return asyncio.run_coroutine_threadsafe(coro, _TURN_LOOP).result()


def _stop_task(task_id: str) -> tuple[int, dict[str, Any]]:
    """外部强制中断(星辰 2026-10-02):调 ``InteractiveSession.interrupt()``,
    协作式停止——在跑的工具先完成,流在下一个块边界停;树上状态已持久化,
    断点重续免费。线程安全(interrupt 可从任意线程调)。

    后端已无在跑的轮时**不回 409 而是返回成功 + interrupted=False**:
    前端的 running 可能是滞留状态(轮已结束/桥接重启过内存态丢失),
    此时"停止"的正确语义是自愈——把滞留的 running 就地纠正,而不是
    让界面永远卡在执行态(实测 2026-10-02)。"""
    if task_id in _RUNNING:
        session = _session_of(task_id)
        if session is None:
            return 409, {"detail": "会话未装配,无法中断"}
        stopped = session.interrupt()
        return 200, {"ok": True, "interrupted": stopped}
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
        if record is not None and record.get("status") == "running":
            record["status"] = "draft"
    return 200, {"ok": True, "interrupted": False}


def _turn_thread(
    task_id: str,
    text: str,
    project: dict[str, Any],
    sessions_root: Path,
    buffer: dict[str, Any],
    primary: Path,
) -> None:
    """后台执行一轮(持久会话);follow-up 队列在本线程**自动续跑**
    ("默认排队"的语义——排队项在当前任务完成后逐条执行)。"""
    try:
        session = _get_or_create_session(
            task_id,
            Path(str(project["repoPath"])),
            str(((_TASKS.get(task_id) or {}).get("access")) or "full"),
            sessions_root,
        )

        async def _run_all() -> str:
            result = await session.send(text)
            statuses = [str(result.status)]
            guard = 0
            while session.has_followups() and guard < 10:
                guard += 1
                if statuses[-1] != "completed":
                    # 中断/出错后**不再自动续跑排队项**——用户点停止的意图是
                    # "停下来",接着把排队的任务跑完违背意图(星辰 2026-10-02)。
                    # 排队项保留在队列里:可续跑后执行,也可在界面手动删除。
                    break
                nxt = session.pop_followup()
                nxt_result = await session.send(nxt)
                statuses.append(str(nxt_result.status))
            return statuses[-1]

        status = _run_on_persistent_loop(_run_all())
        if status != "completed":
            with _TASKS_LOCK:
                record = _TASKS.get(task_id)
                if record is not None:
                    record["status"] = "failed" if status == "error" else "draft"
    except Exception as exc:
        buffer["error"] = f"{type(exc).__name__}: {exc}"
        with _TASKS_LOCK:
            record = _TASKS.get(task_id)
            if record is not None:
                record["status"] = "failed"
                record["lastError"] = buffer["error"]
    finally:
        buffer["done"] = True
        with _TASKS_LOCK:
            _RUNNING.discard(task_id)


def _deltas_payload(task_id: str, since: int) -> dict[str, Any]:
    """``since`` 之后的**结构化块**(文本/思考/工具开始/工具结束);
    running=False 表示轮已结束(含最终错误)。seq = 块总数。"""
    with _DELTAS_LOCK:
        buffer = _DELTAS.get(task_id)
    if buffer is None:
        return {"seq": 0, "pieces": [], "running": False, "error": None}
    with buffer["lock"]:
        pieces = list(buffer["pieces"])
    tail = pieces[since:] if since < len(pieces) else []
    return {
        "seq": len(pieces),
        "pieces": tail,
        "running": not bool(buffer["done"]),
        "error": buffer.get("error"),
    }


def _merged_task_payload(
    primary: Path, sessions_root: Path, task_id: str
) -> dict[str, Any]:
    """执行后的任务载荷:磁盘会话重建(事实),草稿记录补充 access/effort(意图)。"""
    with _TASKS_LOCK:
        record = dict(_TASKS[task_id]) if task_id in _TASKS else None
    payload: dict[str, Any] | None = None
    if session_path(sessions_root, task_id).is_file():
        try:
            payload = _task_payload(
                primary, sessions_root, task_id, first_user_text=None, message_count=0, modified=0
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


def _all_task_payloads(primary: Path, sessions_root: Path) -> list[dict[str, Any]]:
    """草稿(内存)+ 已执行会话(磁盘)合并;同 id 磁盘版本优先(事实更全)。"""
    disk = {
        payload["id"]: payload
        for payload in _list_task_payloads(primary, sessions_root)
    }
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


def _epoch_of(stamp: str) -> float | None:
    """可读时间戳 → epoch(工具耗时计算用);解析不了返回 None。"""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except (ValueError, TypeError):
        return None


def _events_from_history(history: list[Any]) -> list[dict[str, Any]]:
    """把会话树压成前端 ``TaskEvent[]``(只读回放,截断在展示层语义内)。

    kind 映射(参照成熟 agent UI 的区分度,星辰 2026-10-01):
    - 用户/助手消息 → ``message``(+ ``role`` 供气泡分边,助手=平铺正文);
    - assistant 的思考块 → ``thinking``(muted 行,与正文区分);
    - 工具调用 → ``tool_call``:**与结果配对**,带 ``status``(ok/error)与
      ``durationMs``(调用到结果的时间戳差,≈ 口径)——图二的"已完成"列;
    - 悬空调用(无结果=中断)→ status 缺省,前端显示"未完成"。
    取**最后** EVENTS_LIMIT 条——新会话看全,老会话看尾巴。
    """
    events: list[dict[str, Any]] = []
    pending_calls: dict[str, dict[str, Any]] = {}
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
            for block in message.message.content:
                if isinstance(block, ThinkingBlock):
                    thinking = block.thinking.strip()
                    if thinking:
                        events.append(
                            {
                                "id": f"e{len(events)}",
                                "kind": "thinking",
                                "role": "thinking",
                                "text": _clip(thinking, 600),
                                "at": at,
                            }
                        )
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
                    pending_calls[block.id] = {"name": block.name, "at": at, "args": block.arguments}
        elif isinstance(message, ToolResultAgentMessage):
            call = pending_calls.pop(message.tool_call_id, None)
            duration: int | None = None
            if call is not None:
                start, end = _epoch_of(call["at"]), _epoch_of(at)
                if start is not None and end is not None:
                    duration = max(0, round((end - start) * 1000))
            args = json.dumps((call or {}).get("args") or {}, ensure_ascii=False)
            events.append(
                {
                    "id": f"e{len(events)}",
                    "kind": "tool_call",
                    "role": "tool",
                    "tool": message.tool_name,
                    "status": "error" if message.is_error else "ok",
                    "durationMs": duration,
                    "text": _clip(f"{message.tool_name}({args})", 220),
                    "at": at,
                }
            )
    # 悬空调用(assistant 声明了但没有结果 = 中断)→ 无 status,前端显示"未完成"。
    for call in pending_calls.values():
        args = json.dumps(call.get("args") or {}, ensure_ascii=False)
        events.append(
            {
                "id": f"e{len(events)}",
                "kind": "tool_call",
                "role": "tool",
                "tool": call["name"],
                "status": "",
                "durationMs": None,
                "text": _clip(f"{call['name']}({args})", 220),
                "at": call["at"],
            }
        )
    # id 在截断后重排——key 唯一即可,不必与会话节点 id 对应。
    return [{**event, "id": f"e{index}"} for index, event in enumerate(events[-EVENTS_LIMIT:])]


def _task_payload(
    primary: Path,
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
        "projectId": _session_project_id(primary, session_id),
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


def _list_task_payloads(primary: Path, sessions_root: Path) -> list[dict[str, Any]]:
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
                primary,
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
        clean, _, query = self.path.partition("?")
        parts = [p for p in clean.split("/") if p]
        self._query = parse_qs(query)
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
        # /projects:主工作区 + 导入的二级工作区
        if parts == ["projects"]:
            self._send_json(200, _all_projects(self.workspace))
            return
        # /workspace:当前激活的工作区(切换语义)
        if parts == ["workspace"]:
            self._send_json(200, {"activeId": _active_project_id(self.workspace)})
            return
        # /fs?path=&files=1:目录浏览(选择工作区目录 / 引用文件,files=1 追加文件条目)
        if parts == ["fs"]:
            self._send_json(
                200,
                _fs_listing(
                    self._query.get("path", [""])[0],
                    include_files=self._query.get("files", ["0"])[0] == "1",
                ),
            )
            return
        # /tasks(草稿+磁盘合并;过滤参数前端本地做——单项目、量级小)
        if parts == ["tasks"]:
            self._send_json(200, _all_task_payloads(self.workspace, root))
            return
        # /tasks/{id}[/timeline]
        if len(parts) == 2 and parts[0] == "tasks":
            payload = _task_payload_for_id(self.workspace, root, parts[1])
            self._send_json(200, payload)  # 找不到 → null(契约:getTask 返回 Task | null)
            return
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "timeline":
            self._send_json(200, _timeline_payload(root, parts[1]))
            return
        # /tasks/{id}/deltas?since=N:流式增量(TextChunk 钩子缓冲)。
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "deltas":
            try:
                since = int(self._query.get("since", ["0"])[0])
            except ValueError:
                since = 0
            self._send_json(200, _deltas_payload(parts[1], max(0, since)))
            return
        # /tasks/{id}/queues:steering/follow-up 队列 + 待审批项(队列管理与审批卡)。
        if len(parts) == 3 and parts[0] == "tasks" and parts[2] == "queues":
            session = _session_of(parts[1])
            self._send_json(
                200,
                {
                    "running": parts[1] in _RUNNING,
                    "steering": session.pending_steering() if session is not None else [],
                    "followups": session.pending_followups() if session is not None else [],
                    "approvals": _pending_approvals(parts[1]),
                },
            )
            return
        # /automations、/plugins、/models
        if parts == ["automations"]:
            self._send_json(200, [])
            return
        if parts == ["plugins"]:
            self._send_json(200, _plugins_payload(self.workspace))
            return
        if parts == ["models"]:
            # 自定义条目在前(与用户实际模型匹配),内置 preset 在后(默认
            # preset 排第一,前端首载落在它上)。apiKey 绝不回传——只有
            # hasKey 布尔;前端编辑留空 = 保留原值。
            payloads: list[dict[str, Any]] = []
            for entry in _models_registry_read().get("models", []):
                payloads.append(
                    {
                        "id": str(entry.get("id") or ""),
                        "name": str(entry.get("name") or ""),
                        "efforts": list(entry.get("efforts", [])),
                        "custom": True,
                        "modelId": str(entry.get("modelId") or ""),
                        "baseUrl": str(entry.get("baseUrl") or ""),
                        "protocol": str(entry.get("protocol") or "openai-compat"),
                        "effortStyle": str(entry.get("effortStyle") or "reasoning_effort"),
                        "hasKey": bool(entry.get("apiKey")),
                    }
                )
            default = _default_preset()
            registry = builtin_providers()
            names = registry.names()
            ordered = (
                [default] + [n for n in names if n != default] if default in names else names
            )
            for name in ordered:
                spec = registry.resolve(name)
                payloads.append(
                    {
                        "id": name,
                        "name": name,
                        "efforts": [],
                        "custom": False,
                        "modelId": spec.default_model,
                        "baseUrl": spec.base_url,
                        "protocol": spec.protocol,
                    }
                )
            self._send_json(200, payloads)
            return
        # /memory:契约之外的附加数据面(工作台"记忆"区)。
        if parts == ["memory"]:
            self._send_json(200, _memory_payload(self.workspace))
            return
        self._send_json(404, {"detail": f"未知端点:{'/'.join(parts)}"})

    def do_POST(self) -> None:  # noqa: N802 - 基类命名
        clean = self.path.split("?", 1)[0]
        parts = [p for p in clean.split("/") if p]
        # 导入工作区(项目管理)。
        if parts == ["api", "v1", "projects"]:
            self._send_json(*_register_project(self.workspace, self._read_json_body()))
            return
        # 切换/移除工作区。
        if parts == ["api", "v1", "workspace", "activate"]:
            body = self._read_json_body()
            ok = _activate_project(self.workspace, str(body.get("id") or ""))
            self._send_json(200 if ok else 404, {"ok": ok})
            return
        if parts == ["api", "v1", "projects", "remove"]:
            body = self._read_json_body()
            ok, detail = _remove_project(self.workspace, str(body.get("id") or ""))
            self._send_json(200 if ok else 400, {"ok": ok, "detail": detail})
            return
        # 模型设置:新增/更新(留空 apiKey=保留原值)与移除自定义条目。
        if parts == ["api", "v1", "models", "save"]:
            self._send_json(*_save_model_entry(self._read_json_body()))
            return
        if parts == ["api", "v1", "models", "remove"]:
            self._send_json(*_remove_model_entry(self._read_json_body()))
            return
        # 创建草稿任务(Composer / 侧栏「新建会话」)。
        if parts == ["api", "v1", "tasks"]:
            self._send_json(200, _create_task(self._read_json_body()))
            return
        # 发消息:立即返回 running,后台执行,前端经 deltas 流式取增量。
        if len(parts) == 5 and parts[:3] == ["api", "v1", "tasks"] and parts[4] == "messages":
            body = self._read_json_body()
            code, payload = _post_message(
                self.workspace, self.sessions_root, parts[3], str(body.get("text", ""))
            )
            self._send_json(code, payload)
            return
        # /tasks/{id}/steer | /queue | /queue/remove | /queue/edit:
        # 打断注入 / 排队 / 队列管理 / 改写排队文本。
        if (
            len(parts) == 5
            and parts[:3] == ["api", "v1", "tasks"]
            and parts[4] in ("steer", "queue", "queue-remove", "queue-edit")
        ):
            body = self._read_json_body()
            code, payload = _queue_op(parts[3], parts[4], body)
            self._send_json(code, payload)
            return
        # /tasks/{id}/approvals/{req}:审批决策(变更前确认环)。
        if (
            len(parts) == 6
            and parts[:3] == ["api", "v1", "tasks"]
            and parts[4] == "approvals"
        ):
            body = self._read_json_body()
            decision = str(body.get("decision") or "deny")
            ok = _decide_approval(parts[3], parts[5], decision)
            self._send_json(200 if ok else 404, {"ok": ok})
            return
        # /tasks/{id}/access:权限模式中途切换(human-in-the-loop)。
        if (
            len(parts) == 5
            and parts[:3] == ["api", "v1", "tasks"]
            and parts[4] == "access"
        ):
            self._send_json(
                *_set_task_access(self.workspace, parts[3], self._read_json_body())
            )
            return
        # /tasks/{id}/stop:外部强制中断(协作式,块边界停)。
        if (
            len(parts) == 5
            and parts[:3] == ["api", "v1", "tasks"]
            and parts[4] == "stop"
        ):
            self._send_json(*_stop_task(parts[3]))
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
        clean = self.path.split("?", 1)[0]
        parts = [p for p in clean.split("/") if p]
        # /tasks/{id}:删除会话(JSONL/trace 移入回收站,审计可恢复)。
        if len(parts) == 4 and parts[:3] == ["api", "v1", "tasks"]:
            self._send_json(*_delete_task(self.sessions_root, parts[3]))
            return
        self._not_implemented(_what_from_path(self.path), "—")

    def do_PUT(self) -> None:  # noqa: N802 - 基类命名
        self._not_implemented(_what_from_path(self.path), "M2")


def _what_from_path(path: str) -> str:
    """501 回执里的动词宾语(只区分大类,不再细分)。"""
    if "/automations" in path:
        return "自动化调度(σ 未实现调度器)"
    if "/plugins" in path:
        return "插件装卸(M3:接技能/工具装卸)"
    return "任务执行(创建/推进/打断/审批)"


def _task_payload_for_id(
    primary: Path, sessions_root: Path, session_id: str
) -> dict[str, Any] | None:
    """按 id 取单任务;安全网:先过一遍列表映射,避免路径段被拼进文件路径。
    草稿记录里的 access/effort(用户意图)与列表端点同规则合并。
    磁盘上没有 → 回退**内存草稿记录**(列表端点合并草稿、单任务端点也必须
    认——否则草稿期切权限档,前端回读拿到 null 不更新,实测 2026-10-02)。"""
    for payload in _list_task_payloads(primary, sessions_root):
        if payload["id"] == session_id:
            with _TASKS_LOCK:
                record = dict(_TASKS[session_id]) if session_id in _TASKS else None
            if record is not None:
                payload["access"] = str(record.get("access") or "") or payload["access"]
                payload["effort"] = str(record.get("effort") or "") or payload["effort"]
            return payload
    with _TASKS_LOCK:
        record = dict(_TASKS[session_id]) if session_id in _TASKS else None
    if record is not None:
        return _draft_payload(record)
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
