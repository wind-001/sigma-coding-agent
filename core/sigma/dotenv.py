"""本地密钥读取。

用 python-dotenv，而不是自己写解析器——这里有一段需要交代的判断修正。

**我第一版是自己写的**（约 20 行），理由是"批次 1 定过能不新增依赖就不新增"。
**那是把原则用错了地方。**

那条原则的适用条件是"新增依赖会带来实质代价"——当时拒掉 OpenAI SDK，
是因为它**藏起了那个批次要验证的协议细节**（SSE 分块边界、``[DONE]``）。
python-dotenv 不满足这个条件：纯 Python、零传递依赖，
而 ``.env`` 解析**不是本项目的核心能力**，不存在"用别人的库就失去验证能力"的问题。

**代价核算更直接**：自写版本 20 行代码 + **18 个测试**去验证自己的边界处理，
覆盖面仍不如成熟库（引号转义、多行值、变量展开当时都没做）。
**那个成本已经超过依赖本身。**

教训的通用形式：**引用一条原则之前，先检查它的适用条件是否成立。**
"能不新增依赖就不新增"不等于"永远不新增依赖"，
就像"配置跑绿 ≠ 约束生效"也不等于"配置没用"。

**本模块只负责读文件，不负责决定"从哪读"。**
优先级由调用方（``sigma.cli``）决定，见其 ``_resolve_config``。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import dotenv_values

if TYPE_CHECKING:
    from collections.abc import Sequence

ENV_VAR_NAME = "SIGMA_API_KEY"

#: 联网搜索（Tavily）的密钥变量。与 SIGMA_API_KEY 共用同一套解析优先级。
TAVILY_ENV_VAR = "TAVILY_API_KEY"

# 用户级配置目录：**在 git 仓库之外**，所以它不会被误提交。
# 项目根的 .env 也可用（已加进 .gitignore），但仓库外的那个更安全。
USER_CONFIG_DIR = Path.home() / ".sigma"

CANDIDATE_FILES: tuple[Path, ...] = (
    USER_CONFIG_DIR / ".env",
    Path.cwd() / ".env",
)


def load_env_file(path: Path) -> dict[str, str]:
    """读一个 ``.env``。

    **文件不存在返回空 dict，不报错**——"没有配置文件"是正常状态，不是错误。

    解析交给 ``dotenv_values``。它对缺值的键返回 ``None``，这里过滤掉——
    否则下游会拿到 ``None`` 而不是 ``str``，而类型检查又拦不住运行时。
    """
    if not path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if value is not None
    }


def resolve_api_key(
    *,
    explicit: str | None = None,
    candidates: Sequence[Path] | None = None,
    var_name: str = ENV_VAR_NAME,
) -> tuple[str | None, str]:
    """按优先级找 API key。

    优先级（**高 → 低**）：命令行 > 环境变量 > 用户级 ``.env`` > 项目级 ``.env``

    "环境变量高于文件"是有意的：临时想换个 key 时
    ``export`` 一句就该生效，不必去改文件。

    返回 ``(key, 来源说明)``。来源说明用于打印——
    **用户需要知道这个 key 是从哪来的**，否则"改了 ``.env`` 但没生效"
    会变成一个纯靠猜的问题（因为环境变量赢了，而用户不知道）。
    """
    if explicit:
        return explicit, "命令行 --api-key"

    import os

    from_env = os.environ.get(var_name)
    if from_env:
        return from_env, f"环境变量 {var_name}"

    for path in candidates if candidates is not None else CANDIDATE_FILES:
        found = load_env_file(path).get(var_name)
        if found:
            return found, f"配置文件 {path}"

    return None, "未找到"


def resolve_tavily_api_key(
    *,
    explicit: str | None = None,
    candidates: Sequence[Path] | None = None,
) -> tuple[str | None, str]:
    """联网搜索的密钥。

    独立一个函数而不是在调用处传 var_name：**"用哪个变量名"是这里的事**，
    调用方只该问"有没有联网搜索的 key"。这与本模块的分界一致——
    它管"读什么变量"，不管"从哪个目录优先读"（那是 cli 的事）。
    """
    return resolve_api_key(
        explicit=explicit, candidates=candidates, var_name=TAVILY_ENV_VAR
    )
