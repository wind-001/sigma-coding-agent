"""创建一个演示工作区，供手动试用 sigma。

为什么是一个 Python 脚本，而不是几行 shell 命令
    Windows 上不同 shell 的写文件编码不一致：
    **PowerShell 5.1 的 ``Out-File -Encoding utf8`` 会写入 BOM**，
    于是 ``read`` 工具读到的第一行带一个不可见字符，
    而 ``input.txt`` 里的数字就"不是数字"了——**排查方向会完全跑偏**。
    （``utf8NoBOM`` 是 PowerShell 7+ 才有的选项。）

    用 Python 明确指定 ``encoding="utf-8"`` 则跨 shell 一致。

用法::

    .venv/Scripts/python.exe scripts/make_demo_workspace.py

默认在项目根下创建 ``demo/``。**已存在时不覆盖**——
避免清掉你正在手动试验的内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

# 打印解析出的仓库根路径：项目有过一次 parent.parent 解析错的事故，
# 让它一眼可见比事后排查便宜得多（见 P1-批次1.5-详规 第 6 节）。
REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "demo"

INPUT_CONTENT = "41\n"

TASKS_FILE = """# 可以拿来试的任务（复制其中一条到 -p 后面）
#
# 1) 读取数字、加 5、写回
#    读取 input.txt 里的数字，加 5 后写入 result.txt（内容只有数字）
#
# 2) 让模型自己发现文件在哪
#    工作区里有个文件存了一个整数，找到它，把数字乘 2 写入 doubled.txt
#
# 3) 故意制造失败，观察它会不会自己纠错
#    读取 not_exists.txt 并把内容写入 out.txt
#    （文件不存在时 read 会返回 is_error，模型应当向你报告而不是崩溃）
#
# 4) 观察多文件读取
#    读取 a.txt 和 b.txt，把两个文件的内容按 a 在前、b 在后合并写入 merged.txt
"""


def main() -> int:
    print(f"仓库根目录：{REPO_ROOT}")
    print(f"目标工作区：{TARGET}")
    print()

    if TARGET.exists():
        print("工作区已存在，**不做任何改动**（避免清掉你正在试的内容）。")
        print(f"  若要重建：先手动删掉 {TARGET}")
        _list(TARGET)
        return 0

    TARGET.mkdir(parents=True)
    # 明确 utf-8、无 BOM —— 这是本脚本存在的全部理由
    (TARGET / "input.txt").write_text(INPUT_CONTENT, encoding="utf-8")
    (TARGET / "a.txt").write_text("alpha\n", encoding="utf-8")
    (TARGET / "b.txt").write_text("beta\n", encoding="utf-8")
    (TARGET / "任务清单.md").write_text(TASKS_FILE, encoding="utf-8")

    print("已创建：")
    _list(TARGET)
    print()
    print("接下来：")
    print()
    print("  # Git Bash")
    print("  export SIGMA_API_KEY=sk-你的key")
    print("  .venv/Scripts/sigma.exe -p \\")
    print('    "读取 input.txt 里的数字，加 5 后写入 result.txt（内容只有数字）" \\')
    print("    --workspace ./demo")
    print()
    print("  # PowerShell")
    print('  $env:SIGMA_API_KEY="sk-你的key"')
    print('  .\\.venv\\Scripts\\sigma.exe -p "读取 input.txt 里的数字，加 5 后写入 result.txt（内容只有数字）" --workspace .\\demo')
    print()
    return 0


def _list(directory: Path) -> None:
    for item in sorted(directory.iterdir()):
        if item.is_file():
            size = item.stat().st_size
            print(f"  {item.name:<16} {size:>4} 字节")


if __name__ == "__main__":
    sys.exit(main())
