"""工具注册表：内置与扩展走同一条注册路径。

热重载正确性的唯一硬约束（架构方案 4.4 节）
    **调用方只能通过 ``get(name)`` 取工具，不得缓存 ``ToolDefinition`` 实例。**
    违反此约束的症状是"改了代码但行为没变"。

    P1 还没有热重载，所以现在**看不出违反的后果**——
    这条约束必须在 P1 就写进文档，否则 P4 加 ``reload_source`` 时
    会发现调用方到处缓存了实例（待复核项 T1）。
"""

from __future__ import annotations

from typing import Any

from sigma_agent.base import BaseTool, ToolDefinition


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
