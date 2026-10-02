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

import re
from pathlib import Path

import pytest
from sigma.providers.tokens import estimate_text
from sigma.sessions.resources import (
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


# ==========================================================================
# 截断的两条不变量（2026-10-02 补）
#
# 这两条原先只是**隐含**在 test_truncation_prefers_line_boundary 里，
# 且那条测试**自己会漂**：它用 `split("\n\n[项目说明已截断", 1)` 取正文，
# 而标记有两档（长/短），短标记的文案是 `[...AGENTS.md 已截断，全文见 …]`
# —— 那个 split 匹配不到，于是 parts[0] 是**含标记的全文**，断言拿它比
# endswith 必红。实测：同一份文件，路径短（tmp_path 60 字符）选长标记→绿，
# 路径长（84 字符）选短标记→红。**测试的成败取决于临时目录名多长。**
#
# 根因不在测试，在 ``truncate_to_tokens``：它用 ``marker(kept=0)`` 预留
# 长度，但真正构造 marker 时用 ``estimate_text(body)``，正文回退到行边界后
# 这个值会变 ⇒ 标记长度变 ⇒ 余量常只剩个位数字符 ⇒ 整档标记被跳过。
# ⇒ 同一个 AGENTS.md 有时给详细标记、有时只给一句"全文见"，
#    **行为随文件路径长度漂移**。详细标记里才有"丢弃约 N token"与 read 指引。
#
# 所以下面两条把不变量显式钉住，且**不依赖标记文案**——
# 判据用"正文末行是否完整"而不是"找某段固定文字"。
# ==========================================================================

_MARKER_RE = re.compile(r"\n\n\[[^\]]*已截断")
_SHORT_MARKER_RE = re.compile(r"\n\[\.\.\.[^\]]*已截断")


def _split_body(text: str) -> tuple[str, bool]:
    """返回 (正文, 是否用长标记)。短标记时正文取标记前的全段。

    这里**不硬编码长标记文案**——文案是实现细节，改一次就红一次，
    而要保的不变量（正文不落半行、长标记不退化）与文案无关。
    短标记的识别用 ``[...`` 前缀，那是它的**语义**（省略号=信息缺失）。
    """
    match = _MARKER_RE.search(text)
    if match is not None:
        return text[: match.start()], True
    short = _SHORT_MARKER_RE.search(text)
    if short is not None:
        return text[: short.start()], False
    # 两种都匹配不上 = 没截断或格式变了。**不要静默当短标记**——
    # 那会让"标记格式改了"伪装成"检查通过"。
    raise AssertionError(f"文本里既没长标记也没短标记，格式可能变了：尾部 {text[-80:]!r}")


def test_truncation_body_never_lands_mid_line(tmp_path: Path) -> None:
    """**任何文件路径下，正文都不能落在半行上。**

    扫**很长的**路径而不是只测一两个点：真实触发条件是
    ``len(marker) 逼近预算``（标记里内嵌完整文件路径），路径越长标记越长。
    实测退化点在 pathlen≈2286 起（正文被挤到 7–29 字符，触到下限）。
    只测 60–140 那段会**整段漏掉**——门绿而缺陷在，正是最坏的情况。

    注入验证：
      · 还原 ``if newline > cap // 2`` → 本条红
      · 删掉 shrink 那一道 → 本条红（pathlen≥2286 那 3 档）
    """
    body_text = _long_text()
    root = tmp_path
    checked = 0
    # 几何要点：**每层目录名的长度决定能扫到多长的路径**，而退化点
    # 在 pathlen≈2286（正文被挤到 7–29 字符，触到 MIN_BODY_CHARS 下限）。
    # tmp_path 本身约 60–85 字符，每层 "/longdirname" 加 12 字符，
    # 200 层 ≈ 2485 ⇒ 覆盖退化点。层数少扫不到就是**门绿而缺陷在**。
    for pad in (0, 1, 2, 4, 8, 16, 32, 64, 100, 140, 180, 200):
        directory = root
        for _ in range(pad):
            directory = directory / "longdirname"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / AGENTS_MD_FILENAME
        path.write_text(body_text, encoding="utf-8")

        result = load_project_instructions(path)
        if not result.truncated:
            # 路径极长时原文没超预算属正常，但**绝不能**静默跳过这一档：
            # 门必须知道自己检查了什么。
            assert estimate_text(body_text) <= DEFAULT_MAX_TOKENS, (
                f"路径长 {len(str(path))} 时没触发截断，但原文已超预算 —— "
                "扫描失效"
            )
            continue
        body, used_long = _split_body(result.text)
        if not used_long:
            # 短标记档是**两难降级**（正文已被标记挤到装不下完整标记），
            # 那时"保住一行"与"保住全部正文"不可兼得，此处优先后者。
            # 它的退化单独由 test_truncation_marker_does_not_degrade_* 管，
            # 两条门各管一件事，不要混。
            continue
        last = body.rstrip("\n").split("\n")[-1]
        assert last == "" or last.endswith("。"), (
            f"路径长 {len(str(path))} 时截断落在半行上：{last!r}"
        )
        checked += 1
    assert checked >= 8, f"只测了 {checked} 档，扫描本身失效"


def test_truncation_marker_does_not_degrade_with_long_path(tmp_path: Path) -> None:
    """**文件路径变长时不能悄悄退到短标记。**

    短标记只有一句"[...已截断，全文见 …]"，既没说丢了多少 token，
    也没给 read 行动指引——"丢弃必须可见"这条纪律就没了。
    而它退化的触发条件是**环境**（路径多长），不是内容，
    也就是同一份 AGENTS.md 在不同机器上行为不同。
    """
    body_text = _long_text()
    root = tmp_path
    for pad in (0, 2, 4, 8, 16, 32, 64, 128, 200):
        directory = root
        for _ in range(pad):
            directory = directory / "longdirname"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / AGENTS_MD_FILENAME
        path.write_text(body_text, encoding="utf-8")

        result = load_project_instructions(path)
        if not result.truncated:
            continue
        _body, used_long = _split_body(result.text)
        assert used_long, (
            f"路径长 {len(str(path))} 时退到了短标记 —— "
            "丢弃量与 read 指引都丢了，'丢弃必须可见'失效"
        )


def test_truncation_respects_budget_after_shrinking(tmp_path: Path) -> None:
    """砍正文换标记档之后，**总量仍必须 ≤ 预算**。

    防止"为了保住详细标记"把预算撑破——那等于把 D4 主张悄悄改了。
    """
    body_text = _long_text()
    for pad in (0, 8, 64, 200):
        directory = tmp_path
        for _ in range(pad):
            directory = directory / "longdirname"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / AGENTS_MD_FILENAME
        path.write_text(body_text, encoding="utf-8")

        result = load_project_instructions(path)
        assert result.truncated
        assert result.tokens <= DEFAULT_MAX_TOKENS, (
            f"路径长 {len(str(path))} 时总量 {result.tokens} 超过预算 "
            f"{DEFAULT_MAX_TOKENS}"
        )
