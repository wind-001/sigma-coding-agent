"""sigma 的命令行入口。

P1 形态：**一次性模式**（``sigma -p "任务"``）。

REPL 属后续批次——它需要 steering / follow-up 双队列（P3）才有意义，
现在做只能做一个假的：读了输入但没人处理。

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
from sigma.dotenv import ENV_VAR_NAME, USER_CONFIG_DIR, resolve_api_key
from sigma.sdk import default_registry, run_task
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
    parser.add_argument(
        "-p",
        "--prompt",
        help="一次性模式：要执行的任务描述。不传则只打印帮助。",
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
    parser.add_argument("--version", action="version", version=f"sigma {__version__}")
    return parser


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


async def _run(
    args: argparse.Namespace,
    workspace: Path,
    base_url: str,
    model: str,
    api_key: str,
) -> TurnResult:
    provider = OpenAICompatProvider(
        base_url=base_url,
        api_key=api_key,
        provider_name=args.preset or DEFAULT_PRESET,
    )
    try:
        return await run_task(
            args.prompt,
            provider=provider,
            workspace_root=workspace,
            model=model,
            max_rounds=args.max_rounds,
            temperature=args.temperature,
        )
    finally:
        await provider.aclose()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回进程退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.prompt:
        parser.print_help()
        print()
        print('提示：一次性模式用法 —— sigma -p "把 foo.py 里的 off-by-one 修掉"')
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

    print(f"sigma {__version__}")
    print(f"  工作区  {workspace.resolve()}")
    print(f"  模型    {model} @ {base_url}")
    print(f"  工具    {default_registry().names()}")
    print(f"  密钥    已加载（来源：{key_source}）")
    print()
    print("  ⚠ 安全提示：P1 的工具没有任何边界约束（D5 的三层软边界尚未实现）。")
    print("     read 可读任意路径、write/edit 可写任意路径，")
    print("     bash 会以你的用户权限执行**任意命令**，无过滤无沙箱——请只在受控目录内使用。")
    print()

    try:
        result = asyncio.run(_run(args, workspace, base_url, model, api_key))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:  # harness 级故障：报告并给非 0 退出码
        print(f"[harness 错误] {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_HARNESS_ERROR

    _report(result)
    # Q4：任务成没成，退出码都是 0
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
