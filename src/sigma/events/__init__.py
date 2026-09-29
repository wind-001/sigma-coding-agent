"""events 层:生命周期事件定义(纯 dataclass,零行为)。hooks 是订阅者。"""
from sigma.events.lifecycle import HookEvent

__all__ = ["HookEvent"]
