"""repo map 的门槛(P1 收尾,G-P1RM-1..6)。

详规:``docs/plans/P1-repo-map-详规.md``。
设计要点回顾:确定性(进常驻区的前提)、空工作区零注入、cap 内截断可见、
开关关闭时连扫描都不发生、预算表键名同步。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sigma.config.resident_caps import CAPS, caps_sum
from sigma.providers.tokens import estimate_text
from sigma.sessions.context import SessionContext
from sigma.sessions import repo_map as repo_map_module
from sigma.sessions.repo_map import build_repo_map


def _make_workspace(tmp_path: Path) -> Path:
    """一个带 Python/非 Python/排除项的小工作区。"""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "calc.py").write_text(
        "class Calculator:\n"
        "    def add(self) -> int:\n"
        "        return 0\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "pkg" / "util.py").write_text(
        "def helper() -> str:\n    return ''\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    # 排除项:隐藏目录 + 构建产物 + 超大文件
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "x.py").write_text("def hidden(): pass\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "v.py").write_text("def venv_fn(): pass\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "n.py").write_text("def node_fn(): pass\n", encoding="utf-8")
    (tmp_path / ".sigma").mkdir()
    (tmp_path / ".sigma" / "state.py").write_text("def sigma_fn(): pass\n", encoding="utf-8")
    big = tmp_path / "src" / "pkg" / "big.py"
    big.write_text("x = 1\n" * 100_000, encoding="utf-8")  # > 64KB,只列名
    return tmp_path


def test_deterministic_byte_identical(tmp_path: Path) -> None:
    """G-P1RM-1:同一工作区两次构建逐字节相同(进常驻区的前提)。"""
    ws = _make_workspace(tmp_path)
    first = build_repo_map(ws)
    second = build_repo_map(ws)
    assert first == second
    assert first != ""


def test_excludes_and_symbols(tmp_path: Path) -> None:
    """G-P1RM-2:排除目录不出现;py 有顶层符号;非 py 与超大文件只有文件名。"""
    ws = _make_workspace(tmp_path)
    text = build_repo_map(ws)
    assert "src/pkg/calc.py" in text
    assert "class Calculator" in text and "def main" in text
    assert "src/pkg/util.py" in text and "def helper" in text
    assert "README.md" in text
    # 排除项:目录与其中的文件都不出现;隐藏文件(.env)也不进地图
    for forbidden in (".git", ".venv", "node_modules", ".sigma", "hidden", "venv_fn", "node_fn", "sigma_fn"):
        assert forbidden not in text
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    assert ".env" not in build_repo_map(tmp_path)
    # 超大文件只列名、不提符号(big.py 里只有 x=1,本来也没符号;换真符号文件验证)
    big2 = ws / "src" / "pkg" / "huge.py"
    huge_body = "def real_symbol(): pass\n" + ("y = 2\n" * 40_000)
    big2.write_text(huge_body, encoding="utf-8")
    text2 = build_repo_map(ws)
    assert "src/pkg/huge.py" in text2
    assert "real_symbol" not in text2  # 超 64KB 不读内容


def test_cap_truncation_visible(tmp_path: Path) -> None:
    """G-P1RM-3:超 cap 输出仍 ≤ cap,且截断标记在正文里可见。"""
    ws = tmp_path
    for i in range(60):
        d = ws / f"mod{i:02d}"
        d.mkdir()
        (d / "m.py").write_text(
            f"class Thing{i}:\n    def method_a(self):\n        pass\n"
            f"    def method_b(self):\n        pass\n",
            encoding="utf-8",
        )
    out = build_repo_map(ws, max_tokens=500)
    assert estimate_text(out) <= 500
    assert "上限" in out or "省略" in out or "truncat" in out.lower()
    assert out != ""  # 截断不得退化为空串(可见性兜底)


def test_hard_truncation_nonempty(tmp_path: Path) -> None:
    """硬截断分支(两段降级都装不下)必须带标记非空——sigma 仓库自样本即此形态。"""
    from sigma.providers.tokens import estimate_text as est

    ws = tmp_path
    for i in range(120):
        d = ws / f"deep{i:03d}" / "sub"
        d.mkdir(parents=True)
        (d / "m.py").write_text(f"def f{i}():\n    pass\n", encoding="utf-8")
    out = build_repo_map(ws, max_tokens=500)
    assert out != ""
    assert est(out) <= 500
    assert "已截断" in out


def test_empty_workspace_zero_injection(tmp_path: Path) -> None:
    """G-P1RM-4:空工作区返回 "";SessionContext 常驻区与不传 repo_map 逐字节一致。"""
    assert build_repo_map(tmp_path) == ""
    ctx_without = SessionContext(system_prompt="SP", tools_schema=[], clock=lambda: "T")
    ctx_with_empty = SessionContext(
        system_prompt="SP", tools_schema=[], clock=lambda: "T", repo_map=""
    )
    assert ctx_without.resident_text() == ctx_with_empty.resident_text()
    assert ctx_without.fingerprint == ctx_with_empty.fingerprint


def test_repo_map_enters_resident_region(tmp_path: Path) -> None:
    """地图进了常驻区就会被指纹与预算两道断言覆盖——验证接线本身。"""
    ws = _make_workspace(tmp_path)
    text = build_repo_map(ws)
    ctx = SessionContext(
        system_prompt="SP", tools_schema=[], clock=lambda: "T", repo_map=text
    )
    assert "src/pkg/calc.py" in ctx.resident_text()
    # 同一文本 → 指纹稳定(两次构造指纹一致,即"逐字节稳定"在上下文层的投影)
    ctx2 = SessionContext(
        system_prompt="SP", tools_schema=[], clock=lambda: "T", repo_map=text
    )
    assert ctx.fingerprint == ctx2.fingerprint


def test_disabled_flag_skips_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G-P1RM-5:--no-repo-map 时连目录遍历都不发生(monkeypatch 计数,G884 同款)。"""
    import sigma.sdk as sdk_module
    from sigma.providers.fake import FakeProvider
    from sigma.sessions.tree import SessionTree

    calls = 0
    real = repo_map_module.build_repo_map

    def counting(root: Path, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return real(root, **kwargs)

    monkeypatch.setattr(sdk_module, "build_repo_map", counting)
    ws = _make_workspace(tmp_path)

    common = dict(
        provider=FakeProvider.from_rounds([]),
        workspace_root=ws,
        model="fake",
        session_id="rm-test",
    )
    sdk_module.InteractiveSession(**common)  # 默认开:扫一次
    assert calls == 1
    sdk_module.InteractiveSession(**common, enable_repo_map=False)  # 关:零扫描
    assert calls == 1


def test_caps_key_and_sum() -> None:
    """G-P1RM-6:预算表键名同步——"repo map" 具名占用 500;分项和 == 总额(D4 v3:5750)。"""
    assert "repo map" in CAPS
    assert CAPS["repo map"] == 500
    assert "具名预留(repo map 等)" not in CAPS
    # D4 v3(团队任务入场):可选栏 1250→1500、总额 5500→5750(实测 1441)
    assert caps_sum() == 5750
