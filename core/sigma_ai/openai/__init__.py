"""OpenAI 兼容协议的实现（自研，**不是**官方 SDK）。

包名沿用了协议的厂商名，但内容与 ``openai`` 官方 SDK 无关——
本包直连 ``httpx``，理由是"SDK 会藏起本层要验证的协议细节"，
详见 ``provider.py`` 的 docstring。

对外只暴露 ``OpenAICompatProvider``。其余模块（``protocol`` / ``convert`` /
``sse``）是**实现细节**，仅供测试与排查直接引用。
"""

from __future__ import annotations

from sigma_ai.openai.provider import OpenAICompatProvider

__all__ = ["OpenAICompatProvider"]
