"""sigma —— 自研 coding agent harness（唯一顶层包，P6 起按功能域分子包）。

子包与分层
    cli(终端壳) / sdk(唯一装配点) / runtime(核心循环+sub_agent) / agent(消息模型) /
    providers(协议层,最底层) / events(生命周期事件) / hooks(订阅者与总线) /
    tools(工具) / skills(技能) / sessions(会话) / memory(跨会话记忆) /
    security(三层边界) / observability(观测) / prompts(提示词资产) / config(配置)。
    依赖方向由 pyproject 的 import-linter 15 层契约强制——低层引用高层即 CI 失败。

入口
    ``python -m sigma`` / console script → ``sigma.cli.main:main``；
    SDK 组装 → ``sigma.sdk``（唯一装配点,cli 与评测都经它）。

现行结构详录:``docs/architecture.md`` §3.1(v1.9)。
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__: list[str] = ["__version__"]
