"""P5-批次3 的门槛测试:跨会话记忆(G879–G884)。

每条测试对应详规里的一张门槛卡,**每条都有"注入变红"的路径**
(scripts/gate_injection_batch18.py)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sigma.providers.base import CancelToken
from sigma.providers.fake import FakeProvider
from sigma.providers.messages import TextBlock
from sigma.sessions.context import (
    DEFAULT_RESIDENT_BUDGET_TOKENS,
    ResidentBudgetExceeded,
    SessionContext,
)
from sigma.memory.file_store import (
    memory_dir_for,
    render_memory_index,
    scan_memory,
)

import sigma.sdk as sdk_mod
from sigma.sdk import InteractiveSession, build_system_prompt

from sigma.providers.stamps import from_epoch as ts

FIXED_TIME = ts(1_700_000_000)


class _NeverCancelled(CancelToken):
    def is_cancelled(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


def _write_memory(workspace: Path, slug: str, title: str, body: str = "内容") -> None:
    memory_dir = memory_dir_for(workspace)
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{slug}.md").write_text(f"# {title}\n{body}\n", encoding="utf-8")


def _text_round(text: str) -> list[dict[str, object]]:
    return [
        {"type": "text_delta", "text": text, "text_signature": None},
        {"type": "stop", "stop_reason": "stop"},
    ]


def _context(memory_index: str = "", **kwargs: object) -> SessionContext:
    return SessionContext(
        system_prompt="测试",
        tools_schema=[],
        clock=lambda: FIXED_TIME,
        memory_index=memory_index,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# G879:跨会话闭环——会话 1 写的记忆,会话 2 的常驻区看得到
# ---------------------------------------------------------------------------


def test_g879_cross_session_memory_loop(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 会话 1:模型经 write 落盘(这里等价地直接写文件)
    _write_memory(workspace, "project-facts", "项目事实")

    # 会话 2:新建上下文(会话启动扫描)→ 索引进常驻区
    scan = scan_memory(memory_dir_for(workspace))
    index = render_memory_index(scan)
    assert index != ""
    context = _context(memory_index=index)
    messages = context.build_messages()
    # build_messages 返回 agent 层消息:首条是包着 SystemMessage 的包装器
    first = messages[0]
    inner = getattr(first, "message", first)
    system_content = inner.content
    system_text = system_content if isinstance(system_content, str) else "".join(
        block.text for block in system_content
    )

    assert "项目事实" in system_text
    assert "memory/project-facts.md" in system_text
    # 纪律段与索引同源:build_system_prompt(memory=True) 有段、False 逐字节不变
    assert "记忆纪律" in build_system_prompt(memory=True)
    assert build_system_prompt() == build_system_prompt(memory=False)


# ---------------------------------------------------------------------------
# G880:索引硬上限 250 token,截断在正文可见
# ---------------------------------------------------------------------------


def test_g880_index_capped_and_truncation_visible(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # 长标题 × 20:确保总量顶爆 250,截断路径真的被执行
    for i in range(20):
        _write_memory(
            workspace,
            f"memo-{i:02d}",
            f"记忆条目 {i:02d} 的相当长的标题,描述了一条具体且有些冗长的项目事实内容",
        )
    scan = scan_memory(memory_dir_for(workspace))
    assert len(scan.entries) == 20

    index = render_memory_index(scan)
    from sigma.providers.tokens import estimate_text

    assert estimate_text(index) <= 250
    assert "未显示" in index  # 截断必须可见,不静默
    assert "memory/memo-00.md" in index  # 前面的条目仍在
    assert "memory/memo-19.md" not in index  # 后面的条目被截掉


# ---------------------------------------------------------------------------
# G881:空工作区零污染——有记忆机制与没有,常驻区逐字节一致
# ---------------------------------------------------------------------------


def test_g881_empty_workspace_zero_pollution(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()  # 存在但没有任何记忆文件

    scan = scan_memory(memory_dir_for(workspace))
    assert scan.entries == ()
    assert render_memory_index(scan) == ""

    without = _context()
    with_empty = _context(memory_index="")
    assert without.resident_text() == with_empty.resident_text()
    assert without.fingerprint == with_empty.fingerprint
    # 不存在的目录同样处理(全新 workspace 首跑)
    scan_missing = scan_memory(tmp_path / "no-such-dir")
    assert render_memory_index(scan_missing) == ""


# ---------------------------------------------------------------------------
# G882:回滚安全——.sigma/ 在 checkpoint 排除清单,记忆不被回滚滚丢
# ---------------------------------------------------------------------------


def test_g882_memory_survives_rollback(tmp_path: Path) -> None:
    """回滚的正确不变量:**不碰记忆**,而不是"找回记忆"。

    ``.sigma/`` 在 BUILTIN_EXCLUDES 里 → 记忆文件根本不进快照——
    所以 restore 既不会删它(排除清单的意义),也不会找回它
    (记忆不是版本化数据,这是设计不是缺陷)。破坏对象用普通文件。
    """
    from sigma.security.shadow_checkpoint import ShadowCheckpoint

    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_memory(workspace, "keep-me", "必须存活")
    code = workspace / "code.py"
    code.write_text("v1", encoding="utf-8")
    checkpoint = ShadowCheckpoint(
        root=tmp_path / "shadow.git", workspace=workspace, branch="mem-test"
    )
    assert checkpoint.available, checkpoint.unavailable_reason

    ref = checkpoint.mark(label="before-destroy")
    assert ref is not None
    code.write_text("v2", encoding="utf-8")  # 破坏对象:普通文件
    _write_memory(workspace, "added-later", "mark 之后才写的记忆")

    report = checkpoint.restore(ref)
    assert report.ok, report.note
    assert code.read_text(encoding="utf-8") == "v1", "普通文件应被回滚"
    memory_file = memory_dir_for(workspace) / "keep-me.md"
    assert memory_file.exists(), "mark 前的记忆被回滚碰掉——排除清单失效"
    assert "必须存活" in memory_file.read_text(encoding="utf-8")
    later = memory_dir_for(workspace) / "added-later.md"
    assert later.exists(), "mark 后新增的记忆被 read-tree --reset -u 删掉——排除清单失效"


# ---------------------------------------------------------------------------
# G883:memory_index 计入预算——超限如实抛,不豁免
# ---------------------------------------------------------------------------


def test_g883_memory_index_counts_toward_budget() -> None:
    big_index = "\n".join(f"- 记忆条目 {i} 的标题 → memory/m{i}.md" for i in range(800))
    context = _context(memory_index=big_index)
    with pytest.raises(ResidentBudgetExceeded):
        context.verify_resident_budget()
    # 正常尺寸不抛
    normal = _context(
        memory_index=render_memory_index(
            scan_memory(memory_dir_for(_mini_workspace_with_two_memories()))
        )
    )
    normal.verify_resident_budget()
    assert normal.resident_tokens <= DEFAULT_RESIDENT_BUDGET_TOKENS


def _mini_workspace_with_two_memories() -> Path:
    import tempfile

    ws = Path(tempfile.mkdtemp(prefix="sigma-mem-mini-"))
    _write_memory(ws, "a", "甲")
    _write_memory(ws, "b", "乙")
    return ws


# ---------------------------------------------------------------------------
# G884:会话内冻结——sdk 只在构造时扫一次,send 不重扫
# ---------------------------------------------------------------------------


def test_g884_scan_happens_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    real_scan = sdk_mod.scan_memory

    def counting_scan(memory_dir: Path):
        calls["n"] += 1
        return real_scan(memory_dir)

    monkeypatch.setattr(sdk_mod, "scan_memory", counting_scan)
    session = InteractiveSession(
        provider=FakeProvider.from_rounds(
            [_text_round("一"), _text_round("二"), _text_round("三")]
        ),
        workspace_root=tmp_path,
        model="fake",
        registry=_echo_registry(),
        system_prompt="测试",
        enable_compaction=False,
        enable_todo=False,
    )
    import asyncio

    asyncio.run(session.send("第一句"))
    asyncio.run(session.send("第二句"))
    # 会话中途写入新记忆——本会话不可见(冻结),扫描次数仍是构造时的 1
    _write_memory(tmp_path, "late", "迟到的一条")
    asyncio.run(session.send("第三句"))
    assert calls["n"] == 1, "send 路径发生了重扫——常驻区会从变化点失效缓存(D4)"

    # 快照语义:构造时拿到的 scan 不因磁盘变化而变
    scan_snapshot = session._memory_scan  # noqa: SLF001
    assert scan_snapshot is not None
    assert all(entry.slug != "late" for entry in scan_snapshot.entries)


def _echo_registry():
    from pydantic import BaseModel, Field

    from sigma.tools.base import BaseTool
    from sigma.tools.registry import ToolRegistry
    from sigma.agent.types import ToolContext, ToolResult

    class EchoParams(BaseModel):
        message: str = Field(description="内容")

    class EchoTool(BaseTool):
        name = "echo"
        description = "回显"
        read_only = True

        @property
        def params(self) -> type[BaseModel]:
            return EchoParams

        async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
            return ToolResult(content=[TextBlock(text="ok")])

    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry
