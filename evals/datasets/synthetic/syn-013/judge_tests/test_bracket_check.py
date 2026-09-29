"""``bracket_check`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import bracket_check`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from bracket_check import code_only, find_mismatch


# --- code_only：字符串与注释要被等长抹掉 ------------------------------------


def test_code_only_masks_strings():
    assert code_only('f("hi")') == "f(" + " " * 4 + ")"


def test_code_only_masks_comments():
    assert code_only("x = 1  # 说明 (不配对)") == "x = 1  " + " " * 10


def test_code_only_keeps_newlines():
    assert code_only("a\n# c\nb") == "a\n" + " " * 3 + "\nb"


def test_code_only_escaped_quote_does_not_close_string():
    # "a\"b" 是一个完整字符串：6 个字符全部要被抹成空格。
    assert code_only('x = "a\\"b" + 1') == "x = " + " " * 6 + " + 1"


def test_code_only_escaped_quote_pair_fully_masked():
    # "say \"hi\" now"：中间两个 \" 都不结束字符串。
    assert code_only('msg = "say \\"hi\\" now"') == "msg = " + " " * 16


# --- find_mismatch：配对错误要报出来，配对正确要放行 ------------------------


def test_balanced_returns_none():
    assert find_mismatch('x = "(str)"  # (note') is None


def test_balanced_multiline_returns_none():
    text = "def f(a, b):\n    return {'k': (a, b)}\n"
    assert find_mismatch(text) is None


def test_extra_closer_reported():
    msg = find_mismatch(")(")
    assert msg is not None and "多余" in msg


def test_unclosed_reported():
    msg = find_mismatch("(a + b")
    assert msg is not None and "未闭合" in msg


def test_cross_nesting_reported():
    assert find_mismatch("[(]") is not None


def test_escaped_quote_interaction():
    # 字符串里的 \" 不结束字符串：这个文本其实只有一个未闭合的 (。
    msg = find_mismatch('n("a\\") + 1"')
    assert msg is not None and "未闭合" in msg


def test_unclosed_reports_opener_line():
    msg = find_mismatch("ok = (1\n+ 2")
    assert msg is not None and "第 1 行" in msg
