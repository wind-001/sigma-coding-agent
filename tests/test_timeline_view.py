"""P5-批次1 的门槛测试：观测层查看端（--timeline 的纯函数核）。"""

from __future__ import annotations

import json
from pathlib import Path

from sigma_agent.agent_messages import LlmMessageWrapper, ToolResultAgentMessage
from sigma_ai.messages import AssistantMessage, TextBlock, Usage
from sigma_session.store import JsonlStore
from sigma_session.tree import SessionTree
from sigma.timeline import (
    build_timeline,
    render_timeline,
    timeline_to_json,
)

from sigma_ai.stamps import from_epoch as ts

SID = "oldsess"
T0 = ts(1_700_000_000.000)
T1 = ts(1_700_000_001.000)
T2 = ts(1_700_000_002.000)
T3 = ts(1_700_000_003.500)


def _write_session(root: Path) -> Path:
    """构造一个两轮会话：user → assistant(带 usage) → tool_result → assistant。

    时间戳差即近似延迟的原料：轮 1 = T1-T0 = 1000ms；轮 2 = T3-T2 = 1500ms。
    """
    tree = SessionTree(store=JsonlStore(root, SID))
    tree.append(_User(T0))
    tree.append(
        LlmMessageWrapper(
            message=_assistant(prompt=8000, completion=200, cached=6400, stamp=T1),
            timestamp=T1,
        )
    )
    tree.append(
        ToolResultAgentMessage(
            tool_call_id="c1",
            tool_name="read",
            content=[TextBlock(text="ok")],
            timestamp=T2,
        )
    )
    tree.append(
        LlmMessageWrapper(
            message=_assistant(prompt=9000, completion=180, cached=7200, stamp=T3),
            timestamp=T3,
        )
    )
    return JsonlStore(root, SID).path


def _User(stamp: str):  # noqa: N802（测试内小工厂）
    from sigma_ai.messages import UserMessage

    # 落盘的是 agent 层消息：LLM 层消息必须包进 LlmMessageWrapper
    # （与 sdk.send 的装配同形——裸 UserMessage 落盘后加载即 UnknownMessageType）
    return LlmMessageWrapper(
        message=UserMessage(content="任务", timestamp=stamp), timestamp=stamp
    )


def _assistant(*, prompt: int, completion: int, cached: int, stamp: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text="进度")],
        model="fake-model",
        usage=Usage(
            prompt_tokens=prompt, completion_tokens=completion, cached_tokens=cached
        ),
        stop_reason="stop",
        timestamp=stamp,
    )


# ---------------------------------------------------------------------------
# G869：无 trace 文件的旧会话也能渲染——轮数/token/缓存命中率与手算一致，
#       延迟带 ≈、TTFT 空
# ---------------------------------------------------------------------------


def test_g869_legacy_session_renders_from_jsonl_alone(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    session_file = _write_session(root)
    assert not trace_exists(root)  # 前置：确实没有 trace

    report = build_timeline(SID, session_file, None)

    assert report.has_trace is False
    assert len(report.rounds) == 2
    # token 总量 = 两轮之和（不是"最后一轮"——2026-09-21 那个 bug 的观看版）
    assert report.total_prompt == 17000
    assert report.total_completion == 380
    assert report.total_cached == 13600
    assert report.cache_rate is not None
    assert abs(report.cache_rate - 0.8) < 1e-9
    # 轮 1 延迟 = T1-T0 = 1000ms，近似；TTFT 缺席
    r1, r2 = report.rounds
    assert r1.latency_ms == 1000 and r1.latency_approx is True and r1.ttft_ms is None
    # 轮 2 延迟 = T3-T2 = 1500ms（上一条消息是 tool_result）
    assert r2.latency_ms == 1500 and r2.latency_approx is True
    # 工具归轮 1
    assert [t.name for t in r1.tools] == ["read"]
    # 墙钟 = 首末消息差 = 3.5s，近似
    assert report.wall_seconds is not None
    assert abs(report.wall_seconds - 3.5) < 1e-6
    assert report.wall_approx is True

    lines = render_timeline(report)
    rendered = "\n".join(lines)
    assert "≈1.00s" in rendered or "≈1000ms" in rendered  # 近似标记在场
    assert "80%" in rendered
    assert "（无 trace 文件，延迟为近似值）" in rendered


def trace_exists(root: Path) -> bool:
    return any(p.name.endswith(".trace.jsonl") for p in root.iterdir())


# ---------------------------------------------------------------------------
# G870：trace 与会话文件并存 → 用 trace 精确值，不用近似值
# ---------------------------------------------------------------------------


def test_g870_trace_values_win_over_approximation(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    session_file = _write_session(root)
    trace_file = root / f"{SID}.trace.jsonl"
    trace_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": T0, "t": 0.0, "kind": "llm_requested", "round": 1}),
                json.dumps(
                    {
                        "ts": T1,
                        "t": 0.94,
                        "kind": "llm_end",
                        "round": 1,
                        "latency_ms": 940,
                        "ttft_ms": 310,
                        "model": "fake-model",
                    }
                ),
                json.dumps(
                    {"ts": T2, "t": 1.0, "kind": "tool_start", "name": "read", "call_id": "c1"}
                ),
                json.dumps(
                    {
                        "ts": T2,
                        "t": 1.4,
                        "kind": "tool_end",
                        "name": "read",
                        "ok": True,
                        "duration_ms": 400,
                        "tool_call_id": "c1",
                    }
                ),
                json.dumps({"ts": T3, "t": 1.5, "kind": "llm_requested", "round": 2}),
                json.dumps(
                    {"ts": T3, "t": 3.6, "kind": "llm_end", "round": 2, "latency_ms": 2100}
                ),
                json.dumps(
                    {
                        "ts": T3,
                        "t": 3.7,
                        "kind": "turn_end",
                        "status": "completed",
                        "rounds": 2,
                        "prompt_tokens": 17000,
                        "completion_tokens": 380,
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    report = build_timeline(SID, session_file, trace_file)

    assert report.has_trace is True
    r1, r2 = report.rounds
    # 精确值胜出：940ms（非 1000），且不再标近似；TTFT 有值
    assert r1.latency_ms == 940
    assert r1.latency_approx is False
    assert r1.ttft_ms == 310
    assert r2.latency_ms == 2100
    # 工具时长从 trace 配对而来
    assert r1.tools[0].duration_ms == 400
    # 状态与墙钟来自 trace（精确）
    assert report.status == "completed"
    assert report.wall_approx is False
    assert abs((report.wall_seconds or 0) - 3.7) < 1e-6

    rendered = "\n".join(render_timeline(report))
    assert "940ms" in rendered
    assert "≈" not in rendered  # 全部精确值时近似标记消失


# ---------------------------------------------------------------------------
# --json 机器可读版
# ---------------------------------------------------------------------------


def test_json_output_carries_75_metrics(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    session_file = _write_session(root)
    payload = timeline_to_json(build_timeline(SID, session_file, None))
    assert payload["rounds"] == 2
    assert payload["prompt_tokens"] == 17000
    assert payload["cache_rate"] == 0.8
    assert payload["per_round"][0]["latency_ms"] == 1000
    assert payload["per_round"][0]["latency_approx"] is True
