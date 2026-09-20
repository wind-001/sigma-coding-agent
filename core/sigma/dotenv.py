"""极简 .env 读取。

为什么自己写，而不是引入 python-dotenv
    本项目在批次 1 已经定过一条取舍：**能不新增依赖就不新增**
    （当时拒掉 OpenAI SDK 的理由之一就是"httpx 早已在依赖里"）。
    这里的需求极窄——**只读几个 ``KEY=value``，不做变量展开、不做多行值**——
    二十行能写完，没有理由为此引一个包。

支持范围（写出来，其余一律不支持）
    ``KEY=value``             ✅
    ``KEY="value"``           ✅ 去掉外层引号
    ``# 注释`` / 空行          ✅ 跳过
    ``export KEY=value``      ✅ 兼容（手工建的 .env 里很常见）
    ``KEY=$OTHER``            ❌ **不做变量展开**
    多行值 / 转义序列           ❌ 不做

    不支持的部分**不静默带过**：值里出现未处理的 ``$`` 会发一条告警。
    "以为展开了、其实没有"会让 key 变成一个诡异的字符串，
    而症状是 **401 认证失败**——完全不指向根因。

**这个模块只负责读文件，不负责决定"从哪读"。**
优先级由调用方（``sigma.cli``）决定，见其 ``_resolve_config``。
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

ENV_VAR_NAME = "SIGMA_API_KEY"

# 用户级配置目录：**在 git 仓库之外**，所以它不会被误提交。
# 项目根的 .env 也可用（已加进 .gitignore），但仓库外的那个更安全。
USER_CONFIG_DIR = Path.home() / ".sigma"

CANDIDATE_FILES: tuple[Path, ...] = (
    USER_CONFIG_DIR / ".env",
    Path.cwd() / ".env",
)


class UnsupportedEnvSyntax(UserWarning):
    """``.env`` 里出现了本模块不处理的语法（目前只针对 ``$`` 展开）。"""


def parse_env_text(text: str) -> dict[str, str]:
    """解析 .env 文本。纯函数，方便单测。"""
    values: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            # 既不是注释也不是赋值——跳过而不是报错。
            # .env 常被手写，为一个畸形行中断整个启动不值得；
            # 但它不会静默消失：它不是 key，后面自然找不到。
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif "$" in value:
            warnings.warn(
                f"{key} 的值里含 '$'，但本模块**不做变量展开**。"
                "它会被当作字面字符——如果你的 key 确实带 $，那没问题；"
                "如果你期望它被展开，请改用环境变量。",
                UnsupportedEnvSyntax,
                stacklevel=2,
            )

        if key:
            values[key] = value

    return values


def load_env_file(path: Path) -> dict[str, str]:
    """读一个 .env。**文件不存在返回空 dict，不报错**——
    "没有配置文件"是正常状态，不是错误。"""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 读不动就当没有。真正的失败在下一步会以"缺少 API key"浮现，
        # 而那条提示会告诉用户怎么设。
        return {}
    return parse_env_text(text)


def resolve_api_key(
    *,
    explicit: str | None = None,
    candidates: Sequence[Path] | None = None,
) -> tuple[str | None, str]:
    """按优先级找 API key。

    优先级（**高 → 低**）：命令行 > 环境变量 > 用户级 .env > 项目级 .env

    "环境变量高于文件"是有意的：临时想换个 key 时
    ``export`` 一句就该生效，不必去改文件。

    返回 ``(key, 来源说明)``。来源说明用于打印——
    **用户需要知道这个 key 是从哪来的**，否则"改了 .env 但没生效"
    会变成一个纯靠猜的问题（因为环境变量赢了，而用户不知道）。
    """
    if explicit:
        return explicit, "命令行 --api-key"

    import os

    from_env = os.environ.get(ENV_VAR_NAME)
    if from_env:
        return from_env, f"环境变量 {ENV_VAR_NAME}"

    for path in candidates if candidates is not None else CANDIDATE_FILES:
        values = load_env_file(path)
        found = values.get(ENV_VAR_NAME)
        if found:
            return found, f"配置文件 {path}"

    return None, "未找到"
