"""任务模型与状态机。

状态流转约定：
- 新任务 = ``pending``
- ``claim`` 取走 = ``running``
- ``complete`` = ``done``
- ``fail``：``attempts`` 加一后，未达上限回 ``pending`` 等重试，达上限置 ``failed``
- ``blocked``：外部把任务标记为暂时不可执行（持久化里会出现这个状态）
"""

from dataclasses import dataclass

#: 全部合法状态。
VALID_STATES = ("pending", "running", "done", "failed")

#: fail 达到这个次数后置 ``failed``，不再重试。
MAX_ATTEMPTS = 3


@dataclass
class Task:
    id: str
    title: str
    priority: int = 0  # 数字越大越优先
    state: str = "pending"
    attempts: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "priority": self.priority,
            "state": self.state,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        state = data.get("state", "pending")
        if state not in VALID_STATES:
            raise ValueError(f"未知状态：{state!r}")
        return cls(
            id=data["id"],
            title=data["title"],
            priority=data.get("priority", 0),
            state=state,
            attempts=data.get("attempts", 0),
        )


def apply_failure(task: Task) -> None:
    """fail 之后的流转：计数 +1，未达上限回 pending，达上限置 failed。"""
    if task.attempts >= MAX_ATTEMPTS:
        task.state = "failed"
    else:
        task.state = "pending"
