"""依赖图：登记任务与其依赖，回答「哪些已就绪」「有没有环」。

边方向：``add_job(job, deps)`` 表示 ``job`` 依赖 ``deps`` 里的每个任务——
``job`` 必须等 ``deps`` **全部**完成后才就绪。
"""

from __future__ import annotations

from typing import Iterable


class CycleError(ValueError):
    """依赖图里有环：任务永远等不到全部依赖完成。"""


class DepGraph:
    def __init__(self) -> None:
        self._nodes: set[str] = set()
        self._deps: dict[str, tuple[str, ...]] = {}

    def add_job(self, job_id: str, deps: Iterable[str] = ()) -> None:
        """登记任务 job_id，它依赖 deps 里的每个任务。"""
        self._nodes.add(job_id)
        for dep in deps:
            self._nodes.add(dep)
            self._deps[dep] = (job_id,)
        self._deps.setdefault(job_id, ())

    def jobs(self) -> set[str]:
        return set(self._nodes)

    def deps_of(self, job_id: str) -> tuple[str, ...]:
        return self._deps.get(job_id, ())

    def ready(self, done: set[str]) -> set[str]:
        """返回依赖已全部完成、可以立刻开工的任务（不含已完成任务）。"""
        return {
            job
            for job in self._nodes
            if job not in done and all(d in done for d in self._deps.get(job, ()))
        }

    def has_cycle(self) -> bool:
        """图里是否存在环（A 等 B、B 又等 A 之类）。"""
        visited: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            for dep in self._deps.get(node, ()):
                if dep not in visited:
                    if dfs(dep):
                        return True
            return False

        return any(dfs(node) for node in sorted(self._nodes))
