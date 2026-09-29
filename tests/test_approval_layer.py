"""审批拦截层(P3-批次2)与 ask_user 的门槛测试:G103–G107。

G103 拒绝决策:工具不执行、模型收到可纠正的拒绝结果、批次其余照跑。
G104 越界写 × 审批:豁免才放行;批准但未豁免仍被 L1 拒;无审批通道维持 L1。
G105 危险指令:confirmer 收到**含后果文案**的提示;y/a/n 三分支;allowlist 免询问。
G106 allowlist:精确匹配、落盘、跨实例读取、移除。
G107 ask_user:交互挑选 / 非交互自动推荐 / EOF 回退 / 越界推荐下标。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.security.approval import (
    Allowlist,
    CliApprovalGate,
    allowlist_key,
    analyze_call,
)
from sigma.agent.messages import ToolResultAgentMessage
from sigma.tools.base import BaseTool
from sigma.events.lifecycle import (
    ApprovalDecision,
)
from sigma.hooks.base import (
    ApprovalHook,
)
from sigma.tools.registry import ToolRegistry
from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.base import NeverCancelled
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.providers.stamps import from_epoch as ts
from sigma.tools.builtin.ask_user import AskUserTool
from sigma.tools.builtin.bash import BashTool
from sigma.tools.builtin.write import WriteTool
from sigma.sdk import InteractiveSession

FIXED_TIME = ts(1_700_000_000)


# ---------------------------------------------------------------------------
# 用具
# ---------------------------------------------------------------------------


class EchoParams(BaseModel):
    message: str = Field(description="要回显的内容")


class CountingEchoTool(BaseTool):
    """记录执行次数——G103 断言"拒绝了就没有执行"。"""

    name = "echo"
    description = "回显"
    read_only = True

    def __init__(self) -> None:
        self.runs = 0

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        self.runs += 1
        return ToolResult(content=[TextBlock(text="echo: hi")])


class _DenyAllGate(ApprovalHook):
    name = "deny-all"

    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        return ApprovalDecision(allowed=False, reason="测试拒绝")


class _ScriptedGate(ApprovalHook):
    """按脚本回决定,并记录收到的 (name, arguments)。"""

    name = "scripted"

    def __init__(self, decisions: list[ApprovalDecision]) -> None:
        self._decisions = list(decisions)
        self.asked: list[tuple[str, dict[str, Any]]] = []

    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        self.asked.append((name, dict(arguments)))
        if self._decisions:
            return self._decisions.pop(0)
        return ApprovalDecision(allowed=True)


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _call_round(
    arguments: str, *, name: str = "echo", call_id: str = "call_1", index: int = 0
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_call_delta",
            "index": index,
            "id": call_id,
            "name": name,
            "arguments_delta": arguments,
        },
        {"type": "stop", "stop_reason": "tool_use"},
    ]


def _session(
    provider: FakeProvider,
    tmp_path: Path,
    *,
    registry: ToolRegistry,
    approval: ApprovalHook | None = None,
) -> InteractiveSession:
    return InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        registry=registry,
        system_prompt="测试",
        max_rounds=6,
        enable_compaction=False,
        enable_todo=False,
        approval=approval,
    )


def _echo_registry(tool: BaseTool | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool if tool is not None else CountingEchoTool())
    return registry


# ---------------------------------------------------------------------------
# G103:拒绝决策
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_call_is_not_executed_and_model_sees_reason(tmp_path: Path) -> None:
    tool = CountingEchoTool()
    session = _session(
        FakeProvider.from_rounds(
            [_call_round('{"message": "hi"}'), _text_round("知道了")]
        ),
        tmp_path,
        registry=_echo_registry(tool),
        approval=_DenyAllGate(),
    )

    result = await session.send("干活")

    assert result.status == "completed"
    assert tool.runs == 0  # **没有执行**
    denied = [
        m
        for m in session.context.history()
        if isinstance(m, ToolResultAgentMessage) and m.is_error
    ]
    assert len(denied) == 1
    assert "用户拒绝了这次调用" in denied[0].content[0].text  # type: ignore[union-attr]
    assert "测试拒绝" in denied[0].content[0].text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_denial_does_not_block_rest_of_batch(tmp_path: Path) -> None:
    """批次里两个调用,拒第一个、放第二个——"单个失败不中断批次"同构。"""
    tool = CountingEchoTool()
    gate = _ScriptedGate(
        [
            ApprovalDecision(allowed=False, reason="第一个不要"),
            ApprovalDecision(allowed=True),
        ]
    )
    session = _session(
        FakeProvider.from_rounds(
            [
                _call_round('{"message": "a"}', call_id="call_1", index=0),
                _call_round('{"message": "b"}', call_id="call_2", index=1),
                _text_round("完成"),
            ]
        ),
        tmp_path,
        registry=_echo_registry(tool),
        approval=gate,
    )

    result = await session.send("干活")

    assert result.status == "completed"
    assert tool.runs == 1  # 只有放行的那个执行了
    assert len(gate.asked) == 2


# ---------------------------------------------------------------------------
# G104:越界写 × 审批
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outside_write_executes_only_with_explicit_outside_approval(
    tmp_path: Path,
) -> None:
    """豁免(approve_outside=True)→ L1 放行,文件真的写到工作区外。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    gate = _ScriptedGate([ApprovalDecision(allowed=True, approve_outside=True)])
    session = _session(
        FakeProvider.from_rounds(
            [
                _call_round(
                    '{"path": "' + str(outside).replace("\\", "/") + '", "content": "x"}',
                    name="write",
                ),
                _text_round("写完了"),
            ]
        ),
        workspace,
        registry=_echo_registry(WriteTool()),
        approval=gate,
    )

    result = await session.send("写出去")

    assert result.status == "completed"
    assert outside.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_outside_write_without_exemption_is_still_rejected_by_l1(
    tmp_path: Path,
) -> None:
    """批准但**未**明示豁免 → L1 照常拒绝(审批不能悄悄瓦解 L1)。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    gate = _ScriptedGate([ApprovalDecision(allowed=True)])  # 没带 approve_outside
    session = _session(
        FakeProvider.from_rounds(
            [
                _call_round(
                    '{"path": "' + str(outside).replace("\\", "/") + '", "content": "x"}',
                    name="write",
                ),
                _text_round("看到了失败"),
            ]
        ),
        workspace,
        registry=_echo_registry(WriteTool()),
        approval=gate,
    )

    result = await session.send("写出去")

    assert result.status == "completed"
    assert not outside.exists()
    denied = [
        m
        for m in session.context.history()
        if isinstance(m, ToolResultAgentMessage) and m.is_error
    ]
    assert any("路径越界" in str(m.content[0].text) for m in denied)


@pytest.mark.asyncio
async def test_no_approval_channel_keeps_l1_rejection(tmp_path: Path) -> None:
    """无审批通道(评测/非交互路径)→ 行为与没有 L3 之前一致:越界即拒。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    session = _session(
        FakeProvider.from_rounds(
            [
                _call_round(
                    '{"path": "' + str(outside).replace("\\", "/") + '", "content": "x"}',
                    name="write",
                ),
                _text_round("看到了失败"),
            ]
        ),
        workspace,
        registry=_echo_registry(WriteTool()),
        approval=None,
    )

    result = await session.send("写出去")

    assert result.status == "completed"
    assert not outside.exists()


# ---------------------------------------------------------------------------
# G105:危险指令确认 + allowlist 免询问
# ---------------------------------------------------------------------------


def _gate_with_choices(
    workspace: Path, answers: list[int | None]
) -> tuple[CliApprovalGate, list[list[str]]]:
    """chooser 桩:记录收到的标题行,按脚本回选中下标(P3-批次2 追加协议)。"""
    titles: list[list[str]] = []

    async def chooser(
        title_lines: list[str], options: list[str], recommended_index: int | None
    ) -> int | None:
        titles.append(title_lines)
        return answers.pop(0) if answers else None

    gate = CliApprovalGate(
        workspace=workspace,
        chooser=chooser,
        allowlist=Allowlist(workspace / ".sigma" / "allowlist.json"),
    )
    return gate, titles


@pytest.mark.asyncio
async def test_dangerous_command_prompts_with_consequence_and_respects_answer(
    tmp_path: Path,
) -> None:
    """命中危险模式 → chooser 收到的标题里必须有**后果说明**;拒绝/放行两分支。

    P3-批次2 追加:交互形态升级为按键选择器,chooser 收到的是标题行列表
    (选项与推荐由选择器渲染);断言的语义不变——后果可见、两分支都通。
    """
    gate, titles = _gate_with_choices(tmp_path, [2, 0])  # 拒绝 → 允许一次
    tool = BashTool()
    session = _session(
        FakeProvider.from_rounds(
            [
                _call_round('{"command": "chmod 777 target.txt"}', name="bash"),
                _call_round('{"command": "chmod 777 target.txt"}', name="bash"),
                _text_round("完成"),
            ]
        ),
        tmp_path,
        registry=_echo_registry(tool),
        approval=gate,
    )

    result = await session.send("改权限")

    assert result.status == "completed"
    assert len(titles) == 2
    assert any("chmod 777" in line for line in titles[0])
    assert any("后果" in line for line in titles[0])  # **后果说明必须在标题里**
    assert any("软边界" in line for line in titles[0])  # 6.3 诚实声明也在
    denied = [
        m
        for m in session.context.history()
        if isinstance(m, ToolResultAgentMessage)
        and m.is_error
        and "用户拒绝了这次调用" in str(m.content[0].text)
    ]
    assert len(denied) == 1  # 第一个被拒,第二个(允许)执行


@pytest.mark.asyncio
async def test_allowlist_hit_skips_confirmation(tmp_path: Path) -> None:
    """allowlist 命中 → 不问 chooser,直接放行。"""
    gate, titles = _gate_with_choices(tmp_path, [])
    key = allowlist_key("bash", {"command": "chmod 777 target.txt"}, tmp_path)
    gate.allowlist.add(key)

    decision = await gate.approve(
        "bash", {"command": "chmod 777 target.txt"}, "call_1"
    )

    assert decision.allowed is True
    assert "allowlist" in decision.note
    assert titles == []  # **一次都没问**


# ---------------------------------------------------------------------------
# G106:allowlist 存取
# ---------------------------------------------------------------------------


def test_allowlist_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / ".sigma" / "allowlist.json"
    first = Allowlist(path)
    first.add("bash|pytest -q")
    first.add("bash|pytest -q")  # 重复 add 幂等
    assert first.contains("bash|pytest -q")

    second = Allowlist(path)  # 新实例从盘上读
    assert second.contains("bash|pytest -q")

    assert second.remove("bash|pytest -q") is True
    assert second.contains("bash|pytest -q") is False
    assert Allowlist(path).contains("bash|pytest -q") is False


def test_allowlist_exact_match_only(tmp_path: Path) -> None:
    """精确匹配:批过 `pytest -q` 不代表 `pytest -q -x` 也放行(批次 Q4 拍板)。"""
    allowlist = Allowlist(tmp_path / "allowlist.json")
    allowlist.add("bash|pytest -q")
    assert allowlist.contains("bash|pytest -q") is True
    assert allowlist.contains("bash|pytest -q -x") is False


# ---------------------------------------------------------------------------
# G107:ask_user 推荐方向列表
# ---------------------------------------------------------------------------


def _ctx(ask: Any) -> ToolContext:
    return ToolContext(
        session_id="test",
        workspace_root=Path("."),
        signal=NeverCancelled(),
        ask=ask,
    )


async def _run_ask(tool: AskUserTool, ctx: ToolContext) -> ToolResult:
    params = tool.params.model_validate(
        {
            "question": "走哪条路?",
            "options": ["方案A:保守修复", "方案B:重构后修"],
            "recommended_index": 1,
        }
    )
    return await tool.run(params, ctx)


@pytest.mark.asyncio
async def test_ask_user_returns_user_choice() -> None:
    picked: list[tuple[str, list[str], int | None]] = []

    async def ask(question: str, options: list[str], recommended: int | None) -> str:
        picked.append((question, options, recommended))
        return options[0]  # 用户偏偏不选推荐项

    result = await _run_ask(AskUserTool(), _ctx(ask))

    assert result.is_error is False
    assert "选项1" in result.content[0].text  # type: ignore[union-attr]
    assert "方案A" in result.content[0].text  # type: ignore[union-attr]
    assert picked[0][2] == 1  # 推荐下标原样传给通道


@pytest.mark.asyncio
async def test_ask_user_without_channel_auto_adopts_recommendation() -> None:
    """无交互通道(评测/管道)→ 自动采用推荐项,但**显式注明**。"""
    result = await _run_ask(AskUserTool(), _ctx(ask=None))

    assert "选项2" in result.content[0].text  # type: ignore[union-attr]
    assert "非交互模式" in result.content[0].text  # type: ignore[union-attr]
    assert result.details["choice_index"] == 1


@pytest.mark.asyncio
async def test_ask_user_empty_answer_falls_back_to_recommendation() -> None:
    async def ask(question: str, options: list[str], recommended: int | None) -> str:
        return ""  # EOF / 非法输入

    result = await _run_ask(AskUserTool(), _ctx(ask))

    assert "未收到有效选择" in result.content[0].text  # type: ignore[union-attr]
    assert result.details["choice_index"] == 1
