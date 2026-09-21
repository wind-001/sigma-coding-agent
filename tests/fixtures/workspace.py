"""构造回放场景用的**隔离工作区**。

**为什么不能让场景直接跑在仓库的 demo/ 上**

    两个原因，第二个更严重：

    1. **不可复现**：仓库 ``demo/`` 的内容会被人改，于是同一个场景
       在 A 机器上"工具成功"、在 B 机器上"文件不存在"——
       而报告里的失败会指向 loop，实际根因是工作区内容变了。
    2. **不可控**：`bash` 工具**有真实副作用**（能建目录、能删文件）。
       让回放场景对着仓库工作区写，等于让一个测试去改被测项目本身。
       这比"测试失败"坏得多。

    所以每个场景在 pytest 的 ``tmp_path`` 下拿一份**新建的、内容已知的**工作区。
    场景文件里出现的路径（``demo/greeting.py``）都由本模块保证存在。

**为什么文件内容是「故意有瑕疵」的**

    ``read_then_edit`` 这类场景要让 ``edit`` 真的改对东西。
    如果文件内容与 transcript 里的 ``old_string`` 对不上，
    ``edit`` 会守法地报错（它有"必须恰好匹配一次"的约束）——
    那会让场景变成"测 edit 的拒绝路径"，而不是"测完整链路"。
    所以本模块里的内容与 transcript 是同源写死的：
    **改这里就要改 transcript**，反之亦然。
"""

from __future__ import annotations

from pathlib import Path

GREETING_PY = """\"\"\"问候语。\"\"\"


def greet(name):
    return f'Helo, {name}!'
"""

CALC_PY = """\"\"\"最小计算模块。\"\"\"


def add(a, b):
    return a + b
"""

TEST_CALC_PY = """from demo.calc import add


def test_add():
    assert add(1, 1) == 2


def test_add_negative():
    assert add(-1, -1) == -2
"""

MAIN_PY = """\"\"\"演示入口。\"\"\"


def main() -> None:
    print("demo main")


if __name__ == "__main__":
    main()
"""

CONFIG_A = '{"schema_version": 1, "timeout": 5000, "name": "a"}\n'
CONFIG_B = '{"schema_version": 1, "timeout": 5, "name": "b"}\n'

_FILES: dict[str, str] = {
    "demo/greeting.py": GREETING_PY,
    "demo/calc.py": CALC_PY,
    "demo/main.py": MAIN_PY,
    "demo/tests/test_calc.py": TEST_CALC_PY,
    "demo/tests/__init__.py": "",
    "demo/__init__.py": "",
    "config/a.json": CONFIG_A,
    "config/b.json": CONFIG_B,
}


def write_fixture_workspace(root: Path) -> Path:
    """在 ``root`` 下铺一份固定内容的工作区，返回它的路径。

    幂等：已存在的文件被覆盖成同一内容。重复调用不会累积副作用。
    """
    for rel, content in _FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    # 场景里 `ls -1 demo` 要能看到东西。
    (root / "demo" / "out").mkdir(parents=True, exist_ok=True)
    return root


FIXTURE_FILES: tuple[str, ...] = tuple(_FILES)
"""本工作区提供的路径清单。

测试用它断言"transcript 里引用的文件都在这里"——
漏一个的话，症状是"工具报文件不存在"，看起来像工具坏了。
"""
