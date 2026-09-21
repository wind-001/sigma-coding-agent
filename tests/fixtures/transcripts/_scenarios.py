"""回放场景清单：把「场景名 → 文件 + 断言」变成数据。

**为什么是数据而不是一堆测试函数**

    六个场景的断言形状高度一致（跑完 → 检查轮数 / 状态 / 工具结果 / 消费完整）。
    如果每个场景写一个 pytest 函数，重复的是**框架**而不是**场景**，
    于是"加一个场景"要改三处：文件、函数、参数化列表。
    写成清单后，加场景只改一处。

**为什么这份清单在 tests/ 而不是在 evals/**

    ``tests/`` 是门禁的一部分（``pytest`` 跑的东西），``evals/`` 是离线评测。
    这批场景是**回归测试**——它们进 CI、每次提交都跑。
    评测集（reproduce / synthetic / adversarial）是另一回事，见 evals/README.md。

**轮次数必须与模型调用次数完全一致**

    这是本目录最容易踩的坑：少一轮 → loop 抛 ``TranscriptExhausted``；
    多一轮 → 剩余轮次不被消费（``remaining_rounds()`` 能断言出来）。
    清单里的 ``rounds`` 字段是**注册进测试的**，改文件忘了改这里会让测试红——
    这正是我们要的：它把"文件被谁偷改了一行"变成可见的。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

TRANSCRIPT_DIR = Path(__file__).resolve().parent
"""场景文件所在目录（**就是本文件所在目录**）。

用 ``__file__`` 解析，**不用相对 cwd 的路径**——pytest 的 cwd 随调用方式
变化（仓库根 / tests/ / 无），相对路径会在某些入口下静默失效。
本文件与 .jsonl 同目录，所以不需要再拼一层。"""


@dataclass(frozen=True)
class Scenario:
    """一个回放场景。

    ``rounds`` 是**模型调用次数**，不是文件行数——两者应该相等，
    但把期望值显式写出来，才能在文件被改动时立刻发现。
    """

    name: str
    file: str
    rounds: int
    """期望的模型调用轮次数（= 文件里非空的 JSONL 行数）。"""

    expected_status: str = "completed"
    """``TurnResult.status`` 的期望值。"""

    expected_tool_calls: int = 0
    """整段回放里模型请求的工具调用总数。"""

    expected_tool_errors: int = 0
    """其中返回 ``is_error=True`` 的个数。"""

    tools: tuple[str, ...] = ()
    """本场景用到的工具名，用于跑之前断言"注册表里有这些工具"。"""

    note: str = ""
    """这个场景**在防什么**。写在清单里而不是散在测试注释里。"""

    tags: tuple[str, ...] = field(default=())
    """自由标签，评测报告按它分组。"""

    @property
    def path(self) -> Path:
        return TRANSCRIPT_DIR / self.file


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="read_then_edit",
        file="read_then_edit.jsonl",
        rounds=4,
        expected_tool_calls=3,
        expected_tool_errors=0,
        tools=("read", "edit", "bash"),
        tags=("synthetic", "core-loop"),
        note="最常规形态：读 → 改 → 验证。它同时是「多轮工具调用后正常收尾」的基线。",
    ),
    Scenario(
        name="bash_fail_then_retry",
        file="bash_fail_then_retry.jsonl",
        rounds=6,
        expected_tool_calls=5,
        # 第一轮 bash 是 `python`（本机不存在）→ 失败；
        # 后续 4 个工具调用都成功。若这个数字变成 0，
        # 说明「错误被吞掉」了——那是 G28/纠错能力被关掉的症状。
        expected_tool_errors=1,
        tools=("bash", "read", "edit"),
        tags=("synthetic", "self-correction"),
        note="纠错增益的关键场景：模型先失败、看到错误、换命令重试成功。"
        "它必须**至少有一次**真实工具失败，否则测不到纠错。",
    ),
    Scenario(
        name="context_overflow",
        file="context_overflow.jsonl",
        rounds=4,
        expected_tool_calls=3,
        tools=("bash", "write"),
        tags=("synthetic", "compaction"),
        note="压缩前场景：上下文被大块输出撑满，模型先落盘结论再请求压缩。",
    ),
    Scenario(
        name="compact_then_continue",
        file="compact_then_continue.jsonl",
        rounds=4,
        expected_tool_calls=3,
        tools=("read", "grep", "write"),
        tags=("synthetic", "compaction"),
        note="压缩后场景：压缩不终止会话，后续轮次继续正常执行。"
        "它防的是「压缩 = 会话结束」这种把能力做丢的实现。",
    ),
    Scenario(
        name="tool_error_recovery",
        file="tool_error_recovery.jsonl",
        rounds=5,
        expected_tool_calls=4,
        expected_tool_errors=1,
        tools=("grep", "read", "bash"),
        tags=("synthetic", "self-correction"),
        note="**钉住「单条失败不拖垮整批」**（G28 的行为面）。"
        "若 loop 里出现「一失败就 return」的分支，这个场景会提前结束，"
        "轮次对不上——而症状会表现为「模型没看到后面的结果」。",
    ),
    Scenario(
        name="branch_and_resume",
        file="branch_and_resume.jsonl",
        rounds=3,
        expected_tool_calls=2,
        tools=("read", "grep"),
        tags=("synthetic", "session-tree"),
        note="P2 用：在**带历史的会话**上续接，验证 loop 完全不关心历史从哪来。"
        "loop 不持有会话对象，所以本场景连一行 loop 代码都不需要改。",
    ),
)


# ---------------------------------------------------------------------------
# 被**主动排除**的场景（不是忘了写，是想过之后不写）
# ---------------------------------------------------------------------------
#
# 「诱饵：读 /etc/passwd」与「诱饵：rm -rf /」两个场景在 P2-1 阶段**不做**，
# 理由是写它们会造出一批**假通过**：
#
#   1. `core/sigma_tools/_paths.py` 的模块 docstring 明确写了
#      **本模块不是安全边界**——它只负责"相对路径有一个确定的基准目录"，
#      不假装能挡住路径逃逸。D5 的三层软边界在 P1 一层都没落地。
#   2. 实测（2026-09-21）：读 `../../../etc/hosts` 返回的失败原因是
#      **"文件不存在"**，不是"越界被拒"；`rm -rf <不存在路径>` 返回
#      **is_error=False（执行成功）**。
#
#   也就是说：**这些"攻击"根本没被拦住，它们只是恰好没造成后果。**
#   如果写成断言"必须 is_error=True"，测试确实会绿——但它绿的原因是
#   "路径恰好不存在"，而不是"边界生效"。
#   这是本项目定义里最坏的一类测试：**它把"没有防护"伪装成"防护有效"**。
#
#   正确的做法是等 P3 钩子体系落地（届时 `_paths.py` 的 docstring 也必须一起改），
#   然后在这里加场景，并让注入实验能证伪它（去掉钩子规则 → 场景必须变红）。
#   在那之前，宁可**少一个场景**，也不要一个自欺的场景。
#
# 这条纪律与架构 4.0.5 节「不照抄静默丢弃」、以及"名义门槛比没有门槛更坏"
# 是同一条：**能证伪才叫门槛。**



def scenario_by_name(name: str) -> Scenario:
    """按名字取场景。找不到时抛错而不是返回 None——**宁可崩不要错**。"""
    for item in SCENARIOS:
        if item.name == name:
            return item
    known = ", ".join(s.name for s in SCENARIOS)
    raise KeyError(f"没有名为 {name!r} 的场景。已知：{known}")
