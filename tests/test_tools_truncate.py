"""工具输出截断的测试（门槛 G29）。

截断在 P1 是**必需品**而不是优化：没有它，一次大文件读取或一条输出很多
的命令就能撞上上下文上限，任务直接失败。

**截断的文案同样是设计的一部分**——见
``test_truncation_marker_gives_actionable_advice``：那句"缩小输出范围"
不是文案润色，它决定了模型会不会重试同一个命令。
"""

from __future__ import annotations

from sigma_tools.truncate import HEAD_LINES, MAX_BYTES, truncate_output


def test_short_output_passes_through_untouched() -> None:
    """不超过阈值时**原样返回**，不添加任何标记。

    标记只在真截断时出现——否则模型每次读文件都会看到一段无关的提示。
    """
    text = "hello\nworld\n"
    result = truncate_output(text)
    assert result.truncated is False
    assert result.text == text
    assert result.kept_lines == result.total_lines


def test_long_output_keeps_head_and_tail() -> None:
    """保留头**和**尾。

    只保留头部会丢掉最关键的纠错信息——**命令的报错通常在尾部**
    （详规 3.6：错误信息必须变成模型可见的内容，否则无从纠错）。
    """
    # 2000 行 × 约 7 字节 ≈ 14 KB，稳稳超过 8 KB 阈值。
    # （最初写 500 行只有 4.4 KB，根本没触发截断——
    #   而断言"应该截断"的用例若数据不够，就是一条**假绿**。）
    text = "\n".join(f"line {i}" for i in range(2000))
    assert len(text.encode("utf-8")) > MAX_BYTES

    result = truncate_output(text)
    assert result.truncated is True
    assert result.kept_lines < result.total_lines
    assert "line 0" in result.text
    assert "line 1999" in result.text


def test_truncation_marker_gives_actionable_advice() -> None:
    """门槛 G29 的重点：截断文案必须**给出行动指引**，不是单纯告知。

    "输出已被截断"只会让模型重试同一个命令；
    "请缩小输出范围"才让它改变做法。

    这条断言的正是那句指引——**它是设计，不是措辞偏好**。
    """
    text = "\n".join(f"x{i}" for i in range(3000))
    result = truncate_output(text)
    assert result.truncated is True
    assert "缩小" in result.text, "截断标记里必须含可执行的建议"
    # 也要如实说明 P1 不保存完整输出——否则模型会去找一个不存在的文件
    assert "不保存完整输出" in result.text


def test_statistics_are_reported_for_metrics() -> None:
    """统计字段进 ``details``，用于计算"截断发生率"（架构 5.3 节）。

    这个比例本身就是一个值得报告的指标——它说明任务是否频繁撞上预算。
    """
    text = "\n".join(f"x{i}" for i in range(3000))
    result = truncate_output(text)
    assert result.total_lines == 3000
    assert result.total_bytes > MAX_BYTES
    assert result.kept_lines <= 2 * HEAD_LINES


def test_threshold_is_on_bytes_not_lines() -> None:
    """阈值按**字节**而不是行数。

    一条 base64 输出可能只有 3 行却有 10 MB——按行限制拦不住它，
    而真正爆上下文的是 token（≈字节），不是行数。
    """
    # 3 行，每行很长
    long_line = "x" * (MAX_BYTES // 2)
    text = "\n".join([long_line] * 3)
    result = truncate_output(text)
    assert result.total_lines == 3
    assert result.truncated is True


def test_empty_and_tiny_inputs_are_safe() -> None:
    """边界：空串与极小输入不崩、不产生奇怪标记。"""
    assert truncate_output("").truncated is False
    assert truncate_output("a").text == "a"
