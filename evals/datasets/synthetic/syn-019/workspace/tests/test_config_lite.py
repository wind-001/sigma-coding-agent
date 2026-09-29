"""``config_lite`` 的行为测试。

运行方式（**必须 ``python -m pytest``**：它把 CWD 放进 ``sys.path``，
裸 ``pytest`` 不会，于是 ``import config_lite`` 会失败）：

    python -m pytest tests/ -q
"""

from __future__ import annotations

from config_lite import get, parse

SAMPLE = """\
# 服务基本配置
name = worker
port = 8080
debug = on
; 另一种注释风格
[db]
url = postgres://localhost/app
pool_size = 10
timeout = -1
verbose = Yes
twilight = OFF

[ cache ]
ttl = 300
"""

#: SAMPLE 解析完成后应有的完整形态。
FULL: dict[str, dict[str, object]] = {
    "": {"name": "worker", "port": 8080, "debug": True},
    "db": {
        "url": "postgres://localhost/app",
        "pool_size": 10,
        "timeout": -1,
        "verbose": True,
        "twilight": False,
    },
    "cache": {"ttl": 300},
}


# --- 节与裁剪 ----------------------------------------------------------------


def test_global_section_before_any_header() -> None:
    """任何节头之前的键属于全局节 ``""``。"""
    config = parse(SAMPLE)
    assert get(config, "", "name") == "worker"


def test_section_header_is_stripped() -> None:
    config = parse(SAMPLE)
    assert "cache" in config


def test_keys_are_stripped() -> None:
    config = parse(SAMPLE)
    assert "port" in config[""]


def test_comments_are_skipped() -> None:
    config = parse(SAMPLE)
    keys = [key for section in config.values() for key in section]
    assert all("#" not in key and ";" not in key for key in keys)


# --- 类型强转 ----------------------------------------------------------------


def test_int_coercion_positive() -> None:
    assert get(parse(SAMPLE), "", "port") == 8080


def test_int_coercion_negative() -> None:
    """负整数也是整数。"""
    assert get(parse(SAMPLE), "db", "timeout") == -1


def test_bool_true_variants() -> None:
    config = parse(SAMPLE)
    assert get(config, "", "debug") is True
    assert get(config, "db", "verbose") is True


def test_bool_false_variants() -> None:
    config = parse(SAMPLE)
    assert get(config, "db", "twilight") is False


# --- get 的缺省语义 ------------------------------------------------------------


def test_missing_section_returns_default() -> None:
    assert get(parse(SAMPLE), "queue", "max", 7) == 7


def test_missing_key_returns_default() -> None:
    assert get(parse(SAMPLE), "db", "user", "postgres") == "postgres"


# --- 整体 --------------------------------------------------------------------


def test_value_may_contain_equals() -> None:
    """只按第一个 ``=`` 切分，value 里可以再有 ``=``。"""
    config = parse("conn = User=user;Password=a=b\n")
    assert get(config, "", "conn") == "User=user;Password=a=b"


def test_parse_full_document() -> None:
    assert parse(SAMPLE) == FULL
