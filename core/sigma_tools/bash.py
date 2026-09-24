"""``bash`` 工具：执行 shell 命令。**本工具是所有工具里风险最高的一层。**

它能做什么、边界在哪（P3-批次1 更新）
    命令**没有任何过滤**：`python -c`、base64、先写脚本再执行都能绕过字符串匹配，
    所以本项目**不做命令黑名单**——那是名义防护（挡不住绕过，却制造虚假安全感）。
    这一点与 pi 笔记 9.3 的立场一致：钩子只能减少不能消除。

    现在真正起作用的边界有两层（D5）：

    - **L1**：``cwd`` 参数必须落在工作区内（越界直接拒绝）；
    - **L2**：每个写批次前自动做影子 git checkpoint，破坏性操作**可整体回滚**。
      这才是 bash 的兜底——不是拦截，是"拦不住也能退回去"。

    **它仍然不是沙箱**：命令可以 `rm -rf` 工作区外的目录、可以把数据发到网上、
    可以改环境变量。README 与 architecture 6.3 明写了这份暴露面，不藏。

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

为什么超时后要杀**整棵进程树**而不是 ``proc.kill()``
    Windows 上 ``proc.kill()`` 是 TerminateProcess，只杀 bash 本身；
    MSYS2 bash 会把命令（如 ``sleep``）作为**子进程**拉起，孙进程存活时
    会继续持有 stdout/stderr 管道句柄——实测（2026-09，本机复现）
    ``await proc.wait()`` 会一直阻塞到孙进程**自然退出**才返回
    （``sleep 30`` + 1 s 超时 → wait 卡了 28 s）。
    所以 Windows 用 ``taskkill /T /F /PID`` 杀树；POSIX 用
    ``start_new_session=True`` + ``os.killpg`` 杀进程组。
    即使杀树失败，回收等待也有上限（``KILL_GRACE_S``）：工具**必须返回**，
    不能让"超时处理"自己变成新的挂起点。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from typing import Any, cast

from pydantic import BaseModel, Field

from sigma_agent.base import BaseTool
from sigma_agent.types import ToolContext, ToolResult
from sigma_ai.messages import TextBlock
from sigma_tools._paths import PathEscapesWorkspace, resolve_write_path
from sigma_tools.truncate import truncate_output

DEFAULT_TIMEOUT_S = 60
#: 超时上限。600 → 1800（P4-批次2，D-A4）：真实模型跑一条评测任务
#: （多轮对话 + 判定）超过 10 分钟是常态，600 s 会让"一条任务一次 bash 调用"
#: 的评测编排必死。上限**保留但有界放大**——不设上限会让挂起的命令把
#: loop 永久卡死（见模块 docstring「超时必须」）。
MAX_TIMEOUT_S = 1800
#: 杀树后回收进程的等待上限。杀进程是异步生效的，若 ``wait()`` 无上限，
#: 万一进程没能立刻退出，超时处理会自己变成新的挂起点（Windows 实测：
#: 孙进程持有管道句柄时 ``wait()`` 会被拖到孙进程退出，见模块 docstring）。
KILL_GRACE_S = 5


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉 ``proc`` 及其全部子孙进程，然后补一刀杀 ``proc`` 本身兜底。

    跨平台差异：Windows 的 TerminateProcess 只杀直接目标进程，杀树必须用
    ``taskkill /T /F``；POSIX 则让 bash 成为新会话首进程后对整组 ``killpg``。
    所有杀进程调用都吞异常：目标可能刚好自己退出了，这不该让工具报错。
    """
    if proc.returncode is not None:
        return  # 已经退出并回收，无需再杀
    if sys.platform == "win32":
        try:
            # taskkill 实测约 0.1~1 s，subprocess.run 会阻塞事件循环，
            # 丢进线程池避免卡住 loop 上的其他协程。
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        except OSError:  # pragma: no cover - taskkill 不可用的极端环境
            pass
    else:
        try:
            # 依赖创建时 start_new_session=True：bash 是会话首进程，
            # 其 pid 即进程组 id，killpg 一次带走全部子孙。
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # 进程刚好退出 / 无权限，交给下面的直接杀兜底
    # 无论杀树是否成功，都补一刀直接杀 bash 本身：杀树调用可能因为
    # 进程刚好退出而"没杀到"，直接 kill 是幂等的兜底。
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


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
        "**cwd 必须在工作区内**；命令本身没有过滤（这是刻意的，见工具文档），"
        "但每次写操作前的 checkpoint 让破坏性改动可以整体回滚。"
        "输出超过 8 KB 会被截断——请用 head/grep 缩小输出范围。"
    )
    read_only = False

    @property
    def params(self) -> type[BaseModel]:
        return BashParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行命令。失败（非零退出 / 超时 / 环境缺失）一律 ``is_error=True``。"""
        params = cast(BashParams, args)

        if params.cwd is not None:
            # cwd 走 L1 约束：命令会以它为基础改文件，所以它与写路径同级看。
            try:
                cwd = resolve_write_path(ctx, params.cwd)
            except PathEscapesWorkspace as exc:
                return ToolResult(
                    content=[TextBlock(text=str(exc))],
                    details={"cwd": params.cwd, "escaped_workspace": True},
                    is_error=True,
                )
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

        # 让 bash 与本进程隔离开，超时才能"一次带走"它的全部子孙：
        # - Windows：独立进程组，bash 树不接收针对本进程组的 CTRL 事件；
        # - POSIX：新会话，bash 成为会话首进程，killpg 才杀得到整棵树。
        proc_kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            proc_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            proc_kwargs["start_new_session"] = True

        try:
            proc = await asyncio.create_subprocess_exec(
                bash_path,
                "-c",
                params.command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **proc_kwargs,
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
            # 必须杀**整棵树**并回收子进程，否则留下僵尸（Windows 上孙进程
            # 持有管道句柄会把 wait 拖到天荒地老，见模块 docstring）。
            # 部分输出拿不回（见模块 docstring「超时时拿不回部分输出的原因」）
            await _kill_process_tree(proc)
            try:
                # 回收等待必须有上限：杀进程异步生效，无上限就可能挂起。
                await asyncio.wait_for(proc.wait(), timeout=KILL_GRACE_S)
            except asyncio.TimeoutError:
                # 正常不会走到（taskkill /F 实测 <1 s 生效）；真走到也得返回，
                # 工具自己不能变成新的挂起点。
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
