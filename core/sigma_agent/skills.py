"""技能（skill）的发现、索引渲染与正文加载。

**一句话**：技能正文**按需**进上下文，常驻区只放**目录**。

两条注入路径（本模块只管"造出这两样东西"，怎么送进去不归它管）
    1. **索引** → 进**常驻区**（D4 的"技能索引 ≤600"就是它）。会话开始时算一次，
       之后逐字节不变，于是可缓存。
    2. **正文** → 由 ``load_skill`` 工具的结果**追加到上下文尾部**。
       追加不改变前缀，所以前面整段（含索引）的缓存仍然命中。

    这条分法是本项目对"渐进披露会不会爆 prompt cache"的回答——
    研究笔记第 310 行把它标成"Pi 声称的、自研时要实测"的权衡。
    **结构上不破坏前缀**是可以证明的（上面两条）；命中率有没有真上去是另一回事，
    那要跑对照实验，本模块不宣称。

为什么住在 ``sigma_agent`` 而不是 ``sigma_session``
    ``sigma_tools``（load_skill 工具）与 ``sigma_session``（渲染索引进常驻区）
    是**兄弟层**，彼此不能 import。技能元数据是两边都要的东西，
    只能放在它们的公共下界 —— 也就是本层。
    放 ``sigma_session/resources.py``（架构第 338 行的原始设想）会逼工具层违反契约，
    或者自己重写一份扫描逻辑（**两份实现会漂移**）。

    另一个理由是"注册表本来就住 agent 层"：``ToolRegistry`` 就在隔壁。

为什么是**扫描目录**而不是读一个手工清单
    手工清单会漂移：新建了技能却忘了写进清单 → **模型永远看不到它，且不报错**。
    症状是"我明明加了技能，它怎么不用"，排查方向完全错。
    目录即真相，就没有这个失败模式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from sigma_ai.tokens import TruncatedText, estimate_text, truncate_to_tokens

#: 技能目录的约定名。**只用于给调用方一个默认值**，
#: 本模块自己不拿它去搜路径（路径由调用方传，见模块 docstring 的分层理由）。
SKILLS_DIRNAME = "skills"

#: frontmatter 的分隔符。取 `---` 是 Markdown 生态的通行写法。
FRONTMATTER_FENCE = "---"

#: ``load_skill`` 单次注入正文的 token 上限。
#:
#: 1500 的依据：一个技能正文通常是 1–3 KB（约 300–1000 token），
#: 1500 给"写得比较细"的技能留了余量，又不至于一次吃掉动态区的可观份额。
#: **它是动态区的开销，不占常驻预算**——所以这个数字放松一点不会影响 D4。
DEFAULT_MAX_BODY_TOKENS = 1500


class SkillMeta(BaseModel):
    """一个技能在**索引**里的样子。

    三个字段与需求一致：``name`` / ``description`` / ``location``。
    另加 ``source``（绝对路径，运行时用）——**它不进索引文本**，
    因为它既长又是本机路径，对模型没有价值。
    """

    name: str
    description: str
    location: str
    source: Path

    model_config = {"arbitrary_types_allowed": True}


@dataclass
class SkillScan:
    """一次扫描的结果。

    **``problems`` 必须能被调用方拿到并展示**，不能只发 warning：
    一个坏技能文件如果静默消失，症状是"我加了技能它不用"——
    与手工清单漂移是同一种失败。丢弃必须可见。
    """

    skills: list[SkillMeta] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.skills)


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """解析 ``---`` 包起来的 frontmatter，返回键值对。

    **不认识 YAML**，只认 ``key: value`` 这种最简形式。
    理由：为一个"就四行元数据"的场景引入 PyYAML（+ 解析器的行为面）
    是净负债；而一旦引入，别人就会开始在里面写列表和嵌套结构，
    那时"技能元数据"这件事就再也不是四行了。

    返回 ``None`` 表示**没有 frontmatter**；返回空 dict 表示有但没内容——
    这两种情况调用方要分开报，因为处置不同（前者是"忘了写"，后者是"写空了"）。
    """
    if not text.startswith(FRONTMATTER_FENCE):
        return None

    rest = text[len(FRONTMATTER_FENCE) :]
    # 结束围栏必须是**独占一行**的 `---`，所以找 `\n---`
    end = rest.find(f"\n{FRONTMATTER_FENCE}")
    if end < 0:
        return None

    found: dict[str, str] = {}
    for line in rest[:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            # 不认的行**跳过而不是报错**：将来想加注释或别的语法时，
            # 老代码不会因此炸。真正必需字段的缺失由 discover_skills 报。
            continue
        found[key.strip()] = value.strip()
    return found


def strip_frontmatter(text: str) -> str:
    """去掉 frontmatter，只留正文。

    **正文里不该再出现元数据**：那些字段已经进索引了，重复一遍是白花 token，
    而且模型看到两份可能不一致的 `name` 反而会犹豫。
    """
    if not text.startswith(FRONTMATTER_FENCE):
        return text
    rest = text[len(FRONTMATTER_FENCE) :]
    end = rest.find(f"\n{FRONTMATTER_FENCE}")
    if end < 0:
        return text
    after = rest[end + len(FRONTMATTER_FENCE) + 1 :]
    return after.lstrip("\n")


def discover_skills(root: Path) -> SkillScan:
    """扫描 ``root`` 下的技能。**不抛异常**。

    约定：``root/<任意目录名>/<某个>.md``，**每个子目录恰好一个 md**。

    为什么要求"恰好一个"而不是写死 ``<目录名>.md``：
    那样文件名就完全由目录名决定，索引里的 ``location`` 字段会变成
    ``<name>/<name>.md`` 的**冗余复述**，一点信息量都没有。
    放宽成"目录里那个唯一的 md"，``location`` 才真的在说一件事。

    目录不存在时返回空结果 —— **不是错误**：一个还没建技能目录的项目
    不该为此报错（与 ``load_project_instructions`` 对"文件不存在"的处置一致）。
    """
    if not root.is_dir():
        return SkillScan()

    skills: list[SkillMeta] = []
    problems: list[str] = []
    seen: set[str] = set()

    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        found = sorted(directory.glob("*.md"))
        if not found:
            problems.append(f"{directory.name}/：目录里没有 .md 文件，已跳过")
            continue
        if len(found) > 1:
            names = "、".join(p.name for p in found)
            problems.append(
                f"{directory.name}/：有多个 .md（{names}），无法确定用哪个，已跳过"
            )
            continue

        path = found[0]
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{directory.name}/：读不出来（{type(exc).__name__}: {exc}）")
            continue

        meta = parse_frontmatter(text)
        if meta is None:
            problems.append(f"{directory.name}/：缺少 frontmatter（`---` 开头那段元数据）")
            continue

        name = meta.get("name", "").strip() or directory.name
        description = meta.get("description", "").strip()
        if not description:
            problems.append(f"{directory.name}/：frontmatter 缺少 description")
            continue
        if name in seen:
            problems.append(f"{directory.name}/：技能名 {name!r} 与前面重复，已跳过")
            continue

        seen.add(name)
        skills.append(
            SkillMeta(
                name=name,
                description=description,
                # location 是**相对技能根目录**的路径。
                # 根目录在索引头部说一次就够了——每条都带全路径
                # 会把 600 token 的预算吃掉一大半（实测约 3 倍）。
                location=str(path.relative_to(root)).replace("\\", "/"),
                source=path,
            )
        )

    # **顺序必须稳定**：常驻区逐字节不变是 D4 的硬要求（门槛 G26/G50），
    # 而"文件系统返回的顺序"不保证稳定（跨平台、跨次运行都可能不同）。
    skills.sort(key=lambda s: s.name)
    return SkillScan(skills=skills, problems=problems)


def render_index(skills: list[SkillMeta], *, root_label: str) -> str:
    """把技能列表渲染成常驻区里那一段文本。

    ``root_label`` 是技能根目录的**人可读位置**（如 ``extensions/skills/``），
    只在头部出现一次，供人核对。

    空列表返回空串（**不是一段空的"可用技能："**）：
    没有技能时不该在常驻区里占那几十个 token，也不该让模型看到
    一个空的小节而以为"技能系统坏了"。
    """
    if not skills:
        return ""
    lines = [
        f"可用技能（需要完整说明时用 load_skill 工具按名字取；文件在 {root_label}）："
    ]
    lines.extend(
        f"- {skill.name}：{skill.description} → {skill.location}" for skill in skills
    )
    return "\n".join(lines)


def load_body(
    skill: SkillMeta, *, max_tokens: int = DEFAULT_MAX_BODY_TOKENS
) -> TruncatedText:
    """读一个技能的正文（**已去掉 frontmatter**）并按需截断。

    截断走 ``sigma_ai.tokens.truncate_to_tokens`` —— 与 ``AGENTS.md``
    用的是同一份算法。复制一份的代价不是多几行，而是"同一个上限、
    两处结果不一样"这种静默差异。

    读不出来时返回**空文本**，由调用方（``LoadSkillTool``）转成
    ``is_error`` 的工具结果——**不抛**：一个技能文件读不了，
    不该让整轮任务崩掉，模型看到错误还能换个做法。
    """
    try:
        raw = skill.source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return TruncatedText("", False, 0, 0)

    body = strip_frontmatter(raw).strip()

    def marker(original: int, kept: int) -> str:
        dropped = original - kept
        return (
            f"\n\n[技能 {skill.name} 的正文已截断：原文约 {original} token，"
            f"此处只保留前 {kept} token，丢弃约 {dropped} token。"
            f"完整内容请用 read 工具读取 {skill.source}。以上说明可能不完整。]"
        )

    return truncate_to_tokens(body, max_tokens, markers=(marker,))


def body_tokens(skill: SkillMeta) -> int:
    """正文的估算 token 数（**不截断**）。给 CLI 横幅与诊断用。"""
    try:
        raw = skill.source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    return estimate_text(strip_frontmatter(raw).strip())
