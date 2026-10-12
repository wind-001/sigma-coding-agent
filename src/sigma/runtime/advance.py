"""内置推进判定器（推进机制批次 2/3）——第三方扩展面的第一批住客。

形状复刻 L3 审批的内置实现（``CliApprovalGate`` 住 ``security/approval.py``）：
接口在 ``hooks/base.py``（AdvanceHook），询问在 loop 的收敛点，本模块是
**可被整体替换的内置实现**——换判据（比如换成"跑 lint"）另写一个
AdvanceHook 注册进去即可，loop 一行不改。

**为什么不检测"工作区有 diff"再跑**（LoopControl 详规 §3 的原设计是
"有 diff 且声明了命令 → 跑"）：diff 检测在非 git 工作区上不可靠
（评测的临时 workspace、用户随手建的目录都没有 git），而"声明了命令"
本身就是调用方的显式意图——**声明即跑**，成本是一次子进程（ax 任务实测
判定命令 0.3–1s）。把判据从"改没改"简化成"声明没有"，消掉一整类边界。

    与既有 37 条刹车的优先级（推进机制详规 §4）：推进钩子问在硬熔断/
    软检测/信箱兜底**之后**——被停的任务不会因它续命；验收等待走
    ``guard.pause()/resume()``（loop 侧接线），不计 turn 墙钟。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sigma.events.lifecycle import AdvanceDecision, AdvanceFacts
from sigma.hooks.base import AdvanceHook

#: 验收命令输出的注入尾部保留多长。与工具截断同款思路：尾部是 pytest 的
#: 失败摘要所在，头部是进度噪音。1500 字符 ≈ 一次注入的合理体量。
_TAIL_CHARS = 1500


class VerificationGate(AdvanceHook):
    """验证收尾（头号推进机制，治 ax107 型「completed 但判定挂」）：

    模型宣布完成时跑任务的 ``verification_cmd``（评测侧
    ``TaskSpec.judge.command`` 就是现成的一例；CLI ``--verification-cmd``
    是产品壳入口）。exit 0 → 不干预（让它 completed）；不过 → 注入失败
    输出并继续一轮；连续 ``max_rounds`` 轮仍不过 → ``force_stop``
    （优雅 stopped + reason，**不撒谎 completed**）。

    自带上上限（防死循环）：计数器在钩子实例里，**loop 不持有推进状态**
    （机制与策略分离：loop 只问决定，pi 笔记 §14.1 第 1 条）。
    """

    name = "verification-gate"

    def __init__(
        self,
        workspace_root: Path,
        verification_cmd: str,
        *,
        max_rounds: int = 2,
        timeout_s: float = 300.0,
    ) -> None:
        self._workspace_root = Path(workspace_root)
        self._cmd = verification_cmd.strip()
        self._max_rounds = max_rounds
        self._timeout_s = timeout_s
        self._attempts = 0

    async def advance(self, facts: AdvanceFacts) -> AdvanceDecision:
        if not self._cmd:
            return AdvanceDecision()
        self._attempts += 1
        passed, output = await self._run_command()
        if passed:
            # 验收通过：不干预。**注意不是 continue**——目标是"过了就收"，
            # 继续只会烧轮数（工具结果不会变）。
            return AdvanceDecision()
        tail = output[-_TAIL_CHARS:].strip()
        # 失败性质的一句话摘要（超时/没跑起来/测试挂）——进 force_stop 的
        # reason： stopped 时 inject=None，收尾行只有 reason 可看，
        # 不带性质就只剩"未通过"三个字，人没法判断下一步。
        brief = (tail.splitlines() or [output.strip()])[0][:100]
        if self._attempts < self._max_rounds:
            return AdvanceDecision(
                continue_round=True,
                inject=(
                    f"[验证收尾] 验收命令未通过（第 {self._attempts} 次，"
                    f"最多 {self._max_rounds} 次）。命令：{self._cmd}\n"
                    f"输出尾部：\n{tail}\n"
                    "修它；若失败与任务无关，用一两句说明理由后再收尾。"
                ),
                reason=f"验收命令第 {self._attempts} 次未通过：{brief}",
            )
        return AdvanceDecision(
            force_stop=True,
            reason=(
                f"验收命令连续 {self._attempts} 轮未通过（{self._cmd}）："
                f"{brief}——不撒谎 completed"
            ),
        )

    async def _run_command(self) -> tuple[bool, str]:
        """跑验收命令，返回 ``(是否通过, 输出全文)``。

        超时是失败的一种（不是异常）：判定跑的是测试，不该需要很久；
        卡住时**报失败并附上超时事实**，而不是无限等（judges.py 同判据）。
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                self._cmd,
                cwd=self._workspace_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            return False, f"验收命令没跑起来：{type(exc).__name__}: {exc}"
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout_s
            )
        except (TimeoutError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()
            return False, f"验收命令超时（>{self._timeout_s:.0f}s）"
        text = _decode_output(stdout or b"")
        return proc.returncode == 0, text


def _decode_output(raw: bytes) -> str:
    """解码子进程输出：utf-8 优先，GBK 回落。

    **不能不试就解 GBK**：pytest/多数工具输出 utf-8；而 Windows 中文版
    cmd 自己的消息（"'x' 不是内部或外部命令"）是 GBK——硬解 utf-8 会把
    它们变成 ``\\ue8ec`` 乱码，而"为什么挂"恰恰全在这些消息里。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")
