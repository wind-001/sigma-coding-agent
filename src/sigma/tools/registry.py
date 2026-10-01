"""工具注册表：内置与扩展走同一条注册路径。

热重载正确性的唯一硬约束（架构方案 4.4 节）
    **调用方只能通过 ``get(name)`` 取工具，不得缓存 ``ToolDefinition`` 实例。**
    违反此约束的症状是"改了代码但行为没变"。

    P1 还没有热重载，所以现在**看不出违反的后果**——
    这条约束必须在 P1 就写进文档，否则 P4 加 ``reload_source`` 时
    会发现调用方到处缓存了实例（待复核项 T1）。
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from sigma.tools.base import BaseTool, ToolDefinition


@dataclass(frozen=True)
class ReloadReport:
    """一次扩展装载 / 热重载的结果（详规 P3-扩展热重载 §2 第 5 步）。

    frozen dataclass 的载体判定（AGENTS.md 三问）：不生成工具 schema、
    不落盘序列化、不校验外部输入——三问皆否，不进 pydantic。
    字段用 tuple 不用 list：报告是**结果记录**，消费方不该改它。

    ``failed_reason`` 非空 = 本次装载被拒绝、**注册表保持原样**；
    为空 = 装载生效（``added``/``removed`` 说明动了什么）。
    重名拒绝以报告返回而不是抛 ``DuplicateToolError``：两个消费者
    （启动装载要"警告继续"、/reload 要"回显报告"）要的都是**值**，
    不是异常——与 team 工具把 ``ValueError`` 转 ``is_error`` 同一条哲学。
    理由串仍以 ``DuplicateToolError:`` 起头，保留判据的词汇。
    """

    source: str
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    failed_reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed_reason


def _import_fresh(path: Path) -> ModuleType:
    """按路径新鲜导入一个模块：**每次生成新的 module 对象、新的代码对象**。

    架构 4.4 的代码契约，两条防陈旧缺一不可：

    1. uuid 模块名 + 注册 ``sys.modules``——**不用 ``importlib.reload``**：
       它保留同一个 module 对象，其它模块以 ``from x import y`` 形式持有的
       旧引用不会被更新，那是"改了代码但行为没变"的头号来源；
    2. **读源码直接 ``compile``，不走 loader**——``SourceFileLoader`` 会读写
       ``__pycache__``，而它的失效判据是 mtime+size：同一秒内改文件且
       改完尺寸恰好相同（比如只改一个词），解释器就会拿**旧字节码**，
       热重载静默落空。扩展文件都很小，每次重新编译的成本可忽略。
    """
    name = f"sigma_ext_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 构建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        code = compile(path.read_bytes(), str(path), "exec")
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


class DuplicateToolError(ValueError):
    """同名工具被注册两次。

    继承 ``ValueError`` 而非 ``RuntimeError``：这是**值非法**（名字已被占用），
    不是运行时状态错误。与批次 1 的 ``TRY004`` 经验一致——
    类型错误用 ``TypeError``，值非法用 ``ValueError``。
    """


class ToolRegistry:
    """所有工具（内置 / 扩展）走同一路径注册。

    存储形态是"名字 → ``ToolDefinition``"，而 ``ToolDefinition`` 里的
    ``tool`` 指向 ``BaseTool`` 实例。**元数据与可执行引用分开**，
    这是架构 4.2 节两段式的落地。
    """

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        # 扩展来源的登记（详规 §2）：source 串 → 文件路径 / 最近一次导入的模块。
        # 路径供 reload_source 定位；模块供 sys.modules 卫生（换新的一代时移除旧的）。
        # **随实例走，不随 clone 走**：克隆只复制"谁注册了什么"这份账，
        # 扩展路径登记由装载方（会话 / CLI）自己重新装载获得。
        self._extension_paths: dict[str, Path] = {}
        self._extension_modules: dict[str, ModuleType] = {}

    def register(self, tool: BaseTool, *, source: str = "builtin") -> None:
        """注册一个工具。

        **重名时抛错，且注册表保持原样**（门槛 G24）。

        实现上就是"先查再写"这么简单，但**必须用测试钉住**：
        任何"先删旧的、再插新的"的写法，都会在插入失败时留下**半更新状态**，
        而症状是"某个工具永久消失了"，不指向根因。
        这是架构 4.4 节三个失败场景里"半更新状态"的早期版本。

        另一个刻意的选择：**不允许静默覆盖**。要替换内置工具必须显式走
        ``--no-builtin-tools``。静默覆盖是最难排查的一类 bug
        （架构 4.4 节第 3 点）。
        """
        name = tool.name
        if not name:
            raise ValueError(
                f"{type(tool).__name__} 没有 name，无法注册。"
                "工具必须声明类属性 name。"
            )

        if name in self._definitions:
            existing = self._definitions[name]
            raise DuplicateToolError(
                f"工具名 {name!r} 已被注册"
                f"（来源 {existing.source!r}，类型 {type(existing.tool).__name__}）。"
                "若要替换内置工具，请显式走 --no-builtin-tools，"
                "**不允许静默覆盖**（架构方案 4.4 节）。"
            )

        # 注意：schema 生成放在重名检查之后——若 schema 生成本身抛异常
        # （例如 params 不是合法 Pydantic 模型），注册表同样保持未改动。
        definition = ToolDefinition(
            name=name,
            description=tool.description,
            params_schema=tool.json_schema(),
            tool=tool,
            read_only=tool.read_only,
            needs_approval=tool.needs_approval,
            source=source,
        )
        self._definitions[name] = definition

    def get(self, name: str) -> BaseTool:
        """按名取工具。**每次都查表，不返回缓存引用。**

        调用方不得把返回的实例存起来跨轮复用——见模块 docstring 的硬约束。
        """
        try:
            return self._definitions[name].tool
        except KeyError:
            known = ", ".join(self.names()) or "(空)"
            raise KeyError(f"未注册的工具 {name!r}。已注册：{known}") from None

    def names(self) -> list[str]:
        """已注册的工具名，**按字典序**。

        顺序必须稳定：它进常驻区、参与哈希（门槛 G26）。
        若用字典插入顺序，"注册顺序变了"就会变成"常驻区变了"，
        而症状是 prompt cache 永远命中不了——极难排查。
        """
        return sorted(self._definitions)

    def schemas(self) -> list[dict[str, Any]]:
        """拼给 provider 的 ``tools`` 参数。

        顺序与 :meth:`names` 一致（字典序），理由同上。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.params_schema,
                },
            }
            for definition in self.definitions()
        ]

    def definitions(self) -> list[ToolDefinition]:
        """按稳定顺序返回全部定义。

        供审计与测试使用。**调用方不要缓存返回的实例**——
        这是热重载约束的一部分，见模块 docstring。
        """
        return [self._definitions[name] for name in self.names()]

    def clone(self, *, exclude: Collection[str] = ()) -> ToolRegistry:
        """返回一个**同款工具、独立登记簿**的新注册表。

        工具实例**共享**（同一批 ``BaseTool`` 对象，不复制——工具的配置与
        状态属于组装方），登记簿**独立**（往克隆里 register 不会影响原表）。
        这个组合正是"每个会话一份注册表"要的形状（2026-09-24 review 修复）：

        ``InteractiveSession`` 在 ``enable_sub_agent=True`` 时会往注册表里
        **注册 TaskTool**。此前 ``SessionManager`` 的所有会话共享同一个
        registry，第二次 ``_build``（/switch、/new）必然撞
        ``DuplicateToolError``——终端会话当场终结。给每个会话传克隆后，
        会话间互不影响，而 sdk 侧"调用方预注册 task 就拒绝"的防线原样保留。

        ``exclude`` 用于子会话工厂的构造性排除（task/todo 不进子会话）——
        此前那处是手写的 for 循环克隆，同一个概念不该有两份实现。
        """
        cloned = ToolRegistry()
        for definition in self.definitions():
            if definition.name in exclude:
                continue
            cloned.register(definition.tool, source=definition.source)
        return cloned

    # ------------------------------------------------------------------
    # 扩展装载与热重载（D3 / 详规 P3-扩展热重载 §2：五步，失败不进半更新）
    # ------------------------------------------------------------------

    def load_extensions(self, directory: Path) -> list[ReloadReport]:
        """装载 ``directory`` 下全部 ``*.py`` 扩展，逐个返回报告。

        文件按名字序处理（确定性：两个扩展互相重名时，**稳定的那个**赢）。
        单个文件失败只影响自己的报告——调用方（启动路径）要"警告继续"，
        不能让一个坏扩展拖死整个会话。目录不存在返回空表：没有扩展是常态，
        不是错误。同一目录装载两次是**幂等替换**（source=文件路径，同源
        互相替换）——CLI 与 InteractiveSession 各装一次不冲突；
        但**以不同路径串指同一文件**会被当成两个来源而撞重名，
        所以两边必须用同一个 workspace 值（CLI 流程本就共享同一对象）。
        """
        if not directory.is_dir():
            return []
        return [self._install_extension(path) for path in sorted(directory.glob("*.py"))]

    def extension_sources(self) -> list[str]:
        """已装载的扩展来源（source 串，字典序）。/reload 与测试用。"""
        return sorted(self._extension_paths)

    def reload_source(self, source: str) -> ReloadReport:
        """热重载一个扩展来源：改完 ``extensions/<name>.py`` 后当轮生效。

        未知来源抛 ``KeyError``——``/reload foo`` 拼错文件名是调用方的
        输入错误，报"有哪些可选"比静默无操作诚实（与 :meth:`get` 同判据）。
        """
        path = self._extension_paths.get(source)
        if path is None:
            known = ", ".join(self.extension_sources()) or "(无)"
            raise KeyError(f"未知的扩展来源 {source!r}。已装载：{known}")
        return self._install_extension(path)

    def _install_extension(self, path: Path) -> ReloadReport:
        """五步装载（详规 §2）：任何失败都**保留旧表**，不进半更新状态。"""
        source = str(path)
        old_names = sorted(
            name for name, d in self._definitions.items() if d.source == source
        )
        # 1. 新鲜导入：导入异常 → 报告失败，注册表分毫未动（场景 1）。
        try:
            module = _import_fresh(path)
        except BaseException as exc:  # 扩展代码什么都可能抛，全接
            return ReloadReport(
                source=source,
                failed_reason=f"导入失败（{type(exc).__name__}）: {exc}",
            )
        previous = self._extension_modules.get(source)
        if previous is not None:
            sys.modules.pop(previous.__name__, None)
        self._extension_modules[source] = module
        # 2. TOOLS 校验：缺失 / 空 = "扩展已移除"，清理该 source 的旧注册项
        #    （场景 2——文件还在但不再导出工具，是合法的移除方式）；
        #    形状不对（不是列表 / 元素不是 BaseTool）= 坏扩展，保留旧表。
        tools: object = getattr(module, "TOOLS", None)
        if tools is None or (isinstance(tools, list) and not tools):
            for name in old_names:
                del self._definitions[name]
            return ReloadReport(source=source, removed=tuple(old_names))
        if not isinstance(tools, list):
            return ReloadReport(
                source=source,
                failed_reason=(
                    f"TOOLS 必须是 BaseTool 实例列表，得到 {type(tools).__name__}"
                ),
            )
        # 3. 先构造全部 ToolDefinition：schema 生成抛异常 → 报告失败，旧表原样
        #    （先算后写，写阶段才可能"半更新"）。
        new_definitions: list[ToolDefinition] = []
        try:
            for tool in tools:
                if not isinstance(tool, BaseTool) or not tool.name:
                    return ReloadReport(
                        source=source,
                        failed_reason="TOOLS 里有不是 BaseTool（或缺 name）的元素",
                    )
                new_definitions.append(
                    ToolDefinition(
                        name=tool.name,
                        description=tool.description,
                        params_schema=tool.json_schema(),
                        tool=tool,
                        read_only=tool.read_only,
                        needs_approval=tool.needs_approval,
                        source=source,
                    )
                )
        except Exception as exc:
            return ReloadReport(
                source=source,
                failed_reason=f"schema 生成失败（{type(exc).__name__}）: {exc}",
            )
        # 场景 3 的重名检查：与内置**或与其他扩展**（或与文件内自己）重名
        # → 拒绝本次、保留旧表。本 source 的旧项不算重名——那是被替换者。
        incoming = [definition.name for definition in new_definitions]
        intra = {name for name in incoming if incoming.count(name) > 1}
        external = (set(self._definitions) - set(old_names)) & set(incoming)
        if intra or external:
            clash = sorted(intra | external)[0]
            return ReloadReport(
                source=source,
                failed_reason=(
                    f"DuplicateToolError: 工具名 {clash!r} 已被注册。"
                    "不允许静默覆盖（架构 4.4 节）：替换内置工具必须显式"
                    " --no-builtin-tools；扩展之间重名时后装载者拒绝。"
                ),
            )
        # 4. 原子替换：到这里全部校验已过，下面的写不可能失败。
        for name in old_names:
            del self._definitions[name]
        for definition in new_definitions:
            self._definitions[definition.name] = definition
        self._extension_paths[source] = path
        # 5. 报告。
        return ReloadReport(
            source=source,
            added=tuple(sorted(incoming)),
            removed=tuple(old_names),
        )
