"""``EvalProfile``（评测消融档位）与 ``enable_compaction`` 的门禁测试。

背景：B1 的定义是"有工具、满上下文、**无 harness 干预**"。要让这个档位
**可声明**，压缩与 checkpoint 必须能被显式关掉——而 sigma 的设计是
``compaction_policy=None`` 会被替换成默认 32k 策略（"默认开"不能被
``None`` 绕过）。所以 sdk 另立了一条**显式关断**通路
（``enable_compaction=False``），它必须压过：

1. 默认替换（``None`` → 默认 32k 策略）；
2. 调用方显式传入的策略（B1 是"无干预"，不是"换个窗口"）。

**这里的测试就是 G80 注入的靶子**：把 sdk 里"显式关断"分支删掉
（注入后它永远走默认），本文件的 ``test_interactive_session_explicit_
compaction_off`` 必须变红。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sigma_ai.fake import FakeProvider
from sigma_session.compact import CompactionPolicy

from sigma import sdk
from sigma.eval_profile import EvalProfile

# 与 test_session_compact.py 同款的最小轮次构造。
from sigma_ai.messages import AssistantMessage, TextBlock, Usage, UserMessage
from sigma_agent.agent_messages import LlmMessageWrapper
from sigma_ai.stamps import from_epoch as ts


def _text_round(text: str) -> list[dict[str, object]]:
    return [
        {"type": "text_delta", "text": text},
        {"type": "stop", "stop_reason": "stop"},
    ]


# ---------------------------------------------------------------------------
# EvalProfile 本身：档位的声明
# ---------------------------------------------------------------------------


def test_b1_disables_every_intervention() -> None:
    profile = EvalProfile.b1()
    assert profile.name == "B1"
    assert profile.compaction is False
    assert profile.checkpoint is False


def test_b2_is_the_full_harness() -> None:
    profile = EvalProfile.b2()
    assert profile.name == "B2"
    assert profile.compaction is True
    assert profile.checkpoint is True


def test_default_profile_is_b2() -> None:
    assert EvalProfile().name == "B2"


# ---------------------------------------------------------------------------
# 显式关断的优先级（G80 注入的靶子）
# ---------------------------------------------------------------------------


async def test_interactive_session_explicit_compaction_off(tmp_path: Path) -> None:
    """``enable_compaction=False`` 必须同时压过**默认替换**与**显式策略**。

    这里故意同时给了"必然触发"的策略（窗口 2000、ratio 0），并塞了历史——
    若关断分支被绕过，这一轮会先触发压缩。三重断言：策略是 ``None``、
    没有发生压缩、回答照常产出。
    """
    provider = FakeProvider.from_rounds([_text_round("照常回答")])
    session = sdk.InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        compaction_policy=CompactionPolicy(context_window_tokens=2000, trigger_ratio=0.0),
        enable_compaction=False,
    )
    session.context.append(
        *[
            LlmMessageWrapper(
                timestamp=ts(1),
                message=UserMessage(content="旧任务", timestamp=ts(1)),
            ),
            LlmMessageWrapper(
                timestamp=ts(2),
                message=AssistantMessage(
                    content=[TextBlock(text="旧回复")],
                    timestamp=ts(2),
                    provider="fake",
                    model="fake",
                    usage=Usage(prompt_tokens=1, completion_tokens=1),
                    stop_reason="stop",
                ),
            ),
        ]
    )

    result = await session.send("新任务")

    assert session.compaction_policy is None
    assert session.last_compaction is None
    assert result.text == "照常回答"


async def test_interactive_session_default_compaction_on(tmp_path: Path) -> None:
    """对照：不传 ``enable_compaction`` 时默认开——显式策略仍然生效。"""
    provider = FakeProvider.from_rounds([_text_round("回答")])
    session = sdk.InteractiveSession(
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        compaction_policy=CompactionPolicy(context_window_tokens=2000, trigger_ratio=0.0),
    )
    assert session.compaction_policy is not None


async def test_run_task_passes_enable_compaction_through(tmp_path: Path) -> None:
    """``run_task`` 的接线不能断——评测运行器就是从这个入口传档位的。"""
    provider = FakeProvider.from_rounds([_text_round("done")])
    result = await sdk.run_task(
        "任务",
        provider=provider,
        workspace_root=tmp_path,
        model="fake",
        enable_compaction=False,
    )
    assert result.text == "done"


def test_b1_profile_maps_to_sdk_flags() -> None:
    """档位 → sdk 参数的映射必须与 B1 的定义逐字段一致。

    （这段映射写在评测运行器里；这里把"映射应当是什么"钉住，
    防止有人在运行器里把两个 flag 接反。）
    """
    profile = EvalProfile.b1()
    assert (profile.name, profile.compaction, profile.checkpoint) == ("B1", False, False)


def test_flags_default_open() -> None:
    """不传就是全开——档位的"默认 = B2"必须逐字段成立，不能只看 name。"""
    profile = EvalProfile()
    assert profile.compaction is True
    assert profile.checkpoint is True
