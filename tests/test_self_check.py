"""P5-批次2 波 1 的门槛测试:自证矩阵的零成本验证行(G873/G874/G875/G878)。

每条测试对应详规里的一张门槛卡,**每条都有"注入变红"的路径**
(scripts/gate_injection_batch17.py)。运行成本说明:G875 跑全部 32 条
对抗回放、G878 跑 6 次 steering 回放、G874 跑 n=3 git 统计——全部
离线零 API 成本,总量约 10 秒,换来的是"报告行可被 CI 强制"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "evals"))

from gate_replay import run_gate_replay  # noqa: E402
from rollback_stats import run_rollback_stats, tree_diff  # noqa: E402
import self_check as self_check_mod  # noqa: E402

from sigma_agent.agent_messages import LlmMessageWrapper  # noqa: E402
from sigma_ai.messages import AssistantMessage, TextBlock, Usage, UserMessage  # noqa: E402
from sigma_session.store import JsonlStore  # noqa: E402
from sigma_session.tree import SessionTree  # noqa: E402

from sigma_ai.stamps import from_epoch as ts  # noqa: E402

FIXED_TIME = ts(1_700_000_000)


def _write_session_with_usage(
    root: Path, session_id: str, rounds: list[tuple[int, int, int]]
) -> None:
    """构造一个只含 assistant 轮的会话(usage 已知,手算口径的锚点)。"""
    tree = SessionTree(store=JsonlStore(root, session_id))
    tree.append(
        LlmMessageWrapper(
            message=UserMessage(content="任务", timestamp=FIXED_TIME),
            timestamp=FIXED_TIME,
        )
    )
    for i, (prompt, completion, cached) in enumerate(rounds):
        stamp = ts(1_700_000_000 + i + 1)
        tree.append(
            LlmMessageWrapper(
                message=AssistantMessage(
                    content=[TextBlock(text=f"第{i + 1}轮")],
                    model="fake",
                    usage=Usage(
                        prompt_tokens=prompt,
                        completion_tokens=completion,
                        cached_tokens=cached,
                    ),
                    stop_reason="stop",
                    timestamp=stamp,
                ),
                timestamp=stamp,
            )
        )


# ---------------------------------------------------------------------------
# G873:缓存命中率 = 全部会话累加(不是"最近一个"——2026-09-21 bug 的形状)
# ---------------------------------------------------------------------------


def test_g873_cache_stats_accumulates_all_sessions(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    # 会话 A:1000/800(80%);会话 B:2000/1000(50%) → 总口径 1800/3000 = 60%
    _write_session_with_usage(root, "sess-a", [(1000, 100, 800)])
    _write_session_with_usage(root, "sess-b", [(2000, 200, 1000)])

    stats = self_check_mod.cache_stats(root)

    assert [row.session_id for row in stats.sessions] == ["sess-a", "sess-b"]
    assert stats.total_prompt == 3000
    assert stats.total_cached == 1800
    assert stats.rate is not None
    assert abs(stats.rate - 0.6) < 1e-9
    # 单会话口径也各自正确(报告要列每行)
    assert abs((stats.sessions[0].rate or 0) - 0.8) < 1e-9
    assert abs((stats.sessions[1].rate or 0) - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# G874:回滚统计——比对必须内容敏感;n=3 冒烟全过
# ---------------------------------------------------------------------------


def test_g874_tree_diff_is_content_sensitive() -> None:
    """同名不同内容必须被揪出——只比文件名集合的比对是假比对(E874 注入点)。"""
    assert tree_diff({"a.txt": "x"}, {"a.txt": "y"}) == ["内容不一致 a.txt"]
    assert tree_diff({"a.txt": "x"}, {}) == ["多出 a.txt"]
    assert tree_diff({}, {"a.txt": "x"}) == ["缺失 a.txt"]
    assert tree_diff({"a.txt": "x", "b.txt": "1"}, {"b.txt": "1", "a.txt": "x"}) == []


def test_g874_rollback_stats_smoke(tmp_path: Path) -> None:
    report = run_rollback_stats(iterations=3, base_tmp=tmp_path / "rb")
    assert report.all_ok
    assert report.snapshots_checked == 3
    assert report.successes == 3


# ---------------------------------------------------------------------------
# G875:对抗集全量回放——20 拦截 / 0 误拦 / 2 免打扰
# ---------------------------------------------------------------------------


def test_g875_gate_replay_full_dataset() -> None:
    report = run_gate_replay()

    assert report.deny_total == 20
    assert report.denied == 20
    assert report.allow_total == 10
    assert report.false_blocks == 0
    assert report.allowlist_total == 2
    assert report.allowlist_hits == 2
    assert report.failures == []
    assert abs(report.intercept_rate - 1.0) < 1e-9
    assert abs(report.false_block_rate) < 1e-9


# ---------------------------------------------------------------------------
# G878:steering 送达——3/3 下一轮可见 + 落盘 + 结果不变
# ---------------------------------------------------------------------------


def test_g878_steering_delivered_and_harmless(tmp_path: Path) -> None:
    outcomes = self_check_mod.run_steering_scenarios(base_tmp=tmp_path / "steer")

    assert len(outcomes) == self_check_mod.STEERING_SCENARIOS
    for case in outcomes:
        assert case.delivered, f"{case.scenario}: steering 未进入第 2 轮模型输入"
        assert case.persisted, f"{case.scenario}: steering 未落盘进会话树"
        assert case.text_unchanged, f"{case.scenario}: 最终文本与基线不一致"
        assert case.rounds_equal, f"{case.scenario}: 轮数与基线不一致"
        assert case.ok


# ---------------------------------------------------------------------------
# self_check.md 汇总冒烟:四列结构在场、报告由真实运行生成
# ---------------------------------------------------------------------------


def test_self_check_report_smoke(tmp_path: Path) -> None:
    """报告生成自真实运行;空会话目录时主张 4 行如实标无数据(all_ok=False)。"""
    (tmp_path / "empty-sessions").mkdir()
    markdown, all_ok = self_check_mod.build_report(tmp_path / "empty-sessions")

    assert "主张 4|常驻区稳定能吃到缓存" in markdown
    assert "主张 6|checkpoint 是有效边界" in markdown
    assert "主张 7|钩子能减少危险操作" in markdown
    assert "主张 8f|steering 送达" in markdown
    assert "主张 8b|技能渐进披露" in markdown
    assert "无数据" in markdown  # 空目录如实呈现,不编数字
    assert all_ok is False
