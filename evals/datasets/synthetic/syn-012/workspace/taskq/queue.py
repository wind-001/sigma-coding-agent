"""TaskQueue：优先级出队 + 状态流转 + 持久化衔接。"""

import itertools

from taskq.models import Task, apply_failure


class TaskQueue:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._seq = itertools.count(1)

    def add(self, title: str, priority: int = 0) -> Task:
        task = Task(id=f"t{next(self._seq)}", title=title, priority=priority)
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def claim(self) -> Task | None:
        """取一个 pending 任务置为 running。

        出队顺序：**priority 数字大的先出**；同优先级按 id 先进先出
        （``t2`` 早于 ``t10``——注意是按数字比，不是字符串比）。
        没有 pending 时返回 None。
        """
        pending = [t for t in self._tasks.values() if t.state == "pending"]
        if not pending:
            return None
        task = min(pending, key=lambda t: (t.priority, t.id))
        task.state = "running"
        return task

    def complete(self, task_id: str) -> None:
        self._tasks[task_id].state = "done"

    def fail(self, task_id: str) -> None:
        apply_failure(self._tasks[task_id])

    # ---- 持久化衔接 ----

    def save(self, storage: "Storage") -> None:
        storage.save([t.to_dict() for t in self._tasks.values()])

    @classmethod
    def load(cls, storage: "Storage") -> "TaskQueue":
        """从存储恢复队列。恢复后新增任务的 id 必须**不与已有 id 冲突**。"""
        queue = cls()
        for data in storage.load():
            task = Task.from_dict(data)
            queue._tasks[task.id] = task
        return queue
