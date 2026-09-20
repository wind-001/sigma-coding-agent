"""``bash`` 工具：执行 shell 命令。**P1 风险最高的工具。**

无保护（详规 R1，对策乙）
    D5 的三层软边界在 P1 一层都没落地，本工具会以当前用户权限执行
    **任意**命令，没有任何过滤、没有沙箱。防线只有两条：
    CLI 启动时的安全提示，与评测任务全部在临时目录里跑。
    命令黑名单是**名义防护**（挡不住绕过，却制造虚假安全感），
    按"名义门槛比没有门槛更坏"的纪律，留给 P3 的钩子体系。

超时**必须**（详规 3.5）
    不设超时的话，一条挂起的命令（``tail -f`` / 缺输入的 ``cat``）会让
    loop 永久卡死——这不是体验问题，是正确性问题。
    默认 60 s 是拍脑袋值，待批次 5 观测真实分布后校准（详规 T6）。

为什么只认 ``bash``、找不到就报错，而不是回退到 cmd.exe / sh
    本工具的语义是"bash 语法"。Windows 上若静默回退到 cmd.exe，
    ``ls`` / ``export`` / ``&&`` 的行为全部悄悄改变，
    模型会拿着语义完全不同的报错继续纠错——**错着走下去比停下来更糟**。
    找不到 bash 就把事实摆出来（宁可不干活，不要干错活）。

超时时拿不回部分输出的原因
    ``wait_for`` 取消 ``communicate()`` 后，再次调用它的行为没有保证。
    P1 选择"kill + 回收 + 明确告知超时"，不冒险读半截输出。
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._paths import resolve_path
from sigma_tools.truncate import truncate_output

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 600


class BashParams(BaseModel):
    """``bash`` 的参数。"""

    command: str = Field(min_length=1, description="要执行的 bash 命令。")
    timeout_s: int = Field(
        default=DEFAULT_TIMEOUT_S,
        ge=1,
        le=MAX_TIMEOUT_S,
        description=f"超时秒数，默认 {DEFAULT_TIMEOUT_S}，上限 {MAX_TIMEOUT_S}。",
    )
    cwd: str | None = Field(
        default=None,
        description="命令的工作目录。相对路径基于工作区根目录；不传则用工作区根目录。",
    )


class BashTool(BaseTool):
    """执行 bash 命令。属写工具（可能改任何东西），批次中**严格顺序执行**。"""

    name = "bash"
    description = (
        "在 bash 中执行一条命令（如 ls、mkdir -p、运行测试）。"
        "命令没有任何过滤，输出超过 8 KB 会被截断——请用 head/grep 缩小输出范围。"
    )
    read_only = False

    @property
    def params(self) -> type[BaseModel]:
        return BashParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行命令。失败（非零退出 / 超时 / 环境缺失）一律 ``is_error=True``。"""
        params = cast(BashParams, args)

        if params.cwd is not None:
            cwd = resolve_path(ctx, params.cwd)
            if not cwd.is_dir():
                return ToolResult(
                    content=[TextBlock(text=f"工作目录不存在或不是目录：{cwd}")],
                    details={"cwd": str(cwd)},
                    is_error=True,
                )
        else:
            cwd = ctx.workspace_root

        bash_path = shutil.which("bash")
        if bash_path is None:
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            "PATH 中找不到 bash，本工具执行的是 bash 语法的命令。"
                            "Windows 请安装 Git for Windows 并把 bash 加入 PATH。"
                        )
                    )
                ],
                details={"bash_path": None},
                is_error=True,
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                bash_path,
                "-c",
                params.command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return ToolResult(
                content=[TextBlock(text=f"启动 bash 失败：{exc}")],
                details={"bash_path": bash_path},
                is_error=True,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=params.timeout_s
            )
        except asyncio.TimeoutError:
            # 必须回收子进程，否则留下僵尸；部分输出拿不回（见模块 docstring）
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:  # pragma: no cover - 平台差异兜底
                pass
            return ToolResult(
                content=[
                    TextBlock(
                        text=(
                            f"命令超时（超过 {params.timeout_s} s 未结束），已强制终止。\n"
                            "命令可能在等待输入或永远不会结束——"
                            "请检查后换一种不会挂起的写法。"
                        )
                    )
                ],
                details={"command": params.command, "timeout_s": params.timeout_s, "timed_out": True},
                is_error=True,
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        sections: list[str] = []
        if stdout_text:
            sections.append(f"[stdout]\n{stdout_text.rstrip()}")
        if stderr_text:
            sections.append(f"[stderr]\n{stderr_text.rstrip()}")
        if not sections:
            sections.append("(无输出)")

        details: dict[str, Any] = {
            "command": params.command,
            "exit_code": exit_code,
            "timed_out": False,
            "cwd": str(cwd),
            "truncated": False,
        }

        if exit_code != 0:
            # 非零退出码**必须**让模型看到：它是"命令失败了"的判据，
            # 尤其当 stderr 为空时，退出码是唯一的失败信号（如 grep / diff）
            text = f"命令失败，退出码 {exit_code}。\n" + "\n".join(sections)
            result = truncate_output(text)
            details["truncated"] = result.truncated
            return ToolResult(
                content=[TextBlock(text=result.text)], details=details, is_error=True
            )

        result = truncate_output("\n".join(sections))
        details["truncated"] = result.truncated
        return ToolResult(content=[TextBlock(text=result.text)], details=details)
