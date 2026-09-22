"""工具共用的路径解析。

两个函数，**语义刻意不同**

    - :func:`resolve_path`：读路径。**不约束**——read 可以读工作区外的东西，
      这是合法需求（architecture 6.3 明写"L1 只约束写"）。
    - :func:`resolve_write_path`：写路径（P3-批次1 新增）。**resolve 后必须仍在
      workspace_root 之内**，否则抛 :class:`PathEscapesWorkspace`。

为什么写路径必须 `resolve()` 之后再比（而不是 `is_relative_to` 一下了事）
    `Path("a/../../etc/passwd").is_relative_to(root)` 在**文本层面**就出界了，看着没事；
    而 `root/link` 若是指向外部的符号链接，文本上完全"在工作区内"，
    **却会真的写到外面去**。

    这个模块原来特意没做这件事，理由写在旧版 docstring 里：
    "顺手加一句 is_relative_to 是**名义防护**——它挡不住符号链接，更挡不住 bash，
    却会让代码看起来已经有保护了"。**那句判断是对的**，所以现在做的是正解：
    `resolve(strict=False)` 把符号链接与 `..` 一起解开，再用 `normcase` 处理
    Windows 的大小写不敏感（`C:\\Users` 与 `c:\\users` 文本比较会漏判）。

`strict=False` 的选择
    写目标常常**还不存在**（新建文件）。`strict=True` 会对不存在的路径抛错，
    于是"新建文件"这条最普通的路径反而走不通——那是把边界做成了障碍。
    写路径是例外，读路径仍不用 resolve（避免"用户说的路径"与"真实指向"混淆）。

这条边界管不到什么（**说清楚，不假装**）
    它只管**工具参数里的路径**。`bash` 里 `rm -rf /` 照样能跑——字符串匹配挡不住它，
    那一层的兜底是 L2（影子 git checkpoint，可回滚）与超时，不是这里。
"""

from __future__ import annotations

import os
from pathlib import Path

from sigma_agent.types import ToolContext


class PathEscapesWorkspace(ValueError):
    """写路径解析后落在 ``workspace_root`` 之外（L1 边界）。

    继承 ``ValueError`` 而不是 ``RuntimeError``：这是"调用方给的值非法"，
    要模型改参数重试，不是"运行期状态坏了"。
    """


def _norm(path: Path) -> str:
    """比较用的规范化字符串：解析过的大小写 + 平台分隔符。"""
    return os.path.normcase(str(path))


def _same_or_inside(resolved: Path, root: Path) -> bool:
    """``resolved`` 是否就是 ``root`` 或落在它里面。

    不用 ``Path.is_relative_to``：它做的是**纯文本**比较，
    而 Windows 上同一个目录可以有多种大小写写法（`C:\\Users` / `c:\\users`），
    于是同一条路径会被判成"越界"——**误拦比漏拦更糟**（模型改不动，只能重试）。
    """
    inner = _norm(resolved)
    outer = _norm(root).rstrip("\\/")
    return inner == outer or inner.startswith(outer + os.sep)


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    """把工具参数里的路径解析成绝对路径（**读路径，不做边界检查**）。

    绝对路径原样返回；相对路径基于 ``ctx.workspace_root`` 拼接。

    不用 ``Path.resolve()``：它会解析符号链接，且对不存在的路径行为随版本变动。
    读工具需要的是"用户说的那个路径在哪儿"，不是"它真实指向哪儿"。
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    return ctx.workspace_root / candidate


def resolve_write_path(ctx: ToolContext, raw: str) -> Path:
    """把写路径解析成绝对路径，并强制它在工作区内（L1）。

    越界时抛 :class:`PathEscapesWorkspace`——由调用工具转成 ``is_error`` 结果，
    **不穿透到 loop**（工具抛异常等于把纠错能力关掉，详规 3.6）。
    """
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ctx.workspace_root / candidate

    resolved = candidate.resolve(strict=False)
    root = ctx.workspace_root.resolve(strict=False)
    if not _same_or_inside(resolved, root):
        raise PathEscapesWorkspace(
            f"路径越界：{resolved}\n"
            f"  工作区根：{root}\n"
            "写操作只允许落在工作区内（L1 硬边界）。三种做法任选一种：\n"
            "  1) 把输出写进工作区里的路径；\n"
            "  2) 换一个工作区根（CLI 的 --workspace）；\n"
            "  3) 若确实需要写到外面，那是本 harness 明确不做的事——请人工执行。\n"
            "（读操作不受此限制：read 可以读工作区外的文件。）"
        )
    return resolved
