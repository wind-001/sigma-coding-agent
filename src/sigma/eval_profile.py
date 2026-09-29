"""评测用的消融档位（P2-6 详规 §4.2 / §11.5）。

B1 的定义（架构 7.3）=「有工具、满上下文、**无 harness 干预**」。在 sigma 里，
"干预"目前是两件真实存在的事：**自动压缩**与**影子 checkpoint**。这个类型把
它们声明成**可显式关掉的开关**，而不是让评测方删代码或靠"不传参数"碰运气。

为什么必须是显式开关（而不是"不传 ``compaction_policy``"）：
``InteractiveSession`` 会把 ``compaction_policy=None`` 替换成默认 32k 策略
（``sdk.DEFAULT_CONTEXT_WINDOW_TOKENS``）——**"默认开"不能被 ``None`` 绕过**，
那是设计立场不是缺陷（详规 §11.5 第 1 条）。所以另立一条显式的
``enable_compaction=False`` 通路，它的优先级**高于**默认替换。

字段最小化：**只放当前真实接线的行为**。``hooks`` 要等 P3-批次2 的
HookManager 落地、真有钩子可关的那天再加——一个永远不影响行为的字段是
名义开关，比没有更坏（门槛 G57 的教训：写断言却没有任何注入能让它红）。

P4-批次2 扩了两个字段（``todo`` / ``sub_agent``），它们**一落地就真实接线**：
``evals/task_runner.py`` 的 ``--no-todo`` / ``--sub-agent`` 直接映射到
``sdk.run_task(enable_todo=…, enable_sub_agent=…)``——不是名义开关。
"""

from __future__ import annotations

from pydantic import BaseModel


class EvalProfile(BaseModel):
    """消融档位。**只影响 harness 行为，不影响模型与工具本身。**

    报告里每一行数据都必须带 ``name``：没有档位名，两个月后没人说得清
    ``reports/`` 里那份 JSON 是在什么配置下产生的。
    """

    name: str = "B2"
    compaction: bool = True
    checkpoint: bool = True
    #: todo 工具的整组开关（注册表 + 提示词行 + steering 三处同源，见
    #: ``sdk.build_system_prompt(todo=…)``）。A/B 评测的"无 todo"对照臂
    #: 靠它**声明**——不是删代码，是显式档位（P4-批次2 D-A2）。
    todo: bool = True
    #: 主会话是否带 task 工具（可派 sub_agent）。默认关：与 sdk 的
    #: ``enable_sub_agent`` 默认值一致——不带的档位与之前逐字节一致。
    sub_agent: bool = False
    #: 轮数上限（透传 ``run_task`` 的 ``max_rounds``，默认 20）。轮数扫描
    #: （「多少 turn 下完成率不足」）靠它声明档位——显式传入时档位名加
    #: ``-r{N}`` 后缀，报告互不覆盖。
    max_rounds: int = 20

    @classmethod
    def b1(cls) -> EvalProfile:
        """B1：有工具、满上下文、**无**压缩、**无** checkpoint（对照组）。"""
        return cls(name="B1", compaction=False, checkpoint=False)

    @classmethod
    def b2(cls) -> EvalProfile:
        """B2：完整 harness（主结果档）。"""
        return cls(name="B2", compaction=True, checkpoint=True)
