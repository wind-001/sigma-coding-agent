"""验证收尾（VerificationGate，推进机制批次 2）的测试。

判据（每条都能被对应的注入实验证伪，见 scripts/gate_injection_advance.py
的 GA-1 段）：
    G-VG-1  验收命令 exit 0 → 不干预（让它 completed，不是继续）；
    G-VG-2  不过 → continue_round + inject 带输出尾部（模型看得见为什么挂）；
    G-VG-3  连续 max_rounds 轮不过 → force_stop（优雅 stopped，不撒谎）；
    G-VG-4  超时/命令没跑起来 → 按"不过"处置（报事实，不吞）；
    G-VG-5  空命令 → 不干预（没声明就完全不进场）；
    G-VG-6  loop 集成：continue 真多跑一轮；force_stop 真出 stopped+reason；
    G-VG-7  zero 值：不注册 gate 的会话行为与之前逐字节一致（由批次 1 的
             G-ADV-6 与 sdk 的 None 透传钉住，这里不再重复）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from sigma.agent.types import ToolContext, ToolResult
from sigma.events.lifecycle import AdvanceFacts, HookEvent
from sigma.hooks.base import BaseHook, HookManager
from sigma.providers.base import NeverCancelled
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.providers.stamps import from_epoch as ts
from sigma.runtime.advance import VerificationGate
from sigma.runtime.event_loop import AgentLoop
from sigma.tools.base import BaseTool
from sigma.tools.registry import ToolRegistry
from pydantic import BaseModel, Field

FIXED_TIME = ts(1_700_000_000)
_FACTS = AdvanceFacts(round_index=1, last_text="我做完了")


def _script(workspace: Path, name: str, body: str) -> str:
    """在 workspace 里写一个 py 脚本，返回可直接喂 create_subprocess_shell
    的命令串（可执行文件路径带引号——Windows 下路径含空格是常态）。"""
    path = workspace / name
    path.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{path}"'


_FAIL_BODY = "import sys\nprint('FAILED test_x::assert 1 == 2')\nsys.exit(1)\n"
_PASS_BODY = "print('1 passed')\n"


# ---------------------------------------------------------------------------
# 单元：判定逻辑（不经 loop，直接问钩子）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_passes_when_command_succeeds(tmp_path: Path) -> None:
    """G-VG-1：exit 0 → 不干预。不是 continue——过了就收，继续只烧轮数。"""
    gate = VerificationGate(tmp_path, _script(tmp_path, "ok.py", _PASS_BODY))
    decision = await gate.advance(_FACTS)
    assert decision.continue_round is False
    assert decision.force_stop is False


@pytest.mark.asyncio
async def test_gate_continues_then_stops(tmp_path: Path) -> None:
    """G-VG-2/3：第 1 次不过 → 继续+注入；第 2 次还不过 → force_stop。"""
    gate = VerificationGate(
        tmp_path, _script(tmp_path, "bad.py", _FAIL_BODY), max_rounds=2
    )
    first = await gate.advance(_FACTS)
    assert first.continue_round is True
    assert first.force_stop is False
    assert first.inject is not None
    assert "FAILED test_x" in first.inject  # 输出尾部进注入
    assert "验证收尾" in first.inject

    second = await gate.advance(_FACTS)
    assert second.force_stop is True
    assert second.continue_round is False
    assert "2 轮未通过" in second.reason
    assert "不撒谎 completed" in second.reason


@pytest.mark.asyncio
async def test_gate_treats_timeout_as_failure(tmp_path: Path) -> None:
    """G-VG-4a：超时是失败的一种（报事实，不吞、不无限等）。"""
    gate = VerificationGate(
        tmp_path,
        _script(tmp_path, "hang.py", "import time\ntime.sleep(30)\n"),
        max_rounds=1,
        timeout_s=0.5,
    )
    decision = await gate.advance(_FACTS)
    assert decision.force_stop is True  # max_rounds=1：第一次不过就停
    assert "超时" in decision.reason


@pytest.mark.asyncio
async def test_gate_treats_missing_command_as_failure(tmp_path: Path) -> None:
    """G-VG-4b：命令不存在也是失败——且 cmd 的原始错误消息（GBK 编码）
    要能解出来进 reason，不能变成乱码（Windows 中文版实测形状）。"""
    gate = VerificationGate(tmp_path, "this-command-does-not-exist-xyz", max_rounds=1)
    decision = await gate.advance(_FACTS)
    assert decision.force_stop is True
    # 输出经 utf-8→GBK 回落后是可读中文："不是内部或外部命令"
    assert "不是内部或外部命令" in decision.reason


@pytest.mark.asyncio
async def test_gate_without_command_never_intervenes(tmp_path: Path) -> None:
    """G-VG-5：空命令 → 永远不干预（没声明就完全不进场）。"""
    gate = VerificationGate(tmp_path, "   ")
    decision = await gate.advance(_FACTS)
    assert decision.continue_round is False
    assert decision.force_stop is False


# ---------------------------------------------------------------------------
# 集成：经 loop 真跑（FakeProvider 管模型，gate 管收尾）
# ---------------------------------------------------------------------------


class _NoopParams(BaseModel):
    pass


class _NoopTool(BaseTool):
    name = "noop"
    description = "空转工具"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return _NoopParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="ok")])


def _text_round(text: str) -> list[dict[str, Any]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _make_loop_with_gate(
    workspace: Path,
    rounds: list[list[dict[str, Any]]],
    gate: VerificationGate,
    recorder: Any = None,
) -> AgentLoop:
    registry = ToolRegistry()
    registry.register(_NoopTool())
    hooks = HookManager()
    if recorder is not None:
        hooks.register(recorder)
    hooks.register_advance(gate)
    return AgentLoop(
        provider=FakeProvider.from_rounds(rounds),
        registry=registry,
        model="fake",
        workspace_root=workspace,
        signal=NeverCancelled(),
        clock=lambda: FIXED_TIME,
        hooks=hooks,
    )


@pytest.mark.asyncio
async def test_loop_stops_with_reason_when_gate_forces(tmp_path: Path) -> None:
    """G-VG-6：loop 侧——第 1 轮收手 gate 不过 → 续跑；第 2 轮还不过 →
    stopped + reason（不抛异常、树不丢）。transcript 只够两轮，第三轮
    请求会 TranscriptExhausted——那正是 force_stop 必须在第二次就发出的证据。
    """
    gate = VerificationGate(
        tmp_path, _script(tmp_path, "bad.py", _FAIL_BODY), max_rounds=2
    )
    loop = _make_loop_with_gate(
        tmp_path, [_text_round("我做完了"), _text_round("又试了一次")], gate
    )
    result = await loop.run_turn([])

    assert result.status == "stopped"
    assert "2 轮未通过" in (result.reason or "")
    assert result.rounds == 2
    # 注入的失败输出进了 produced（树上原文，审计可见）
    produced_text = " ".join(
        str(getattr(getattr(m, "message", None), "content", "")) for m in result.messages
    )
    assert "验证收尾" in produced_text


@pytest.mark.asyncio
async def test_loop_completes_when_gate_passes(tmp_path: Path) -> None:
    """G-VG-1 的 loop 侧：命令一次过 → 第 1 轮就 completed（不续命）。

    且 AdvanceDecided 必须落一条（全 False）——trace 里要分得清
    "没注册 gate"和"注册了且验收已过"，否则审计链缺一环。
    """
    from sigma.events.lifecycle import AdvanceDecided

    class _Recorder(BaseHook):
        name = "rec"

        def __init__(self) -> None:
            self.seen: list[Any] = []

        def events(self) -> tuple[type[HookEvent], ...]:
            return (AdvanceDecided,)

        def on_event(self, event: HookEvent) -> None:
            self.seen.append(event)

    recorder = _Recorder()
    gate = VerificationGate(tmp_path, _script(tmp_path, "ok.py", _PASS_BODY))
    loop = _make_loop_with_gate(
        tmp_path, [_text_round("我做完了")], gate, recorder=recorder
    )
    result = await loop.run_turn([])

    assert result.status == "completed"
    assert result.rounds == 1
    assert len(recorder.seen) == 1
    decision = recorder.seen[0].decision
    assert decision.continue_round is False
    assert decision.force_stop is False


@pytest.mark.asyncio
async def test_gate_recovers_after_fix_within_budget(tmp_path: Path) -> None:
    """G-VG-2 的正剧：第 1 轮不过 → 模型"修好" → 第 2 轮过 → completed。

    命令是个随调用翻转的脚本（第一次 exit 1、之后 exit 0）——模拟
    "注入失败输出后模型真修对了"的真实序列。
    """
    flip = tmp_path / "flip.py"
    flip.write_text(
        "import sys, pathlib\n"
        f"marker = pathlib.Path(r'{tmp_path / 'marker'}')\n"
        "if marker.exists():\n"
        "    print('1 passed')\n"
        "    sys.exit(0)\n"
        "marker.write_text('tried')\n"
        "print('FAILED test_x')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    cmd = f'"{sys.executable}" "{flip}"'
    gate = VerificationGate(tmp_path, cmd, max_rounds=2)
    loop = _make_loop_with_gate(
        tmp_path,
        [
            _text_round("我做完了"),
            _text_round("修好了，再看看"),
        ],
        gate,
    )
    # 时序：r1 收手 → advance#1（marker 不存在 → exit 1 → continue 并注入）
    # → r2 收手 → advance#2（marker 已由第一次调用创建 → exit 0 → 不干预）
    # → completed。两次 advance 之间正好隔着第 2 轮模型调用，
    # "模型修好了"由脚本自身状态模拟（免去再写一个假 provider）。
    result = await loop.run_turn([])
    assert result.status == "completed"
    assert result.rounds == 2
