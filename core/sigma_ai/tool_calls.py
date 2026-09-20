"""把 ``ToolCallDelta`` 分片拼装成完整调用。

为什么这件事属于**协议层**
    ``tool_calls[].index``、arguments 跨片拼接、流结束前的收尾——
    这些都是 wire protocol 的知识，与"这个调用该不该执行"无关。

    边界判据（详见 `docs/plans/P1-sigma_ai重构-详规.md` 第 4 节）：
    **协议层负责"把字节流变成结构化数据"；
    agent 层负责"这个调用该不该执行、失败了怎么办"。**

为什么需要它（而不是两处各写一遍）
    在拆出本模块之前，**累积逻辑存在两份**：
    ``openai/provider.py`` 内部按 index 分组累积，
    ``sigma_agent/loop.py`` 又自己写了一遍——因为 provider 累积完就 ``del`` 掉了，
    没把结果交出来。

    两份实现意味着两处可错的代码，而**多工具并行时 index 交错到达**
    恰恰是最容易写错的地方（冒烟 P4 在真实 API 上实测到 16 个分片拼成一个调用）。

    本模块把那份逻辑收敛成**一个类**，两边共用。

本模块**不做**什么
    **不做 JSON schema 校验**——那需要 ``tool.params``，只有 agent 层有。

    只做"拼装 + JSON 解析"，且解析失败**如实记录原因、不抛异常**：
    失败必须变成模型可见的信息（详规 3.6），抛异常等于关掉纠错能力。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sigma_ai.messages import ToolCallBlock

if TYPE_CHECKING:
    from sigma_ai.events import ToolCallDelta


@dataclass
class AssembledCall:
    """一次工具调用的拼装结果。

    **它是"拼装产物"，既不是协议类型、也不是消息类型**——
    这个归属本身是有争议的（详规 R3 已确认放协议层，理由是它只依赖协议知识）。

    ``parse_error`` 为空表示拼装成功。**不抛异常**（理由见模块 docstring）。
    """

    index: int
    id: str | None
    name: str | None
    raw_arguments: str
    arguments: dict[str, Any] | None
    parse_error: str = ""

    @property
    def ok(self) -> bool:
        """拼装是否成功。"""
        return not self.parse_error

    def to_block(self) -> ToolCallBlock:
        """转成 LLM 层的工具调用块。

        只在拼装成功时可调用——**失败的调用不该伪装成一次合法调用**。
        若把原始文本塞进 ``arguments``，错误文案会变成"缺字段 __raw__"这类
        误导性信息，反而掩盖真正的根因（JSON 非法）。
        """
        if self.parse_error or self.arguments is None or not self.name:
            raise ValueError(
                f"第 {self.index} 个调用拼装失败，不能转成 ToolCallBlock：{self.parse_error}"
            )
        return ToolCallBlock(
            id=self.id or f"call_{self.index}",
            name=self.name,
            arguments=self.arguments,
        )


class ToolCallAssembler:
    """按 ``index`` 累积分片，最后产出完整调用。

    用法::

        assembler = ToolCallAssembler()
        async for event in provider.stream(...):
            if isinstance(event, ToolCallDelta):
                assembler.feed(event)
        calls = assembler.finish()
    """

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, Any]] = {}

    def feed(self, delta: ToolCallDelta) -> None:
        """喂一个分片。

        **按 ``index`` 归属，不按到达顺序**——多工具并行时两个 index 会交错到达，
        按到达顺序硬拼会拼出两个半截 JSON，而那个症状（模型拿错参数）
        不指向根因。
        """
        slot = self._slots.setdefault(
            delta.index, {"id": None, "name": None, "arguments": ""}
        )
        if delta.id:
            slot["id"] = delta.id
        if delta.name:
            slot["name"] = delta.name
        slot["arguments"] += delta.arguments_delta

    @property
    def has_calls(self) -> bool:
        """到目前为止是否见过至少一个分片。"""
        return bool(self._slots)

    def finish(self) -> list[AssembledCall]:
        """收尾，产出**按 index 升序**的完整调用列表。

        顺序固定为 index 升序，因为**结果顺序必须与调用顺序一致**（门槛 G27）：
        顺序错了会让 ``tool_call_id`` 与结果错配，而症状是
        "模型拿着 A 的结果回答 B 的问题"——长对话里极难定位。
        """
        return [self._finalize(index) for index in sorted(self._slots)]

    def _finalize(self, index: int) -> AssembledCall:
        """把单个 index 的分片拼到位，并做**一级校验**（JSON 层面）。"""
        slot = self._slots[index]
        raw = str(slot.get("arguments") or "")
        name = str(slot.get("name") or "")
        call_id = slot.get("id")

        if not name:
            return AssembledCall(
                index=index,
                id=call_id,
                name=None,
                raw_arguments=raw,
                arguments=None,
                parse_error=(
                    f"第 {index} 个工具调用没有 name 字段，无法确定要调用哪个工具。"
                    f"原始内容（前 200 字符）：{raw[:200]}"
                ),
            )

        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            return AssembledCall(
                index=index,
                id=call_id,
                name=name,
                raw_arguments=raw,
                arguments=None,
                parse_error=(
                    f"工具 {name} 的 arguments 不是合法 JSON（{exc}）。"
                    f"原始内容（前 300 字符）：{raw[:300]}\n"
                    "请重新输出**完整且合法**的 JSON 参数。"
                ),
            )

        if not isinstance(parsed, dict):
            return AssembledCall(
                index=index,
                id=call_id,
                name=name,
                raw_arguments=raw,
                arguments=None,
                parse_error=(
                    f"工具 {name} 的 arguments 必须是 JSON 对象，实际是 "
                    f"{type(parsed).__name__}。原始内容：{raw[:300]}"
                ),
            )

        return AssembledCall(
            index=index,
            id=call_id,
            name=name,
            raw_arguments=raw,
            arguments=parsed,
        )
