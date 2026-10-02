# LoopControl — 循环控制重构详规（护栏分层：硬熔断 / 软检测 / 验证收尾）

> 状态：**待星辰评审**（2026-10-02）。本文是详规，不是实施记录。  
> 背景：`e2dd862` 把主任务 `max_rounds` 改为 `None`（while True，模型收敛即出）。  
> 本详规回答："收敛即出"之后，失控谁来管。  
> 运行环境：本地 CLI + Web 工作台（桥接服务 8301），工具集 bash / read / write /  
> edit / grep / web_fetch / web_search / todo / ask_user / team(multi_agent)。

---

## 0. 结论先行

1. `max_rounds=None` **保留**，但它必须是"无默认上限"而不是"无上限"——加一条  
   **绝对保险丝**（默认 200 轮，可配），语义从"策略"降级为"安全网"。策略交给  
   模型收敛，安全网永远是数字。
2. 失控防护做**三层**：硬熔断（确定性、每轮必查、命中即停）→ 软检测（启发式、  
   先提示后停止）→ 验证收尾（客观判据决定"完成"是否成立）。裁判模型放最后一层，  
   且 MVP 不做。
3. 停止永远是**优雅停止**（`status="stopped"` + reason），不抛异常、不丢现场；  
   现场恢复复用已有资产：JSONL 会话树（天然逐轮 checkpoint）、git 快照 +  
   `/rollback-to`、`continue` 断点续跑、todo 清单即待办交接。
4. MVP 三件事（覆盖 ~80% 失控）：**token/墙钟预算 + 同签名重复检测 + 停滞  
   nudge-then-stop**。全是纯内存计算，零外部依赖，一个 `LoopGuard` 类装下。

---

## 1. 当前设计的失效场景（对着现有代码逐条核实过）

| #  | 场景                                        | 触发样例                              | 现有防线                         | 缺口                     |
| -- | ----------------------------------------- | --------------------------------- | ---------------------------- | ---------------------- |
| F1 | **同参重试循环**：grep 搜不到 → 原样再搜；bash 报错 → 原样重跑 | 最常见失控，token 线性烧                   | 无                            | 无任何检测                  |
| F2 | **振荡**：改 A → 测试挂 → 改回 B → 又挂 → 循环         | edit↔revert 乒乓                    | 无                            | 单参签名抓不住，需要序列模式         |
| F3 | **慢烧**：每轮都"有点动静"但方向错                      | 模型反复读大文件、跑测试、微调                   | 无                            | 最难；MVP 用预算封顶损失，根治靠验证收尾 |
| F4 | **收尾幻觉**：没验证就宣布完成                         | 截图实证：说"完成"但任务没做完                  | 无                            | 需要客观判据（见 §3）           |
| F5 | **破坏性升级**：full access 下反复危险命令             | 连续失败后模型换更狠的命令                     | 审批闸（可热切）+ L1 路径边界            | 连续失败本身无闸               |
| F6 | **队列放大**：排队项无限续跑                          | 排队 10 条 × 每条失控                    | `_run_all` guard<10（已修中断不续跑） | 基本覆盖                   |
| F7 | **墙钟挂死**                                  | provider 挂起（已修双闸）、工具层 bash 超时（已有） | 都有                           | 缺 turn 级总闸             |
| F8 | **轮数截断**（旧病）                              | 20/30 轮硬截断                        | 已改 None                      | 反向风险：现在完全裸奔            |

关键认识：**F1/F2 是高频低成本可检出的；F3 低频高损伤只能封顶；F4 是"完成"语义  
问题，熔断救不了，要靠验证；F5 已有审批闸，护栏只补"连续失败"这一个信号。**

---

## 2. 多层退出机制

### 2.1 分层与优先级（命中即短路，高层永远压过低层）

```
优先级（高 → 低）：
  L0 用户打断（InterruptToken）        —— 已有，永远最高
  L1 硬熔断（确定性，每轮必查，O(1)）
     顺序即优先级：连续错误 > 同签名重复 > token/成本预算 > 墙钟 > 绝对轮数
     （最具体/最危险的信号最先判）
  L2 软检测（启发式，窗口统计）
     触发 1~2 次 → nudge（作为 steering 注入，模型自纠）
     持续触发   → stop（优雅停止）
  L3 验证收尾（模型宣布完成时才跑，客观判据裁决"完成"是否成立）
  L4 裁判模型（MVP 不做；若做，只在 L2 升级后调一次，封顶成本）
```

兜底关系：

- 硬熔断**不需要**软检测同意——确定性信号直接停；
- 软检测的 nudge 是**给模型自纠机会**，不是放行：nudge 用尽仍停滞 → stop；
- L3 只在模型**不再调工具**（自认为完成）时跑：验收命令不过 → 注入失败输出，  
  最多 2 轮修复机会，再不过 → stopped（不撒谎说 completed）；
- 所有 stop 统一走 `TurnResult(status="stopped", reason=...)`，reason 进  
  statusDetail → 前端收尾行。**不抛异常**——异常会绕过树持久化。

### 2.2 各硬熔断的判定口径

| 闸        | 判定                         | 默认         | 备注                       |
| -------- | -------------------------- | ---------- | ------------------------ |
| 连续错误     | 连续 N 次 `ToolEnd(ok=False)` | N=5        | 错误后有一次成功文本轮则清零           |
| 同签名重复    | `(tool, 规范化args哈希)` 计数     | 3 次        | 规范化：键排序、路径归一、数字截断        |
| token 预算 | 累计 prompt+completion       | 2M（可配）     | 有单价可折算成钱；缓存 token 单独计价可选 |
| 墙钟       | `time.monotonic` 起点差       | 30min/turn | 只数 turn，不含挂起等待审批的时间      |
| 绝对轮数     | round_index 上限             | 200        | 保险丝，不是策略；正常任务永远碰不到       |

### 2.3 软检测的两个判定器（纯内存）

- **S1 无进展**：观察窗口（6 轮）内所有工具轮的**结果指纹**完全相同 → 模型在  
  反复得到同一个世界；
- **S2 循环模式**：动作签名序列尾部存在周期 ≤3 的重复（A,B,A,B…）→ 振荡。

两个都是"动了但世界没变"的定义。指纹 = 对工具结果取截断哈希（结果可能很长，  
哈希 512 字节足够）。

---

## 3. 收敛性保障：三个候选的取舍

| 方案                  | 判断                    | 理由                                                                                                                                                                                                   |
| ------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| planner-executor 分离 | **不做**                | 当前规模（单人、loop ~1k 行）引入第二个 agent 是复杂度翻倍；todo 工具已经是轻量 planner（P4 拍板）。停滞 nudge 里指路 todo（"重新审视清单再决定"）即可获得 80% 收益                                                                                          |
| 显式状态机               | **不做**（保留现有 turn 级状态） | FSM 的价值在"非法转移不可达"，但本轮状态集（completed/stopped/error）已由 TurnResult Literal 保证；再加一层 FSM 是把 if 写成类                                                                                                         |
| **验证驱动收尾**          | **做，且是首选**            | "完成"必须是客观判据不是模型自称。落地：任务可选 `verification_cmd`（如 `pytest -q`）；模型停止调工具时若工作区有 diff 且声明了验收命令 → 自动跑（timeout 300s）→ 过则 completed，不过注入反馈最多 2 轮，再不过 stopped。工作台任务创建时已可带 effort/access，加 verification 字段是小契约扩展 |

取舍原则：**复杂度放进检测器（纯函数、可单测），不放进控制流。** loop 主干保持  
"取事件 → 累积 → 问护栏"，所有新逻辑都在护栏类里。

---

## 4. 失控后的处理：挂起、汇报、恢复

已有资产盘点（全部现成，护栏只负责"触发时用它们"）：

| 资产                      | 现状                           | 在本方案中的角色                                          |
| ----------------------- | ---------------------------- | ------------------------------------------------- |
| 会话 JSONL                | 逐事件追加落盘                      | **逐轮 checkpoint 免费拿**，stop 后树状态完整                 |
| trace                   | `trace_path_for`（已修漂移）       | 失控轮的完整证据链，如实判 stopped                             |
| git 快照 + `/rollback-to` | 工作台已有（含 pre-restore 可达性缺陷待修） | 恢复现场：stop 时打轻量标记 ref `sigma-stall/<session>-r<N>` |
| `continue` 断点续跑         | 已有                           | 恢复执行入口                                            |
| todo 清单                 | 三动作 + revise                 | **待办交接的自然载体**                                     |
| statusDetail / 收尾行      | 已透传                          | 汇报出口                                              |

停止时的汇报流程（全部在 `_turn_end` 附近，一次组装）：

1. `TurnResult(status="stopped", reason=<护栏 reason>)`；
2. **最后一次优雅 nudge**（stop 前注入一轮："汇总当前进度、卡点、更新 todo 后  
   简短收尾"）——这轮的输出就是给人看的失控报告，进事件流；
3. 工作台打 stall ref（`git branch sigma-stall/<id>-r<N>`，零成本、不碰工作区）；
4. 前端收尾行显示 reason + "输入继续可接续 / 输入回滚可退回"。

恢复路径：`continue`（从树状态续跑，已保存的工作不丢）或 `/rollback-to <ref>`  
（丢弃失控段的文件改动）——**两条路都通，报告里写清楚选哪个**。

---

## 5. 核心实现（Python，可直接作为实现参考）

```python
# src/sigma/runtime/loop_guard.py —— 与 event_loop 解耦,检测器全部纯函数
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class GuardConfig:
    # 硬熔断(确定性;顺序即优先级,见 _hard)
    max_rounds: int | None = 200          # 绝对保险丝,None 语义收归这里
    wall_clock_s: float | None = 30 * 60
    token_budget: int | None = 2_000_000
    same_call_limit: int = 3
    consecutive_errors_limit: int = 5
    # 软检测
    stall_window: int = 6
    stall_min_actions: int = 3
    nudge_max: int = 2


@dataclass(frozen=True)
class RoundFacts:
    """每轮结束由 loop 回填的最小事实;护栏不 import loop,只吃这个。"""
    round_index: int
    tool_name: str | None          # None = 纯文本轮(通常是收敛轮)
    args_signature: str | None     # 规范化参数指纹
    result_signature: str | None   # 结果指纹(截断哈希)
    ok: bool | None
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class Verdict:
    action: str              # "continue" | "nudge" | "stop"
    reason: str              # 进 TurnResult.reason → statusDetail → 收尾行
    layer: str               # "hard" | "soft" | "none"
    nudge_text: str | None = None


_NUDGE = (
    "[停滞提醒] 检测到重复动作/相同结果。先调 todo(action=\"list\") 重新审视清单,"
    "换一种方法或缩小问题范围;若任务实际无法推进,直接汇总已完成的进度、当前卡点"
    "和剩余待办,简短收尾。不要重复刚才的调用。"
)


def normalize_args(args: dict) -> str:
    """规范化参数指纹:键排序 + 值截断,让语义相同/字节不同的调用落进同一桶。"""
    def cut(value: object) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text[:256]
    return json.dumps({k: cut(v) for k, v in sorted(args.items())}, ensure_ascii=False)


def result_fingerprint(text: str) -> str:
    return hashlib.sha256(text[:512].encode("utf-8")).hexdigest()[:16]


class LoopGuard:
    """循环护栏:硬熔断先行,软检测升级,判定与执行分离。
    用法:每轮结束 guard.observe(facts) → verdict;
    "nudge" → 把 nudge_text 作为 steering 注入下一轮;"stop" → 优雅停止。"""

    def __init__(self, config: GuardConfig, now: Callable[[], float] = time.monotonic) -> None:
        self.cfg = config
        self._now = now
        self._start = now()
        self._tokens = 0
        self._sig_counts: Counter[tuple[str, str]] = Counter()
        self._recent: deque[RoundFacts] = deque(maxlen=config.stall_window * 2)
        self._nudges = 0

    # ---------- 硬熔断:先判最具体最危险的信号 ----------
    def _hard(self, f: RoundFacts) -> Verdict | None:
        c = self.cfg
        if f.tool_name is not None and f.ok is False:
            errors = 0
            for r in reversed(self._recent):
                if r.ok is False:
                    errors += 1
                else:
                    break
            if errors + 1 >= c.consecutive_errors_limit:
                return Verdict("stop", f"连续 {errors + 1} 次工具失败", "hard")
            if self._sig_counts[(f.tool_name, f.args_signature or "")] + 1 >= c.same_call_limit:
                return Verdict("stop", f"同参数调用 {f.tool_name} 已 {c.same_call_limit} 次", "hard")
        if c.token_budget is not None and self._tokens >= c.token_budget:
            return Verdict("stop", f"token 预算耗尽({self._tokens})", "hard")
        elapsed = self._now() - self._start
        if c.wall_clock_s is not None and elapsed >= c.wall_clock_s:
            return Verdict("stop", f"墙钟超时({elapsed:.0f}s)", "hard")
        if c.max_rounds is not None and f.round_index >= c.max_rounds:
            return Verdict("stop", f"达到绝对轮数上限 {c.max_rounds}", "hard")
        return None

    # ---------- 软检测:S1 结果全同 / S2 序列周期 ----------
    def _soft(self) -> Verdict | None:
        actions = [r for r in list(self._recent)[-self.cfg.stall_window:]
                   if r.tool_name is not None]
        if len(actions) < self.cfg.stall_min_actions:
            return None
        sigs = [f"{r.tool_name}:{r.args_signature}" for r in actions]
        same_results = len({r.result_signature for r in actions}) == 1
        cyclic = any(len(sigs) >= 2 * p and sigs[-p:] == sigs[-2 * p:-p]
                     for p in range(1, 4))
        if not (same_results or cyclic):
            return None
        if self._nudges < self.cfg.nudge_max:
            self._nudges += 1
            return Verdict("nudge", "检测到停滞", "soft", nudge_text=_NUDGE)
        return Verdict("stop", "停滞持续,nudge 无效", "soft")

    def observe(self, f: RoundFacts) -> Verdict:
        self._tokens += f.prompt_tokens + f.completion_tokens
        if f.tool_name is not None:
            self._sig_counts[(f.tool_name, f.args_signature or "")] += 1
        self._recent.append(f)
        return self._hard(f) or self._soft() or Verdict("continue", "", "none")
```

loop 侧接入（`event_loop.run_turn`，改动集中在轮循环收口处）：

```python
guard = LoopGuard(GuardConfig())          # 配置从 SamplingParams/session 传入
for round_index in itertools.count(1):    # None 语义保留;保险丝在护栏里
    ...                                   # 现有:打断检查 / 信箱 / 模型调用 / 工具执行
    verdict = guard.observe(RoundFacts(
        round_index=round_index,
        tool_name=round.main_tool,        # 本轮主工具;文本轮为 None
        args_signature=normalize_args(round.tool_args) if round.main_tool else None,
        result_signature=result_fingerprint(round.tool_output) if round.main_tool else None,
        ok=round.tool_ok,
        prompt_tokens=assistant.usage.prompt_tokens,
        completion_tokens=assistant.usage.completion_tokens,
    ))
    if verdict.action == "stop":
        return TurnResult(status="stopped", messages=produced, text=last_text,
                          rounds=round_index, usage=total_usage, reason=verdict.reason)
    if verdict.action == "nudge":
        self._inject_steering(verdict.nudge_text)   # 复用现有信箱通道
# 循环自然退出(模型不再调工具)后 —— 验证收尾(§3):
if task.verification_cmd and workspace_has_diff():
    if not await run_verification(task.verification_cmd, inject_feedback=self._inject_steering,
                                  max_retries=2):
        return TurnResult(status="stopped", ..., reason="验收命令未通过,挂起待处理")
return TurnResult(status="completed", ...)
```


```

测试口径（确定性会红）：
- 同签名 3 次必停（两次不同参数不停——防误伤）；
- 结果指纹全同窗口 → 第一次 nudge、第二次 nudge、第三次 stop；
- 连续 5 错必停、错-成功-错不清零计数错误；
- `max_rounds=200` 保险丝在 200 轮必停（构造 200 轮假 provider）；
- 验收命令失败两轮后 stopped 而非 completed。

---

## 6. 最小可行版本（覆盖 ~80% 失控）

只做三件，全部零外部依赖：

1. **token/墙钟预算**（`GuardConfig` 前四个字段里的两个）——损失封顶，10 行；
2. **同签名重复 + 连续错误闸**——抓 F1/F5，纯 Counter，20 行；
3. **停滞 nudge-then-stop**（S1+S2 合并）——抓 F2，给模型自纠机会再停，30 行。

明确的**不做的**（防堆砌）：裁判模型（贵、慢、判定 itself 不可靠，nudge 文本已
携带"换方法或收尾"的指令）、planner-executor、重 FSM。验证收尾排 MVP+1——它
价值最高但要动任务契约（verification_cmd 字段），单独一批做。

## 7. 实施切分（评审通过后）

- 批次 1：`loop_guard.py` + `event_loop` 接入 + 单测（护栏独立可测）；
- 批次 2：`InteractiveSession`/工作台传 `GuardConfig`（工作台默认 30min/2M，
  CLI 默认更宽）；收尾行/stop 汇报文案；
- 批次 3（MVP+1）：任务 `verification_cmd` 契约 + 验证收尾 + 工作台 UI 入口；
- 批次 4（可选）：stall ref 打标（依赖 pre-restore 可达性缺陷修复）。
```
