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
import re
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
    MessageInjected,
    TextChunk,
    ThinkingChunk,
    ToolEnd,
    ToolStart,
)
from sigma.hooks.base import ApprovalHook, BaseHook
from sigma.memory.file_store import memory_dir_for, scan_memory
from sigma.observability.timeline import build_timeline
import re
import socket
import time

from sigma.observability.trace import trace_path_for
from sigma.prompts.system_prompt import build_system_prompt
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
from sigma.sdk import (
    DEFAULT_ENABLE_MEMORY,
    InteractiveSession,
    default_registry,
    scan_skills,
)
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
#: 随手问(2026-10-03):不绑定任何代码工作区的虚拟项目,承载日常非 coding 任务。
#: 会话照常落 ~/.sigma/sessions 并陈列在侧栏;工作区用中立目录 ~/.sigma/inbox
#(无 AGENTS.md、无项目技能、无 .git,系统提示词天然是"干净通用助手"口径)。
INBOX_PROJECT_ID = "proj-inbox"
INBOX_WORKSPACE = Path.home() / ".sigma" / "inbox"
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
#: 服务启动时刻(工作台状态面板的"已运行"字段)。
_STARTED_AT = time.time()

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


#: 联网工具的两个密钥(惰性解析一次;解析失败=没有,不抛)。
#: 测试可替换 —— 与其他全局注入点同一处置。
class _WebKeys:
    tavily: str | None = None
    firecrawl: str | None = None
    resolved: bool = False

    # 返回 ``type[_WebKeys]`` 而不是 ``_WebKeys``：classmethod 给的cls 就是类本身，
    # 注解写实例会把 mypy 判成 return-value 错（实测踩过）。
    @classmethod
    def get(cls) -> type["_WebKeys"]:
        if not cls.resolved:
            from sigma.config.settings import (  # noqa: PLC0415 - 惰性:多数会话不开联网
                resolve_firecrawl_api_key,
                resolve_tavily_api_key,
            )

            # 解析不到就是 None(不是异常):key 是可选能力,缺它不该让服务起不来。
            cls.tavily = resolve_tavily_api_key()[0] or None
            cls.firecrawl = resolve_firecrawl_api_key()[0] or None
            cls.resolved = True
        return cls


_WEB_KEYS = _WebKeys()

#: 启动旗标(默认**关**——星辰 2026-10-02 拍板"加一个是否开启联网搜索的判断
#: 按钮,默认不启动")。与 CLI 的 `web_search_on = not no_web_search and key`
#: **方向相反**:CLI 是"有 key 就开",这里是"有key 也要显式开"。
#: 由 main() 从命令行写入,默认 False。
_STARTUP_FLAGS: dict[str, bool] = {"web_search": False, "web_fetch": False}


def _web_flags(record: dict[str, Any]) -> tuple[bool, bool, str]:
    """算这个会话的联网档位 → ``(web_search, web_fetch, 状态说明)``。

    优先级:**任务记录 > 启动旗标**。记录里没有该字段(旧任务/纯磁盘会话)时
    回落到启动旗标,也就是"默认关"。

    三个理由,按重要性:
    1. **默认关**(星辰拍板):key 存在≠ 开启。所以 key 只是**必要条件**。
    2. **key 缺失与"用户没开"要分开说**:前者的处置是配 key,后者是点按钮。
       混成一句"联网不可用"会把用户引向错误的动作。
    3. ``web_fetch`` 跟着 ``web_search`` 一起开(同一个按钮统管),但仍各自
       校验自己的 key —— 只配了 Tavily 的机器开搜索不精读,反之亦然
       (与 CLI 同一条判据)。
    """
    want = record.get("web")
    if want is None:
        want = _STARTUP_FLAGS.get("web_search", False)
    want_search = bool(want)
    want_fetch = bool(want) and _STARTUP_FLAGS.get("web_fetch", False)
    keys = _WebKeys.get()
    web_search = want_search and bool(keys.tavily)
    web_fetch = want_fetch and bool(keys.firecrawl)
    if want_search and not keys.tavily:
        note = "已请求开启,但没配 TAVILY_API_KEY"
    elif not want_search:
        note = "未开启(默认关,需点界面开关或--web-search)"
    else:
        note = "已开启(Tavily)"
    return web_search, web_fetch, note


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
# 模型配置注册表(工作台"模型设置"):用户自定义 base_url/model_id 的条目,
# 与内置 preset 并列进 /models;执行链按名字解析。**密钥本体只存
# ~/.sigma/.env**(2026-10-03 拍板,与全局密钥同一处):条目只落密钥**变量名**
# apiKeyEnv,JSON 里没有任何明文 key;GET 载荷带变量名与 hasKey 布尔,
# 两者均非机密。
# ---------------------------------------------------------------------------

_MODELS_REGISTRY_PATH = Path.home() / ".sigma" / "workbench-models.json"

#: 密钥变量名的合法性:环境变量的命名规则(字母/下划线开头,只含
#: 字母/数字/下划线)。它是"指向 ~/.sigma/.env 里某一行"的指针,
#: 写错了等于指了个空气,必须在保存时就拦下。
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

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
    密钥只落**变量名** ``apiKeyEnv``(本体在 ~/.sigma/.env,服务端
    从不接触 key 本身,也就不存在"留空 = 保留原值"的问题——变量名
    不是机密,GET 原样回传,每次保存都整体覆盖)。"""
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
    # 密钥本体在 ~/.sigma/.env,条目只记变量名(空 = 走全局 SIGMA_API_KEY 链)。
    # 旧版条目曾把明文 key 存在这里,任何一次保存都顺手清掉残留字段。
    api_key_env = str(body.get("apiKeyEnv") or "").strip()
    if api_key_env != "" and ENV_NAME_RE.fullmatch(api_key_env) is None:
        return 400, {"detail": "apiKeyEnv 必须是合法环境变量名(字母/下划线开头,只含字母/数字/下划线)"}
    target["apiKeyEnv"] = api_key_env
    target.pop("apiKey", None)
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
    # 随手问恒在列(可被激活为 active,Composer 下拉/侧栏由此渲染)
    INBOX_WORKSPACE.mkdir(parents=True, exist_ok=True)
    projects.append(
        {
            "id": INBOX_PROJECT_ID,
            "name": "随手问",
            "repoPath": str(INBOX_WORKSPACE),
            "branch": "",
            "createdAt": "",
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
        属性赋值原子性(模式字符串无中间态)。

        **同时唤醒已挂起的审批等待**,按新模式重新裁决(2026-10-02 实测:
        手动确认下挂着的工具,切到自动审批后原等待不感知新模式,界面
        卡在"执行中")——新模式下无需人工确认的立即放行,写类在只读/
        计划模式下立即拒绝,仍需确认的继续等。"""
        self._mode = mode if mode in ACCESS_MODES else "full"
        with _APPROVALS_LOCK:
            pending = list(_APPROVALS.get(self._task_id, []))
        for entry in pending:
            if entry["event"].is_set():
                continue
            verdict = self._verdict_without_asking(
                self._mode, str(entry.get("tool") or "?"), dict(entry.get("args") or {})
            )
            if verdict is None:
                continue  # 新模式下仍需人工确认 → 原样等待
            with _APPROVALS_LOCK:
                if entry["event"].is_set():
                    continue  # 与前端决策并发:先到先得,不覆盖
                entry["decision"] = "approve" if verdict.allowed else "deny"
                if not verdict.allowed:
                    entry["reason"] = str(verdict.reason or "已按新权限模式拒绝")
                entry["event"].set()

    def _ask(self, name: str, arguments: dict[str, Any]) -> ApprovalDecision:
        request_id = uuid.uuid4().hex[:8]
        entry: dict[str, Any] = {
            "id": request_id,
            "tool": name,
            "args": arguments,
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

    def _verdict_without_asking(
        self, mode: str, name: str, arguments: dict[str, Any]
    ) -> ApprovalDecision | None:
        """按给定模式直接裁决;``None`` = 该调用需要人工确认(_ask 等待)。

        approve() 与 set_mode() 共用同一判定——**切换模式时对已挂起的审批
        按新模式重新裁决**(星辰实测 2026-10-02:手动确认下挂着的工具,
        切到自动审批后必须立即放行,否则界面卡死在"执行中"的体感里)。
        """
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
                return None
            return ApprovalDecision(allowed=True)
        # confirm:写类工具与危险调用都要确认
        if name in _WRITE_TOOLS or findings:
            return None
        return ApprovalDecision(allowed=True)

    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        decision = self._verdict_without_asking(self._mode, name, arguments)
        if decision is not None:
            return decision
        return await asyncio.to_thread(self._ask, name, arguments)


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
    # 先在锁外补纯磁盘会话的最小记录底稿(归属扫描是磁盘活,不进锁——
    # 锁内做磁盘 IO 会拖死所有并发请求,与 _post_message 排队分支同纪律):
    # 锁内只做 dict 读写。
    fresh_record: dict[str, Any] | None = None
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
    if record is None:
        # 纯磁盘会话(本进程没跑过):补一条最小记录,让载荷与后续
        # 执行都读到新档位。归属按注册表如实回填。
        fresh_record = {
            "id": task_id,
            "projectId": _session_project_id(primary, task_id),
            "title": "",
            "access": mode,
        }
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)  # 双检:补底稿期间可能已被并发创建
        if record is None:
            _TASKS[task_id] = fresh_record or {}
        else:
            record["access"] = mode
    with _GATES_LOCK:
        gate = _GATES.get(task_id)
    if gate is not None:
        gate.set_mode(mode)
    return 200, {"ok": True, "access": mode}


def _set_task_web(
    primary: Path, task_id: str, body: dict[str, Any]
) -> tuple[int, dict[str, Any]]:
    """切换联网开关(星辰 2026-10-02拍板:**默认不启动**,要显式开)。

    **生效时机是"下次执行",不是立即热切**——与 ``_set_task_access`` 不同,
    差别不是随意选的:access 改的是 gate 上的一个 ``str`` 属性
    (``approve()`` 逐调用读它),而联网要换 ``AgentLoop`` 持有的 registry
    (``runtime/event_loop.py`` 在构造期就存了引用)。轮中换 loop 会丢
    in-flight 状态(loop 是无状态的,消息视图是 ``run_turn`` 启动时的快照)。

    所以这里的处置是**把存活会话摘掉**,下次发消息时按新档重新装配——
    树、历史、压缩视图由 SDK 的 ``rebuild``路径原样移交(``_SESSIONS``
    里存的是会话,树在 store 里),丢的只有 steering/follow-up 队列。
    **队列丢了必须让用户知道**(返回 ``pendingDropped``),否则界面上
    "排队的消息"会静默消失。

    会话正**在跑**时不摘:在跑的轮已经持有旧表,中途换 registry 与 loop
    都不安全(见上)。这一档如实回 ``applied=False`` + 原因,让 UI 提示
    "本轮跑完后生效"。
    """
    raw = body.get("enabled")
    if not isinstance(raw, bool):
        return 400, {"detail": "enabled 必须是布尔"}
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
        if record is None:
            # 纯磁盘会话(本进程没跑过):补最小底稿,让后续装配读到新档位。
            _TASKS[task_id] = {
                "id": task_id,
                "projectId": _session_project_id(primary, task_id),
                "title": "",
                "web": raw,
            }
        else:
            record["web"] = raw
    # 如实算一遍"最终会不会真开":用户点了开但没配 key ≠ 开了。
    web_search, web_fetch, note = _web_flags({"web": raw})
    applied = False
    pending = 0
    reason = "已记录,下次执行按新档装配"
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(task_id)
    if session is not None:
        if session.turn_running:
            reason = "本轮正在跑,跑完后下次执行生效"
        else:
            pending = len(session.pending_steering()) + len(session.pending_followups())
            with _SESSIONS_LOCK:
                _SESSIONS.pop(task_id, None)
            applied = True
            reason = "已重建工具表,下一声消息生效"
    else:
        applied = True
        reason = "已记录,下次执行按新档装配"
    return 200, {
        "ok": True,
        "web": raw,
        "webSearch": web_search,
        "webFetch": web_fetch,
        "note": note,
        "applied": applied,
        "reason": reason,
        "pendingDropped": pending,
    }


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
    trace = trace_path_for(sessions_root, task_id)
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
    # ⚠ `body.get("index") or -1` 是陷阱(星辰 2026-10-02 实测):0 是 falsy,
    # 队列**第一项**的下标会被吞成 -1 → drop_queued 越界返回 False →
    # 「立即」/删除/编辑对首项一律失效,而 2..n 正常(症状:只有第一条删不掉)。
    # 显式判 None,不做真值兜底。
    raw_index = body.get("index")
    index = int(raw_index) if raw_index is not None else -1
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
        return (TextChunk, ThinkingChunk, ToolStart, ToolEnd, MessageInjected)

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
        elif isinstance(event, MessageInjected):
            # 注入即时上屏(星辰 2026-10-02"点立即后面板上要及时渲染"):
            # steering/信箱/提醒注入此前只在轮结束的最终载荷可见——直播
            # 区看不到。UserMessage 注入推块:「[」开头是系统条(任务清单
            # 提醒/子任务回报/停滞提醒),否则是用户说的话(steering/排队)。
            inner = getattr(event.message, "message", None)
            content = getattr(inner, "content", None)
            if not isinstance(inner, UserMessage) or not isinstance(content, str):
                piece = None
            else:
                piece = {
                    "k": "note" if content.startswith("[") else "user",
                    "t": _clip(content, 500),
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
       model_id/协议按条目;密钥按条目声明的 ``apiKeyEnv`` 变量名解析
       (环境变量 > ~/.sigma/.env),未声明才退到全局 SIGMA_API_KEY 链;
       档位按条目声明的 ``effortStyle`` 随附;
    2. **内置 preset**(与 CLI ``--preset`` 同一套注册表词汇)——显式选择
       整体接管 preset/base_url/model;
    3. 都没有 → 与 CLI 同链(env SIGMA_BASE_URL / SIGMA_MODEL / SIGMA_PRESET
       > 厂商预设默认值;密钥:命令行 > 环境变量 > ~/.sigma/.env)。

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
            # 密钥:条目声明的变量名优先,同一套 resolve(环境变量 >
            # ~/.sigma/.env);未声明变量名或没解析到才退到全局
            # SIGMA_API_KEY 链。判断用真值而不是 == "":resolve 未命中
            # 返回的是 None,None == "" 是 False,会漏掉回退与报错。
            api_key: str | None = ""
            var_name = str(entry.get("apiKeyEnv") or "").strip()
            if var_name != "":
                api_key, _source = resolve_api_key(var_name=var_name)
            if not api_key:
                chained, _source = resolve_api_key()
                api_key = chained or ""
            if not api_key:
                raise LookupError(
                    f"模型「{hint}」未配置密钥:把 key 写进 ~/.sigma/.env,"
                    "并在模型设置里把密钥变量名指向它(如 DEEPSEEK_API_KEY);"
                    "或直接用 SIGMA_API_KEY。"
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
                    "未配置模型密钥:设环境变量 SIGMA_API_KEY,或写进 ~/.sigma/.env"
                    "——与 sigma CLI 用的是同一份配置。"
                )
            return (
                spec.base_url,
                api_key,
                spec.default_model,
                spec.protocol,
                # 随附方式取自spec（厂商事实），不再在此处写死
                # "reasoning_effort" —— 写死的话，智谱系 preset 即便
                # 声明了 thinking 也发不出去。
                _effort_extra_body(spec.effort_style, effort_hint),
            )
    chained, _source = resolve_api_key()
    api_key = chained or ""
    if not api_key:
        raise LookupError(
            "未配置模型密钥:设环境变量 SIGMA_API_KEY,或写进 ~/.sigma/.env"
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
        # 同上：走 spec 声明的随附方式，不写死。
        _effort_extra_body(spec.effort_style, effort_hint),
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

    # 联网工具(星辰 2026-10-02 修的缺陷:此前整页**零提及**,agent 于是答
    # "我没有联网工具"——它说的是实话,工具表里真的没有 web_search)。
    # 口径与 CLI 相反,**按星辰拍板:默认关**,要显式开(按钮或启动旗标)。
    # key 缺失不是"静默不开":横幅与按钮文案都要说清是哪一种。
    web_on, fetch_on, web_note = _web_flags(record)
    skills_scan, _skill_index = scan_skills(repo)
    # 提示词与注册表**同源**（同批修）:此前用裸常量 SYSTEM_PROMPT,于是三件事
    # 同时出错——① 联网工具行永远不在;② 记忆纪律段永远不在(enable_memory
    # 默认 True,索引扫了却没渲染,记忆功能形同虚设);③ 技能工具行也不在
    # (技能索引在常驻区里摆着,load_skill 却没被告知)。三条都是同一个病:
    # 拼提示词时没看注册表。
    system_prompt = build_system_prompt(
        web_search=web_on,
        web_fetch=fetch_on,
        skills=bool(skills_scan.skills),
        # 记忆纪律段与``enable_memory`` 同源。引用SDK 的常量而不是写字面量:
        # 拼提示词与装配会话引同一个真值,两处不会漂移(漂移症状 =
        # 索引在常驻区摆着、提示词只字未提)。
        memory=DEFAULT_ENABLE_MEMORY,
    )
    system_prompt += ("\n\n" + _PLAN_INSTRUCTION) if access == "plan" else ""
    goal = _GOALS.get(task_id)
    if goal:
        # /goal 设的目标随(重)装配注入;活会话已在设置时经 steering 通告过。
        system_prompt += f"\n\n[会话目标] {goal}"
    registry = default_registry(
        web_search=web_on,
        tavily_api_key=_WEB_KEYS.tavily,
        web_fetch=fetch_on,
        firecrawl_api_key=_WEB_KEYS.firecrawl,
        skills=skills_scan.skills,
    )
    session = InteractiveSession(
        provider=provider,
        workspace_root=repo,
        model=model,
        # ⚠ 这三行是本批次修的**根因**:此前这里一个都没传,
        # 于是 InteractiveSession 落到 sdk 侧的兜底`default_registry(todo=...)`
        # ——而它的web_search 默认False。结果工具表里根本没有 web_search,
        # agent 说"我没有联网工具"是**实话**,不是幻觉。
        # 装配与提示词同源,两处必须一起改(否则常驻区自相矛盾)。
        registry=registry,
        system_prompt=system_prompt,
        # max_rounds 走默认 None = 无上限:主任务由模型自己收敛(星辰
        # 2026-10-02 拍板);打断/信箱/审批仍是退出通道,强制中断随时可用。
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
        # 联网开关随建任务一起落记录（2026-10-02 补）。
        # 此前**只读不回写**：_draft_payload 会读 record["web"]，
        # 但创建时没写 ⇒ 首页 Composer 传什么都会被这条"缺字段=False"吃掉，
        # 症状是「开关能点、开了没效果」——假功能。
        # 已知限制：这里用 bool() 宽容解析（与其他字段同款），所以
        # 字符串 "false" 会被当成开。热切那条路径（_set_task_web）是
        # 严格 isinstance(raw, bool) 校验——**两者口径刻意不同**：
        # 建任务是首载、调用方唯一是自己；热切是用户手点，脏值该被拒。
        "web": bool(body.get("web") or False),
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
        # 联网开关(布尔,与 access/effort 同款"下次执行的参数")。
        # 缺字段= False:默认关(星辰 2026-10-02 拍板)。
        "web": bool(record.get("web") or False),
        "model": str(record.get("model") or ""),
        "effort": str(record.get("effort") or ""),
        "createdAt": created,
        "updatedAt": created,
        "events": [],
        "messageCount": 0,
        "sizeBytes": 0,
        "modifiedEpoch": 0,
    }


# ===========================================================================
# 斜杠命令(2026-10-03,对标 Claude Code / Aider / Gemini CLI 的成熟设计)
#
# 约定(与 CLI REPL 同源,repl.py 的 COMMANDS 表):
#   - "/" 开头**永不进模型**,在 _post_message 最上游拦截——且必须在
#     running 判定之前,否则执行中的 /stop 会被当 followup 排队;
#   - 注册表统一元数据(name/args/description/when),别名同表;
#   - 未知命令报错并指向 /help,绝不静默转发;
#   - 输出走 deltas 的 note 块(前端直播区零改动即可渲染),HTTP 响应
#     同时带 command/output(结构化消费面)。
# ===========================================================================

#: /goal 的会话目标(task 记录是草稿专属,磁盘会话没有记录,单独存;
#: 会话(重)装配时注入系统提示,见 _get_or_create_session)。
_GOALS: dict[str, str] = {}
#: /checkpoints 的 序号→ref 映射(REPL index_map 的 Web 对应物)。
#: 会话级 UI 状态,服务端内存即可;重启后重新 /checkpoints 刷新。
_CHECKPOINT_INDEX: dict[str, dict[str, str]] = {}
_CHECKPOINT_LOCK = threading.Lock()

#: 处理器签名:(task_id, session, arg, primary, sessions_root) → (输出行, 附加载荷)。
#: 附加载荷并入 HTTP 响应(如 /new 的 newTaskId,前端跳转用)。
CommandHandler = Callable[..., tuple[list[str], dict[str, Any]]]

_COMMANDS: dict[str, dict[str, Any]] = {}


def _command(name: str, args: str, description: str, when: str = "always") -> Callable[[CommandHandler], CommandHandler]:
    """注册装饰器。``when``:always=随时可用;idle=仅空闲(会动工作区/跑长任务)。"""
    def wrap(fn: CommandHandler) -> CommandHandler:
        _COMMANDS[name] = {
            "handler": fn, "args": args, "description": description, "when": when,
        }
        return fn

    return wrap


@_command("help", "", "列出全部斜杠命令")
def _cmd_help(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    lines = ["可用命令:"]
    for name in sorted(_COMMANDS):
        meta = _COMMANDS[name]
        hint = f" {meta['args']}" if meta["args"] else ""
        lines.append(f"  /{name}{hint}  — {meta['description']}")
    lines.append("技能:输入 / 选技能或命令;技能可用 /技能名 [任务] 直接调用。")
    return lines, {}


@_command("stop", "", "中断当前执行(执行中也可用)")
def _cmd_stop(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    code, payload = _stop_task(task_id)
    if code != 200:
        return [f"✗ {payload.get('detail', '停止失败')}"], {}
    if payload.get("interrupted"):
        return ["✓ 已发送中断,当前工具完成后停止"], {}
    return ["· 当前没有执行中的任务"], {}


@_command("reload", "[文件名]", "热重载 extensions 下的扩展工具", when="idle")
def _cmd_reload(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    if session is None:
        return ["✗ 会话尚未装配(发第一条消息后再用)"], {}
    reports = session.reload_tools(arg or None)
    if not reports:
        return ["· 没有可装载的扩展(<workspace>/extensions/*.py)"], {}
    lines: list[str] = []
    for report in reports:
        failed = str(getattr(report, "failed_reason", "") or "")
        if failed:
            lines.append(f"✗ {report.source}:{failed}")
        else:
            added = ", ".join(report.added) or "无"
            removed = ", ".join(report.removed) or "无"
            lines.append(f"✓ {report.source}  新增 [{added}]  移除 [{removed}]")
    return lines, {}


@_command("checkpoints", "", "列出影子库回滚点(/rollback 用)")
def _cmd_checkpoints(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    if session is None:
        return ["✗ 会话尚未装配(发第一条消息后再用)"], {}
    cp = session.checkpoint
    if cp is None or not cp.available:
        reason = str(getattr(cp, "unavailable_reason", "") or "") if cp is not None else "未启用影子库"
        return [f"✗ 影子库不可用:{reason}"], {}
    refs = cp.refs()
    if not refs:
        return ["· 影子库还没有任何快照(写过文件的会话才有)"], {}
    with _CHECKPOINT_LOCK:
        _CHECKPOINT_INDEX[task_id] = {str(i + 1): info.ref for i, info in enumerate(refs)}
    lines = ["回滚点(新 → 旧;用 /rollback <序号> 恢复):"]
    for i, info in enumerate(refs):
        lines.append(f"  {i + 1}. {info.label}  ({info.ref[:8]})")
    return lines, {}


@_command("rollback", "<序号|ref前缀>", "把工作区回滚到某个回滚点", when="idle")
def _cmd_rollback(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    if session is None:
        return ["✗ 会话尚未装配"], {}
    cp = session.checkpoint
    if cp is None or not cp.available:
        return ["✗ 影子库不可用"], {}
    if arg == "":
        return ["✗ 用法:/rollback <序号>(先 /checkpoints 查看)"], {}
    with _CHECKPOINT_LOCK:
        index = dict(_CHECKPOINT_INDEX.get(task_id, {}))
    ref = index.get(arg)
    if ref is None:
        matches = [info.ref for info in cp.refs() if info.ref.startswith(arg)]
        if len(matches) == 1:
            ref = matches[0]
        elif len(matches) > 1:
            return ["✗ 该前缀命中多个回滚点,请用完整序号"], {}
        else:
            return ["✗ 找不到该回滚点,先 /checkpoints 查看"], {}
    report = cp.restore(ref)
    if not report.ok:
        return [f"✗ 回滚失败:{report.note}"], {}
    lines = [
        f"✓ 已回滚到 {report.ref[:8]}",
        f"  恢复/改动 {len(report.changed)} 个文件,删除 {len(report.deleted)} 个,受保护跳过 {report.protected} 个",
    ]
    if report.pre_restore_ref:
        lines.append(f"  回滚前状态已自存为 {report.pre_restore_ref[:8]}(可再回滚)")
    return lines, {}


@_command("new", "", "新建一个会话(工作台切过去)")
def _cmd_new(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    record = _TASKS.get(task_id) or {}
    project = _project_of(task_id, record, primary)
    created = _create_task({"projectId": str(project.get("id") or "")})
    return [
        f"✓ 已创建新会话 {created['id'][:8]}(侧栏可见,前端即将切换)"
    ], {"newTaskId": created["id"]}


@_command("compact", "", "立即压缩上下文(无视自动阈值)", when="idle")
def _cmd_compact(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    if session is None:
        return ["✗ 会话尚未装配(发第一条消息后再用)"], {}
    outcome = _run_on_persistent_loop(session.compact())
    if outcome is None:
        return ["· 没有可压缩的内容(历史太短或压缩已关)"], {}
    lines = [
        f"✓ 压缩完成:压缩 {outcome.compacted_messages} 条,保留 {outcome.kept_messages} 条"
    ]
    text = str(getattr(outcome.summary, "summary", "") or "")
    if text:
        lines.append(_clip(text, 200))
    return lines, {}


@_command("goal", "[文本]", "显示或设置会话目标(注入模型上下文)")
def _cmd_goal(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    if arg == "":
        with _TASKS_LOCK:
            record = _TASKS.get(task_id) or {}
        current = _GOALS.get(task_id) or str(record.get("goal") or "")
        return [f"当前目标:{current or '(未设置,用 /goal <文本> 设置)'}"], {}
    with _TASKS_LOCK:
        record2 = _TASKS.get(task_id)
        if record2 is not None:
            record2["goal"] = arg
    _GOALS[task_id] = arg
    lines = [f"✓ 会话目标已设:{arg}"]
    if session is not None:
        # 活会话靠 steering 通告(空闲时下一轮开头注入;执行中在下一个块边界注入);
        # 重建装配则走 _get_or_create_session 的系统提示注入。
        session.submit_steering(f"[会话目标] {arg}")
        lines.append("  已通告当前会话")
    else:
        lines.append("  (会话装配时自动注入)")
    return lines, {"goal": arg}


@_command("plan", "", "切换到规划模式(只读规划,不改工作区)", when="idle")
def _cmd_plan(
    task_id: str, session: InteractiveSession | None, arg: str, primary: Path, sessions_root: Path
) -> tuple[list[str], dict[str, Any]]:
    code, payload = _set_task_access(primary, task_id, {"access": "plan"})
    if code != 200:
        return [f"✗ {payload.get('detail', '切换失败')}"], {}
    return ["✓ 已切到 plan 模式(只读规划)。直接输入任务即可开始规划。"], {"access": "plan"}


def _commands_payload(workspace: Path) -> dict[str, Any]:
    """面板数据源:命令注册表 + 技能清单(复用 /plugins 的技能扫描)。"""
    commands = [
        {
            "name": name,
            "args": _COMMANDS[name]["args"],
            "description": _COMMANDS[name]["description"],
        }
        for name in sorted(_COMMANDS)
    ]
    skills = [
        {"name": p["name"], "description": p["description"]}
        for p in _plugins_payload(workspace)
        if str(p.get("id", "")).startswith("skill:")
    ]
    return {"commands": commands, "skills": skills}


def _find_skill(primary: Path, name: str) -> dict[str, str] | None:
    """/技能名 兜底:在技能扫描清单里找同名技能(id 形如 skill:<name>)。"""
    for plugin in _plugins_payload(primary):
        if str(plugin.get("id", "")) == f"skill:{name}":
            return {"name": str(plugin["name"]), "description": str(plugin["description"])}
    return None


def _dispatch_command(
    primary: Path, sessions_root: Path, task_id: str, text: str
) -> tuple[int, dict[str, Any]]:
    """拦截到的 "/命令":执行并把输出写进 deltas(前端零改动上屏)。

    失败姿态与 REPL 一致:任何命令异常都降级为一条 ✗ 输出行,
    绝不把命令文本漏给模型,也绝不 500(输出面在,用户看得见)。
    """
    name, _, arg = text[1:].partition(" ")
    name = name.strip().lower()
    arg = arg.strip()
    entry = _COMMANDS.get(name)
    if entry is None:
        # 技能兜底:命令名未注册但命中技能清单 → 以规范提示语起**真实一轮**,
        # 模型经 load_skill 工具加载技能(前端面板选中即填 /技能名,同一条路径)。
        skill = _find_skill(primary, name)
        if skill is not None:
            prompt = f"请使用技能 {skill['name']}。"
            if arg:
                prompt = f"请使用技能 {skill['name']}:{arg}"
            code, payload = _post_message(primary, sessions_root, task_id, prompt)
            if code == 200:
                payload["skill"] = skill["name"]
            return code, payload
        lines = [f"✗ 未知命令 /{name}(命令不会发给模型)。可用命令见 /help"]
        extra: dict[str, Any] = {}
    else:
        if entry["when"] == "idle" and task_id in _RUNNING:
            lines = ["✗ 任务执行中,请先 /stop 再使用该命令"]
            extra = {}
        else:
            try:
                lines, extra = entry["handler"](
                    task_id, _session_of(task_id), arg, primary, sessions_root
                )
            except Exception as exc:  # noqa: BLE001 - 命令失败是输出不是错误
                lines = [f"✗ 命令失败:{type(exc).__name__}: {exc}"]
                extra = {}
    with _DELTAS_LOCK:
        _DELTAS[task_id] = {
            "pieces": [{"k": "note", "t": _clip(line, 500)} for line in lines],
            "lock": threading.Lock(),
            "done": True,
            "error": None,
        }
    payload = _merged_task_payload(primary, sessions_root, task_id)
    payload["command"] = True
    payload["output"] = lines
    payload.update(extra)
    return 200, payload


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
    # 斜杠命令拦截(2026-10-03):"/" 开头永不进模型。必须在 running 判定
    # **之前**——否则执行中的 /stop 会被当 followup 排队(见命令层注释)。
    if text.startswith("/"):
        return _dispatch_command(primary, sessions_root, task_id, text)
    queued = False
    with _TASKS_LOCK:
        record = _TASKS.get(task_id)
        if record is None and not session_path(sessions_root, task_id).is_file():
            return 404, {"detail": f"任务 {task_id} 不存在(草稿随工作台重启消失,已执行的会话在磁盘上)"}
        if task_id in _RUNNING:
            # 排队判定只做标记,**锁外处理**——排队分支要调
            # _merged_task_payload(内部重入 _TASKS_LOCK),threading.Lock
            # 不可重入,锁内调 = 自死锁并拖死全部请求(全量测试抓到,
            # 与线上"切权限后全面卡死"同族)。
            queued = True
        else:
            _RUNNING.add(task_id)
            if record is not None:
                record["status"] = "running"
    if queued:
        session = _session_of(task_id)
        if session is None:
            return 409, {"detail": "该会话正在执行中但尚未装配完成,请稍后再试"}
        session.submit_followup(text)
        payload = _merged_task_payload(primary, sessions_root, task_id)
        payload["status"] = "running"
        payload["queued"] = True
        return 200, payload
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
        payload["web"] = bool(record.get("web") or False)
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
            payload["web"] = bool(record.get("web") or False)
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
    """trace 与会话文件同目录同名不同后缀(P5-批次1);不存在的返回 None。

    路径构造**必须走核心的 trace_path_for**(jsonl.stem 拼后缀)——本模块
    曾自抄一份 jsonl.name 拼接(多出一个 .jsonl),路径永远对不上号 →
    hasTrace 恒 False → 轮数截断的轮被兜底判据误判成"执行完成"
    (星辰实测 2026-10-02)。核心 docstring 警告的"抄一份必然漂移"
    一字不差应验。"""
    trace = trace_path_for(sessions_root, session_id)
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
            content_str = str(message.message.content)
            if content_str.startswith("[任务清单提醒]"):
                # loop 注入的防跑偏提醒是**系统消息**，不是用户说的话——
                # 渲染成用户气泡会让"我没发过这条"的困惑(星辰 2026-10-02)。
                # 展示层按前缀识别为 note(灰条),审计事实(JSONL)不动。
                events.append(
                    {
                        "id": f"e{len(events)}",
                        "kind": "note",
                        "text": _clip(content_str, 4000),
                        "at": at,
                    }
                )
                continue
            events.append(
                {
                    "id": f"e{len(events)}",
                    "kind": "message",
                    "role": "user",
                    "text": _clip(content_str, 4000),
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
    status, status_note = _derive_status(tree, timeline.status)
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
        # 状态说明(为何失败/截断):之前被丢弃,失败收尾行只能给泛泛文案。
        "statusDetail": status_note,
        # access/effort 是"下一次执行"的参数,只读回放没有这个语义,留空;
        # model 是事实(来自最后一轮 provider 返回),如实给。
        "access": "",
        "model": model,
        "effort": "",
        # web 同access:是"下一次执行"的参数,只读回放没有这个语义。
        # 写死 False 而不是省略字段——前端要按同一个形状读(省略会让
        # undefined 漏进按钮的初始态)。
        "web": False,
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


def _system_status(sessions_root: Path, workspace: Path, port: int) -> dict[str, Any]:
    """工作台状态(底部菜单的状态区):版本/工作区/磁盘会话数/已运行。"""
    sessions = len(list(sessions_root.glob("*.jsonl")))
    return {
        "version": __version__,
        "workspace": str(workspace),
        "sessions": sessions,
        "uptimeSeconds": int(time.time() - _STARTED_AT),
        "port": port,
    }


def _local_addresses(port: int) -> list[str]:
    """局域网访问地址(手机访问面板):列出本机内网 IPv4。

    出口网卡 IP 用 UDP connect 技巧取(不发包,223.5.5.5 是阿里公共 DNS,
    国内可达);getaddrinfo 兜底扫本机名。只留私网段——127.0.0.1 对手机没意义。"""
    urls: list[str] = []
    seen: set[str] = {"127.0.0.1", "localhost"}
    candidates: list[str] = []
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("223.5.5.5", 53))
            candidates.append(str(sock.getsockname()[0]))
        finally:
            sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidates.append(str(info[4][0]))
    except OSError:
        pass
    for ip in candidates:
        if ip in seen or not ip.startswith(("192.168.", "10.", "172.")):
            continue
        seen.add(ip)
        urls.append(f"http://{ip}:{port}")
    return urls


#: 回收站文件名的时间戳模式(_now_stamp 去冒号后的形态):日期 + T + 时刻 + 毫秒。
#: 它**自带点号**,task_id 也含点——按点切必然切错(实测),必须按戳模式剥离。
_TRASH_STAMP_RE = re.compile(r"\.(\d{4}-\d{2}-\d{2}T\d{6}\.\d+)$")


def _trash_task_id(stem: str) -> str:
    """从回收站文件 stem 里剥出原 task_id;没有戳尾巴的(手工放的)原样返回。"""
    match = _TRASH_STAMP_RE.search(stem)
    return stem[: match.start()] if match is not None else stem


def _trash_payload() -> list[dict[str, Any]]:
    """回收站清单:删除的会话/trace 都躺在这(_delete_task 的审计安全设计)。

    回收站文件名 = ``{task_id}.{stamp}{后缀}``;task_id 本身含点
    (如 20261003-164418.624-9a9f),所以剥掉后缀后按**最后一个点**切出 task_id
    ——stamp 只含数字与横线,没有点,这个切分是安全的。"""
    if not _TRASH_DIR.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in _TRASH_DIR.iterdir():
        if not path.is_file():
            continue
        name = path.name
        kind = "session" if name.endswith(".jsonl") else "trace"
        stem = name[: -len(".jsonl")] if name.endswith(".jsonl") else name
        task_id = _trash_task_id(stem)
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append(
            {
                "name": name,
                "taskId": task_id,
                "kind": kind,
                "sizeBytes": stat.st_size,
                "deletedAt": int(stat.st_mtime),
            }
        )
    entries.sort(key=lambda entry: entry["deletedAt"], reverse=True)
    return entries[:200]


def _trash_restore(sessions_root: Path, name: str) -> tuple[int, dict[str, Any]]:
    """从回收站恢复一个会话(*.jsonl):移回会话目录,trace 兄弟文件一并找回。"""
    safe = Path(name).name
    src = _TRASH_DIR / safe
    if not src.is_file():
        return 404, {"detail": "回收站里没有这个文件"}
    if not safe.endswith(".jsonl"):
        return 400, {"detail": "只支持恢复会话文件(*.jsonl)"}
    stem = safe[: -len(".jsonl")]
    task_id = _trash_task_id(stem)
    dest = sessions_root / f"{task_id}.jsonl"
    if dest.exists():
        return 409, {"detail": f"会话 {task_id} 已存在,无法恢复"}
    src.replace(dest)
    for candidate in _TRASH_DIR.iterdir():
        if candidate.is_file() and candidate.name.startswith(f"{stem}.") and not candidate.name.endswith(".jsonl"):
            trace_dest = trace_path_for(sessions_root, task_id)
            if not trace_dest.exists():
                candidate.replace(trace_dest)
            break
    return 200, {"ok": True, "taskId": task_id}


def _trash_clear() -> tuple[int, dict[str, Any]]:
    """清空回收站(不可恢复——客户端必须先确认)。"""
    count = 0
    if _TRASH_DIR.is_dir():
        for path in _TRASH_DIR.iterdir():
            if path.is_file():
                try:
                    path.unlink()
                    count += 1
                except OSError:
                    pass
    return 200, {"ok": True, "cleared": count}


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
            # .webp 是背景图压缩后新增的（2026-10-02，1902KB -> 48KB）。
            # ⚠ 别以为「漏了会退回 application/octet-stream 也能显示」——
            # 实测确实是 octet-stream。浏览器对 CSS 里的图片多数仍会渲染，
            # 但严格 MIME 校验的场合（部分代理、`X-Content-Type-Options: nosniff`、
            # 某些框架的资源管线）会**拒收**，症状是背景静默不出现。
            # 所以：新增资源类型必须同时改这张表，否则压缩白做。
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".woff2": "font/woff2",
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

    @property
    def _listen_port(self) -> int:
        """监听端口。server_address 的静态类型是联合(可能是 str),运行时恒为
        (host, port) 元组——收窄后取 [1];异常形态回退配置默认 8301。"""
        address = self.server.server_address
        return int(address[1]) if isinstance(address, tuple) else 8301

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
        # /commands:斜杠命令注册表 + 技能清单(工作台输入面板的数据源)。
        if parts == ["commands"]:
            self._send_json(200, _commands_payload(self.workspace))
            return
        if parts == ["plugins"]:
            self._send_json(200, _plugins_payload(self.workspace))
            return
        if parts == ["models"]:
            # 自定义条目在前(与用户实际模型匹配),内置 preset 在后(默认
            # preset 排第一,前端首载落在它上)。密钥本体在 ~/.sigma/.env,
            # 这里只有变量名 apiKeyEnv 与 hasKey 布尔,均非机密。
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
                        "apiKeyEnv": str(entry.get("apiKeyEnv") or ""),
                        # 兼容迁移窗口:旧条目若还残留明文 apiKey,hasKey 别突然翻 false
                        "hasKey": bool(entry.get("apiKeyEnv") or entry.get("apiKey")),
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
                        # preset 的档位**来自 spec**（厂商事实，实测得来），
                        # 不是这里现编的。留空=该 preset 不支持档位，前端
                        # 据 efforts.length>0 隐藏档位选择器。
                        "efforts": list(spec.efforts),
                        "custom": False,
                        "modelId": spec.default_model,
                        "baseUrl": spec.base_url,
                        "protocol": spec.protocol,
                        "effortStyle": spec.effort_style,
                    }
                )
            self._send_json(200, payloads)
            return
        # /memory:契约之外的附加数据面(工作台"记忆"区)。
        # /system/status:工作台状态(底部菜单状态区)。
        if parts == ["system", "status"]:
            self._send_json(
                200,
                _system_status(root, self.workspace, self._listen_port),
            )
            return
        # /system/addresses:局域网访问地址(手机访问面板)。
        if parts == ["system", "addresses"]:
            self._send_json(200, {"urls": _local_addresses(self._listen_port)})
            return
        # /system/trash:会话回收站清单(删除先入回收站,这里是 second chance)。
        if parts == ["system", "trash"]:
            self._send_json(200, {"entries": _trash_payload()})
            return
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
        # 回收站:恢复 / 清空(清空不可恢复,客户端必须先确认)。
        if parts == ["api", "v1", "system", "trash", "restore"]:
            body = self._read_json_body()
            self._send_json(*_trash_restore(self.sessions_root, str(body.get("name") or "")))
            return
        if parts == ["api", "v1", "system", "trash", "clear"]:
            self._send_json(*_trash_clear())
            return
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
        # /tasks/{id}/web:联网开关(星辰 2026-10-02,**默认不启动**)。
        # 与上面 access 的差别在生效时机:那边是热替换闸(一个 str 属性),
        # 这边要重建工具表与 loop,所以是"下次执行生效"。
        if (
            len(parts) == 5
            and parts[:3] == ["api", "v1", "tasks"]
            and parts[4] == "web"
        ):
            self._send_json(
                *_set_task_web(self.workspace, parts[3], self._read_json_body())
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
                payload["web"] = bool(record.get("web") or False)
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
    # 默认 0.0.0.0(星辰 2026-10-03 拍板):手机访问是默认能力,重启不再需要带参。
    # 无鉴权——只在可信网络运行,启动日志有提醒。
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8301)
    parser.add_argument("--sessions-dir", default=None, help="会话目录,默认 ~/.sigma/sessions")
    parser.add_argument("--workspace", default=None, help="工作区根,默认当前目录")
    parser.add_argument(
        "--dist", default=None, help="前端构建产物目录,默认 src/sigma-frontend/dist"
    )
    # 联网工具:**默认关**(星辰 2026-10-02 拍板)。与 CLI 方向相反——
    # CLI 是"配了 key 就开",这里是"配了 key 也要显式开"(--web-search 或界面开关)。
    # 不给这两个旗标时启动横幅会明说"未开启",免得又一次出现
    # "agent 说没有联网工具"而没人知道为什么。
    parser.add_argument(
        "--web-search",
        action="store_true",
        help="启动时开启联网搜索(需 TAVILY_API_KEY)。默认关",
    )
    parser.add_argument(
        "--web-fetch",
        action="store_true",
        help="启动时同时开启网页精读(需 FIRECRAWL_API_KEY)。默认关",
    )
    args = parser.parse_args(argv)
    _STARTUP_FLAGS["web_search"] = args.web_search
    _STARTUP_FLAGS["web_fetch"] = args.web_fetch

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
    if args.host == "0.0.0.0":
        # 监听全网卡时必须把无鉴权这件事说到脸上(成熟工具的通用做法)。
        print("  ⚠ 监听 0.0.0.0:同一网络内任何人都能打开工作台(无登录鉴权),请只在可信网络运行。")
    print(f"  会话目录  {sessions_root}")
    print(f"  工作区    {workspace}")
    print(f"  前端产物  {dist_dir}{'(存在)' if dist_dir.is_dir() else '(未构建)'}")
    # 联网工具状态:与 CLI 同口径**如实打印**(此前工作台完全没有这一行,
    # 于是"agent 说没有联网工具"这件事在界面上毫无线索)。
    _s, _f, _note = _web_flags({})
    keys = _WebKeys.get()
    print(f"  联网      {_note}")
    if not keys.tavily:
        print("            (没有 TAVILY_API_KEY——配了才能开)")
    if not keys.firecrawl:
        print("            (没有 FIRECRAWL_API_KEY——网页精读不可用)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
