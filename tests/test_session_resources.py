"""``AGENTS.md`` 的读取与硬截断（``resources.py``）。

对应 ``docs/plans/P2-会话树与上下文-详规.md`` 第 5 节门槛 **G54**（`AGENTS.md` 硬截断）。

**这个文件里最值得看的是"截断必须可见"那几条**

    截断本身不是难点，难的是**截断之后模型知不知道**。
    它不知道的话，会以为"项目约定就这些"，于是不会去补看——
    而 `AGENTS.md` 恰恰是最容易写成一篇长文的东西。

    所以断言不只是"长度对上"，还包括：
    正文里有截断标记、标记里说清丢了多少、**给出怎么拿到全文**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sigma_ai.tokens import estimate_text
from sigma_session.resources import (
    AGENTS_MD_FILENAME,
    DEFAULT_MAX_TOKENS,
    ProjectInstructions,
    load_project_instructions,
)


def _long_text(lines: int = 400) -> str:
    """造一段明显超预算的项目说明。"""
    return "\n".join(
        f"- 约定第 {i} 条：这是一条比较长的项目约定说明文字，用来把文件撑到超预算。"
        for i in range(1, lines + 1)
    )


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """**文件不存在不是错误。**

    绝大多数项目没有 ``AGENTS.md``。若它报错，每个入口都要先
    ``if exists()``——而那是最容易漏的约定，漏了的症状是
    "没有这个文件的机器跑不起来"。
    """
    result = load_project_instructions(tmp_path / "没有这个文件.md")

    assert result.text == ""
    assert result.source is None
    assert result.error == ""
    assert not result.loaded


def test_none_path_returns_empty(tmp_path: Path) -> None:
    """``path=None`` 表示"明确不要项目说明"，不是"读失败"。"""
    result = load_project_instructions(None)
    assert result == ProjectInstructions()


def test_small_file_is_loaded_verbatim(tmp_path: Path) -> None:
    """没超预算时**逐字节原样返回**——不能偷偷加标记或改行尾。"""
    path = tmp_path / AGENTS_MD_FILENAME
    text = "# 约定\n\n- 用 python3\n- 先写测试\n"
    path.write_text(text, encoding="utf-8")

    result = load_project_instructions(path)

    assert result.text == text
    assert not result.truncated
    assert result.tokens == estimate_text(text)
    assert result.original_tokens == result.tokens
    assert result.found and result.loaded


def test_empty_file_is_loaded_as_empty(tmp_path: Path) -> None:
    """空文件是"存在但没内容"——与"不存在"不同，但都不该报错。"""
    path = tmp_path / AGENTS_MD_FILENAME
    path.write_text("", encoding="utf-8")

    result = load_project_instructions(path)

    assert result.text == ""
    assert result.source == path
    assert result.error == ""
    assert not result.loaded  # 没内容可注入


def test_long_file_is_truncated_within_budget(tmp_path: Path) -> None:
    """**G54 本体**：超长文件被硬截断，且截断后**确实在上限之内**。

    注意断言的是"截断后的 token ≤ 上限"，不是"截断后约等于上限"——
    后者会把"标记自己也占 token"这类问题放过去
    （先按上限切、再拼标记，就会自己超上限）。
    """
    path = tmp_path / "AGENTS.md"
    path.write_text(_long_text(), encoding="utf-8")

    result = load_project_instructions(path)

    assert result.truncated
    assert result.original_tokens > DEFAULT_MAX_TOKENS
    assert result.tokens <= DEFAULT_MAX_TOKENS, (
        f"截断后 {result.tokens} token 超过上限 {DEFAULT_MAX_TOKENS}"
    )
    # 截断后要比原文短得多——否则"截断"没发生
    assert result.tokens < result.original_tokens


def test_truncation_marker_is_visible_and_actionable(tmp_path: Path) -> None:
    """**丢弃必须可见**：正文里要有标记，且标记必须给行动指引。

    三样都要断言：
    ① 有标记（模型知道内容不完整）；
    ② 说清丢了多少（否则它不知道差多远）；
    ③ 给出怎么拿到全文（只说"被截断"等于让模型干瞪眼）。
    """
    path = tmp_path / "AGENTS.md"
    path.write_text(_long_text(), encoding="utf-8")

    result = load_project_instructions(path)

    assert "已截断" in result.text
    assert "丢弃约" in result.text
    assert "read" in result.text          # 行动指引：告诉它怎么拿全文
    assert "AGENTS.md" in result.text     # 指向具体文件


def test_truncation_prefers_line_boundary(tmp_path: Path) -> None:
    """尽量切在行边界上。

    切在句子中间会产出一句**"看起来完整、实际断了"**的约定——
    那比明确少一整条更危险，因为模型会照着半句话执行。
    """
    path = tmp_path / "AGENTS.md"
    path.write_text(_long_text(), encoding="utf-8")

    body = load_project_instructions(path).text.split("\n\n[项目说明已截断", 1)[0]

    # 保留的部分必须由完整的行构成：最后一行要么是空行，要么是完整的条目
    last = body.rstrip("\n").split("\n")[-1]
    assert last == "" or last.endswith("。"), f"截断落在半行上：{last!r}"


def test_custom_budget_is_respected(tmp_path: Path) -> None:
    """上限可配——调用方可能想给得更紧（比如与技能索引共享余量）。"""
    path = tmp_path / "AGENTS.md"
    path.write_text(_long_text(), encoding="utf-8")

    result = load_project_instructions(path, max_tokens=50)

    assert result.truncated
    assert result.tokens <= 50


def test_unreadable_file_returns_error_not_exception(tmp_path: Path) -> None:
    """读不出来（比如路径是个目录）→ **返回 ``error``，不抛异常**。

    一份读不了的项目说明不该让整个会话起不来；但也不能静默——
    所以原因作为数据返回，由调用方决定要不要展示。
    """
    directory = tmp_path / "AGENTS.md"
    directory.mkdir()  # 同名目录：read_text 会抛 OSError(IsADirectoryError)

    result = load_project_instructions(directory)

    assert result.text == ""
    assert result.error != ""
    assert not result.loaded
    assert not result.found


def test_undecodable_file_returns_error(tmp_path: Path) -> None:
    """非 UTF-8 内容同样不抛——返回 error。"""
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"\xff\xfe\x00\x00\x80\x81 not utf-8 at all")

    result = load_project_instructions(path)

    assert result.text == ""
    assert "UnicodeDecodeError" in result.error


@pytest.mark.parametrize("budget", [10, 50, 200, DEFAULT_MAX_TOKENS])
def test_budget_is_never_exceeded(tmp_path: Path, budget: int) -> None:
    """**参数化扫一遍各档上限**：任何上限下都不得超。

    单点断言可能恰好避开边界；扫多个值才能把"标记占的 token 没扣掉"
    这类**恒定的偏差**逼出来（它会在小上限下格外明显）。
    """
    path = tmp_path / "AGENTS.md"
    path.write_text(_long_text(), encoding="utf-8")

    result = load_project_instructions(path, max_tokens=budget)

    assert result.tokens <= budget, f"上限 {budget} 被突破：{result.tokens}"
