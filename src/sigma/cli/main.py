"""sigma 的命令行入口。

两种形态（2026-09-21 起）

1. **一次性模式** ``sigma -p "任务"``：跑完退出，退出码见下；
2. **交互模式** 不带 ``-p`` 且 stdin 是 TTY：逐条输入任务、跨轮累积历史。

两种模式**共用**渲染器与会话组装（``sigma.cli.render`` / ``sigma.sdk``），
不各写一套——理由见 ``sdk.py``：两份组装会各自漂移。

**交互模式不支持中途打断**（P3 的 steering 未做）：一条任务跑完整轮
才能输入下一条。这是明确边界，写进启动横幅，不假装支持。

退出码（Q4 已拍板）
    ``0``   正常结束——**任务成没成都不影响退出码**
    非 ``0`` harness 自身失败（缺 key、工作区不存在、provider 连不上）

    把"任务没做对"混进退出码，会让"harness 崩了"与"模型没做对"无法区分，
    而 CI 与脚本调用只关心前者。

安全姿态（每次启动都要提示）
    D5 的三层软边界在 P1 **一层都没落地**。``read`` 能读任意路径、
    ``write`` / ``edit`` 能写任意路径，``bash`` 能以当前用户权限执行任意命令，
    没有任何约束。
    **不知道边界在哪，比边界不存在更危险**——所以这条提示不能省。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigma import __version__
from sigma.config.settings import (
    ENV_VAR_NAME,
    FIRECRAWL_ENV_VAR,
    TAVILY_ENV_VAR,
    USER_CONFIG_DIR,
    resolve_api_key,
    resolve_firecrawl_api_key,
    resolve_tavily_api_key,
)
from sigma.cli.repl import run_repl
from sigma.prompts.system_prompt import build_system_prompt
from sigma.sdk import (
    InteractiveSession,
    default_registry,
    run_task,
    scan_skills,
)
from sigma.tools.registry import ToolRegistry
from sigma.security.shadow_checkpoint import ShadowCheckpoint
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sigma.security.approval import DANGEROUS_PATTERNS, Allowlist, CliApprovalGate
from sigma.cli.interactive import choose_option
from sigma.cli.repl import _LineBroker, _start_stdin_reader
from sigma.observability.timeline import build_timeline, render_timeline, timeline_to_json
from sigma.memory.file_store import memory_dir_for, scan_memory
from sigma.sessions.repo_map import build_repo_map
from sigma.observability.trace import trace_path_for
from sigma.cli.render import TerminalRenderer
from sigma.hooks.base import (
    ApprovalHook,
)
from sigma.agent.messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
)
from sigma.agent.types import TurnResult
from sigma.providers.base import BaseProvider
from sigma.providers.openai import OpenAICompatProvider
from sigma.providers.registry import builtin_providers
from sigma.sessions.sessions import (
    SESSION_SUFFIX,
    SessionPreview,
    list_sessions,
    latest_session_id,
    new_session_id,
    session_path,
    session_previews,
)
from sigma.sessions.store import JsonlStore
from sigma.sessions.tree import SessionTree

EXIT_OK = 0
EXIT_HARNESS_ERROR = 2
EXIT_INTERRUPTED = 130

# provider 列表**不在这一层**——它属于协议层（`sigma.providers.registry`）。
# 2026-09-20 重构子项 E：此前这份列表硬编码在这里，那是分层错误——
# 换一个入口（直接调 sdk.run_task、评测运行器）就得再抄一份。
# 现在这里只留"默认用哪个"这一个**产品决策**。
DEFAULT_PRESET = "deepseek"

#: 会话文件放哪——**"策略"在这一层**。
#:
#: P2 详规 Q1 定了落点是 `~/.sigma/sessions/<id>.jsonl`，而 `store.py` 与
#: `sessions.py` **都刻意不拼 `Path.home()`**：它们只接受一个 `root`。
#: 判据是「谁决定策略，谁传参」——与批次 8 的"工具层不自己去猜密钥路径"同源。
#: 放在用户级配置目录（而不是 `<workspace>/.sigma/`）还有一个具体理由：
#: **往用户的工作区里写目录会污染别人的仓库**（那个项目未必 gitignore 它）。
DEFAULT_SESSIONS_DIR = USER_CONFIG_DIR / "sessions"
SESSION_DIR_HINT = "~/.sigma/sessions"


def build_parser() -> argparse.ArgumentParser:
    """CLI 参数。"""
    parser = argparse.ArgumentParser(
        prog="sigma",
        description="sigma —— 一个自研的 coding agent harness",
    )
    # -p 与 -i 互斥：一个是"跑一条就走"，一个是"进去聊"，同时给没有意义
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-p",
        "--prompt",
        help="一次性模式：要执行的任务描述。",
    )
    mode_group.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help=(
            "强制交互模式。**Git Bash / mintty / 管道里必须用这个**——"
            "那些环境判断不出 stdin 是不是控制台（见 stdin_is_interactive 的说明）。"
        ),
    )
    parser.add_argument(
        "--preset",
        choices=builtin_providers().names(),
        default=None,
        help=f"厂商预设，默认 {DEFAULT_PRESET}",
    )
    parser.add_argument("--base-url", default=None, help="覆盖 base_url")
    parser.add_argument("--model", default=None, help="覆盖模型名")
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key；不传则读环境变量 SIGMA_API_KEY（推荐，不进 shell 历史）",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help=(
            "工作区根目录，默认当前目录。**写操作的硬边界**（L1：write/edit/bash 的 cwd "
            "不得越出它）；读操作不受限。它不是沙箱——bash 仍能以你的用户权限执行任意命令"
        ),
    )
    parser.add_argument("--max-rounds", type=int, default=20, help="最大轮数，默认 20")
    parser.add_argument("--temperature", type=float, default=0.0, help="采样温度，默认 0")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="流式输出之外，结束再打印一遍完整消息序列（调试 / 评测用）",
    )
    parser.add_argument(
        "--timeline",
        nargs="?",
        const="",
        default=None,
        metavar="ID",
        help=(
            "查看一个会话的执行时间线（轮次×延迟/TTFT/token/缓存×工具×审批），"
            "然后退出；不带 ID 看最近的会话。不启动模型。旧会话没有 trace 文件时"
            "延迟用消息时间戳差近似（标 ≈）；审批留痕自 trace 层引入起才有"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="--timeline 配套：输出机器可读 JSON（字段对齐 architecture 7.5 口径）",
    )
    parser.add_argument(
        "--no-obs",
        action="store_true",
        help=(
            "关闭观测层（trace 文件采集，P5-批次1）。默认开启——trace 与会话文件"
            "同目录、不进模型上下文、不进会话树；采集失败只停用观测、不影响任务"
        ),
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help=(
            "关闭跨会话记忆（P5-批次3）。默认开启——模型可把学到的东西写进"
            ".sigma/memory/（你随时可读可改可删），下个会话的常驻区会带上"
            "记忆索引；关闭后不扫描、不注入，行为与没有该机制完全一致"
        ),
    )
    parser.add_argument(
        "--no-repo-map",
        action="store_true",
        help=(
            "关闭 repo map（工作区结构快照进常驻区）。默认开启——会话启动时"
            "扫一次文件清单与 Python 顶层符号，帮模型第一轮 grep 就有方向；"
            "关闭后不扫描、不注入，行为与没有该功能完全一致"
        ),
    )
    parser.add_argument(
        "--no-approval",
        action="store_true",
        help=(
            "关闭 L3 审批层（危险指令/越界访问的执行前确认）。"
            "默认开启——关掉后危险动作将直接执行、只靠 L1/L2 兜底，横幅会明说"
        ),
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help=(
            "关闭影子 git checkpoint（D5 的 L2：写批次前自动快照、可整体回滚）。"
            "默认开启——关掉之后破坏性操作**不可回滚**，横幅会明说这一点"
        ),
    )
    parser.add_argument(
        "--checkpoint-watermark-mb",
        type=int,
        default=512,
        help=(
            "影子库水位上限（MB），默认 512。库目录总大小超过它就按"
            "“最旧优先”清理：先删闲置超 1 小时的其他会话分支，仍超则把"
            "当前会话砍到基线+最近 20 个快照；清完仍超会在横幅明说。"
            "传 0 关闭水位治理"
        ),
    )
    rollback = parser.add_mutually_exclusive_group()
    rollback.add_argument(
        "--rollback",
        action="store_true",
        help="回到最近一次写操作之前的状态（执行前会先自动快照一次，回滚本身也可回滚）",
    )
    rollback.add_argument(
        "--rollback-to",
        default=None,
        metavar="REF",
        help="回到指定的快照；用 --list-checkpoints 看有哪些（接受 ref 前缀）",
    )
    parser.add_argument(
        "--list-checkpoints",
        action="store_true",
        help="列出本会话的可用快照（标签 + ref），然后退出",
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help=(
            "禁用整个联网工具组（web_search 搜索 / web_fetch 精读）。"
            "默认：解析到哪个 key 就启用哪个（各 1000 credits/月，超额自动禁用）"
        ),
    )
    parser.add_argument(
        "--sub-agent",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "task 工具默认开启：模型可派后台子 agent（独立上下文、最多 3 个并发）"
            "执行子任务，完成后自动回报；**是否派发由模型按任务自行判断**"
            "（星辰 2026-09-27 拍板）。传 --no-sub-agent 关闭。"
            "子任务会消耗额外 token。"
        ),
    )
    # 会话接续（P2-5）。两者互斥：一个说"续最近那个"，一个说"用这个 id"，
    # 同时给没有意义——而 argparse 的互斥组会把这句话变成启动时的报错，
    # 比"后者静默覆盖前者"好。
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument(
        "--continue",
        dest="resume",
        action="store_true",
        help=(
            "续接**最近修改**的那个会话（含它的全部历史）。"
            "一个会话都没有时开一个新的，并明确告诉你。"
        ),
    )
    session_group.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="使用指定 id 的会话；不存在就新建。不传则每次生成一个新的。",
    )
    parser.add_argument(
        "--sessions-dir",
        default=None,
        metavar="PATH",
        help=f"会话文件目录，默认 {SESSION_DIR_HINT}。",
    )
    parser.add_argument(
        "--skills-dir",
        default=None,
        metavar="PATH",
        help="技能目录，默认 <工作区>/extensions/skills。没有技能时不注册 load_skill。",
    )
    parser.add_argument("--version", action="version", version=f"sigma {__version__}")
    return parser


def stdin_is_interactive() -> bool:
    """stdin 是不是一个**真控制台**。

    Windows 上不能只信 ``sys.stdin.isatty()``
        NUL 设备（``subprocess.DEVNULL``、``< /dev/null``）在 MSVCRT 里
        **也算字符设备，``isatty`` 返回 True**。于是 CI、脚本、CI hook 里
        调用 ``sigma``（不带 ``-p``）会进 REPL 等输入——症状是"莫名卡住"，
        既不报错也不退出，排查成本极高。**这不是假想，是实测出来的。**

        能区分二者的是 ``GetConsoleMode``：它只对**真实控制台句柄**成功，
        对 NUL 失败。所以 Windows 分支用它。

    代价（写清楚，不藏）
        mintty（Git Bash）与管道的 stdin 不是控制台句柄，本函数会返回 False。
        这些环境里想交互**必须显式加 ``-i``**。

        这是有意取舍：**宁可让人多打两个字符，也不要让 CI 静默挂住**——
        后者的代价是别人的一整晚排查，前者是一次鼠标提醒。
    """
    if sys.platform != "win32":
        return bool(sys.stdin.isatty())
    try:
        import ctypes
        from ctypes import wintypes

        STD_INPUT_HANDLE = -10
        INVALID_HANDLE_VALUE = -1
        handle = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == INVALID_HANDLE_VALUE:
            return False
        mode = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        return bool(ok)
    except Exception:
        # 探测不了就当非交互：猜错了最多是退到帮助页，不会挂住
        return False


def _configure_console() -> None:
    """Windows 下把控制台输出设成 UTF-8（W13）。

    ``cmd.exe`` 默认用 936（GBK），直接打印 ``⏺`` / ``✓`` 会乱码或抛
    ``UnicodeEncodeError``——**输出崩掉意味着整轮对话都看不到**。

    ``errors="replace"`` 是必要的：宁可显示一个替换字符，也不要因为
    一个生僻字让整次输出失败。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # Windows Terminal 下本就是 UTF-8，调用失败不影响功能
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        # 用 isinstance 收窄，而不是 `# type: ignore[union-attr]`：
        # 那种 ignore 只在 **Windows** 上必要（`sys.stdout` 的声明类型随平台不同），
        # 到了 CI 的 Linux 上就变成"多余注释"，而 strict 开了 `warn_unused_ignores`
        # → **本地绿、CI 红**。2026-09-22 第一次跑 CI 就是这么挂的。
        # 判据：**能靠收窄类型解决的，就不要写平台相关的 ignore。**
        if not isinstance(stream, io.TextIOWrapper):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _resolve_config(
    args: argparse.Namespace,
) -> tuple[str, str, str | None, str]:
    """解析配置。

    优先级（高 → 低）：``--api-key`` > 环境变量 ``SIGMA_API_KEY``
    > ``~/.sigma/.env`` > ``./.env``

    "环境变量高于文件"是有意的：临时换 key 时 ``export`` 一句就该生效，
    不必去改文件。

    返回 ``(base_url, model, api_key, key 来源说明)``。
    **来源必须打出来**——否则"改了 .env 却没生效"（因为环境变量赢了）
    会变成一个纯靠猜的问题。
    """
    preset_name = args.preset or DEFAULT_PRESET
    spec = builtin_providers().resolve(preset_name)
    base_url = args.base_url or os.environ.get("SIGMA_BASE_URL") or spec.base_url
    model = args.model or os.environ.get("SIGMA_MODEL") or spec.default_model
    api_key, key_source = resolve_api_key(explicit=args.api_key)
    return base_url, model, api_key, key_source


def _render(message: AgentMessage, index: int) -> str:
    """把一条产出消息渲染成一行。"""
    if isinstance(message, LlmMessageWrapper):
        blocks = message.message.content
        parts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                kind = getattr(block, "type", "")
                if kind == "text":
                    parts.append(f"说 {getattr(block, 'text', '')[:70]!r}")
                elif kind == "tool_call":
                    parts.append(
                        f"调用 {getattr(block, 'name', '?')}"
                        f"({getattr(block, 'arguments', {})})"
                    )
        return f"[{index}] 模型: " + ("；".join(parts) or "(空)")

    if isinstance(message, ToolResultAgentMessage):
        text = ""
        for block in message.content:
            if getattr(block, "type", "") == "text":
                text = getattr(block, "text", "")[:90]
                break
        flag = "失败" if message.is_error else "结果"
        return f"[{index}] {flag} <- {message.tool_name}: {text!r}"

    return f"[{index}] {type(message).__name__}"


def _report(result: TurnResult) -> None:
    """打印执行过程与结果（P4-批次6：过程与统计入面板，最终文本保持裸 print）。"""
    body = "\n".join(
        f"  {_render(message, index)}"
        for index, message in enumerate(result.messages, start=1)
    )
    _CONSOLE.print(
        Panel(
            body if body else "  （本次没有产出消息）",
            title="执行过程",
            border_style="dim",
        )
    )

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("状态", result.status)
    grid.add_row("轮数", str(result.rounds))
    if result.usage is not None:
        grid.add_row(
            "token",
            f"prompt={result.usage.prompt_tokens} "
            f"completion={result.usage.completion_tokens}",
        )
    _CONSOLE.print(grid)

    print()
    print(result.text if result.text else "(模型没有给出最终文本)")

    if result.status == "stopped":
        print()
        print(f"  ⚠ 达到轮数上限（{result.reason}）——**任务可能没有完成**。")
        print("     用 --max-rounds 调大后可重试。")


def _make_provider(
    args: argparse.Namespace, base_url: str, api_key: str
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        provider_name=args.preset or DEFAULT_PRESET,
    )


@dataclass(frozen=True)
class SessionBinding:
    """``--session`` / ``--continue`` 解析出来的结果。

    ``previous_messages`` 单独带出来是为了**在横幅里说清"续上了多少"**：
    只说"已续接"而不说续到几条，用户无法判断这是不是他想要的那个会话——
    而会话 id 是自动生成的，光看 id 认不出来。
    """

    session_id: str
    tree: SessionTree
    resumed: bool
    previous_messages: int


def resolve_session(args: argparse.Namespace, sessions_root: Path) -> SessionBinding:
    """把三个开关解析成"要用哪个会话、接哪棵树"。

    **三种情况都要有明确行为，一种都不能静默**：

    | 输入 | 行为 |
    | --- | --- |
    | ``--continue`` 且目录里有会话 | 续最近那个（按 mtime，同秒按 id，见 `list_sessions`）|
    | ``--continue`` 但一个都没有 | **开新的，并在横幅里明说"没有可续的"** |
    | ``--session ID`` | 有就用、没有就建（`resumed` 由文件是否存在决定）|
    | 都没给 | 新 id，正常持久化 |

    "``--continue`` 找不到时就静默开一个新的"是**必须避免**的：
    用户以为自己在续上下文，实际拿到一个空白会话，于是模型"忘了"之前说过的
    一切——而它看起来只是"这次回答得不好"。
    """
    if args.resume:
        existing = latest_session_id(sessions_root)
        if existing is not None:
            tree = SessionTree.from_store(JsonlStore(sessions_root, existing))
            return SessionBinding(existing, tree, True, len(tree))
        # 空目录也要**带 store**（2026-09-24 review 修复）：``SessionTree()``
        # 是纯内存树——这条分支起的会话全程不落盘，退出后 ``--continue``
        # 永远找不到它，症状是"我刚才那个会话没了"。与下面正常新建路径
        # （``SessionTree(store=store)``）必须同形。
        new_id = new_session_id()
        store = JsonlStore(sessions_root, new_id)
        return SessionBinding(new_id, SessionTree(store=store), False, 0)

    session_id = args.session or new_session_id()
    store = JsonlStore(sessions_root, session_id)
    tree = SessionTree.from_store(store) if store.exists() else SessionTree(store=store)
    return SessionBinding(session_id, tree, store.exists(), len(tree))


#: ``/sessions`` 一次列几个（详规 D-S5）。20 行足够覆盖"最近几次"，
#: 再多就要滚动屏幕、而每一行都要读一次盘（4 KB）。
SESSIONS_LIST_LIMIT = 20


@dataclass(frozen=True)
class SwitchOutcome:
    """``SessionManager.switch_to`` 的结果。

    **为什么要有这个类型而不是返回 ``bool``**：REPL 要打印三种不同的回执——
    "切过去了（续上 N 条）"、"切过去了（本来就是它，重来了）"、
    "没这个会话（报错，但**列表已经显示过，不再重复打一遍**）"。
    只回 ``bool`` 的话，REPL 得自己去判断"是不是当前会话"，
    那等于把 manager 的状态复制一份到 REPL 里——两份状态必然漂移。
    """

    ok: bool
    session_id: str
    messages: int
    switched: bool


def replace_binding_count(binding: SessionBinding, messages: int) -> SessionBinding:
    """换掉 ``previous_messages``，其余原样。

    单独一个函数只为让 ``switch_to`` 里那句"先建树、再数长度"读起来是一条直线——
    树必须先建出来才能 ``len()``，而 ``SessionBinding`` 是 frozen 的。
    """
    return SessionBinding(
        session_id=binding.session_id,
        tree=binding.tree,
        resumed=binding.resumed,
        previous_messages=messages,
    )


def shadow_git_dir_for(workspace: Path) -> Path:
    """工作区级影子库路径（D5 的 L2；P4-批次7 起按工作区共享）。

    **落点 = ``<workspace>/.sigma/session/shadow.git``**（P4-批次8 起挪到
    ``session/`` 子目录下,星辰指定）,与 todo / allowlist 同判据:
    工作区级状态落工作区 ``.sigma/``。库里所有会话共享对象、各占一个分支
    （分支名 = session_id）——同工作区第二次启动 baseline 几乎零成本；
    旧设计"每会话一个全新裸库"会把整个工作区全量复制 N 遍（实测 803MB）。
    ``.sigma/`` 在 checkpoint 的 BUILTIN_EXCLUDES 里,库不会被自己快照。
    """
    return workspace / ".sigma" / "session" / "shadow.git"


def migrate_legacy_shadow_dir(workspace: Path) -> bool:
    """旧路径库(批次7 的 ``.sigma/shadow.git``)原子迁移到批次8 新路径。

    **同分区 ``os.replace`` 是 O(1) 的目录改名**,不复制任何对象数据,
    回滚点零丢失;迁移后旧路径不复存在。新库已存在(重复启动/已迁移)
    或旧库不存在时是无害的 no-op。返回是否真的迁移了。

    迁移而不是"新库从零重建":重建等于丢掉全部历史回滚点,而用户对
    "还能不能回滚到上周"没有任何心理预期——**搬家可以,烧家不行**。
    """
    legacy = workspace / ".sigma" / "shadow.git"
    new = shadow_git_dir_for(workspace)
    if not legacy.is_dir() or new.exists():
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    os.replace(legacy, new)
    return True


def checkpoint_disabled_reason(workspace: Path, *, no_checkpoint_flag: bool) -> str | None:
    """L2 是否关停的**唯一判定点**（P4-批次7 家目录守门，拍板 A）。

    返回关停原因（``None`` = 启用）：
    ``"--no-checkpoint"``——用户显式关；
    ``"home"``——工作区是家目录。全量快照会复制整个 AppData
    （实测单次扫描 65 秒、每会话数百 MB），而"把家目录回滚到几分钟前"
    本身就是危险动作。只影响**新建会话**；``--rollback`` 是人的恢复
    动作，不受此门限制。
    """
    if no_checkpoint_flag:
        return "--no-checkpoint"
    if workspace.resolve() == Path.home().resolve():
        return "home"
    return None


#: 面板/表格输出。markup/highlight 全关：CLI 的文本承载模型数据与中文方括号，
#: 解释样式会吃字（与 G98 同源）；颜色只经 style=/border_style= 参数。
_CONSOLE = Console(markup=False, highlight=False, emoji=False)
#: stderr 错误输出：默认红色。
_ERR_CONSOLE = Console(stderr=True, markup=False, highlight=False, emoji=False, style="red")


def _err(message: str) -> None:
    """stderr 错误行：红色、与 print 同形（harness 自身的失败必须显眼）。"""
    _ERR_CONSOLE.print(message, soft_wrap=True)


def _safe_session_id(session_id: str) -> str:
    """把用户敲进来的 id 净化成"能当文件名用的 id"。

    **为什么要有这个函数，而不是让 ``/switch`` 自己拼路径**：
    ``/switch`` 的输入**是人手敲的**（还可能从别处粘过来）。
    ``../../secret`` 这类 id 在净化之前会拼出一个会话目录之外的路径，
    而 ``switch_to`` 的第一件事就是 ``path.is_file()``——
    那已经是一次**越界的文件存在性探测**了。先净化再拼，探测范围就被钉在目录内。

    **判据必须与 ``JsonlStore.path`` 逐字一致**（``/`` ``\\`` → ``_``），
    不能用 ``Path(...).name`` 那种"取末段"的写法——那会把 ``a/b`` 变成 ``b``，
    而 ``JsonlStore`` 会把它存成 ``a_b.jsonl``。两份净化一旦不一致，
    症状是**切换到一个永远不存在的会话**：影子库路径对不上、
    ``--rollback`` 找不到快照，而 ``path.is_file()`` 又确实返回 False
    （``a/b.jsonl`` 会去找子目录），排查时看起来像"文件丢了"。
    （这条是实测出来的——第一版就是 ``Path(...).name``，当场被
    ``test_cli_shadow_dir_is_next_to_session_file`` 抓住。）

    ``..`` 单独挡一道（**不能靠"把点都换成下划线"**）：
    会话 id 本身就含点（``20260923-110000.000-bbbb`` 里的毫秒位），
    把点全换掉会把**每一个真实 id 都改成不存在的名字**——
    症状是"所有 `/switch` 都说没这个会话"，而会话明明在列表里
    （第一版就是这么错的，被 ``test_manager_switch_accepts_file_suffix``
    与三条 REPL 切换测试当场抓住）。

    所以这里只处理**真的想上跳**的形状：``..`` 整段替换掉即可——
    替换之后没有分隔符残留，``Path(root) / "a_b"`` 不可能走出目录。

    ``.jsonl`` 后缀也在这里剥掉：``/switch abc.jsonl`` 与 ``/switch abc``
    应当是同一个会话——用户在 ``/sessions`` 里看到的是 id，
    但从资源管理器里看到的是文件名，两种写法都得认。
    """
    safe = session_id.replace("/", "_").replace("\\", "_").replace("..", "_")
    if safe.endswith(SESSION_SUFFIX):
        safe = safe[: -len(SESSION_SUFFIX)]
    return safe


def run_rollback(
    args: argparse.Namespace, sessions_root: Path, workspace: Path
) -> int:
    """执行 ``--rollback`` / ``--rollback-to`` / ``--list-checkpoints``。

    这三件事**都不该启动模型**：回滚是人的动作。让模型去调用"回滚"更危险
    （它可以把自己刚搞坏的状态"回滚"成另一个坏状态，而用户看不到）。
    """
    session_id = args.session or latest_session_id(sessions_root)
    if session_id is None:
        _err(f"[harness 错误] {sessions_root} 里没有会话，无法回滚。")
        return EXIT_HARNESS_ERROR

    # 独立入口也可能先于交互模式跑(--rollback 脚本化):迁移放构造**之前**——
    # 先构造会让新路径先初始化成空库,旧库改名就被挡住了。
    migrate_legacy_shadow_dir(workspace)
    shadow = ShadowCheckpoint(
        root=shadow_git_dir_for(workspace), workspace=workspace, branch=session_id
    )
    if not shadow.available:
        _err(f"[harness 错误] 影子库不可用：{shadow.unavailable_reason}")
        return EXIT_HARNESS_ERROR

    # 工作区配对（**这条闸挡住了一次真事故**）：
    # 影子库只对"它创建时那个工作区"有意义。不检查的后果是 `reset --hard`
    # 拿 A 的快照去改 B——把 B 里快照没有的文件全删掉。
    # 这里用**创建时记下的**工作区来执行，用户的 `--workspace` 只用于核对。
    mismatch = shadow.workspace_mismatch()
    if mismatch:
        _err(f"[harness 错误] {mismatch}")
        return EXIT_HARNESS_ERROR
    recorded = shadow.recorded_workspace
    assert recorded is not None  # mismatch 为空 ⇒ 一定有记录
    workspace = recorded

    refs = shadow.refs()
    table = Table(title=f"会话 {session_id} 的快照（新 → 旧）")
    table.add_column("#", justify="right", style="dim")
    table.add_column("ref")
    table.add_column("标签")
    if not refs:
        table.add_row("-", "-", "（还没有任何快照）")
    for index, info in enumerate(refs):
        table.add_row(str(index), info.ref[:8], info.label)
    _CONSOLE.print(table)

    if args.list_checkpoints:
        return EXIT_OK

    target = args.rollback_to
    if target is None:
        # `--rollback`：回到"最近一次写之前"。基线（baseline）不算"写之前"，
        # 所以要跳过它——没有其它快照时退到基线，那正是"这一轮什么写都没发生过"。
        written = [info for info in refs if not info.label.startswith("baseline")]
        if not written:
            print("没有可回滚的写操作（本会话还没有写过东西）。")
            return EXIT_OK
        target = written[0].ref
    else:
        # 接受 ref 前缀：人手敲 40 位哈希不现实，而前缀在单个仓库里足够唯一。
        matched = [info.ref for info in refs if info.ref.startswith(target)]
        if not matched:
            _err(f"[harness 错误] 找不到快照 {target!r}。")
            return EXIT_HARNESS_ERROR
        if len(matched) > 1:
            _err(
                f"[harness 错误] 前缀 {target!r} 匹配到 {len(matched)} 个快照，"
                "请多给几位。"
            )
            return EXIT_HARNESS_ERROR
        target = matched[0]

    report = shadow.restore(target)
    if not report.ok:
        _err(f"[harness 错误] 回滚失败：{report.note}")
        return EXIT_HARNESS_ERROR

    print(
        f"已回滚到 {report.ref[:8]}：恢复 {len(report.changed)} 个文件、"
        f"删除 {len(report.deleted)} 个新增文件。"
    )
    if report.deleted:
        for name in report.deleted[:20]:
            print(f"  已删除 {name}")
    if report.protected:
        # **保护数必须打出来**：用户得知道"有些文件本来就不在回滚范围内"，
        # 否则他会以为回滚不完整，或者更糟——以为 .env 之类也回到了旧版。
        print(f"  （{report.protected} 个被 .gitignore/大小上限排除的文件未参与回滚）")
    if report.pre_restore_ref:
        print(f"  回滚前的状态也已快照：{report.pre_restore_ref[:8]}（回滚可再回滚）")
    return EXIT_OK


def run_timeline(args: argparse.Namespace, sessions_root: Path) -> int:
    """``--timeline``：渲染一个会话的执行时间线。

    与回滚同一条理由的**只读的人的动作**：不启动模型，所以排在
    API key 检查之前——"看看上次跑了什么"不该被密钥挡住。
    会话 JSONL 是主数据源（旧会话也能查）；trace 文件存在时叠加
    精确延迟/TTFT 与审批留痕（详规 §2.4 的优先级）。
    """
    session_id = args.timeline or latest_session_id(sessions_root)
    if session_id is None:
        _err(f"[harness 错误] {sessions_root} 里没有会话，无法查看时间线。")
        return EXIT_HARNESS_ERROR
    safe = _safe_session_id(session_id)
    path = session_path(sessions_root, safe)
    if not path.is_file():
        _err(f"[harness 错误] 没有这个会话：{safe}")
        return EXIT_HARNESS_ERROR
    trace_file = trace_path_for(sessions_root, safe)
    report = build_timeline(safe, path, trace_file if trace_file.is_file() else None)
    if args.json:
        print(json.dumps(timeline_to_json(report), ensure_ascii=False, indent=2))
    else:
        for line in render_timeline(report):
            _CONSOLE.print(line, soft_wrap=True)
    return EXIT_OK


async def _run_once(
    args: argparse.Namespace,
    workspace: Path,
    base_url: str,
    model: str,
    api_key: str,
    *,
    registry: ToolRegistry,
    system_prompt: str,
    tree: SessionTree,
    session_id: str,
    shadow_git_dir: Path | None = None,
    skills_root: Path | None = None,
    approval: ApprovalHook | None = None,
    ask: Any = None,
    checkpoint_watermark_bytes: int | None = None,
    enable_trace: bool = True,
    enable_memory: bool = True,
    enable_repo_map: bool = True,
) -> TurnResult:
    """一次性模式。渲染器与交互模式**同一个**（``TerminalRenderer``）。

    ``approval``/``ask``：一次性模式**也弹确认**（批次 Q2 拍板）；
    stdin 不可交互时 confirmer 自行回退为拒绝。
    """
    provider = _make_provider(args, base_url, api_key)
    try:
        return await run_task(
            args.prompt,
            provider=provider,
            workspace_root=workspace,
            model=model,
            max_rounds=args.max_rounds,
            temperature=args.temperature,
            registry=registry,
            system_prompt=system_prompt,
            extra_hooks=[TerminalRenderer()],
            session_id=session_id,
            tree=tree,
            shadow_git_dir=shadow_git_dir,
            skills_root=skills_root,
            enable_sub_agent=args.sub_agent,
            approval=approval,
            ask=ask,
            checkpoint_watermark_bytes=checkpoint_watermark_bytes,
            enable_trace=enable_trace,
            enable_memory=enable_memory,
            enable_repo_map=enable_repo_map,
        )
    finally:
        await provider.aclose()


class SessionManager:
    """交互模式里"当前用哪个会话"的唯一持有者（P5 详规 D-S2）。

    **为什么需要它，而不是把开关散在 REPL 里**
        `/switch` 不是"改一个字符串变量"——它要换掉**整棵 ``SessionTree``**、
        换掉 ``shadow_git_dir``、换掉时间戳，也就是**重造一个
        ``InteractiveSession``**（D-S1 方案 A）：
        ``InteractiveSession`` 的构造参数有二十来个，散在 REPL 里意味着
        每新增一个参数就要在 REPL 里补一次，而**漏补不会报错**——
        症状是"切换之后 checkpoint 不记录了 / 技能没了"，看起来像随机故障。
        所以组装参数收在这里**一次性**持有，REPL 只发号施令。

    **分层**（D-S2）：解析在 ``repl.py``、组装在本类、列表在
    ``sigma.sessions.sessions``。REPL 不碰 ``JsonlStore`` 与 ``SessionTree``。

    **为什么不是就地改现有的 ``InteractiveSession``**（方案 B 被否）
        会话对象里绑了 ``provider`` / ``registry`` / ``context`` / ``checkpoint`` /
        ``todo`` 等一堆状态，其中 ``checkpoint`` 与 ``todo`` 是**构造时**决定的。
        就地改 = 在 ``sdk.py`` 上开一堆 setter，每个 setter 都是一个
        "改完之后哪些状态没跟着改"的坑。重建则**只有一条路径**，
        与"只有一处组装，不会漂移"是同一条判据。
        代价是切换时重扫一次技能索引——可忽略，切换是人手动触发的低频动作。

    **``todo`` 账本刻意不随会话切换**：它在 ``<workspace>/.sigma/todo.json``，
    是**工作区级**的（详规 D-S4）。换会话而 todo 跟着换，会让"我列了五条待办、
    切个会话回来全没了"——而待办本来就是跨会话的工作记忆。
    """

    def __init__(
        self,
        *,
        args: argparse.Namespace,
        workspace: Path,
        base_url: str,
        model: str,
        api_key: str,
        registry: ToolRegistry,
        system_prompt: str,
        sessions_root: Path,
        binding: SessionBinding,
        shadow_git_dir: Path | None,
        skills_root: Path | None,
        approval: ApprovalHook | None = None,
        ask: Any = None,
        checkpoint_watermark_bytes: int | None = None,
        enable_trace: bool = True,
        enable_memory: bool = True,
        enable_repo_map: bool = True,
        make_provider: Callable[[], BaseProvider] | None = None,
    ) -> None:
        self._args = args
        self._workspace = workspace
        self._base_url = base_url
        self._model = model
        self._api_key = api_key
        self._registry = registry
        self._system_prompt = system_prompt
        self._sessions_root = sessions_root
        self._shadow_git_dir = shadow_git_dir
        self._skills_root = skills_root
        # L3 审批门与 ask_user 通道：整个进程一份，跨会话共享
        # （审批决定与 allowlist 不随会话切换而丢）。
        self._approval = approval
        self._ask = ask
        #: 影子库水位(字节,批次8):None = 关。每个新建会话都带着同一份。
        self._checkpoint_watermark_bytes = checkpoint_watermark_bytes
        #: 观测层(P5-批次1,Q2 拍板默认开):--no-obs 整层关。
        self._enable_trace = enable_trace
        #: 跨会话记忆(P5-批次3,默认开):--no-memory 整层关。
        self._enable_memory = enable_memory
        #: repo map(P1 收尾,默认开):--no-repo-map 整层关(连目录遍历都不做)。
        self._enable_repo_map = enable_repo_map
        self._binding = binding
        #: provider 的**造法**可注入：测试要离线跑（``FakeProvider``），
        #: 而默认路径要真造 ``OpenAICompatProvider``。注入的是"造法"不是
        #: "实例"，因为 provider **整个进程只造一个**——切换会话时不重建它
        #: （重建等于把连接池丢掉，而切换跟 provider 无关）。
        self._make_provider = make_provider or (
            lambda: _make_provider(args, base_url, api_key)
        )
        self._provider = self._make_provider()
        self._session = self._build(binding, shadow_git_dir)

    # -- 组装 ---------------------------------------------------------------

    def _build(
        self, binding: SessionBinding, shadow_git_dir: Path | None
    ) -> InteractiveSession:
        """按一份 binding 造会话。

        快照隔离靠**分支**（P4-批次7）：共享库里所有会话共用同一个
        ``shadow_git_dir``，``InteractiveSession`` 用 ``session_id`` 当分支名——
        切换会话即切换分支，互不可见。旧的"每会话独立影子库"时代，
        这里必须按 session_id 现算路径，否则新会话把快照写进旧会话的库；
        现在路径恒定，隔离责任移到了分支上（仍由 session_id 派生，同源）。

        ⚠️ 注册表传的是**克隆**（2026-09-24 review 修复）：``--sub-agent`` 时
        ``InteractiveSession`` 会往注册表里注册 TaskTool，而所有会话此前共享
        同一个 ``self._registry``——第二次 ``_build``（/switch、/new）必撞
        ``DuplicateToolError``，REPL 当场终结。克隆后每个会话一份登记簿，
        会话间互不影响；TaskTool 的信箱状态也天然随会话走，不会串。
        工具实例仍共享（同一批对象），只有"谁注册了什么"这份账各自记。
        """
        return InteractiveSession(
            provider=self._provider,
            workspace_root=self._workspace,
            model=self._model,
            max_rounds=self._args.max_rounds,
            temperature=self._args.temperature,
            registry=self._registry.clone(),
            system_prompt=self._system_prompt,
            extra_hooks=[TerminalRenderer()],
            checkpoint_watermark_bytes=self._checkpoint_watermark_bytes,
            approval=self._approval,
            ask=self._ask,
            session_id=binding.session_id,
            tree=binding.tree,
            shadow_git_dir=shadow_git_dir,
            skills_root=self._skills_root,
            enable_sub_agent=self._args.sub_agent,
            enable_trace=self._enable_trace,
            enable_memory=self._enable_memory,
            enable_repo_map=self._enable_repo_map,
        )

    # -- 查询 ---------------------------------------------------------------

    @property
    def current(self) -> InteractiveSession:
        """当前会话。**每次调用都取最新**——REPL 不能缓存它。"""
        return self._session

    @property
    def current_id(self) -> str:
        return self._binding.session_id

    @property
    def approval_gate(self) -> ApprovalHook | None:
        """/allowlist 斜杠命令用;--no-approval 时为 None。"""
        return self._approval

    @property
    def sessions_root(self) -> Path:
        return self._sessions_root

    def list(self) -> list[SessionPreview]:
        """最近 20 个会话的预览（``/sessions`` 的数据源）。

        **不缓存**：用户随时可能在另一个终端里跑了 ``sigma -p``，
        缓存会让 ``/sessions`` 少显示一个刚产生的会话——
        而"我刚跑的那个会话在哪"正是他会敲这条命令的原因。
        """
        return session_previews(self._sessions_root, limit=SESSIONS_LIST_LIMIT)

    # -- 切换 ---------------------------------------------------------------

    def new(self) -> str:
        """开一个新会话并立即生效。返回新 id。

        ``SessionTree(store=...)``（带 store 的空树）而不是 ``SessionTree()``：
        前者让新会话一下笔就落到新文件上——两条路都通，但带 store 的那种
        不必在第一次 write 时再决定"写到哪"。
        """
        session_id = new_session_id()
        store = JsonlStore(self._sessions_root, session_id)
        binding = SessionBinding(
            session_id=session_id,
            tree=SessionTree(store=store),
            resumed=False,
            previous_messages=0,
        )
        self._binding = binding
        self._session = self._build(binding, self._shadow_dir_for(session_id))
        return session_id

    def switch_to(self, session_id: str) -> SwitchOutcome:
        """切到指定会话，**历史与 checkpoint 都接上**（详规 D-S4）。

        ``SessionTree.from_store`` 是这里的关键动作：它把磁盘上的 JSONL
        读成一棵有历史的消息树。少了它，切换就变成"只换了个 id 的空会话"——
        **症状是模型突然忘了刚才说的一切**，而它照样能答，
        看起来只是"这次答得不好"。G87 就是钉这条的。

        切到当前会话是**合法**的（返回 ``switched=False``）：用户可能只是想
        "重来一遍"（把内存里未落盘的状态丢掉）。这不是错误，不该报错。
        """
        safe = _safe_session_id(session_id)
        path = session_path(self._sessions_root, safe)
        if not path.is_file():
            return SwitchOutcome(ok=False, session_id=safe, messages=0, switched=False)

        binding = SessionBinding(
            session_id=safe,
            tree=SessionTree.from_store(JsonlStore(self._sessions_root, safe)),
            resumed=True,
            previous_messages=0,
        )
        messages = len(binding.tree)
        binding = replace_binding_count(binding, messages)
        switched = safe != self._binding.session_id
        self._binding = binding
        self._session = self._build(binding, self._shadow_dir_for(safe))
        return SwitchOutcome(ok=True, session_id=safe, messages=messages, switched=switched)

    def _shadow_dir_for(self, session_id: str) -> Path | None:
        """共享库时代路径恒定（P4-批次7）：所有会话同一个 root，
        隔离由 ``InteractiveSession`` 内部的**分支**（session_id）承担。
        ``session_id`` 参数保留——``switch_to`` 的调用行是注入实验
        （``gate_injection_p5.py`` E76）的字面锚点，不动它。
        """
        if self._shadow_git_dir is None:
            return None
        return self._shadow_git_dir

    # -- 生命周期 -----------------------------------------------------------

    async def aclose(self) -> None:
        """关掉 provider。**provider 归 manager 所有，会话只是借用**——
        所以切换时不能关它（关了就再也发不出请求），只能在这里关一次。

        ``aclose`` 是**可选**能力（``BaseProvider`` 上没有它，
        ``FakeProvider`` 也没有），所以这里按"有没有"调用，而不是
        ``assert`` 一个基类字段。代价是拼错方法名不会在类型层被发现——
        所以下面那行断言把它钉住（拼错时 AttributeError 立刻暴露，
        而不是静默地永远不关连接）。

        P4-批次7：关停前对共享影子库跑一次 ``gc``（30s 超时、失败静默）
        ——loose objects 不打包会越攒越多。库里所有会话共享对象，
        收尾打一次包全体受益；``gc.packRefs=false`` 保证松散 ref 不变量不被破坏。
        """
        checkpoint = getattr(self._session, "checkpoint", None)
        if checkpoint is not None:
            try:
                checkpoint.gc()
                # 水位治理(批次8):收尾 gc 后是第二个触发点——退进度前
                # 最后一次把库压回水位以下。失败静默,同 gc 的保险丝语义。
                checkpoint.enforce_watermark()
            except Exception:
                # GC/水位是优化不是正确性：失败不打扰收尾（last_error 里留痕）。
                pass
        closer = getattr(self._provider, "aclose", None)
        if closer is None:
            return
        await closer()


async def _run_interactive(
    args: argparse.Namespace,
    workspace: Path,
    base_url: str,
    model: str,
    api_key: str,
    *,
    registry: ToolRegistry,
    system_prompt: str,
    tree: SessionTree,
    session_id: str,
    sessions_root: Path,
    shadow_git_dir: Path | None = None,
    skills_root: Path | None = None,
    approval: ApprovalHook | None = None,
    ask: Any = None,
    broker: _LineBroker | None = None,
    checkpoint_watermark_bytes: int | None = None,
    enable_trace: bool = True,
    enable_memory: bool = True,
    enable_repo_map: bool = True,
) -> int:
    """交互模式。会话对象跨轮复用，历史才不会丢（门槛 G35）。

    组装搬进了 :class:`SessionManager`（P5 详规 D-S2）；本函数只剩
    "造 manager → 跑 REPL → 收尾"三件事。
    """
    binding = SessionBinding(session_id, tree, resumed=False, previous_messages=0)
    manager = SessionManager(
        args=args,
        workspace=workspace,
        base_url=base_url,
        model=model,
        api_key=api_key,
        registry=registry,
        system_prompt=system_prompt,
        sessions_root=sessions_root,
        binding=binding,
        shadow_git_dir=shadow_git_dir,
        skills_root=skills_root,
        approval=approval,
        ask=ask,
        checkpoint_watermark_bytes=checkpoint_watermark_bytes,
        enable_trace=enable_trace,
        enable_memory=enable_memory,
        enable_repo_map=enable_repo_map,
    )
    if broker is not None:
        _start_stdin_reader(broker)
    try:
        return await run_repl(manager, broker=broker)
    finally:
        await manager.aclose()


def _report_web_tool(
    registry: ToolRegistry, name: str, label: str, source: str, env_var: str
) -> list[str]:
    """联网工具的状态行（进横幅面板）：是否注册、额度还剩多少、账本有没有异常。

    为什么按"注册表里有没有"判断，而不是按"key 有没有"：
        两者在同一处决定（本函数上方），但**注册表才是事实**——
        将来多一个开关键时，这里不会静默打印出与实际不符的状态。
    """
    if name not in registry.names():
        return [f"  {label}    未启用（未找到 {env_var}）"]
    tool: Any = registry.get(name)
    usage = tool.quota.snapshot()
    lines = [
        f"  {label}    已启用（{source}）"
        f"｜剩余 {usage.remaining}/{usage.cycle_limit} credits"
    ]
    # 账本写不下去时必须**说出来**：静默失败的后果是
    # "额度计数每次都从 0 开始"，而用户只会觉得"额度怎么用不完"。
    if usage.last_error:
        lines.append(f"          ⚠ 额度账本异常：{usage.last_error}")
    return lines


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    _configure_console()  # 必须在任何 print 之前：中文与 ⏺ 靠它才不乱码

    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # W11：既没有 -p 又没有 -i，且 stdin 不是真控制台 → **绝不能进 REPL**。
    # 否则脚本 / CI / 管道里调用 sigma 会挂住等输入，
    # 症状是"卡住"而不是报错。
    #
    # 回滚三件事是例外：它们不需要 prompt、不启动模型，是**独立的动作**
    # （`sigma --list-checkpoints` 就该能在脚本里跑）。少了这半个条件，
    # 回滚在非控制台（含 CI 与本次冒烟）里只会打印帮助——第一版就是这样。
    wants_rollback = args.rollback or args.rollback_to is not None or args.list_checkpoints
    if (
        not args.prompt
        and not args.interactive
        and not wants_rollback
        and args.timeline is None
        and not stdin_is_interactive()
    ):
        parser.print_help()
        print()
        print('提示：一次性模式用法 —— sigma -p "把 foo.py 里的 off-by-one 修掉"')
        print("      想进交互模式：sigma -i（或在真实控制台里直接敲 sigma）")
        return EXIT_HARNESS_ERROR

    base_url, model, api_key, key_source = _resolve_config(args)
    workspace = Path(args.workspace).expanduser()

    # 以下都是"harness 自己跑不起来"，属 Q4 里的非 0 ——与任务成败无关
    if not workspace.exists() or not workspace.is_dir():
        _err(f"[harness 错误] 工作区不存在或不是目录：{workspace}")
        return EXIT_HARNESS_ERROR

    sessions_root = (
        Path(args.sessions_dir).expanduser()
        if args.sessions_dir
        else DEFAULT_SESSIONS_DIR
    )

    # 影子库路径迁移(P4-批次8):旧库 .sigma/shadow.git → .sigma/session/shadow.git。
    # 必须排在回滚分支**之前**——回滚恰恰发生在旧库还在的时刻。
    migrate_legacy_shadow_dir(workspace)

    # 回滚三件事（--rollback / --rollback-to / --list-checkpoints）**不启动模型**：
    # 它们是人的动作。所以它们**必须排在 API key 检查之前**——
    # 否则"模型密钥失效 / 没配 key"会顺带把"把工作区回滚回去"也堵死，
    # 而那恰恰是最需要回滚的时刻。第一版就把它排在了后面，冒烟时当场发现。
    if args.rollback or args.rollback_to is not None or args.list_checkpoints:
        if args.no_checkpoint:
            _err(
                "[harness 错误] --no-checkpoint 与回滚开关同时给出："
                "关掉 checkpoint 就没有快照可回滚。"
            )
            return EXIT_HARNESS_ERROR
        return run_rollback(args, sessions_root, workspace)
    if args.timeline is not None:
        return run_timeline(args, sessions_root)
    if not api_key:
        _err("[harness 错误] 缺少 API key。三种设置方式，任选一种：")
        _err(f"  1) 写进 {USER_CONFIG_DIR / '.env'}（推荐，在项目目录之外，不会被误提交）")
        _err(f"     内容一行即可：{ENV_VAR_NAME}=sk-xxx")
        _err(f"  2) 临时用：export {ENV_VAR_NAME}=sk-xxx")
        _err("  3) 只用一次：--api-key sk-xxx")
        return EXIT_HARNESS_ERROR

    # 联网工具是可选工具组：**有 key 才注册**。工具 schema 进常驻区，
    # 没配 key 的机器不该为它付 token（批次 7 详规 Q1；批次 8 从两个 key 各自判断）。
    # 提示词与注册表同源：关掉时不出现"有个工具叫 web_search"这句话。
    tavily_key, tavily_source = resolve_tavily_api_key()
    firecrawl_key, firecrawl_source = resolve_firecrawl_api_key()
    web_search_on = not args.no_web_search and bool(tavily_key)
    web_fetch_on = not args.no_web_search and bool(firecrawl_key)

    # 技能：**在构造注册表之前扫一次**——因为它同时决定三样东西：
    # 注册表里有没有 load_skill、提示词里有没有那一行、横幅上打什么。
    # （InteractiveSession 内部还会再扫一次；两次扫描的取舍写在 sdk.scan_skills 的
    #  docstring 里：宁可启动时多读两个目录，也不在 sdk 与 shell 之间传一份状态。）
    skills_root = (
        Path(args.skills_dir).expanduser() if args.skills_dir else None
    )
    skill_scan, _skill_index = scan_skills(workspace, skills_root=skills_root)

    registry = default_registry(
        web_search=web_search_on,
        tavily_api_key=tavily_key,
        web_fetch=web_fetch_on,
        firecrawl_api_key=firecrawl_key,
        skills=skill_scan.skills,
    )
    system_prompt = build_system_prompt(
        web_search=web_search_on,
        web_fetch=web_fetch_on,
        skills=bool(skill_scan.skills),
        task=args.sub_agent,
        memory=not args.no_memory,
    )

    binding = resolve_session(args, sessions_root)

    # 家目录守门（P4-批次7 拍板 A）：判定收在 checkpoint_disabled_reason 一处，
    # main 只消费它的结论；横幅按原因如实展示（见下方安全边界面板）。
    disabled_reason = checkpoint_disabled_reason(
        workspace, no_checkpoint_flag=args.no_checkpoint
    )
    home_gate = disabled_reason == "home"
    shadow_dir = None if disabled_reason else shadow_git_dir_for(workspace)
    # 水位换算(批次8):旗标给 MB,checkpoint 层收字节;0 = 关闭治理。
    checkpoint_watermark_bytes = (
        args.checkpoint_watermark_mb * 1024 * 1024
        if args.checkpoint_watermark_mb > 0
        else None
    )

    # L3 审批门（P3-批次2）：allowlist 落工作区 .sigma/（回滚安全）；
    # --no-approval 整层关闭（脚本场景），横幅如实展示。
    # stdin 行仲裁器:整个进程一份。审批确认 / ask_user / 提示符读输入
    # 都经过它——多线程 input() 会互相抢行,单读者是结构解。
    broker = _LineBroker()

    async def _cli_chooser(
        title_lines: list[str], options: list[str], recommended_index: int | None
    ) -> int | None:
        """交互模式的决策入口:真终端按键选择;非 TTY 编号降级。"""
        return await choose_option(
            title_lines,
            options,
            recommended_index=recommended_index,
            interactive_fallback=broker.ask_line,
        )

    async def _cli_ask(
        question: str, options: list[str], recommended_index: int | None
    ) -> str:
        index = await _cli_chooser([question], options, recommended_index)
        if index is None:
            # 取消(EOF/Esc)→ ask_user 契约:回退推荐项,结果里显式注明
            return ""
        return options[index]

    if args.no_approval:
        approval_hook: ApprovalHook | None = None

        async def ask_channel(
            question: str, options: list[str], recommended_index: int | None
        ) -> str:
            return ""

    else:
        approval_hook = CliApprovalGate(
            workspace=workspace,
            chooser=_cli_chooser,
            allowlist=Allowlist(workspace / ".sigma" / "allowlist.json"),
        )
        ask_channel = _cli_ask

    # 横幅面板（P4-批次6）：内容行与旧版逐字一致，只是从裸 print 换成
    # Panel 承载——"排版"归面板，"说什么"不归它改。
    mode = "一次性" if args.prompt else "交互"
    lines: list[str] = [
        f"  工作区  {workspace.resolve()}",
        f"  模型    {model} @ {base_url}",
        f"  工具    {registry.names()}",
        f"  密钥    已加载（来源：{key_source}）",
    ]
    if args.no_web_search:
        lines.append("  联网    已按 --no-web-search 全部禁用（web_search / web_fetch）")
    else:
        lines.extend(
            _report_web_tool(registry, "web_search", "搜索", tavily_source, TAVILY_ENV_VAR)
        )
        lines.extend(
            _report_web_tool(registry, "web_fetch", "精读", firecrawl_source, FIRECRAWL_ENV_VAR)
        )
    # 技能：**数量 + 问题都要打**。
    # 问题清单尤其重要——一个坏技能文件如果只是被静默跳过，
    # 症状是"我加了技能它怎么不用"，与手工清单漂移是同一种失败。
    if skill_scan.skills:
        names = "、".join(s.name for s in skill_scan.skills)
        lines.append(f"  技能    {len(skill_scan.skills)} 个：{names}")
    for problem in skill_scan.problems:
        lines.append(f"          ⚠ {problem}")
    # 记忆横幅行（P5-批次3）：条数让人知道"它记了多少"，与 sdk 的扫描
    # 同一次语义（各扫一次——与技能"宁可多读一个目录"同一取舍）。
    # 格式异常条数也要打：静默的问题会变成"写了怎么不用"的悬案。
    if args.no_memory:
        lines.append("  记忆    未开启（--no-memory）")
    else:
        memory_scan = scan_memory(memory_dir_for(workspace))
        memory_line = f"  记忆    {len(memory_scan.entries)} 条（.sigma/memory/，--no-memory 关闭）"
        if memory_scan.problems:
            memory_line += f"｜⚠ {len(memory_scan.problems)} 条格式异常"
        lines.append(memory_line)
    # repo map 横幅行（P1 收尾）：与记忆横幅同一取舍——壳侧为展示再算一次
    # （构建是纯本地扫描，成本可忽略）；条数 = 注入的文件行数。
    if args.no_repo_map:
        lines.append("  repo map  未开启（--no-repo-map）")
    else:
        repo_map_text = build_repo_map(workspace)
        file_count = sum(1 for l in repo_map_text.splitlines() if l.startswith("- "))
        lines.append(f"  repo map  {file_count} 个文件（--no-repo-map 关闭）")
    if binding.resumed:
        lines.append(
            f"  会话    已续接 {binding.session_id}"
            f"（载入 {binding.previous_messages} 条历史）"
        )
    elif args.resume:
        # `--continue` 却没东西可续：**必须说出来**。静默开一个新的，
        # 用户会以为自己在续上下文，而模型其实"忘了"之前的一切——
        # 那看起来只是"这次答得不好"。
        lines.append(f"  会话    没有可续的会话（{sessions_root} 是空的），已新建 {binding.session_id}")
    elif args.session:
        lines.append(f"  会话    新建 {binding.session_id}")
    else:
        lines.append(f"  会话    {binding.session_id}（新）")
    if not args.prompt:
        # 只在交互模式打：一次性模式没有 REPL，列命令会让用户以为能敲。
        # **不列全清单**（那是 `/help` 的事）——横幅里只放"存在斜杠命令"这件事，
        # 否则每加一个命令就要改两处文案，而漏改的那处会慢慢过期。
        lines.append("  交互    /help 看命令（/sessions 列会话、/switch 切会话、/new 开新的）")
    if args.no_approval:
        lines.append("  拦截    L1 写路径 · L2 影子快照 · L3 审批已按 --no-approval 关闭")
    else:
        # 非 --no-approval 分支必然已建门(run_rollback 式收窄:条件互斥保证)
        allowlist_count = len(
            Allowlist(workspace / ".sigma" / "allowlist.json").items()
        )
        # 家目录守门（P4-批次7）：L2 被禁时横幅不许再说"L2 影子快照"在岗。
        l2_text = (
            "L2 快照已禁用（家目录工作区）"
            if home_gate
            else "L2 影子快照"
        )
        lines.append(
            f"  拦截    L1 写路径 · {l2_text} · L3 审批（危险模式 {len(DANGEROUS_PATTERNS)}"
            f" · allowlist {allowlist_count} 条；命中即确认）"
        )
    _CONSOLE.print(
        Panel("\n".join(lines), title=f"sigma {__version__}（{mode}模式）", border_style="cyan")
    )
    # 安全边界现状（P3-批次1 起**与代码同源**，不再是"什么都没有"）。
    # 这一段的每一句都要能在代码里指到对应实现，否则它又会变回"文档里的边界"。
    if shadow_dir is None and home_gate and not args.no_checkpoint:
        _CONSOLE.print(
            Panel(
                "  ⚠ 安全提示：工作区是家目录，L2 影子快照已禁用——\n"
                "     全量快照会复制整个 AppData（家目录实测单次扫描 65 秒、每会话数百 MB），\n"
                "     而\"把家目录回滚到几分钟前\"本身就是危险动作。\n"
                "     需要回滚保障：cd 到项目目录，或 --workspace 指向项目目录。\n"
                "  仍未保护：bash 能以你的用户权限执行任意命令、可访问网络与工作区外的路径。",
                border_style="yellow",
            )
        )
    elif shadow_dir is None:
        _CONSOLE.print(
            Panel(
                "  ⚠ 安全提示：影子 checkpoint 已按 --no-checkpoint 关闭——\n"
                "     **本次的破坏性操作不可回滚**（bash 仍能执行任意命令）。",
                border_style="yellow",
            )
        )
    else:
        _CONSOLE.print(
            Panel(
                "  L1 写路径：write/edit/bash 的 cwd 不得越出工作区（越界即拒绝）。\n"
                "  L2 可回滚：每次写操作前自动快照（首个写批次快照即基线）；\n"
                "     必要时用 sigma --rollback 退回。\n"
                "  仍未保护：bash 能以你的用户权限执行任意命令、可访问网络与工作区外的路径——\n"
                "  请只在**受控目录**里使用，且不要让它接触不信任的脚本。",
                title="⚠ 安全边界（不是沙箱）",
                border_style="yellow",
            )
        )
    print()

    try:
        if args.prompt:
            result = asyncio.run(
                _run_once(
                    args,
                    workspace,
                    base_url,
                    model,
                    api_key,
                    registry=registry,
                    system_prompt=system_prompt,
                    tree=binding.tree,
                    session_id=binding.session_id,
                    shadow_git_dir=shadow_dir,
                    skills_root=skills_root,
                    approval=approval_hook,
                    ask=ask_channel,
                    checkpoint_watermark_bytes=checkpoint_watermark_bytes,
                    enable_trace=not args.no_obs,
                    enable_memory=not args.no_memory,
        enable_repo_map=not args.no_repo_map,
                )
            )
            if args.trace:
                _report(result)
            # Q4：任务成没成，退出码都是 0
            return EXIT_OK
        return asyncio.run(
            _run_interactive(
                args,
                workspace,
                base_url,
                model,
                api_key,
                registry=registry,
                system_prompt=system_prompt,
                tree=binding.tree,
                session_id=binding.session_id,
                sessions_root=sessions_root,
                shadow_git_dir=shadow_dir,
                skills_root=skills_root,
                approval=approval_hook,
                ask=ask_channel,
                broker=broker,
                checkpoint_watermark_bytes=checkpoint_watermark_bytes,
                enable_trace=not args.no_obs,
                enable_memory=not args.no_memory,
        enable_repo_map=not args.no_repo_map,
            )
        )
    except KeyboardInterrupt:
        _err("\n已中断。")
        return EXIT_INTERRUPTED
    except Exception as exc:  # harness 级故障：报告并给非 0 退出码
        _err(f"[harness 错误] {type(exc).__name__}: {exc}")
        return EXIT_HARNESS_ERROR


if __name__ == "__main__":
    sys.exit(main())
