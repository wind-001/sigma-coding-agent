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
import os
import sys
from pathlib import Path

from sigma import __version__
from sigma.dotenv import (
    ENV_VAR_NAME,
    TAVILY_ENV_VAR,
    USER_CONFIG_DIR,
    resolve_api_key,
    resolve_tavily_api_key,
)
from sigma.repl import run_repl
from sigma.sdk import (
    InteractiveSession,
    build_system_prompt,
    default_registry,
    run_task,
)
from sigma_agent.registry import ToolRegistry
from sigma_tools.web_search import WebSearchTool
from sigma.render import TerminalRenderer
from sigma_agent.agent_messages import (
    AgentMessage,
    LlmMessageWrapper,
    ToolResultAgentMessage,
)
from sigma_agent.types import TurnResult
from sigma_ai.openai import OpenAICompatProvider
from sigma_ai.registry import builtin_providers

EXIT_OK = 0
EXIT_HARNESS_ERROR = 2
EXIT_INTERRUPTED = 130

# provider 列表**不在这一层**——它属于协议层（`sigma_ai.registry`）。
# 2026-09-20 重构子项 E：此前这份列表硬编码在这里，那是分层错误——
# 换一个入口（直接调 sdk.run_task、评测运行器）就得再抄一份。
# 现在这里只留"默认用哪个"这一个**产品决策**。
DEFAULT_PRESET = "deepseek"


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
        help="工作区根目录，默认当前目录。**这不是安全边界**（见文件顶部说明）",
    )
    parser.add_argument("--max-rounds", type=int, default=20, help="最大轮数，默认 20")
    parser.add_argument("--temperature", type=float, default=0.0, help="采样温度，默认 0")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="流式输出之外，结束再打印一遍完整消息序列（调试 / 评测用）",
    )
    parser.add_argument(
        "--no-web-search",
        action="store_true",
        help=(
            "禁用联网搜索工具 web_search。"
            "默认：解析到 TAVILY_API_KEY 就启用（额度 1000 credits/月，超额自动禁用）"
        ),
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
        try:
            # reconfigure 不在 typing.TextIO 的协议里（它是 TextIOWrapper 的方法）
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
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


async def _run_once(
    args: argparse.Namespace,
    workspace: Path,
    base_url: str,
    model: str,
    api_key: str,
    *,
    registry: ToolRegistry,
    system_prompt: str,
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
    )
    try:
        return await run_repl(session)
    finally:
        await provider.aclose()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    _configure_console()  # 必须在任何 print 之前：中文与 ⏺ 靠它才不乱码

    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # W11：既没有 -p 又没有 -i，且 stdin 不是真控制台 → **绝不能进 REPL**。
    # 否则脚本 / CI / 管道里调用 sigma 会挂住等输入，
    # 症状是"卡住"而不是报错。
    if not args.prompt and not args.interactive and not stdin_is_interactive():
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

    # 联网搜索是可选工具：**有 key 才注册**。工具 schema 进常驻区，
    # 没配 key 的机器不该为它付 token（批次 7 详规 Q1）。
    # 提示词与注册表同源：关掉时不出现"有个工具叫 web_search"这句话。
    tavily_key, tavily_source = resolve_tavily_api_key()
    web_search_on = not args.no_web_search and bool(tavily_key)
    registry = default_registry(web_search=web_search_on, tavily_api_key=tavily_key)
    system_prompt = build_system_prompt(web_search=web_search_on)

    mode = "一次性" if args.prompt else "交互"
    print(f"sigma {__version__}（{mode}模式）")
    print(f"  工作区  {workspace.resolve()}")
    print(f"  模型    {model} @ {base_url}")
    print(f"  工具    {registry.names()}")
    print(f"  密钥    已加载（来源：{key_source}）")
    if web_search_on:
        tool = registry.get("web_search")
        if isinstance(tool, WebSearchTool):
            usage = tool.quota.snapshot()
            print(
                f"  联网    已启用（{tavily_source}）"
                f"｜剩余 {usage.remaining}/{usage.cycle_limit} credits"
            )
            # 账本写不下去时必须**说出来**：静默失败的后果是
            # "额度计数每次都从 0 开始"，而用户只会觉得"额度怎么用不完"。
            if usage.last_error:
                print(f"          ⚠ 额度账本异常：{usage.last_error}")
    elif args.no_web_search:
        print("  联网    已按 --no-web-search 禁用")
    else:
        print(f"  联网    未启用（未找到 {TAVILY_ENV_VAR}）")
    print()
    print("  ⚠ 安全提示：P1 的工具没有任何边界约束（D5 的三层软边界尚未实现）。")
    print("     read 可读任意路径、write/edit 可写任意路径，")
    print("     bash 会以你的用户权限执行**任意命令**，无过滤无沙箱——请只在受控目录内使用。")
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
