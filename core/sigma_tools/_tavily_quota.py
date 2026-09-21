"""Tavily 免费额度账本（工具层内部件）。

为什么单独一个文件，而不是把计数写在 web_search.py 里
    配额是跨调用、跨会话、跨工作区的状态，web_search.py 管的是"一次请求"。
    混在一起会让"记账口径"藏在一个 async 方法里，改起来没有着力点。
    与 _paths.py / truncate.py 同级：工具层的内部件，不是公共 API。

两个数据源，谁是权威
    服务端 GET /usage 是权威（返回本计费周期该 key 的 usage / limit），
    本地计数是快路径（零额外请求）。分工：

    - 每次调用前读本地；
    - 快照过期（TTL）、已用 ≥ 90%、或服务端报超限 → 强制校准；
    - 校准失败不阻断搜索：这个 key 只有我们在用，本地计数对自己是准确的，
      /usage 只是交叉校验。失败记进 last_error，不抛。

口径：credits 不是"次数"
    免费档是 1,000 credits/月。basic / fast / ultra-fast 每次 1 credit，
    advanced 每次 2 credits。所以"1000 次/月"只在默认档成立。
    按 credits 记账是唯一不会超额的记法。

预留-结算（reserve / release）
    check 与 charge 若分成两步，两个并发调用会双双通过检查再双双扣费，
    结果是超发。所以 reserve 在同一个锁里先扣再加放行：

    - 成功 → 什么都不用做（已记）；
    - 确定未在服务端发生（连接失败 / 401 / 429 / 4xx）→ release 退回；
    - 超时 / 5xx → 不退（请求可能已在服务端发生）。
      宁可本地多记一次，不可漏记——漏记是越界，多记只是保守。
      漂移由 /usage 校准收敛。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable

TAVILY_BASE_URL = "https://api.tavily.com"

FREE_MONTHLY_CREDITS = 1000

#: advanced 的单价是其余档位的两倍（Tavily 官方计费表）
ADVANCED_COST = 2
DEFAULT_COST = 1

#: 快照多久算过期。300s 意味着"最多多花 300 秒的信息"，不是安全阈值。
SYNC_TTL_S = 300.0

#: 已用超过这个比例就每次都校准——把"临近上限"这一段看紧。
SYNC_AT_RATIO = 0.9

DEFAULT_TIMEOUT_S = 20.0


class TavilyUsage(BaseModel):
    """落盘的状态。字段刻意少：写盘的东西越多，越容易半写坏。"""

    cycle_used: int = 0
    cycle_limit: int = FREE_MONTHLY_CREDITS
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
    limit: int = FREE_MONTHLY_CREDITS
    remaining: int = 0
    reserved: int = 0
    synced: bool = False


class TavilyQuota:
    """本地计数 + 服务端校准的账本。"""

    def __init__(
        self,
        *,
        api_key: str,
        state_path: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = TAVILY_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = SYNC_TTL_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._api_key = api_key
        self._state_path = state_path
        self._transport = transport
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._ttl_s = ttl_s
        # 用墙钟而不是 monotonic：synced_at 要落盘、要跨进程比较
        self._clock: Callable[[], float] = clock or time.time
        # 只读工具是并发执行的（loop 里 asyncio.gather），而 reserve 是读改写。
        # 没有这把锁就是丢计数，症状是"额度没用完却超了"，不指向根因。
        self._lock = asyncio.Lock()
        self._state: TavilyUsage | None = None

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def _load(self) -> TavilyUsage:
        """读状态。坏文件不崩：重置为 0 并把原因记进 last_error。"""
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            self._state = TavilyUsage.model_validate(raw)
        except FileNotFoundError:
            self._state = TavilyUsage()
        except (OSError, ValueError) as exc:
            self._state = TavilyUsage(
                last_error=f"状态文件不可读（{type(exc).__name__}），已重置为 0：{exc}"
            )
            self._persist()
        return self._state

    def _persist(self) -> None:
        """原子落盘。写失败只记 last_error，不阻断搜索。"""
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

    def snapshot(self) -> TavilyUsage:
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
                        f"联网搜索额度已用尽（{state.cycle_used}/{state.cycle_limit} credits），"
                        "本工具已禁用。不要重试 web_search，"
                        "请改用本地信息或其它工具；额度在下一个计费周期自动恢复。"
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
                        f"本次需要 {cost} credits，但剩余不足"
                        f"（{state.cycle_used}/{state.cycle_limit}）。"
                        "不要再尝试 web_search——用尽免费额度会越界，"
                        "请改用本地信息或其它工具。"
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

    def _needs_sync(self, state: TavilyUsage) -> bool:
        if state.synced_at <= 0.0:
            return True
        if self._clock() - state.synced_at > self._ttl_s:
            return True
        # 临近上限时每次都校准——这一段是唯一真正会越界的区间
        return state.cycle_used >= int(state.cycle_limit * SYNC_AT_RATIO)

    async def _sync(self, state: TavilyUsage) -> bool:
        """用 GET /usage 校准。返回是否成功。

        失败不阻断：记 last_error、把 synced_at 推到当前（避免每次都打），
        然后沿用本地计数继续判定。
        """
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout_s,
            ) as client:
                response = await client.get(
                    f"{self._base_url}/usage",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                payload: Any = response.json()
        except Exception as exc:
            state.last_error = (
                f"/usage 校准失败（沿用本地计数）：{type(exc).__name__}: {exc}"
            )
            state.synced_at = self._clock()
            self._persist()
            return False

        key = payload.get("key") if isinstance(payload, dict) else None
        used = key.get("usage") if isinstance(key, dict) else None
        limit = key.get("limit") if isinstance(key, dict) else None
        if isinstance(used, int) and used >= 0:
            state.cycle_used = used
        if isinstance(limit, int) and limit > 0:
            state.cycle_limit = limit
        state.disabled = state.cycle_used >= state.cycle_limit
        state.synced_at = self._clock()
        state.last_error = None
        self._persist()
        return True
