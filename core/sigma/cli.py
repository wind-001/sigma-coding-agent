"""sigma 的命令行入口。

两种形态（2026-09-21 起）

1. **一次性模式** ``sigma -p "任务"``：跑完退出，退出码见下；
2. **交互模式** 不带 ``-p`` 且 stdin 是 TTY：逐条输入任务、跨轮累积历史。

两种模式**共用**渲染器与会话组装（``sigma.render`` / ``sigma.sdk``），
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
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sigma import __version__
from sigma.dotenv import (
    ENV_VAR_NAME,
    FIRECRAWL_ENV_VAR,
    TAVILY_ENV_VAR,
    USER_CONFIG_DIR,
    resolve_api_key,
    resolve_firecrawl_api_key,
    resolve_tavily_api_key,
)
from sigma.repl import run_repl
from sigma.sdk import (
    InteractiveSession,
    build_system_prompt,
    default_registry,
    run_task,
    scan_skills,
)
from sigma_agent.registry import ToolRegistry
from sigma_agent.checkpoint import ShadowCheckpoint
from sigma.render import TerminalRenderer
from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
)
from sigma_agent.types import TurnResult
from sigma_ai.openai import OpenAICompatProvider
from sigma_ai.registry import builtin_providers
from sigma_session.sessions import (
    list_sessions,
    latest_session_id,
    new_session_id,
)
from sigma_session.store import JsonlStore
from sigma_session.tree import SessionTree

EXIT_OK = 0
EXIT_HARNESS_ERROR = 2
EXIT_INTERRUPTED = 130

# provider 列表**不在这一层**——它属于协议层（`sigma_ai.registry`）。
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
        "--no-checkpoint",
        action="store_true",
        help=(
            "关闭影子 git checkpoint（D5 的 L2：写批次前自动快照、可整体回滚）。"
            "默认开启——关掉之后破坏性操作**不可回滚**，横幅会明说这一点"
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
        action="store_true",
        help=(
            "启用 task 工具：模型可派后台子 agent（独立上下文、最多 3 个并发）"
            "执行子任务，完成后自动回报。子任务会消耗额外 token。"
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
    """打印执行过程与结果。"""
    print("─" * 74)
    for index, message in enumerate(result.messages, start=1):
        print(f"  {_render(message, index)}")
    print("─" * 74)

    print(f"  状态   {result.status}")
    print(f"  轮数   {result.rounds}")
    if result.usage is not None:
        print(
            f"  token  prompt={result.usage.prompt_tokens} "
            f"completion={result.usage.completion_tokens}"
        )

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
        return SessionBinding(new_session_id(), SessionTree(), False, 0)

    session_id = args.session or new_session_id()
    store = JsonlStore(sessions_root, session_id)
    tree = SessionTree.from_store(store) if store.exists() else SessionTree(store=store)
    return SessionBinding(session_id, tree, store.exists(), len(tree))


def shadow_git_dir_for(sessions_root: Path, session_id: str) -> Path:
    """本会话的影子库路径（D5 的 L2）。

    **与会话文件同层扁平放置**：``<sessions>/<id>.shadow.git``——
    这样 ``--continue`` 续上一个会话时，天然续上它的 checkpoint 历史。
    """
    safe = session_id.replace("/", "_").replace("\\", "_")
    return sessions_root / f"{safe}.shadow.git"


def run_rollback(
    args: argparse.Namespace, sessions_root: Path, workspace: Path
) -> int:
    """执行 ``--rollback`` / ``--rollback-to`` / ``--list-checkpoints``。

    这三件事**都不该启动模型**：回滚是人的动作。让模型去调用"回滚"更危险
    （它可以把自己刚搞坏的状态"回滚"成另一个坏状态，而用户看不到）。
    """
    session_id = args.session or latest_session_id(sessions_root)
    if session_id is None:
        print(
            f"[harness 错误] {sessions_root} 里没有会话，无法回滚。",
            file=sys.stderr,
        )
        return EXIT_HARNESS_ERROR

    shadow = ShadowCheckpoint(
        root=shadow_git_dir_for(sessions_root, session_id), workspace=workspace
    )
    if not shadow.available:
        print(f"[harness 错误] 影子库不可用：{shadow.unavailable_reason}", file=sys.stderr)
        return EXIT_HARNESS_ERROR

    # 工作区配对（**这条闸挡住了一次真事故**）：
    # 影子库只对"它创建时那个工作区"有意义。不检查的后果是 `reset --hard`
    # 拿 A 的快照去改 B——把 B 里快照没有的文件全删掉。
    # 这里用**创建时记下的**工作区来执行，用户的 `--workspace` 只用于核对。
    mismatch = shadow.workspace_mismatch()
    if mismatch:
        print(f"[harness 错误] {mismatch}", file=sys.stderr)
        return EXIT_HARNESS_ERROR
    recorded = shadow.recorded_workspace
    assert recorded is not None  # mismatch 为空 ⇒ 一定有记录
    workspace = recorded

    refs = shadow.refs()
    print(f"会话 {session_id} 的快照（新 → 旧）：")
    if not refs:
        print("  （还没有任何快照）")
    for index, info in enumerate(refs):
        print(f"  [{index}] {info.ref[:8]}  {info.label}")

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
            print(f"[harness 错误] 找不到快照 {target!r}。", file=sys.stderr)
            return EXIT_HARNESS_ERROR
        if len(matched) > 1:
            print(
                f"[harness 错误] 前缀 {target!r} 匹配到 {len(matched)} 个快照，"
                "请多给几位。",
                file=sys.stderr,
            )
            return EXIT_HARNESS_ERROR
        target = matched[0]

    report = shadow.restore(target)
    if not report.ok:
        print(f"[harness 错误] 回滚失败：{report.note}", file=sys.stderr)
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
) -> TurnResult:
    """一次性模式。渲染器与交互模式**同一个**（``TerminalRenderer``）。"""
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
            observer=TerminalRenderer(),
            session_id=session_id,
            tree=tree,
            shadow_git_dir=shadow_git_dir,
            skills_root=skills_root,
            enable_sub_agent=args.sub_agent,
        )
    finally:
        await provider.aclose()


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
    shadow_git_dir: Path | None = None,
    skills_root: Path | None = None,
) -> int:
    """交互模式。会话对象跨轮复用，历史才不会丢（门槛 G35）。"""
    provider = _make_provider(args, base_url, api_key)
    session = InteractiveSession(
        provider=provider,
        workspace_root=workspace,
        model=model,
        max_rounds=args.max_rounds,
        temperature=args.temperature,
        registry=registry,
        system_prompt=system_prompt,
        observer=TerminalRenderer(),
        session_id=session_id,
        tree=tree,
        shadow_git_dir=shadow_git_dir,
        skills_root=skills_root,
        enable_sub_agent=args.sub_agent,
    )
    try:
        return await run_repl(session)
    finally:
        await provider.aclose()


def _report_web_tool(
    registry: ToolRegistry, name: str, label: str, source: str, env_var: str
) -> None:
    """打印一个联网工具的状态：是否注册、额度还剩多少、账本有没有异常。

    为什么按"注册表里有没有"判断，而不是按"key 有没有"：
        两者在同一处决定（本函数上方），但**注册表才是事实**——
        将来多一个开关键时，这里不会静默打印出与实际不符的状态。
    """
    if name not in registry.names():
        print(f"  {label}    未启用（未找到 {env_var}）")
        return
    tool: Any = registry.get(name)
    usage = tool.quota.snapshot()
    print(
        f"  {label}    已启用（{source}）"
        f"｜剩余 {usage.remaining}/{usage.cycle_limit} credits"
    )
    # 账本写不下去时必须**说出来**：静默失败的后果是
    # "额度计数每次都从 0 开始"，而用户只会觉得"额度怎么用不完"。
    if usage.last_error:
        print(f"          ⚠ 额度账本异常：{usage.last_error}")


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
        print(f"[harness 错误] 工作区不存在或不是目录：{workspace}", file=sys.stderr)
        return EXIT_HARNESS_ERROR

    sessions_root = (
        Path(args.sessions_dir).expanduser()
        if args.sessions_dir
        else DEFAULT_SESSIONS_DIR
    )

    # 回滚三件事（--rollback / --rollback-to / --list-checkpoints）**不启动模型**：
    # 它们是人的动作。所以它们**必须排在 API key 检查之前**——
    # 否则"模型密钥失效 / 没配 key"会顺带把"把工作区回滚回去"也堵死，
    # 而那恰恰是最需要回滚的时刻。第一版就把它排在了后面，冒烟时当场发现。
    if args.rollback or args.rollback_to is not None or args.list_checkpoints:
        if args.no_checkpoint:
            print(
                "[harness 错误] --no-checkpoint 与回滚开关同时给出："
                "关掉 checkpoint 就没有快照可回滚。",
                file=sys.stderr,
            )
            return EXIT_HARNESS_ERROR
        return run_rollback(args, sessions_root, workspace)
    if not api_key:
        print("[harness 错误] 缺少 API key。三种设置方式，任选一种：", file=sys.stderr)
        print(
            f"  1) 写进 {USER_CONFIG_DIR / '.env'}（推荐，在项目目录之外，不会被误提交）",
            file=sys.stderr,
        )
        print(f"     内容一行即可：{ENV_VAR_NAME}=sk-xxx", file=sys.stderr)
        print(f"  2) 临时用：export {ENV_VAR_NAME}=sk-xxx", file=sys.stderr)
        print("  3) 只用一次：--api-key sk-xxx", file=sys.stderr)
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
    )

    binding = resolve_session(args, sessions_root)

    shadow_dir = (
        None
        if args.no_checkpoint
        else shadow_git_dir_for(sessions_root, binding.session_id)
    )

    mode = "一次性" if args.prompt else "交互"
    print(f"sigma {__version__}（{mode}模式）")
    print(f"  工作区  {workspace.resolve()}")
    print(f"  模型    {model} @ {base_url}")
    print(f"  工具    {registry.names()}")
    print(f"  密钥    已加载（来源：{key_source}）")
    if args.no_web_search:
        print("  联网    已按 --no-web-search 全部禁用（web_search / web_fetch）")
    else:
        _report_web_tool(registry, "web_search", "搜索", tavily_source, TAVILY_ENV_VAR)
        _report_web_tool(registry, "web_fetch", "精读", firecrawl_source, FIRECRAWL_ENV_VAR)
    # 技能：**数量 + 问题都要打**。
    # 问题清单尤其重要——一个坏技能文件如果只是被静默跳过，
    # 症状是"我加了技能它怎么不用"，与手工清单漂移是同一种失败。
    if skill_scan.skills:
        names = "、".join(s.name for s in skill_scan.skills)
        print(f"  技能    {len(skill_scan.skills)} 个：{names}")
    for problem in skill_scan.problems:
        print(f"          ⚠ {problem}")
    if binding.resumed:
        print(
            f"  会话    已续接 {binding.session_id}"
            f"（载入 {binding.previous_messages} 条历史）"
        )
    elif args.resume:
        # `--continue` 却没东西可续：**必须说出来**。静默开一个新的，
        # 用户会以为自己在续上下文，而模型其实"忘了"之前的一切——
        # 那看起来只是"这次答得不好"。
        print(f"  会话    没有可续的会话（{sessions_root} 是空的），已新建 {binding.session_id}")
    elif args.session:
        print(f"  会话    新建 {binding.session_id}")
    else:
        print(f"  会话    {binding.session_id}（新）")
    print()
    # 安全边界现状（P3-批次1 起**与代码同源**，不再是"什么都没有"）。
    # 这一段的每一句都要能在代码里指到对应实现，否则它又会变回"文档里的边界"。
    if shadow_dir is None:
        print("  ⚠ 安全提示：影子 checkpoint 已按 --no-checkpoint 关闭——")
        print("     **本次的破坏性操作不可回滚**（bash 仍能执行任意命令）。")
    else:
        print("  ⚠ 安全边界（不是沙箱）：")
        print("     L1 写路径：write/edit/bash 的 cwd 不得越出工作区（越界即拒绝）。")
        print("     L2 可回滚：每次写操作前自动快照；必要时用 sigma --rollback 退回。")
        print("     仍未保护：bash 能以你的用户权限执行任意命令、可访问网络与工作区外的路径——")
        print("     请只在**受控目录**里使用，且不要让它接触不信任的脚本。")
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
                shadow_git_dir=shadow_dir,
                skills_root=skills_root,
            )
        )
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # harness 级故障：报告并给非 0 退出码
        print(f"[harness 错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_HARNESS_ERROR


if __name__ == "__main__":
    sys.exit(main())
