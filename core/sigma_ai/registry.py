"""Provider 注册表：按名解析 provider 的**连接参数**。

为什么要它
    架构方案第 248 行的分层职责表把「Provider Registry · 事件流 · 消息变换」
    一起划给 ``sigma_ai``。事件流（``events.py``）与消息变换
    （``openai/convert.py``、``convert_to_llm``）都有实现，
    **只有这个注册表是 2026-09-20 才补上的**。

    在那之前，"有哪些 provider、它们的 base_url 与默认模型是什么"
    被硬编码在 ``core/sigma/cli.py`` 里——**那是产品壳层，不该持有协议层的知识**。
    后果很具体：换一个入口（直接调 `sdk.run_task`、批次 5 的评测运行器）
    就得再抄一份列表；P4 想注册自定义 provider 时没有落点。

**它只存构造参数，不存 provider 实例**
    实例的创建要传 ``api_key`` / ``httpx.AsyncClient`` / 超时，
    那些是**调用期的决定**，固化进注册表就等于把凭据和连接池塞进了配置。
    这也是它**不复用 `ToolRegistry`** 的原因——后者存的是"有行为的实例"，
    两者形态不同，共用抽象会把两件事硬捏成一个。

P1 明确不做（写清边界，避免被读成"忘了"）
    - **不做热重载**（属 P4）
    - **不做 OAuth**（Pi 的 pi-ai 有；sigma 明确不做）
    - **不做成本追踪**（同上）
    - **不做从配置文件加载**（``~/.pi/agent/models.json`` 那种，属 P4）
    - **不做别名**（没有任何 P1 需求；加一个用不上的字段就是负债）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence


class ProviderSpec(BaseModel):
    """一个 provider 的**静态描述**。

    刻意只有三个字段——只装"怎么连上它"，**不含凭据**。
    凭据属于调用期（见模块 docstring）。
    """

    name: str
    base_url: str
    default_model: str


class UnknownProvider(KeyError):
    """按名解析时找不到。

    继承 ``KeyError`` 而不是自定义 ``Exception``：它确实是"键不存在"，
    且这样调用方可以用既有的异常处理路径接住。
    与批次 1 的 ``TRY004`` 经验一致（类型错误用 ``TypeError``，值非法用 ``ValueError``，
    键缺失用 ``KeyError``）。
    """

    def __init__(self, name: str, known: Sequence[str]) -> None:
        self.name = name
        self.known = list(known)
        available = ", ".join(self.known) or "(空)"
        super().__init__(f"未注册的 provider {name!r}。已注册：{available}")


class ProviderRegistry:
    """名字 → :class:`ProviderSpec` 的登记与解析。"""

    def __init__(self) -> None:
        self._specs: dict[str, ProviderSpec] = {}

    def register(self, spec: ProviderSpec) -> None:
        """登记一个 provider。

        **重名直接抛**，不做静默覆盖——与 `ToolRegistry` 同一条原则：
        静默覆盖是最难排查的一类 bug（改了配置却看不出哪份生效）。
        """
        if spec.name in self._specs:
            existing = self._specs[spec.name]
            raise ValueError(
                f"provider 名 {spec.name!r} 已被注册"
                f"（现有 base_url={existing.base_url!r}）。"
                "**不允许静默覆盖**——同一名字对应两个端点是最难排查的一类问题。"
            )
        self._specs[spec.name] = spec

    def resolve(self, name: str) -> ProviderSpec:
        """按名解析。找不到抛 :class:`UnknownProvider`（含可用列表）。"""
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownProvider(name, self.names()) from None

    def names(self) -> list[str]:
        """已登记的名字，**按字典序**。

        顺序稳定是为了让它的使用者（CLI 的 `--help`、`--preset` 的 choices）
        输出可复现——不稳定的顺序会让"帮助文本变了"成为噪音。
        """
        return sorted(self._specs)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)


def builtin_providers() -> ProviderRegistry:
    """P1 内置的 provider 列表。

    这五个都是 **OpenAI 兼容协议**的服务，所以共用同一个
    `OpenAICompatProvider`——注册表登记的是**连接参数**，不是实现类。
    将来要接 Anthropic 那类非兼容协议时，`ProviderSpec` 需要增加
    "用哪个实现类"的字段；**现在不加**，因为没有第二个实现可填。
    """
    registry = ProviderRegistry()
    for spec in (
        ProviderSpec(
            name="deepseek",
            base_url="https://api.deepseek.com/v1",
            default_model="deepseek-chat",
        ),
        ProviderSpec(
            name="moonshot",
            base_url="https://api.moonshot.cn/v1",
            default_model="moonshot-v1-8k",
        ),
        ProviderSpec(
            name="zhipu",
            base_url="https://open.bigmodel.cn/api/paas/v4",
            default_model="glm-4-flash",
        ),
        ProviderSpec(
            name="dashscope",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            default_model="qwen-plus",
        ),
        ProviderSpec(
            name="ollama",
            base_url="http://localhost:11434/v1",
            default_model="qwen2.5:7b",
        ),
    ):
        registry.register(spec)
    return registry
