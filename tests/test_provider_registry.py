"""Provider 注册表的测试。

它补的是架构第 248 行规划、但此前**一直没实现**的那一块
（「Provider Registry · 事件流 · 消息变换」里的第一项）。
"""

from __future__ import annotations

import pytest

from sigma_ai.registry import (
    ProviderRegistry,
    ProviderSpec,
    UnknownProvider,
    builtin_providers,
)


def _spec(name: str, *, url: str = "https://example.invalid/v1") -> ProviderSpec:
    return ProviderSpec(name=name, base_url=url, default_model="m")


class TestRegister:
    def test_register_and_resolve(self) -> None:
        registry = ProviderRegistry()
        registry.register(_spec("x", url="http://x/v1"))
        spec = registry.resolve("x")
        assert spec.base_url == "http://x/v1"
        assert spec.default_model == "m"

    def test_duplicate_name_raises(self) -> None:
        """重名直接抛，不静默覆盖——与 `ToolRegistry` 同一条原则。"""
        registry = ProviderRegistry()
        registry.register(_spec("x", url="https://a/v1"))
        with pytest.raises(ValueError, match="不允许静默覆盖"):
            registry.register(_spec("x", url="https://b/v1"))

    def test_duplicate_leaves_registry_intact(self) -> None:
        """抛错之后注册表**保持旧状态**。

        与 `ToolRegistry` 的 G24 同源：任何"先删后插"的写法都会在失败时
        留下半更新状态，而症状是"配置看着对但行为不对"，不指向根因。
        """
        registry = ProviderRegistry()
        first = _spec("x", url="https://a/v1")
        registry.register(first)

        with pytest.raises(ValueError):
            registry.register(_spec("x", url="https://b/v1"))

        assert registry.resolve("x") == first


class TestResolve:
    def test_unknown_raises_with_available_list(self) -> None:
        """找不到时**必须给出可用列表**——否则用户只能去翻源码。"""
        registry = ProviderRegistry()
        registry.register(_spec("alpha"))

        with pytest.raises(UnknownProvider) as exc_info:
            registry.resolve("nope")

        assert exc_info.value.name == "nope"
        assert "alpha" in str(exc_info.value)

    def test_unknown_provider_is_a_keyerror(self) -> None:
        """继承 `KeyError` 是刻意的：它确实是"键不存在"，
        调用方可以用既有的异常处理路径接住。"""
        registry = ProviderRegistry()
        with pytest.raises(KeyError):
            registry.resolve("nope")

    def test_names_are_sorted(self) -> None:
        """顺序稳定——CLI 的 `--help` 与 `--preset` 的 choices 依赖它可复现。

        不稳定的顺序会让"帮助文本变了"成为噪音（每次 diff 都动）。
        """
        registry = ProviderRegistry()
        for name in ("zeta", "alpha", "mu"):
            registry.register(_spec(name))
        assert registry.names() == ["alpha", "mu", "zeta"]

    def test_contains_and_len(self) -> None:
        registry = ProviderRegistry()
        registry.register(_spec("x"))
        assert "x" in registry
        assert "y" not in registry
        assert len(registry) == 1


class TestBuiltins:
    def test_expected_names_present(self) -> None:
        names = builtin_providers().names()
        for expected in ("deepseek", "moonshot", "zhipu", "dashscope", "ollama"):
            assert expected in names

    def test_deepseek_matches_the_pinned_decision(self) -> None:
        """Q3 拍板的首个被测模型是 DeepSeek `deepseek-chat`。

        注册表里的默认模型必须与那条决策一致——否则 **CLI 的默认行为
        会与文档不符**，而这类不一致只有真跑一次才会被发现。
        """
        assert builtin_providers().resolve("deepseek").default_model == "deepseek-chat"

    def test_non_local_providers_use_https(self) -> None:
        """除本地服务外，**必须是 https**。

        这不是洁癖：**api_key 会放进 `Authorization` 头**，
        明文 http 等于把 key 暴露给链路上的任何人。
        本地服务（ollama）不受此限——它不出网。
        """
        registry = builtin_providers()
        for name in registry.names():
            spec = registry.resolve(name)
            if name == "ollama":
                assert spec.base_url.startswith("http://localhost")
            else:
                assert spec.base_url.startswith("https://"), (
                    f"{name} 的 base_url 不是 https：{spec.base_url}"
                )

    def test_spec_carries_no_credentials(self) -> None:
        """`ProviderSpec` **不得**含凭据字段。

        凭据属于调用期（`OpenAICompatProvider(api_key=...)`），
        固化进注册表就等于把密钥塞进了配置对象——
        而配置对象会被打印、会被序列化进日志。
        """
        fields = set(ProviderSpec.model_fields)
        assert fields == {"name", "base_url", "default_model"}
        assert not any("key" in f or "token" in f for f in fields)
