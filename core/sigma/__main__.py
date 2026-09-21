"""``python -m sigma`` 的入口。

为什么要留这一个入口
    console script（``sigma.exe``）是 pip 在安装时生成的，venv 重建、
    换机器、或者用 ``--no-scripts`` 安装时都可能没有它；
    而 ``python -m sigma`` 只要解释器找得到包就能跑。

    两条路径通向**同一个** ``cli.main``——启动方式只是外壳，逻辑只有一处。
    这与 ``sdk.py`` 那句"三处组装会各自漂移"是同一条纪律：
    **入口可以有多个，组装只能有一个。**
"""

from __future__ import annotations

import sys

from sigma.cli import main

if __name__ == "__main__":
    sys.exit(main())
