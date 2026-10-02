"""循环护栏（LoopControl 详规批次 1，2026-10-02 评审通过后实施）。

``max_rounds=None``（模型收敛即出）之后失控防护的承接层。分层与优先级：

- **L1 硬熔断**（确定性，每轮 ``end_round`` 必查，命中即停，顺序即优先级）：
  连续错误 > 同签名重复（**仅适用失败调用**——成功调用的原地踏步归 L2 软检测，
  避免误伤合法轮询）> token 预算 > 墙钟 > 绝对轮数保险丝。
- **L2 软检测**（启发式）：S1 结果指纹全同窗口 / S2 动作序列周期。
  先 nudge（steering 注入，给模型自纠机会）后停止。
- L3 验证收尾 / L4 裁判模型：**不在本类**（批次 3 / 明确不做）。

判定与执行分离：本类**只判定**，返回 :class:`Verdict`；loop 负责执行
（stopped 收尾、nudge 注入）。护栏不 import sigma 任何模块——纯函数可单测。

计数口径（评审必修 1 的修正结果）：
``observe_call`` 先计数（含本轮），``end_round`` 判定用 ``>= limit`` **不带 +1**
——"第 3 次失败停"就是计数到达 3 时停，第 2 次必须 continue。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable

__all__ = [
    "CallFacts",
    "GuardConfig",
    "Verdict",
    "LoopGuard",
    "normalize_args",
    "result_fingerprint",
    "NUDGE_TEXT",
]


@dataclass(frozen=True)
class GuardConfig:
    """护栏配置。硬熔断字段全部可调；``None`` = 关闭该闸。

    默认值即批次 2 将下发给工作台/CLI 的口径（批次 1 全调用方统一默认）。
    """

    # ---- 硬熔断（确定性）----
    max_rounds: int | None = 200  # 绝对保险丝:策略在 max_rounds,这里是安全网
    wall_clock_s: float | None = 30 * 60  # 不含审批等待时间(pause/resume)
    token_budget: int | None = 2_000_000  # 累计 prompt+completion
    same_call_limit: int | None = 3  # 同 (tool, args) **失败**调用次数上限
    consecutive_errors_limit: int | None = 5  # 连续工具失败(不同签名也计)
    # ---- 软检测（启发式）----
    soft_enabled: bool = True  # False = 只跑硬闸（子 agent 用：预算语义由
    # max_rounds 唯一裁决，软检测的 nudge/stop 会改变三档预算的消耗口径——
    # 评审拍板"子 agent 不变"）
    stall_window: int = 6  # 观察窗口(工具轮数)
    stall_min_actions: int = 3  # 窗口内至少这么多工具轮才判停滞
    nudge_max: int = 2  # nudge 用尽仍停滞 → stop

    @classmethod
    def disabled(cls) -> "GuardConfig":
        """全关配置：护栏只记录判定（verdicts），不改变任何行为。

        用途：子 agent——它的预算语义由 ``max_rounds`` 唯一裁决（P4 三档
        重派闭环依赖 cap），任何提前打断都会改变"low=10 轮"的口径
        （评审拍板"子 agent 不变"）。
        """
        return cls(
            soft_enabled=False,
            max_rounds=None,
            wall_clock_s=None,
            token_budget=None,
            same_call_limit=None,
            consecutive_errors_limit=None,
        )


@dataclass(frozen=True)
class CallFacts:
    """一次工具调用的最小事实（由 loop 在工具结果落定时回填）。

    ``result_signature`` 是结果指纹——**成功调用也回填**：S1"成功但原地
    踏步"（grep 空结果原样再搜）靠它检测（评审必修 3 的口径划分）。
    """

    name: str
    args_signature: str
    result_signature: str | None
    ok: bool


@dataclass(frozen=True)
class Verdict:
    """护栏判定。``action`` 三值：continue / nudge / stop。"""

    action: str  # "continue" | "nudge" | "stop"
    reason: str  # 进 TurnResult.reason → statusDetail → 前端收尾行
    layer: str  # "hard" | "soft" | "none"
    nudge_text: str | None = None


NUDGE_TEXT = (
    "[停滞提醒] 检测到重复动作或相同结果。先调 todo(action=\"list\") 重新审视"
    "清单，换一种方法或缩小问题范围；若任务实际无法推进，直接汇总已完成的进度、"
    "当前卡点和剩余待办，简短收尾。不要重复刚才的调用。"
)

_CONTINUE = Verdict(action="continue", reason="", layer="none")


def normalize_args(args: dict[str, Any]) -> str:
    """规范化参数指纹：键排序 + 值截断，语义相同/字节不同的调用落进同一桶。"""

    def cut(value: object) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text[:256]

    return json.dumps({k: cut(v) for k, v in sorted(args.items())}, ensure_ascii=False)


def result_fingerprint(text: str) -> str:
    """结果指纹：截断哈希——结果可能很长，哈希前 512 字节足够区分。"""
    return hashlib.sha256(text[:512].encode("utf-8")).hexdigest()[:16]


class LoopGuard:
    """循环护栏：``observe_call``（每个工具结果落定时）+ ``end_round``（每轮末）。

    用法::

        guard = LoopGuard(GuardConfig())
        # 工具批次执行完、逐个回填：
        guard.observe_call(CallFacts(name, normalize_args(args), result_fingerprint(out), ok))
        # 每轮末（含纯文本轮——纯文本轮也要过 token/墙钟/轮数闸）：
        verdict = guard.end_round(round_index, prompt_tokens=..., completion_tokens=...)
        if verdict.action == "stop":
            return TurnResult(status="stopped", reason=verdict.reason, ...)
        if verdict.action == "nudge":
            # 把 verdict.nudge_text 作为尾部 user 消息注入下一轮（steering 同模式）

    ``verdicts`` 保留全部判定记录（护栏运行日志的原料）；``pause``/``resume``
    供审批等待期间挂起墙钟（评审必修 5：墙钟不含审批等待）。
    """

    def __init__(
        self,
        config: GuardConfig | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = config or GuardConfig()
        self._now = now
        self._start = now()
        self._paused_total = 0.0
        self._pause_started: float | None = None
        self._tokens = 0
        # 同签名**失败**计数（评审必修 3：成功调用不计数、不进硬闸）
        self._fail_sig_counts: Counter[tuple[str, str]] = Counter()
        # 最近调用序列（软检测原料，成功失败都进）；64 = 足够覆盖窗口与连续错误判定
        self._recent: deque[CallFacts] = deque(maxlen=64)
        # 每轮聚合的动作签名序列 / 结果指纹集合（窗口 = stall_window 个工具轮）
        self._round_calls: list[CallFacts] = []
        self._round_sigs: deque[tuple[tuple[str, str], ...]] = deque(maxlen=self.cfg.stall_window)
        self._round_results: deque[frozenset[str]] = deque(maxlen=self.cfg.stall_window)
        self._nudges = 0
        self._clean_rounds = 0  # 连续未触发停滞的轮数（必修 4 的恢复判据）
        self.verdicts: list[tuple[int, Verdict]] = []

    # ------------------------------------------------------------------
    # 墙钟挂起（评审必修 5：审批等待不计入墙钟）
    # ------------------------------------------------------------------

    def pause(self) -> None:
        """审批等待开始：挂起墙钟。重复调用以第一次为准。"""
        if self._pause_started is None:
            self._pause_started = self._now()

    def resume(self) -> None:
        """审批等待结束：恢复计时。未 pause 过的 resume 是 no-op。"""
        if self._pause_started is not None:
            self._paused_total += self._now() - self._pause_started
            self._pause_started = None

    def _elapsed(self) -> float:
        now = self._now() if self._pause_started is None else self._pause_started
        return now - self._start - self._paused_total

    # ------------------------------------------------------------------
    # 回填
    # ------------------------------------------------------------------

    def observe_call(self, call: CallFacts) -> None:
        """工具结果落定时回填一次。先计数后判定（判定在 end_round）。"""
        self._recent.append(call)
        self._round_calls.append(call)
        if not call.ok:
            self._fail_sig_counts[(call.name, call.args_signature)] += 1

    def end_round(
        self, round_index: int, *, prompt_tokens: int = 0, completion_tokens: int = 0
    ) -> Verdict:
        """每轮末判定一次。纯文本轮也必须调（token/墙钟/轮数闸对它生效）。"""
        self._tokens += prompt_tokens + completion_tokens
        # 本轮聚合进窗口（只收工具轮——纯文本轮不参与停滞判定）
        if self._round_calls:
            self._round_sigs.append(tuple((c.name, c.args_signature) for c in self._round_calls))
            results = frozenset(c.result_signature for c in self._round_calls if c.result_signature)
            self._round_results.append(results)
            self._round_calls = []

        verdict = self._hard(round_index) or self._soft() or _CONTINUE
        self.verdicts.append((round_index, verdict))
        # 必修 4：软检测连续两个观察未触发（模型恢复正常推进）→ nudge 配额清零
        if verdict.layer == "soft":
            self._clean_rounds = 0
        else:
            self._clean_rounds += 1
            if self._clean_rounds >= 2 and self._nudges > 0:
                self._nudges = 0
        return verdict

    # ------------------------------------------------------------------
    # L1 硬熔断：顺序即优先级（最具体/最危险的信号最先判）
    # ------------------------------------------------------------------

    def _hard(self, round_index: int) -> Verdict | None:
        c = self.cfg
        # 连续错误（不同签名也计——破坏性升级的信号）
        if c.consecutive_errors_limit is not None:
            errors = 0
            for call in reversed(self._recent):
                if call.ok:
                    break
                errors += 1
            if errors >= c.consecutive_errors_limit:
                return Verdict("stop", f"连续 {errors} 次工具失败", "hard")
        # 同签名失败重复（必修 3：仅失败调用；成功调用的原地踏步归软检测）
        if c.same_call_limit is not None:
            for (name, sig), count in self._fail_sig_counts.items():
                if count >= c.same_call_limit:
                    return Verdict("stop", f"同参数失败调用 {name} 已 {count} 次", "hard")
        if c.token_budget is not None and self._tokens >= c.token_budget:
            return Verdict("stop", f"token 预算耗尽({self._tokens})", "hard")
        if c.wall_clock_s is not None and self._elapsed() >= c.wall_clock_s:
            return Verdict("stop", f"墙钟超时({self._elapsed():.0f}s,不含审批等待)", "hard")
        if c.max_rounds is not None and round_index >= c.max_rounds:
            return Verdict("stop", f"达到绝对轮数上限 {c.max_rounds}", "hard")
        return None

    # ------------------------------------------------------------------
    # L2 软检测：S1 结果指纹全同 / S2 动作序列周期
    # ------------------------------------------------------------------

    def _soft(self) -> Verdict | None:
        c = self.cfg
        if not c.soft_enabled:
            return None
        rounds = list(self._round_sigs)
        results = list(self._round_results)
        if len(rounds) < c.stall_min_actions:
            return None
        # S1 无进展：窗口内每轮的结果指纹集合完全相同（世界没变）
        same_results = len(set(results)) == 1 and all(results)
        # S2 循环模式：动作签名序列尾部存在周期 ≤ 3 的重复，**且结果也按同周期
        # 重复**——只看参数会把"读→改→再读"（A,B,A,B、结果在变）这种最健康的
        # 节奏误判成振荡；结果在变 = 有进展，不算停滞。
        cyclic = any(
            len(rounds) >= 2 * p
            and rounds[-p:] == rounds[-2 * p : -p]
            and results[-p:] == results[-2 * p : -p]
            for p in range(1, 4)
        )
        if not (same_results or cyclic):
            return None
        if self._nudges < c.nudge_max:
            self._nudges += 1
            return Verdict("nudge", "检测到停滞(重复动作/相同结果)", "soft", nudge_text=NUDGE_TEXT)
        return Verdict("stop", "停滞持续,nudge 无效", "soft")
