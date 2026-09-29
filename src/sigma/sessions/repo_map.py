"""repo map(P1 收尾):工作区结构快照,常驻区"具名预留 500"的具名消费。

**它是什么、不是什么**
    repo map = 会话启动时对工作区的**一次性快照**:文件清单 + Python 顶层
    符号(类/函数名),渲染成文本进常驻区,让模型第一轮 grep 就有方向。
    它**不是**实时的:会话中文件的变化不在地图里——那不是缺陷,
    是 D4(常驻区逐字节稳定)的直接推论,与记忆索引的"会话内冻结"(G884)
    同一条纪律。文件变了,下个会话可见。

**为什么住 ``sigma/sessions``**
    它喂 :class:`~sigma.sessions.context.SessionContext`,与
    ``resources.py``(AGENTS.md 读取)、记忆索引(产品壳扫好传入)同族:
    **文本进上下文,扫描策略归产品壳**。ast 提符号是纯 stdlib——
    零新依赖(不引 tree-sitter,极简判据)。

**确定性(逐字节稳定的地基)**
    同一工作区两次构建结果逐字节相同:文件按 posix 路径字符串排序、
    符号按源码出现序、无时间戳。这是它能进常驻区的前提——
    进了指纹断言(G26)管辖的东西,不允许任何不确定性。

**消费纪律(5.1.0)**
    吃的是 resident_caps 里具名预留的 500 token:cap 内装不下时先降级
    (去符号明细),仍超则 :func:`truncate_to_tokens` 硬截——
    **截断标记拼进正文**,模型知道自己看到的不是全部(批次 8 纪律)。

详规:``docs/plans/P1-repo-map-详规.md``(门槛 G-P1RM-1..6)。
"""

from __future__ import annotations

import ast
from pathlib import Path

from sigma.providers.tokens import estimate_text, truncate_to_tokens

#: 除点开头目录外,额外跳过的非隐藏目录(构建产物/依赖,永远不是地图素材)。
SKIP_DIRS: frozenset[str] = frozenset(
    {"node_modules", "__pycache__", "build", "dist", "target"}
)

#: 只对 Python 提取符号(ast 只懂 Python;其他语言列文件名——显式不做,见详规 §3)。
SYMBOL_SUFFIXES: frozenset[str] = frozenset({".py"})

#: 超过这个大小的文件只列名、不读内容提符号(读它又慢又可能不是文本)。
MAX_FILE_BYTES: int = 64 * 1024

#: 常驻区分项 cap(resident_caps.CAPS["repo map"],两处数值一致,G885 守护分项和)。
DEFAULT_MAX_TOKENS: int = 500

_HEADER = "工作区结构(会话启动时快照;文件变化下个会话可见):"


def _is_skipped_dir(name: str) -> bool:
    """点开头目录全部跳过(覆盖 .git/.sigma/.venv/.pytest_cache/...),再加固定清单。"""
    return name.startswith(".") or name in SKIP_DIRS


def _collect_files(workspace_root: Path) -> list[Path]:
    """确定性收集:先序遍历,按 posix 路径字符串排序。

    点开头的**文件**同样跳过(.env 是密钥、.gitignore 是配置——
    它们不该出现在给模型的地图里,这也是安全上的顺手防御)。
    """
    files: list[Path] = []
    stack = [workspace_root]
    while stack:
        current = stack.pop()
        for entry in sorted(current.iterdir(), key=lambda p: p.name, reverse=True):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                files.append(entry)
    files.sort(key=lambda p: p.relative_to(workspace_root).as_posix())
    return files


def _python_symbols(path: Path) -> list[str]:
    """顶层 class/def 名,按源码出现序;解析失败给空表不报错(坏文件不挡地图)。"""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError, ValueError, UnicodeError):
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(f"def {node.name}")
        elif isinstance(node, ast.ClassDef):
            names.append(f"class {node.name}")
    return names


def _render(
    workspace_root: Path, files: list[Path], *, with_symbols: bool
) -> str:
    """渲染 stage:with_symbols=True 全量(路径 — 符号),False 只列路径。"""
    lines: list[str] = [_HEADER, ""]
    for path in files:
        rel = path.relative_to(workspace_root).as_posix()
        if with_symbols and path.suffix in SYMBOL_SUFFIXES:
            symbols = _python_symbols(path)
            if symbols:
                shown = ", ".join(symbols[:8])
                extra = len(symbols) - 8
                if extra > 0:
                    shown += f", …(+{extra})"
                lines.append(f"- {rel} — {shown}")
                continue
        lines.append(f"- {rel}")
    return "\n".join(lines)


def _truncation_marker(original_tokens: int, kept_tokens: int) -> str:
    """硬截断时的可见标记(truncate_to_tokens 的 markers 回调)。

    **必须传 markers**:tokens.py 的退化分支是"候选标记全装不下 → 返回空文本,
    可见性由调用方兜住"——repo map 的兜法就是给一个短标记,
    让"模型看到的是残缺地图"这件事本身留在正文里。
    """
    return (
        f"\n\n(repo map 已截断:完整约 {original_tokens} token,此处保留 "
        f"{kept_tokens} token;需要完整清单时用 bash 遍历目录)"
    )


def build_repo_map(
    workspace_root: Path, *, max_tokens: int = DEFAULT_MAX_TOKENS
) -> str:
    """构建工作区结构地图。**空工作区返回 ""**(零注入,常驻区逐字节不变)。

    两段降级:全量(路径+符号)装不下 → 只列路径;仍装不下 →
    :func:`truncate_to_tokens` 硬截(带可见标记)。两段都是确定性的——
    同一工作区永远走同一条路(G-P1RM-1)。
    """
    if not workspace_root.is_dir():
        return ""
    files = _collect_files(workspace_root)
    if not files:
        return ""

    text = _render(workspace_root, files, with_symbols=True)
    if estimate_text(text) <= max_tokens:
        return text

    text = _render(workspace_root, files, with_symbols=False) + (
        f"\n\n(文件较多,符号明细因超出 {max_tokens} token 上限省略;"
        "需要时用 read 查看具体文件)"
    )
    if estimate_text(text) <= max_tokens:
        return text

    return truncate_to_tokens(
        text, max_tokens, markers=[_truncation_marker]
    ).text
