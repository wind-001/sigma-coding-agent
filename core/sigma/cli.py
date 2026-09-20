"""sigma 的命令行入口。

P0 阶段只有一个占位实现：它存在的作用是让 ``[project.scripts]``
里的 ``sigma = "sigma.cli:main"`` 可解析，从而验证打包配置是对的。
真正的 REPL 与一次性模式属于 P1。
"""

from __future__ import annotations

import sys

from sigma import __version__


def main() -> int:
    """CLI 入口。返回进程退出码。"""
    print(f"sigma {__version__} —— P0 骨架，尚未实现。")
    print("当前可用：lint-imports / mypy / pytest 三道自检。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
