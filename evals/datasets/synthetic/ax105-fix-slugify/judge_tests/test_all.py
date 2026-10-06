from slugify import slugify


def test_hello_world() -> None:
    assert slugify('Hello,  World!') == 'hello-world'


def test_collapse_and_strip() -> None:
    assert slugify('--a--b--') == 'a-b'


def test_cjk_kept() -> None:
    assert slugify('  中文 标题! ') == '中文-标题'


def test_empty() -> None:
    assert slugify('   ') == ''


def test_underscore_is_sep() -> None:
    assert slugify('a_b') == 'a-b'


def test_digits_kept() -> None:
    assert slugify('v2.0 release') == 'v2-0-release'
