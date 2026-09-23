"""任务集评测用的数据类型。

元数据用 ``BaseModel``、纯数据也用 ``BaseModel``——与项目其余部分一致
（判据见架构 4.0：**抽象用 ABC、数据用 BaseModel**）。

这些模型同时承担一件事：**它们是 meta.json 的 schema**。
字段写错、必填项缺失，在加载那一刻就报错，而不是等到报告里出现一个空白。
"""

from __future__ import annotations

from pydantic import BaseModel


class JudgeSpec(BaseModel):
    """判定命令。``cwd`` 相对于任务目录。"""

    command: str
    cwd: str = "workspace"


class JudgeTestsSpec(BaseModel):
    """判定用测试的原始副本（防篡改）。

    ``source`` 与 ``target`` 都相对于任务目录。
    """

    source: str
    target: str
    reason: str


class InitialFailure(BaseModel):
    """这条任务**在初始状态下确实失败**的证据（详规 §2.2 的硬性顺序）。

    没有这个字段的 B 类任务是无效的：它可能是"先写实现再补测试"造出来的，
    那样的任务测的是"本项目的实现恰好能过"，评测就失去意义。
    """

    command: str
    cwd: str
    exit_code: int
    summary: str
    failed_tests: list[str]
    recorded_at: str
    note: str = ""


class TaskSpec(BaseModel):
    """一条任务的全部元数据（``meta.json`` 的对映）。"""

    id: str
    kind: str
    title: str
    description: str
    judge: JudgeSpec
    workspace: str
    judge_tests: JudgeTestsSpec | None = None
    initial_failure: InitialFailure


class TaskResult(BaseModel):
    """agent 跑完一条任务后采集到的指标。

    ``eval_profile`` **每一行都要带**（详规 §4.2）：报告里每一行数据都要能回答
    "这是在哪个配置下产生的"。没有档位名，两个月后没人说得清 ``reports/`` 里那份
    JSON 是什么。
    """

    task_id: str
    eval_profile: str
    status: str
    rounds: int
    tool_calls: int
    tool_failures: int
    prompt_tokens: int
    completion_tokens: int
    wall_clock_s: float
    error: str | None = None
