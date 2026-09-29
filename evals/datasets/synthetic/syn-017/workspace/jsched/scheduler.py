"""调度器：在依赖允许的前提下，每一步挑「最紧急」的任务执行。

- priority 数字越小越紧急；
- 同 priority 按登记顺序（先 add 的先执行）；
- 依赖：一个任务的所有依赖都执行完，它才可执行；
- ``drain()`` 返回完整执行顺序；依赖成环时抛 ``CycleError``。
"""

from __future__ import annotations

from typing import Iterable

from jsched.graph import CycleError, DepGraph
from jsched.heap import MinHeap


class Job:
    __slots__ = ("id", "priority", "seq")

    def __init__(self, job_id: str, priority: int, seq: int) -> None:
        self.id = job_id
        self.priority = priority  # 数字越小越紧急
        self.seq = seq  # 登记顺序，同优先级先到先执行


class Scheduler:
    def __init__(self) -> None:
        self._graph = DepGraph()
        self._jobs: dict[str, Job] = {}
        self._seq = 0

    def add(
        self, job_id: str, priority: int = 5, deps: Iterable[str] = ()
    ) -> "Scheduler":
        """登记一个任务；deps 里是它依赖的任务 id。"""
        if job_id in self._jobs:
            raise ValueError(f"任务重复登记：{job_id!r}")
        self._seq += 1
        self._jobs[job_id] = Job(job_id, priority, self._seq)
        self._graph.add_job(job_id, deps)
        return self

    def drain(self) -> list[str]:
        """按依赖与优先级给出执行顺序；依赖成环时抛 CycleError。

        每一轮把「刚就绪」的任务压入堆，再弹出最紧急的一个执行；
        同优先级按登记顺序先到先执行。
        """
        if self._graph.has_cycle():
            raise CycleError("依赖成环：永远等不到全部依赖完成")
        done: set[str] = set()
        queued: set[str] = set()
        order: list[str] = []
        heap = MinHeap()
        while len(order) < len(self._jobs):
            ready_ids = self._graph.ready(done)
            for job in sorted(self._jobs.values(), key=lambda j: j.seq):
                if job.id not in done and job.id not in queued and job.id in ready_ids:
                    heap.push(job.priority, job.seq, job.id)
                    queued.add(job.id)
            if len(heap) == 0:
                raise RuntimeError("没有可执行的任务：存在永远无法满足的依赖")
            entry = heap.pop()
            done.add(entry.payload)
            order.append(entry.payload)
        return order
