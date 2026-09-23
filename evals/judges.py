"""任务集的判定器。

按 `P2-6-评测任务集-详规.md` §3.1 的接口：``Judge`` 是抽象基类，
``JudgeVerdict`` 是三字段的结果。**只实现已被任务用到的判定器**——
本项目明确反对"骨架到位、实现没接"，所以这里没有 C 类的空壳。

判定与执行分离的理由：**判定要能脱离 agent 单独跑一遍**。
`scripts/check_dataset.py` 会拿同一个判定器去验"这条任务的初始状态确实失败"，
而那时根本没有 agent 参与。判定逻辑写进运行器里就做不到这件事。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from task_types import TaskResult

#: 判定命令的超时。判定跑的是测试，不该需要很久；
#: 卡住时**报失败并附上超时事实**，而不是无限等。
JUDGE_TIMEOUT_SECONDS = 300


class JudgeVerdict(BaseModel):
    """一次判定的结论。

    ``evidence`` **必填**（详规 §3.1）：报告里每条失败都要能回答"为什么算失败"。
    只留一个 ``passed=False`` 的话，失败会退化成"它就是没过"——等于没有信息，
    而"为什么没过"恰恰是这份报告唯一值得看的东西。
    """

    passed: bool
    reason: str
    evidence: dict[str, object]


class Judge(ABC):
    """判定器的接口。

    ``result`` 是 agent 跑完那一条任务后采集到的指标（见 ``evals/types.py``）。
    多数判定器不看它——但签名里保留，因为 C 类的判定要参考 agent 的动作序列。
    """

    @abstractmethod
    def judge(self, workspace: Path, result: TaskResult) -> JudgeVerdict: ...


class PytestJudge(Judge):
    """跑指定命令，``exit 0`` 即通过。A / B 类用它。

    **防篡改是它的一部分，不是可选项。**

        如果测试文件在 workspace 里，模型可以把它改松（甚至改成 ``pass``）
        来让 exit 0。那样测到的是"模型愿不愿意改测试"，而不是"它会不会修 bug"
        —— 前者与 harness 的有效性无关。

        所以判定前先用原始副本覆盖回去。这同时让"测试文件是否被改动"
        成为一个可记录的证据：        **改了但照样通过**，说明它自己找到了正确修法；
        **改了才通过**，说明判定闸起作用了。

    ⚠️ **``cwd`` 与 ``restore_tests`` 的目标都相对「工作区」，不是相对「任务目录」。**

        这条语义是踩出来的：第一版把 ``cwd`` 当成"相对任务目录"，而运行器
        传进来的 ``workspace`` 已经是工作区本身 → 拼成
        ``<临时目录>/workspace/workspace`` → ``WinError 267 目录名称无效``。
        **同一个词（workspace）在两处指了不同的东西。**

        而它的姊妹版本更隐蔽：第一版让 ``restore_tests`` 的目标指向**数据集本体**，
        于是覆盖的是数据集，而 agent 改过的那份副本**原封不动**
        —— 防篡改成了摆设，且是**静默**的：覆盖"成功"了，只是盖错了地方。
        这类"动作成功、作用对象错"的缺陷，只能靠**明确写出两边分别是谁**来防。
    """

    def __init__(
        self,
        command: str,
        *,
        cwd: str = ".",
        restore_tests: tuple[Path, str] | None = None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._restore = restore_tests

    def _argv(self) -> list[str]:
        """命令字符串 → argv，并**把前导的 python 换成当前解释器**。

        任务里的判定命令写的是 ``python -m pytest tests/ -q``，而评测机上
        PATH 里的 ``python`` **不一定是跑这个评测的那一个**——本机 PATH 就指向
        anaconda，而项目纪律要求一律用 ``.venv`` 的那个。

        不换的后果很隐蔽：``subprocess`` 抛 ``OSError: 找不到文件``，而判定器
        把它归到"未通过"里 —— 于是**"命令跑不起来"看起来像"任务失败"**。
        （这一条是本次实测踩到的：``--check-only`` 报了"初始状态确实失败 ✓"，
        而 evidence 里 ``exit_code`` 是 ``None``。）

        只换**前导**那一个：命令中间出现的 ``python``（比如
        ``bash -c "python x.py"``）属于任务自己的语义，不该动。
        """
        parts = self._command.split()
        if parts and Path(parts[0]).stem in {"python", "python3"}:
            parts[0] = sys.executable
        return parts

    def judge(self, workspace: Path, result: TaskResult) -> JudgeVerdict:
        evidence: dict[str, object] = {"command": self._command}
        run_dir = workspace / self._cwd

        # **先确认命令的工作目录存在**，再去跑。
        # 不检查的话 subprocess 会抛 WinError 267，而那会被归到"未通过"里
        # ——"跑不起来"就伪装成了"任务失败"。判定的第一件事是**确认它真的在判**。
        if not run_dir.is_dir():
            evidence["invalid"] = f"命令的工作目录不存在：{run_dir}"
            return JudgeVerdict(
                passed=False,
                reason=f"判定无效：工作目录不存在（cwd={self._cwd!r}）",
                evidence=evidence,
            )

        if self._restore is not None:
            source, target_rel = self._restore
            target = workspace / target_rel
            evidence["tests_restored_from"] = str(source)
            evidence["tests_restored_to"] = target_rel
            # 先看有没有被动过，再覆盖——"改过"这件事本身要留下记录。
            evidence["tests_were_modified"] = _differs(source, target)
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(source, target)

        try:
            proc = subprocess.run(
                self._argv(),
                cwd=run_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=JUDGE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            evidence["timeout_seconds"] = JUDGE_TIMEOUT_SECONDS
            return JudgeVerdict(
                passed=False,
                reason=f"判定命令超时（>{JUDGE_TIMEOUT_SECONDS}s）",
                evidence=evidence,
            )
        except OSError as exc:
            evidence["os_error"] = str(exc)
            return JudgeVerdict(
                passed=False, reason="判定命令无法启动", evidence=evidence
            )

        evidence["exit_code"] = proc.returncode
        evidence["summary"] = _tail(proc.stdout)
        if proc.stderr.strip():
            evidence["stderr_tail"] = _tail(proc.stderr)

        if proc.returncode == 0:
            return JudgeVerdict(passed=True, reason="判定命令 exit 0", evidence=evidence)
        return JudgeVerdict(
            passed=False,
            reason=f"判定命令 exit {proc.returncode}",
            evidence=evidence,
        )


def _tail(text: str, lines: int = 12) -> str:
    """取输出的末尾若干行。

    取末尾而不是开头：pytest 的失败清单和汇总都在末尾，
    而开头是收集阶段的噪声。
    """
    stripped = [line for line in text.splitlines() if line.strip()]
    return "\n".join(stripped[-lines:])


def _differs(source: Path, target: Path) -> bool:
    """``target`` 与 ``source`` 是否有内容差异（逐文件比对，不依赖 git）。"""
    if not target.exists():
        return True
    source_files = {p.relative_to(source) for p in source.rglob("*") if p.is_file()}
    target_files = {p.relative_to(target) for p in target.rglob("*") if p.is_file()}
    if source_files != target_files:
        return True
    return any(
        (source / rel).read_bytes() != (target / rel).read_bytes() for rel in source_files
    )
