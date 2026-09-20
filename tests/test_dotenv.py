"""``.env`` 解析与密钥查找的测试。

重点不在"能不能解析"——而在**优先级**。
它是"我改了 .env 但没生效"这类问题的唯一解释来源，
所以优先级必须被钉死，不能靠读代码去推断。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from sigma.dotenv import (
    ENV_VAR_NAME,
    UnsupportedEnvSyntax,
    load_env_file,
    parse_env_text,
    resolve_api_key,
)


class TestParseEnvText:
    def test_basic_pair(self) -> None:
        assert parse_env_text("A=1\n") == {"A": "1"}

    def test_comments_and_blank_lines_are_skipped(self) -> None:
        assert parse_env_text("# 注释\n\n   \nA=1\n# 尾注释\n") == {"A": "1"}

    def test_double_quotes_are_stripped(self) -> None:
        assert parse_env_text('A="x y"\n') == {"A": "x y"}

    def test_single_quotes_are_stripped(self) -> None:
        assert parse_env_text("A='x y'\n") == {"A": "x y"}

    def test_export_prefix_is_tolerated(self) -> None:
        assert parse_env_text("export A=1\n") == {"A": "1"}

    def test_value_may_contain_equals(self) -> None:
        """按第一个 ``=`` 切分。

        key 的 base64 里可能有 ``=``；若按最后一个 ``=`` 切，值会被截断。
        """
        assert parse_env_text("A=a=b=c\n") == {"A": "a=b=c"}

    def test_malformed_line_is_skipped_not_fatal(self) -> None:
        """.env 常被手写，为一个畸形行中断整个启动不值得。"""
        assert parse_env_text("这不是赋值\nA=1\n") == {"A": "1"}

    def test_dollar_sign_warns_but_keeps_value(self) -> None:
        """不做变量展开 —— 但**要告警**。

        "以为展开了、其实没有"会让 key 变成一个诡异的字符串，
        而症状是 401，完全不指向根因。
        """
        with pytest.warns(UnsupportedEnvSyntax):
            payload = parse_env_text("A=$HOME\n")
        assert payload == {"A": "$HOME"}

    def test_quoted_dollar_does_not_warn(self) -> None:
        """带引号的值走"去引号"分支，不触发 ``$`` 告警。

        引号本身就是"这是字面量"的表态，再告警是噪音。
        """
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            payload = parse_env_text('A="$HOME"\n')
        assert payload == {"A": "$HOME"}

    def test_empty_key_is_dropped(self) -> None:
        assert parse_env_text("=nokey\nA=1\n") == {"A": "1"}


class TestLoadEnvFile:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """**"没有配置文件"是正常状态，不是错误。**"""
        assert load_env_file(tmp_path / "nope.env") == {}

    def test_reads_utf8(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("A=中文值\n", encoding="utf-8")
        assert load_env_file(path) == {"A": "中文值"}


class TestResolveApiKey:
    def test_explicit_wins_over_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_VAR_NAME, "from-env")
        key, source = resolve_api_key(explicit="from-cli", candidates=[])
        assert key == "from-cli"
        assert "命令行" in source

    def test_env_wins_over_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """环境变量高于文件 —— 临时换 key 时 ``export`` 一句就该生效。"""
        monkeypatch.setenv(ENV_VAR_NAME, "from-env")
        env_file = tmp_path / ".env"
        env_file.write_text(f"{ENV_VAR_NAME}=from-file\n", encoding="utf-8")

        key, source = resolve_api_key(explicit=None, candidates=[env_file])
        assert key == "from-env"
        assert "环境变量" in source

    def test_file_used_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)
        env_file = tmp_path / ".env"
        env_file.write_text(f"{ENV_VAR_NAME}=from-file\n", encoding="utf-8")

        key, source = resolve_api_key(explicit=None, candidates=[env_file])
        assert key == "from-file"
        # 来源必须能指出具体的文件路径
        assert str(env_file) in source

    def test_nothing_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)
        key, source = resolve_api_key(explicit=None, candidates=[])
        assert key is None
        assert source == "未找到"

    def test_candidate_order_defines_priority(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """顺序即优先级，第一个命中的胜出。

        这条用例锁的是 ``CANDIDATE_FILES`` 的语义——
        用户级 ``~/.sigma/.env`` 排在项目级 ``./.env`` 之前（仓库外更安全）。
        """
        monkeypatch.delenv(ENV_VAR_NAME, raising=False)
        first = tmp_path / "first.env"
        second = tmp_path / "second.env"
        first.write_text(f"{ENV_VAR_NAME}=first\n", encoding="utf-8")
        second.write_text(f"{ENV_VAR_NAME}=second\n", encoding="utf-8")

        key, _ = resolve_api_key(explicit=None, candidates=[first, second])
        assert key == "first"


def test_user_config_is_outside_repo() -> None:
    """``~/.sigma/.env`` 必须在仓库之外。

    这是它比项目根 ``.env`` 更安全的**唯一**理由——
    如果哪天有人把它改成项目内路径，这条用例会失败。
    """
    from sigma.dotenv import USER_CONFIG_DIR

    assert USER_CONFIG_DIR == Path.home() / ".sigma"
    assert not USER_CONFIG_DIR.is_relative_to(Path.cwd())
