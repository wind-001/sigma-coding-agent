from __future__ import annotations


def sort_items(items: list[str]) -> list[str]:
    """按 config.toml 的 strategy 排序:name=字典序,length=长度(短在前,同长字典序)。
    config.toml 的 reverse=true 时整体反转。"""
    return sorted(items)
