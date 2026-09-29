"""Anthropic Messages 协议的实现（自研，**不是**官方 SDK）。

包名沿用协议的厂商名，但内容与 ``anthropic`` 官方 SDK 无关——
本包直连 ``httpx``。理由与 ``openai/`` 那边一致（那边写了完整五条论证），
要点：零新增依赖、SDK 恰好藏起本层要验证的协议细节（``event:`` 行分帧、
``message_stop`` 结束锚）、两个协议实现之间保持互不依赖。

对外只暴露 ``AnthropicProvider``。其余模块（``protocol`` / ``convert`` /
``sse``）是**实现细节**，仅供测试与排查直接引用。
"""

from __future__ import annotations

from sigma.providers.anthropic.provider import AnthropicProvider

__all__ = ["AnthropicProvider"]
