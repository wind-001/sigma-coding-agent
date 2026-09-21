"""pytest 的路径注入。

**为什么需要这个文件**

    本仓库的包分两处：产品代码在 ``core/``（由 `.venv` 的 `.pth`
    以 editable 方式加进 ``sys.path``），而**测试夹具**在 ``tests/``、
    **评测代码**在 ``evals/``。后两者不在 ``testpaths`` 之外被 pytest 自动插入。

    ``tests/test_transcript_scenarios.py`` 需要 ``from evals.runner import ...``
    ——**刻意复用**评测侧同一份 ``run_scenario``，而不是在测试里另抄一遍。
    （两份实现意味着修一个忘另一个，本项目已在别处踩过。）

**为什么不用 `pytest-pythonpath` 插件的 `pythonpath = [...]` 配置项**

    那个选项属于一个**第三方插件**，本项目没装。写进 pyproject 也不生效——
    而"配置写了但没作用"正是本项目最忌讳的**名义约束**：
    它给人"路径已配好"的错觉，实际靠的是别的机制在兜。

**为什么用 conftest.py 而不是在每个测试文件里 `sys.path.insert`**

    conftest.py 是 pytest 的官方扩展点：它在此目录下的**所有**测试模块
    被导入之前执行。逐个文件插路径会让"在哪儿跑"影响"能不能导入"——
    那是个隐蔽的环境依赖，本项目已为此踩过一次（`.venv` 的 `.pth`
    把绝对路径写死，导致"复制到临时目录再跑"变成假实验）。

    注意这里**不引入任何新的运行期依赖**：只用 stdlib 的 ``pathlib`` 与 ``sys``。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

for _relative in ("tests", "evals", "core"):
    _path = str(REPO_ROOT / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)
