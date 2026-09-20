"""``.env`` 读取与密钥查找的测试。

**测试范围在 2026-09-20 缩过一次**，原因值得记下来。

第一版是自写解析器，配了 18 个用例（引号、注释、``export`` 前缀、``$`` 告警……）。
改用 python-dotenv 后，其中 **10 个用例（整个 ``TestParseEnvText``）变成了
"在测试 dotenv 库本身"**——它们对**我的**代码没有任何断言价值，整组删掉。

**用例数 18 → 9，不是覆盖变差，而是不再测别人的代码。**

留下的三类都是我自己的逻辑：

1. ``load_env_file`` 的封装行为（文件不存在、None 值过滤）；
2. ``resolve_api_key`` 的**优先级**——它是"改了 .env 却没生效"这类问题的
   唯一解释来源，不能靠读代码推断；
3. ``USER_CONFIG_DIR`` 必须在仓库之外。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sigma.dotenv import (
    ENV_VAR_NAME,
    USER_CONFIG_DIR,
    load_env_file,
    resolve_api_key,
)


class TestLoadEnvFile:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """"没有配置文件"是正常状态，不是错误。"""
        assert load_env_file(tmp_path / "nope.env") == {}

    def test_reads_utf8_value(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("A=中文值\n", encoding="utf-8")
        assert load_env_file(path) == {"A": "中文值"}

    def test_values_are_never_none(self, tmp_path: Path) -> None:
        """返回的 dict 里不允许出现 ``None`` 值。

        ``dotenv_values`` 对某些写法（缺值的键）会返回 ``None``，
        而返回类型声明的是 ``dict[str, str]``。类型检查拦不住这种运行时差异，
        所以由 :func:`load_env_file` 过滤，由这条用例钉住。
        """
        path = tmp_path / ".env"
        path.write_text("A=1\nB\nC=\n", encoding="utf-8")
        values = load_env_file(path)
        assert all(isinstance(v, str) for v in values.values())
        assert values.get("A") == "1"


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
        # 来源必须指出具体文件路径 —— 这是排查"改了没生效"的关键信息
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

        这条锁的是 ``CANDIDATE_FILES`` 的语义——用户级 ``~/.sigma/.env``
        排在项目级 ``./.env`` 之前（仓库外更安全）。
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
    若哪天有人把它改成项目内路径，这条用例会失败。
    """
    assert USER_CONFIG_DIR == Path.home() / ".sigma"
    assert not USER_CONFIG_DIR.is_relative_to(Path.cwd())
