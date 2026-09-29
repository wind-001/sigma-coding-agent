"""跨会话记忆(P5-批次3):文件式记忆的发现与索引渲染。

**记忆是什么、不是什么**(详规 §1 的边界表,一句话版)
    记忆 = agent 学到的、对未来的自己的**提示**(``.sigma/memory/<slug>.md``);
    不是事实记录(那是会话树)、不是用户指令(那是 AGENTS.md)、
    不是程序性知识(那是 skills)。允许有损、允许过时,溯源靠会话树。

三条设计判据(详规 T1–T7,这里只留影响本文件实现的)
1. **文件式,零依赖**:markdown、无 YAML(与 skills 同判据:极简 frontmatter
   连 YAML 都不引)。用户可随时打开读、改、删——可审计性压过检索花活。
2. **宽容 + 可见**:不合规文件(无 `# 标题` 首行)**照收**,标题回落为文件名,
   同时进 ``problems`` 清单——静默跳过会让"我写了记忆它怎么不用"变成
   无从排查的悬案(skills 同款:问题必须可见)。
3. **索引进常驻、正文按需**:本模块只渲染索引(硬上限 250 token、截断可见);
   正文由模型用 ``read`` 取——渐进披露,常驻区前缀不动(不破 prompt cache)。

**会话内冻结**(G884):``scan_memory`` 是快照,调用方(产品壳)在会话启动时
扫一次、不再扫。模型本会话新写的记忆不在索引里——刷新会让常驻区从变化点
逐字节改变,缓存全部失效(D4);下个会话自动可见。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sigma_ai.tokens import estimate_text

#: 记忆目录名(挂在工作区 ``.sigma/`` 之下——该目录在 checkpoint 的
#: BUILTIN_EXCLUDES 里,回滚不会把记忆滚丢,allowlist 同款判据)。
MEMORY_DIR_PARTS: tuple[str, ...] = (".sigma", "memory")

#: 索引总 token 上限(与 ``resident_caps.CAPS["记忆索引"]`` 同值——
#: G885 验表和、G880 验行为,两处测试互为对账;不直接 import 是因为
#: 两文件分属两条实现线,值漂移时测试会当场红)。
MEMORY_INDEX_CAP_TOKENS = 250

#: 单条索引行的 token 上限——超了裁标题,保证一条长标题吃不掉整个索引。
MEMORY_ENTRY_LINE_CAP_TOKENS = 25

_TITLE_PATTERN = re.compile(r"^#\s+(.+)$")


@dataclass(frozen=True)
class MemoryEntry:
    """一条记忆:文件名 slug + 首行标题。

    ``title`` 来自文件首个非空行的 ``# 标题``;没有就用 slug(宽容,
    同时记入 ``problems``——可见,不静默)。
    """

    slug: str
    title: str


@dataclass(frozen=True)
class MemoryScan:
    """一次扫描的快照。**frozen**:会话内冻结语义的结构化表达(G884)。

    ``problems`` 是"看见了但不挡"的清单:文件系统层的坏(读不了)
    与格式层的不合规(无标题行)都在这里,渲染时原样带出。
    """

    entries: tuple[MemoryEntry, ...]
    problems: tuple[str, ...] = ()


def memory_dir_for(workspace_root: Path) -> Path:
    """工作区根 → 记忆目录。**落点只在这一个函数**——
    sdk 的扫描与 cli 的横幅统计都走它,两处拼路径必然漂移。"""
    return workspace_root.joinpath(*MEMORY_DIR_PARTS)


def scan_memory(memory_dir: Path) -> MemoryScan:
    """扫描记忆目录(会话启动时**恰好一次**,快照语义)。

    目录不存在不是错误:没有记忆的 workspace 是常态(G881 的
    "空工作区零污染"就靠"空扫描 → 空索引 → 常驻区逐字节一致")。
    """
    entries: list[MemoryEntry] = []
    problems: list[str] = []
    if not memory_dir.is_dir():
        return MemoryScan(entries=(), problems=())
    for path in sorted(memory_dir.glob("*.md"), key=lambda p: p.name):
        if not path.is_file():
            continue
        slug = path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{slug}: 读不了({type(exc).__name__}),已跳过")
            continue
        title = _title_of(text)
        if title is None:
            title = slug
            problems.append(f"{slug}: 首行没有 '# 标题',索引里用文件名代替")
        entries.append(MemoryEntry(slug=slug, title=title))
    return MemoryScan(entries=tuple(entries), problems=tuple(problems))


def _title_of(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _TITLE_PATTERN.match(stripped)
        return match.group(1).strip() if match else None
    return None


def render_memory_index(scan: MemoryScan) -> str:
    """把扫描结果渲染成常驻区索引段。**空扫描 → 空串**(G881:零污染)。

    形态::

        跨会话记忆索引（正文用 read 查看，路径相对工作区）：
        - 用户偏好 → memory/user-prefs.md
        - 构建命令 → memory/build.md
        [... 另有 N 条未显示（索引上限 250 token）]

    两条裁剪:单条超 25 token 裁标题(一条长标题不能吃掉整个索引);
    总量超 250 停止装填、余数进截断标记(AGENTS.md 硬截断同款:
    截断必须**在正文里可见**,不静默)。

    截断标记自身**必须算进预算**(首版漏算,整体渲染超 cap 被 G880
    当场抓住)——所以装填阶段就按"扣掉标记预留"的额度来。
    """
    if not scan.entries:
        return ""
    header = "跨会话记忆索引（正文用 read 查看，路径相对工作区）："
    trailer_template = f"[... 另有 99 条未显示（索引上限 {MEMORY_INDEX_CAP_TOKENS} token）]"
    reserve = estimate_text(trailer_template) + 1
    lines: list[str] = [header]
    used = estimate_text(header)
    shown = 0
    truncated = False
    for entry in scan.entries:
        title = _fit_title(entry.title, entry.slug)
        line = f"- {title} → memory/{entry.slug}.md"
        line_tokens = estimate_text(line)
        limit = MEMORY_INDEX_CAP_TOKENS - (reserve if shown + 1 < len(scan.entries) else 0)
        if used + 1 + line_tokens > limit:
            truncated = True
            break
        lines.append(line)
        used += 1 + line_tokens
        shown += 1
    if truncated:
        remaining = len(scan.entries) - shown
        lines.append(f"[... 另有 {remaining} 条未显示（索引上限 {MEMORY_INDEX_CAP_TOKENS} token）]")
    if scan.problems:
        lines.append(f"（⚠ {len(scan.problems)} 条记忆文件格式异常：{'; '.join(scan.problems[:3])}）")
    return "\n".join(lines)


def _fit_title(title: str, slug: str) -> str:
    """把单条索引行压到 ``MEMORY_ENTRY_LINE_CAP_TOKENS`` 内(裁标题,保留结构)。"""
    line = f"- {title} → memory/{slug}.md"
    if estimate_text(line) <= MEMORY_ENTRY_LINE_CAP_TOKENS:
        return title
    trimmed = title
    while len(trimmed) > 1:
        trimmed = trimmed[:-2] + "…" if len(trimmed) > 2 else trimmed[0]
        if estimate_text(f"- {trimmed} → memory/{slug}.md") <= MEMORY_ENTRY_LINE_CAP_TOKENS:
            return trimmed
    return slug
