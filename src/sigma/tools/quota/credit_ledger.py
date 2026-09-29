"""额度账本的公共实现（工具层内部件）。

为什么抽这一层（2026-09-21，批次 8）
    批次 7 只有 Tavily 一家，`TavilyQuota` 是一个自有完整实现；
    批次 8 加了 Firecrawl，于是**第二个真实实现出现了**——两家的差别只有两处：

        校准端点（USAGE_PATH）与校准响应的解析（_parse_usage）

    其余（落盘、锁、TTL 校准、预留-结算、耗尽禁用）逐字相同。
    判据沿用删 `BaseLoop` 时定下的那条：
    **"只有一个子类的抽象基类 = 纯间接"要删；被真实出现的第二个实现所要求的抽象要抽。**
    所以这里抽基类，而不是复制一份 `FirecrawlQuota`——
    复制出来的两份会在下一次计费口径变化时各自漂移，而症状是"只有其中一个工具的额度算错了"。

为什么单独一个文件，而不是写进 web_search.py / web_fetch.py
    配额是跨调用、跨会话、跨工作区的状态；工具类管的是"一次请求"。
    混在一起会让"记账口径"藏在一个 async 方法里，改起来没有着力点。

两个数据源，谁是权威
    服务端用量端点是权威（返回本计费周期该 key 的用量/上限），本地计数是快路径（零额外请求）。分工：

    - 每次调用前读本地；
    - 快照过期（TTL）、已用 ≥ 90%、或服务端报超限 → 强制校准；
    - 校准失败**不阻断**调用：这个 key 只有我们在用，本地计数对自己是准确的，
      校准只是交叉校验。失败记进 `last_error`，不抛。

口径：credits 不是"次数"
    Tavily 与 Firecrawl 的免费档都是 **1,000 credits/月**，但单价不同：
    Tavily basic 1 / advanced 2；Firecrawl scrape 1。所以"1000 次/月"只在默认档成立。
    按 credits 记账是唯一不会超额的记法。

预留-结算（reserve / release）
    check 与 charge 若分成两步，两个并发调用会双双通过检查再双双扣费，结果是超发。
    所以 reserve 在同一个锁里先扣再放行：

    - 成功 → 什么都不用做（已记）；
    - 确定未在服务端发生（连接失败 / 401 / 429 / 4xx）→ release 退回；
    - 超时 / 5xx → 不退（请求可能已在服务端发生）。
      宁可本地多记一次，不可漏记——漏记是越界，多记只是保守。漂移由校准收敛。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

#: 快照多久算过期。300s 意味着"最多多花 300 秒的信息"，不是安全阈值。
SYNC_TTL_S = 300.0

#: 已用超过这个比例就每次都校准——把"临近上限"这一段看紧。
SYNC_AT_RATIO = 0.9

DEFAULT_TIMEOUT_S = 20.0

#: 两家免费档都是 1000 credits/月（2026-09-21 分别实测确认）。
#: 它只是**初值**：第一次校准就会被服务端返回的真实限额覆盖。
DEFAULT_CYCLE_LIMIT = 1000


class LedgerUsage(BaseModel):
    """落盘的状态。字段刻意少：写盘的东西越多，越容易半写坏。"""

    cycle_used: int = 0
    cycle_limit: int = DEFAULT_CYCLE_LIMIT
    synced_at: float = 0.0
    disabled: bool = False
    last_error: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.cycle_limit - self.cycle_used)


class QuotaDecision(BaseModel):
    """一次额度判定的结果。allowed=False 时 reason 必须能直接给模型看。"""

    allowed: bool
    reason: str = ""
    used: int = 0
    limit: int = DEFAULT_CYCLE_LIMIT
    remaining: int = 0
    reserved: int = 0
    synced: bool = False


class CreditLedger(ABC):
    """本地计数 + 服务端校准的账本基类。

    子类只给四样东西：``LABEL`` / ``TOOL_NAME``（给模型看的文案）、
    ``USAGE_PATH``（校准端点）、``_parse_usage``（校准响应解析）。
    构造函数不接受任何 provider 专属参数——**新增一家只需要新增一个子类**。
    """

    #: 给模型看的名字，进拒绝文案。例如"联网搜索"。
    LABEL: ClassVar[str] = ""

    #: 工具名，进拒绝文案（模型据此知道"不要再重试哪个工具"）。
    TOOL_NAME: ClassVar[str] = ""

    #: 校准端点（相对 base_url）。子类必须给。
    USAGE_PATH: ClassVar[str] = ""

    #: base_url 缺省值。子类必须给。
    DEFAULT_BASE_URL: ClassVar[str] = ""

    def __init__(
        self,
        *,
        api_key: str,
        state_path: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = SYNC_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._api_key = api_key
        self._state_path = state_path
        self._transport = transport
        self._base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s
        # 用墙钟而不是 monotonic：synced_at 要落盘、要跨进程比较
        self._clock: Callable[[], float] = clock or time.time
        # 只读工具是并发执行的（loop 里 asyncio.gather），而 reserve 是读改写。
        # 没有这把锁就是丢计数，症状是"额度没用完却超了"，不指向根因。
        self._lock = asyncio.Lock()
        self._state: LedgerUsage | None = None

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def _load(self) -> LedgerUsage:
        """读状态。坏文件不崩：重置为 0 并把原因记进 last_error。"""
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._state = LedgerUsage.model_validate(raw)
        except FileNotFoundError:
            self._state = LedgerUsage()
        except (OSError, ValueError) as exc:
            self._state = LedgerUsage(
                last_error=f"状态文件不可读（{type(exc).__name__}），已重置为 0：{exc}"
            )
            self._persist()
        return self._state

    def _persist(self) -> None:
        """原子落盘。写失败只记 last_error，不阻断调用。"""
        state = self._state
        if state is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(self._state_path.name + ".tmp")
            tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            state.last_error = f"状态文件写入失败（{type(exc).__name__}）：{exc}"

    def snapshot(self) -> LedgerUsage:
        """当前账本（同步）。供 CLI 横幅显示剩余额度。"""
        return self._load()

    # ------------------------------------------------------------------
    # 判定与记账
    # ------------------------------------------------------------------

    async def reserve(self, cost: int) -> QuotaDecision:
        """判定并预留 cost 个 credits。放行时已扣，不必再 charge。"""
        if cost <= 0:
            raise ValueError(f"cost 必须为正，收到 {cost}")
        async with self._lock:
            state = self._load()
            synced = False
            if self._needs_sync(state):
                synced = await self._sync(state)

            if state.disabled:
                return QuotaDecision(
                    allowed=False,
                    reason=(
                        f"{self.LABEL}额度已用尽（{state.cycle_used}/{state.cycle_limit} credits），"
                        f"本工具已禁用。不要重试 {self.TOOL_NAME}，"
                        "请改用本地信息、已有搜索结果摘要或其它工具；"
                        "额度在下一个计费周期自动恢复。"
                    ),
                    used=state.cycle_used,
                    limit=state.cycle_limit,
                    remaining=0,
                    synced=synced,
                )

            if state.cycle_used + cost > state.cycle_limit:
                state.disabled = True
                self._persist()
                return QuotaDecision(
                    allowed=False,
                    reason=(
                        f"本次需要 {cost} credits，但{self.LABEL}剩余不足"
                        f"（{state.cycle_used}/{state.cycle_limit}）。"
                        f"不要再尝试 {self.TOOL_NAME}——用尽免费额度会越界，"
                        "请改用本地信息、已有搜索结果摘要或其它工具。"
                    ),
                    used=state.cycle_used,
                    limit=state.cycle_limit,
                    remaining=state.remaining,
                    synced=synced,
                )

            state.cycle_used += cost
            if state.cycle_used >= state.cycle_limit:
                state.disabled = True
            self._persist()
            return QuotaDecision(
                allowed=True,
                used=state.cycle_used,
                limit=state.cycle_limit,
                remaining=state.remaining,
                reserved=cost,
                synced=synced,
            )

    async def release(self, cost: int) -> None:
        """退回一次确定没有在服务端发生的预留。"""
        async with self._lock:
            state = self._load()
            state.cycle_used = max(0, state.cycle_used - cost)
            self._persist()

    async def mark_exhausted(self, reason: str) -> None:
        """服务端说额度已用尽：把本地账本对齐到上限并禁用。"""
        async with self._lock:
            state = self._load()
            state.cycle_used = max(state.cycle_used, state.cycle_limit)
            state.disabled = True
            state.last_error = reason
            self._persist()

    # ------------------------------------------------------------------
    # 校准
    # ------------------------------------------------------------------

    def _needs_sync(self, state: LedgerUsage) -> bool:
        if state.synced_at <= 0.0:
            return True
        if self._clock() - state.synced_at > self._ttl_s:
            return True
        # 临近上限时每次都校准——这一段是唯一真正会越界的区间
        return state.cycle_used >= int(state.cycle_limit * SYNC_AT_RATIO)

    async def _sync(self, state: LedgerUsage) -> bool:
        """用服务端用量端点校准。返回是否成功。

        失败不阻断：记 last_error、把 synced_at 推到当前（避免每次都打），
        然后沿用本地计数继续判定。
        """
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_s,
            ) as client:
                response = await client.get(
                    f"{self._base_url}{self.USAGE_PATH}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                payload: Any = response.json()
        except Exception as exc:
            state.last_error = (
                f"{self.USAGE_PATH} 校准失败（沿用本地计数）：{type(exc).__name__}: {exc}"
            )
            state.synced_at = self._clock()
            self._persist()
            return False

        parsed = self._parse_usage(payload)
        if parsed is None:
            state.last_error = f"{self.USAGE_PATH} 校准响应认不出（沿用本地计数）"
            state.synced_at = self._clock()
            self._persist()
            return False

        used, limit = parsed
        state.cycle_used = max(0, used)
        if limit > 0:
            state.cycle_limit = limit
        state.disabled = state.cycle_used >= state.cycle_limit
        state.synced_at = self._clock()
        state.last_error = None
        self._persist()
        return True

    @abstractmethod
    def _parse_usage(self, payload: Any) -> tuple[int, int] | None:
        """把校准响应解析成 ``(used, limit)``；认不出返回 None。

        **认不出时必须返回 None 而不是抛异常**：端点改形状是外部世界的事，
        它不该让整个工具挂掉——沿用本地计数是正确降级。
        """
        raise NotImplementedError


