"""工具与循环的抽象基类，以及工具元数据。

为什么 ``ToolDefinition`` 在这个文件、而不是 ``types.py``
    因为它引用 ``BaseTool``。若放进 ``types.py``，就会出现
    ``types`` → ``base``（为了 ``BaseTool``）而 ``base`` → ``types``
    （为了 ``ToolResult``）的**循环导入**。
    同模块放置是最直白的解法，不需要任何延迟求值魔法。

抽象方法为什么不给默认实现（除了一处例外）
    默认实现会让"忘了实现"变成静默错误。判据是：
    **有没有一个对所有子类都正确的实现？**
    见 ``params`` 与 ``json_schema`` 的对照。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    # 删掉 BaseLoop 之后这里也精简了：原先还要 `AgentMessage` 与 `TurnResult`
    # （它们是 `BaseLoop.run_turn` 的参数与返回类型），现在只有 `BaseTool`
    # 需要这两个类型。**未使用的 TYPE_CHECKING 导入同样是负债**——
    # 它们会让读者以为这个模块还依赖那些类型。
    from sigma_agent.types import ToolContext, ToolResult


class BaseTool(ABC):
    """所有工具（内置 / 扩展）的抽象基类。二者走同一条注册路径。

    类属性是**元数据**，由子类赋值声明。注册表读这些值不需要实例化。
    """

    name: str = ""
    description: str = ""
    read_only: bool = False
    needs_approval: bool = False

    @property
    @abstractmethod
    def params(self) -> type[BaseModel]:
        """参数模型。Pydantic 模型 → 自动生成 JSON Schema。

        **必须是抽象的**：每个工具的参数都不同。
        若给默认实现（比如返回一个空模型），"忘了定义"就会变成
        "模型永远调不对参数"，而那个症状不指向根因。
        """
        raise NotImplementedError

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """执行工具。

        **单个工具失败必须返回 ``is_error=True`` 的 ``ToolResult``，
        不得向上抛异常**（架构方案 4.3 节第 3 点 / 详规 3.6）。

        这不是宽容，是功能：**模型看到失败原因才能纠错**，
        而"能纠错"是「纠错增益」这个核心指标的全部前提。
        工具抛异常等于把纠错能力关掉。
        """
        raise NotImplementedError

    def json_schema(self) -> dict[str, Any]:
        """由 ``params`` 生成 JSON Schema。

        **默认实现、非抽象**——因为它对所有子类都是正确的。
        让它抽象只会逼 5 个内置工具各写一遍完全相同的代码。

        判据同上层：**有通用正确实现的不该抽象。**
        """
        return self.params.model_json_schema()


# ---------------------------------------------------------------------------
# 关于 BaseLoop 的删除（2026-09-20）
# ---------------------------------------------------------------------------
#
# 这里原本有一个 `BaseLoop(ABC)`，`AgentLoop` 是它唯一的子类，
# 并有门槛 G30 断言「`__subclasses__()` 恰好只有 `AgentLoop`」。
#
# **2026-09-20 删除。** 理由是它与架构 4.3 节自己的原则冲突：
# 那一节写着「当前只允许一个子类，不得并行存在第二个实现」——
# 而**一个只允许有唯一子类的抽象基类，等价于给那唯一的实现加一层纯间接**。
#
# 架构给的保留理由是「将来若要做两阶段规划循环或反思循环，不必改动公共接口」，
# 但那个"将来"没有对应任何具体需求，而代价是**现在就存在**：
# 多一个类型要维护、多一条门槛要跑、多一层跳转要读。
#
# 这与 4.3 节末「不做没有需求的设计」是同一条原则——
# **那条原则该用在它自己身上。**
#
# 将来真要做第二种循环策略时，选择依据会比现在充分（有真实需求），
# 届时用 `Protocol` 还是真基类，都能就事论事地定。
#
# 门槛 G30 已改写为「全项目只有一个类定义 `run_turn`」（见 tests/test_agent_loop.py）——
# 那条断言其实**比原来更强**：即使有人新写一个不继承任何基类的 loop，也能拦住。


class ToolDefinition(BaseModel):
    """注册表里存的东西。**只装元数据，不装可执行引用。**

    这是架构 4.2 节「``BaseTool`` 行为 + ``ToolDefinition`` 元数据」两段式的落地。

    ``tool`` 指向 ``BaseTool`` 实例而不是裸 ``Callable``——
    这一条让"扩展工具与内置工具同路径"成为**类型层面的约束**，
    而不只是一句约定（架构 4.4 节）。

    ``arbitrary_types_allowed`` 是必需的：``BaseTool`` 是 ``ABC``，
    Pydantic 无法校验它，只做 ``isinstance`` 检查。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    params_schema: dict[str, Any]
    tool: BaseTool
    read_only: bool = False
    needs_approval: bool = False
    source: str = "builtin"
