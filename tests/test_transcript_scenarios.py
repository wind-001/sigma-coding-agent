"""回放场景的门禁测试：把 ``evals/runner.py`` 的期望值变成回归断言。

**为什么不直接把 runner 当测试用**

    runner 是**评测侧**工具：它产出报告、打印表格、失败时返回非零。
    但它没有 pytest 的粒度——一个场景塌了，其余场景仍然跑完，
    而 CI 上想要的是"具体哪个场景的哪条期望不符"。

    所以这里用**同一份清单**（``fixtures/transcripts/_scenarios.py``）
    做断言，而不是另抄一份期望值。
    **两份实现意味着修一个忘另一个**——本项目已在 `.venv` 的 `.pth`
    与 loop 的重复 parse 上各踩过一次。

**本文件测的是「P2 会用到的东西在 P1 就成立」**

    branch_and_resume / compact_then_* 这些场景的**文件**现在就有，
    但消费它们的会话树、压缩器是 P2 才写的。
    本轮先钉住的是**不变量**：loop 在这几种输入下行为正确、
    transcript 被完整消费、单条工具失败不拖垮整批。
    P2 改 context 时，这些断言必须**一条都不变地继续绿**——
    它们就是"P2 的重构没有改坏 P1 行为"的证据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.runner import build_registry, run_scenario
from fixtures.transcripts._scenarios import SCENARIOS, scenario_by_name
from fixtures.workspace import FIXTURE_FILES, write_fixture_workspace

# ---------------------------------------------------------------------------
# transcript 文件本身的完整性
# ---------------------------------------------------------------------------


def test_every_scenario_file_exists() -> None:
    """清单里每个场景的文件都必须在。

    文件删了而清单没删，症状是 ``FileNotFoundError``——
    它看起来像"路径解析坏了"，实际只是文件不在了。
    """
    missing = [s.name for s in SCENARIOS if not s.path.exists()]
    assert not missing, f"清单里有场景但文件不存在：{missing}"


def test_scenario_names_are_unique() -> None:
    """场景名必须唯一——它同时是 ``--scenario`` 的键与报告里的行标识。"""
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names)), f"场景名重复：{names}"


def test_scenario_files_are_not_empty() -> None:
    """每个场景至少要有一轮。

    空文件是**最危险的一种"绿"**：``load_transcript`` 返回 0 轮，
    loop 第一次请求就抛 ``TranscriptExhausted``——至少会红。
    但如果某个实现吞掉异常，空场景会静默通过。这里先把它挡住。
    """
    for scenario in SCENARIOS:
        assert scenario.rounds >= 1, f"{scenario.name} 声明了 0 轮"


def test_scenarios_cover_the_five_tags() -> None:
    """场景集必须覆盖几类关键形态。

    这不是"凑数量"，而是防**覆盖率悄悄退化**：
    有人删掉 ``tool_error_recovery``（它比较绕），
    剩下的场景全绿，而"单条失败不拖垮整批"就再也没人测了。
    """
    tags = {tag for scenario in SCENARIOS for tag in scenario.tags}
    for required in ("core-loop", "self-correction", "compaction", "session-tree"):
        assert required in tags, f"场景集里没有 {required!r} 形态的用例"


def test_self_correction_scenarios_have_a_real_failure() -> None:
    """「自我纠错」场景必须**至少包含一次真实工具失败**。

    **这条是整套场景里最容易自欺的地方。**
    如果 ``expected_tool_errors == 0``，"纠错增益"就无从测起——
    场景名还叫 self-correction，实际只测了"顺序调用两个工具"。
    """
    for scenario in SCENARIOS:
        if "self-correction" in scenario.tags:
            assert scenario.expected_tool_errors >= 1, (
                f"{scenario.name} 标了 self-correction，"
                "但期望的工具失败数是 0——它测不到纠错"
            )


# ---------------------------------------------------------------------------
# 隔离工作区
# ---------------------------------------------------------------------------


def test_fixture_workspace_provides_every_referenced_path() -> None:
    """transcript 里引用的路径，fixture 工作区里都得有。

    漏一个的症状是"工具报文件不存在"，读起来像工具坏了，
    实际是测试夹具不全。这条断言把根因提前暴露。
    """
    required = {
        "demo/greeting.py",
        "demo/main.py",
        "demo/tests/test_calc.py",
    }
    assert required <= set(FIXTURE_FILES), (
        f"fixture 工作区缺少：{sorted(required - set(FIXTURE_FILES))}"
    )


def test_fixture_workspace_is_idempotent(tmp_path: Path) -> None:
    """重复铺同一份工作区不报错、结果一致。

    runner 会对每个场景各铺一次；如果 ``write_fixture_workspace``
    在有残留时行为不同（比如 append 而不是覆盖），
    "单跑绿、全跑红"就会出现。
    """
    first = write_fixture_workspace(tmp_path / "a")
    write_fixture_workspace(tmp_path / "a")
    second = write_fixture_workspace(tmp_path / "a")

    for rel in FIXTURE_FILES:
        assert (first / rel).read_text(encoding="utf-8") == (
            second / rel
        ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 逐场景回放（参数化）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_scenario_replays_as_specified(
    scenario: object, tmp_path: Path
) -> None:
    """每个场景都按清单里声明的期望值跑通。

    断言的是**清单里的期望值**，不是硬编码的数字——
    这样"改期望"只需改一处，且会留下 diff 供review。
    """
    from fixtures.transcripts._scenarios import Scenario

    assert isinstance(scenario, Scenario)
    report = await run_scenario(scenario, tmp_path)

    assert report.mismatches == [], (
        f"{scenario.name} 与期望不符：\n  " + "\n  ".join(report.mismatches)
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
async def test_scenario_status_is_completed(scenario: object, tmp_path: Path) -> None:
    """所有回放场景都应以 ``completed`` 收尾，**不应是 stopped**。

    ``stopped`` 意味着撞到了 ``max_rounds``——而 runner 设的上限是
    ``rounds + 5``，撞到它只可能是 loop 在空转（模型一直要工具却没进展）。
    """
    from fixtures.transcripts._scenarios import Scenario

    assert isinstance(scenario, Scenario)
    report = await run_scenario(scenario, tmp_path)
    assert report.status == "completed", (
        f"{scenario.name} 状态是 {report.status}，"
        f"期望 completed（stopped 通常意味着撞上了 max_rounds）"
    )


async def test_tool_error_does_not_abort_the_batch(tmp_path: Path) -> None:
    """**单条工具失败不得中止整批**（G28 的行为面）。

    这是"纠错增益"这个核心指标的全部前提。如果 loop 里混进
    "一失败就 return"的分支，这条路会立刻断掉——
    而症状是"模型看不到后面的结果"，看起来像模型自己决定不做了。

    ``tool_error_recovery`` 场景专门为它设计：第 3 轮的 bash 失败，
    第 4、5 轮仍然继续执行。若批次被中止，轮数会从 5 掉到 3。
    """
    scenario = scenario_by_name("tool_error_recovery")
    report = await run_scenario(scenario, tmp_path)

    assert report.tool_errors >= 1, "场景里应当有真实的工具失败"
    assert report.rounds_used == scenario.rounds, (
        f"只跑到第 {report.rounds_used} 轮（期望 {scenario.rounds}）——"
        "工具失败后 loop 没有继续"
    )
    assert report.transcript_fully_consumed


async def test_registry_has_no_web_tools(tmp_path: Path) -> None:
    """回放用的注册表**不含联网工具**。

    它们是可选的、要 key、要花额度。混进来会让离线回放变成
    需要网络与凭据——那就不叫"确定性"了。
    """
    names = set(build_registry().names())
    assert names == {"read", "write", "edit", "bash", "grep"}, names
