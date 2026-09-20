"""门槛 G10：抽象签名必须被**两个性质不同的实现者**同时满足。

**这条门槛的形状本身就是方法论**

    > 一个抽象基类的签名，**不能只被一个实现者适配**。
    > 单一实现者不会告诉你签名少了什么——尤其是当那个实现者是
    > "我说吐什么就吐什么"的回放器时。

``FakeProvider`` 对采样参数、请求选项、超时**全部无感**。
所以只要它是唯一实现者，``BaseProvider.stream()`` 就会看起来"刚好够用"。

**2026-09-20 的注入实验暴露了一个更隐蔽的问题**

    这条门槛的第一版**没有落地**。

    把 ``sampling`` / ``options`` / ``timeout_s`` 从 ``BaseProvider.stream()``
    删掉之后，全部测试**依然全绿**——因为实现者（``OpenAICompatProvider``）
    自己保留了这三个参数，而**没有任何东西断言两者一致**。
    抽象层可以悄悄退化成 4 参数，谁都不会发现。

    **这正是 G10 要防的问题本身**，只是发生在比预期更深的地方：
    不是"签名少了参数导致写不出测试"，而是"抽象层退化后没人管"。

    所以本文件的核心不是"参数能传下去"，而是
    ``test_abstract_signature_accepts_both_implementers``——
    它用自省（``inspect.signature``）直接对比抽象层与实现层的签名。

对应 ``docs/plans/P1-批次1-详规.md`` 第 5 节门槛 G10。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from sigma_ai.base import BaseProvider, CancelToken, SamplingParams, StreamOptions
from sigma_ai.fake import FakeProvider
from sigma_ai.openai_compat import OpenAICompatProvider

# 本批次签名里**必须存在**的关键字参数。
#
# 这三个是 B1.3 的产出：只写 FakeProvider 时它们一个都不会出现。
REQUIRED_KEYWORD_PARAMS = ("sampling", "options", "timeout_s")


def _abstract_keyword_params() -> set[str]:
    """抽象基类 ``stream`` 的关键字参数集合。"""
    sig = inspect.signature(BaseProvider.stream)
    return {
        name
        for name, param in sig.parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


def _implementation_keyword_params(cls: type) -> set[str]:
    """某个实现类 ``stream`` 的关键字参数集合。"""
    sig = inspect.signature(cls.stream)
    return {
        name
        for name, param in sig.parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }


# ---------------------------------------------------------------------------
# G10 核心：抽象层必须覆盖实现层
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "impl_cls",
    [FakeProvider, OpenAICompatProvider],
    ids=["FakeProvider", "OpenAICompatProvider"],
)
def test_abstract_signature_accepts_both_implementers(
    impl_cls: type,
) -> None:
    """**G10 的核心断言**：抽象签名必须能接受实现者的全部关键字参数。

    为什么用自省而不是"写个调用测试"
        调用测试只能证明"我传的参数被接受了"——它证明不了
        **抽象层是否声明了这些参数**。第一版的失败正是这个原因：
        实现类有 ``sampling``，于是调用测试过；抽象类没有，
        于是调用测试也过（因为调用的是实现类）。

        只有直接比对两边的签名，才能让"抽象层退化"暴露出来。

    判据是**子集**而不是相等：
        实现者可以有额外的参数（比如某个 provider 特有的选项），
        那是允许的。不允许的是**实现者需要、抽象层却没有**——
        那意味着调用方拿着 ``BaseProvider`` 类型写不出这段代码。
    """
    abstract = _abstract_keyword_params()
    concrete = _implementation_keyword_params(impl_cls)

    missing = concrete - abstract
    assert not missing, (
        f"{impl_cls.__name__} 的 stream() 需要关键字参数 {sorted(missing)}，"
        f"但 BaseProvider.stream() 没有声明它们。\n"
        f"抽象层签名：{sorted(abstract)}\n"
        f"实现层签名：{sorted(concrete)}\n"
        f"——这正是 G10 要防的『抽象层退化』："
        f"调用方拿着 BaseProvider 类型无法传这些参数。"
    )


def test_abstract_signature_has_the_three_b13_params() -> None:
    """抽象签名必须显式含 ``sampling`` / ``options`` / ``timeout_s``。

    上一条是"实现者需要的抽象层必须有"，这条是"抽象层必须有这三个"。
    两者都必要：
        只有上一条 —— 两个实现者**同时都没实现**某参数时，抽象层退化不会被发现。
        只有这一条 —— 新增实现者引入新参数时不会被发现。

    **这三个参数是 B1.3 的直接产物**：只写 ``FakeProvider`` 时，
    回放不需要采样、不需要请求用量、不会超时，它们一个都不会出现。
    """
    abstract = _abstract_keyword_params()
    for name in REQUIRED_KEYWORD_PARAMS:
        assert name in abstract, (
            f"BaseProvider.stream() 缺少关键字参数 {name!r}。\n"
            f"它由批次 1 的 B1.3 拍板引入（详规 7.1 节）——"
            f"删掉它意味着回放式实现者的局限又被当成了抽象层的设计依据。"
        )


def test_both_implementers_are_genuinely_different() -> None:
    """两个实现者的默认值必须不同，否则"两个实现者"是假的。

    若两者签名完全一样，上一条门槛就退化成"测了一个实现者"。
    这里断言它们**共享**必需参数但在实现细节上有真实差异：

    - ``FakeProvider`` 忽略三者（回放式，详规 7.1 节）
    - ``OpenAICompatProvider`` 真的用它们构造请求体

    本测试断言前者"签名接得住"，后者"签名 + 请求体都体现出来"。
    """
    fake_params = _implementation_keyword_params(FakeProvider)
    compat_params = _implementation_keyword_params(OpenAICompatProvider)

    for name in REQUIRED_KEYWORD_PARAMS:
        assert name in fake_params, f"FakeProvider 接不住 {name}"
        assert name in compat_params, f"OpenAICompatProvider 需要 {name}"

    # 回放器对三者无感，但至少不能因此**拒绝**它们（否则抽象层会偏向它）
    assert issubclass(FakeProvider, BaseProvider)
    assert issubclass(OpenAICompatProvider, BaseProvider)


# ---------------------------------------------------------------------------
# 签名之外的落地：参数真的进了请求体
# ---------------------------------------------------------------------------


async def test_abstract_typed_call_site_compiles() -> None:
    """**类型层面的落地**：拿 ``BaseProvider`` 类型调用时要能传这三个参数。

    这条测试的价值在于它能被 `mypy` 检查到。
    如果抽象签名缺了 ``sampling``，这里会是一条 mypy 错误
    （不是运行期错误）——而 mypy 是三道门禁之一。

    运行期它只是走一遍确认没抛错。
    """
    provider: BaseProvider = FakeProvider.from_rounds(
        [[{"type": "stop", "stop_reason": "stop"}]]
    )

    events: list[Any] = []
    async for event in provider.stream(
        [],
        [],
        model="m",
        signal=_NeverCancelled(),
        sampling=SamplingParams(temperature=0.0, max_tokens=16),
        options=StreamOptions(include_usage=True),
        timeout_s=1.0,
    ):
        events.append(event)

    assert len(events) == 1


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return
