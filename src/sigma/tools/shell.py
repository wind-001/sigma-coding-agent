"""工具壳的公共件:schema 压薄与参数级拒绝。

为什么独立成模块(tools 层的"同层公共位置")
    ``team_board`` 与 ``multi_agent`` 两个壳各写了一份**逐字相同**的
    ``_strip_schema_noise`` 与同签名的 ``_reject``——两份实现意味着
    改一处忘另一处,而这份漂移最先伤到的正是预算实测数字(G-TEAM-6 拿
    ``json_schema()`` 的产物去比 cap)。抽到这里,两份变一份。

    不放进 ``base.py``:那里是 ``BaseTool`` / ``ToolDefinition`` 的家,
    这两个函数与抽象无关——它们是**壳件的工具函数**,给任何工具用。

为什么不做成 ``BaseTool`` 的方法
    同 ``base.py`` 里 json_schema 那条判据:**有通用正确实现的不该抽象**。
    但反过来也不该硬塞进基类——基类多一个方法与多一个自由函数是两回事,
    前者会污染每个子类的面,而这两个函数与实例状态无关。
"""

from __future__ import annotations

from typing import Any

from sigma.agent.types import ToolResult
from sigma.providers.messages import TextBlock


def strip_schema_noise(schema: dict[str, Any]) -> dict[str, Any]:
    """递归剥掉 pydantic 自动加的 ``title`` / ``default``。

    **字段 description 保留**——那是模型唯一的使用说明;剥掉的只是给
    pydantic 文档看的字段名标题与"required 已经说过一次"的默认值。
    常驻区每轮重付,这笔省下来的钱直接体现在预算表的实测数字里。
    """
    schema.pop("title", None)
    schema.pop("default", None)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for field_schema in properties.values():
            if isinstance(field_schema, dict):
                strip_schema_noise(field_schema)
    return schema


def reject_result(message: str, details: dict[str, Any]) -> ToolResult:
    """参数级拒绝:直接给 ``is_error`` 结果,**不进状态机**。

    与状态机的迁移守卫分开:守卫管"板上/系统里的规则",这里管"调用是否成形"
    (缺参数、越权角色一类)。理由更直白,而且省掉一次事务。
    """
    return ToolResult(
        content=[TextBlock(text=message)], details=details, is_error=True
    )
