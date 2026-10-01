"""扩展热重载的门槛测试（P3-扩展热重载，G-HR-1..5；详规 §5）。

G-HR-1  reload 后当轮生效：``get(name)`` 返回新实例（替换实现验证）。
G-HR-2  三失败场景：导入异常保留旧表 / 空 TOOLS 清理旧项 / 重名拒绝。
G-HR-3  会话内 reload：指纹重算不炸、历史保留、persist 钩子接到新 context。
G-HR-4  启动装载：坏扩展警告继续、``enable_extensions=False`` 全关。
G-HR-5  ReloadReport 字段与内置重名拒绝（DuplicateToolError 词汇）。

扩展文件全部由测试**临时写盘**（tmp_path/extensions/*.py）——
装载走的是真实的 ``spec_from_file_location`` + ``exec_module``，
与产品路径同一条代码，没有为测试开的旁门。
registry 由测试**自己构造再传入**（InteractiveSession 只增不改的账），
断言直接打在测试持有的引用上，不翻会话的私有属性。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from sigma.agent.messages import LlmMessageWrapper
from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.messages import TextBlock, UserMessage
from sigma.providers.stamps import from_epoch as ts
from sigma.sdk import InteractiveSession
from sigma.tools.base import BaseTool
from sigma.tools.registry import ReloadReport, ToolRegistry

FIXED_TIME = ts(1_700_000_000)

# ---------------------------------------------------------------------------
# 测试用具
# ---------------------------------------------------------------------------

#: 一份**能装载**的扩展。version 与 description 带 "v1"，v2 只改这两处——
#: G-HR-1 靠它们识别"新实例真的换上了"。
EXT_GREET_V1 = '''"""Greet 扩展 v1。"""
from pydantic import BaseModel, Field

from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.messages import TextBlock
from sigma.tools.base import BaseTool

VERSION = "v1"


class GreetParams(BaseModel):
    who: str = Field(default="", description="跟谁打招呼")


class GreetTool(BaseTool):
    name = "greet"
    description = "打招呼（v1）"
    read_only = True
    version = VERSION

    @property
    def params(self) -> type[BaseModel]:
        return GreetParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="hi v1")])


TOOLS = [GreetTool()]
'''

EXT_GREET_V2 = EXT_GREET_V1.replace('VERSION = "v1"', 'VERSION = "v2"').replace(
    "打招呼（v1）", "打招呼（v2）"
)

#: 语法错误文件：导入必炸（场景 1 的触发器）。
EXT_SYNTAX_ERROR = 'def broken(:\n    pass\n'

#: 空 TOOLS：模块能导入但不再导出工具 = "扩展已移除"（场景 2）。
EXT_EMPTY_TOOLS = '"""Greet 扩展，已撤空。"""\nTOOLS: list = []\n'

#: 与内置 echo 重名（场景 3）。
EXT_CLASH_ECHO = '''"""冒充内置 echo 的坏扩展。"""
from pydantic import BaseModel

from sigma.agent.types import ToolContext, ToolResult
from sigma.providers.messages import TextBlock
from sigma.tools.base import BaseTool


class FakeParams(BaseModel):
    message: str = ""


class FakeEchoTool(BaseTool):
    name = "echo"
    description = "冒牌货"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return FakeParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="fake")])


TOOLS = [FakeEchoTool()]
'''

#: TOOLS 形状不对（不是列表）——坏扩展，保留旧表。
EXT_BAD_SHAPE = 'TOOLS = "不是列表"\n'


class EchoParams(BaseModel):
    message: str = Field(default="", description="要回显的内容")


class EchoTool(BaseTool):
    """充当"内置工具"的重名对照物（场景 3 要撞的就是它）。"""

    name = "echo"
    description = "回显 message"
    read_only = True

    @property
    def params(self) -> type[BaseModel]:
        return EchoParams

    async def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content=[TextBlock(text="echo")])


class _SilentProvider:
    """从不被真正调用的 provider 占位（本文件的会话测试不跑轮）。

    只实现类型层要求的两个名字； InteractiveSession 构造期不会碰它。
    """

    async def stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("本文件的测试不应真的发起模型请求")

    def estimate_tokens(self, messages: list[Any]) -> int:  # pragma: no cover
        return 10


def _write_ext(workspace: Path, filename: str, body: str) -> Path:
    """往 ``<workspace>/extensions/`` 写一个扩展文件，返回路径。"""
    directory = workspace / "extensions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def _registry_with_builtin() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return registry


def _make_session(
    workspace: Path, *, enable_extensions: bool
) -> tuple[InteractiveSession, ToolRegistry]:
    """带扩展装载的最小会话。registry 返还给测试——断言打在持有引用上。"""
    registry = _registry_with_builtin()
    session = InteractiveSession(
        provider=_SilentProvider(),  # type: ignore[arg-type]
        workspace_root=workspace,
        model="fake",
        registry=registry,
        system_prompt="测试",
        max_rounds=6,
        enable_todo=False,
        enable_extensions=enable_extensions,
    )
    return session, registry


# ---------------------------------------------------------------------------
# G-HR-1：reload 后当轮生效
# ---------------------------------------------------------------------------


def test_reload_takes_effect_get_returns_new_instance(tmp_path: Path) -> None:
    """改文件 → reload_source → get(name) 必须是**新实例**（G-HR-1 全部内容）。"""
    registry = _registry_with_builtin()
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    reports = registry.load_extensions(tmp_path / "extensions")
    assert [r.ok for r in reports] == [True]
    first = registry.get("greet")
    assert first.version == "v1"  # type: ignore[attr-defined]

    path.write_text(EXT_GREET_V2, encoding="utf-8")
    report = registry.reload_source(str(path))

    assert report.ok
    assert report.added == ("greet",)
    assert report.removed == ("greet",)
    second = registry.get("greet")
    assert second is not first
    assert second.version == "v2"  # type: ignore[attr-defined]


def test_session_reload_tools_replaces_tool(tmp_path: Path) -> None:
    """会话级当轮生效：reload_tools 之后经 registry 拿到的是 v2。"""
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    session, registry = _make_session(tmp_path, enable_extensions=True)
    old = registry.get("greet")

    path.write_text(EXT_GREET_V2, encoding="utf-8")
    reports = session.reload_tools()

    assert len(reports) == 1 and reports[0].ok
    assert registry.get("greet") is not old
    assert registry.get("greet").version == "v2"  # type: ignore[attr-defined]


def test_reload_without_extensions_is_noop(tmp_path: Path) -> None:
    """没有已装载来源时 reload_tools 返回 [] 且**不重建 context**（同一对象）。"""
    session, _registry = _make_session(tmp_path, enable_extensions=False)
    context = session.context
    assert session.reload_tools() == []
    assert session.context is context


# ---------------------------------------------------------------------------
# G-HR-2 / G-HR-5：三个失败场景 + 报告字段
# ---------------------------------------------------------------------------


def test_import_error_keeps_old_table(tmp_path: Path) -> None:
    """场景 1：导入异常 → 报告失败，旧实例原封不动。"""
    registry = _registry_with_builtin()
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    registry.load_extensions(tmp_path / "extensions")
    first = registry.get("greet")

    path.write_text(EXT_SYNTAX_ERROR, encoding="utf-8")
    report = registry.reload_source(str(path))

    assert not report.ok
    assert "导入失败" in report.failed_reason
    assert registry.get("greet") is first


def test_empty_tools_removes_old_entries(tmp_path: Path) -> None:
    """场景 2：TOOLS 置空 = 扩展已移除，旧注册项被清理。"""
    registry = _registry_with_builtin()
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    registry.load_extensions(tmp_path / "extensions")

    path.write_text(EXT_EMPTY_TOOLS, encoding="utf-8")
    report = registry.reload_source(str(path))

    assert report.ok
    assert report.added == ()
    assert report.removed == ("greet",)
    with pytest.raises(KeyError):
        registry.get("greet")


def test_duplicate_with_builtin_rejected_and_builtin_intact(tmp_path: Path) -> None:
    """场景 3：扩展冒充内置 echo → 拒绝（DuplicateToolError 词汇），内置完好。"""
    registry = _registry_with_builtin()
    builtin_echo = registry.get("echo")
    _write_ext(tmp_path, "fake_echo.py", EXT_CLASH_ECHO)

    reports = registry.load_extensions(tmp_path / "extensions")

    assert len(reports) == 1
    assert not reports[0].ok
    assert reports[0].failed_reason.startswith("DuplicateToolError")
    assert registry.get("echo") is builtin_echo


def test_duplicate_on_reload_keeps_previous_version(tmp_path: Path) -> None:
    """重载时新增了与内置重名的工具 → 整批拒绝，v1 原样在岗（不进半更新）。"""
    registry = _registry_with_builtin()
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    registry.load_extensions(tmp_path / "extensions")
    first = registry.get("greet")

    # v2 文件 = greet（v2）之外还定义 FakeEchoTool 并一起导出 → 重名拦**整批**。
    # 拼接顺序：先两个类定义，最后才是 TOOLS 行（模块自上而下执行）。
    path.write_text(
        EXT_GREET_V2.split("TOOLS = [GreetTool()]", 1)[0]
        + EXT_CLASH_ECHO.split("TOOLS = [FakeEchoTool()]", 1)[0]
        + "TOOLS = [GreetTool(), FakeEchoTool()]\n",
        encoding="utf-8",
    )
    report = registry.reload_source(str(path))

    assert not report.ok
    assert report.failed_reason.startswith("DuplicateToolError")
    assert "echo" in report.failed_reason
    assert registry.get("greet") is first
    assert registry.get("echo").description == "回显 message"


def test_reload_unknown_source_raises_keyerror(tmp_path: Path) -> None:
    """未知来源抛 KeyError 并列出已装载来源——拼错文件名要能自查。"""
    registry = _registry_with_builtin()
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    registry.load_extensions(tmp_path / "extensions")

    with pytest.raises(KeyError, match="nope"):
        registry.reload_source(str(tmp_path / "extensions" / "nope.py"))


def test_bad_tools_shape_keeps_old_table(tmp_path: Path) -> None:
    """TOOLS 不是列表 → 坏扩展，报告失败、旧表不动（与"空 TOOLS=移除"分开）。"""
    registry = _registry_with_builtin()
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    registry.load_extensions(tmp_path / "extensions")
    first = registry.get("greet")

    path.write_text(EXT_BAD_SHAPE, encoding="utf-8")
    report = registry.reload_source(str(path))

    assert not report.ok
    assert "TOOLS 必须是" in report.failed_reason
    assert registry.get("greet") is first


def test_reload_report_is_frozen() -> None:
    """报告是结果记录，消费方不该改它（frozen dataclass）。"""
    report = ReloadReport(source="x", added=("a",))
    with pytest.raises(Exception):
        report.source = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# G-HR-3：会话内 reload——指纹重算、历史保留、persist 接线
# ---------------------------------------------------------------------------


def test_session_reload_rebuilds_context_keeps_history(tmp_path: Path) -> None:
    """重建后：新指纹、build_messages 不炸、历史与压缩视图原样。"""
    _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    session, _registry = _make_session(tmp_path, enable_extensions=True)
    keep_node = session.context.append(
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="早前的任务", timestamp="2026-10-01T00:00:00.000"),
        )
    )
    # 预置一个压缩视图（摘要 + 保留窗口起点）——rebuild 必须把它带过去。
    session.context.apply_compaction(
        LlmMessageWrapper(
            timestamp=FIXED_TIME,
            message=UserMessage(content="（摘要）此前压过一轮", timestamp="2026-10-01T00:00:01.000"),
        ),
        keep_from=keep_node or "",
    )
    history_before = len(session.history())
    fingerprint_before = session.context.fingerprint

    (tmp_path / "extensions" / "greet.py").write_text(EXT_GREET_V2, encoding="utf-8")
    reports = session.reload_tools()

    assert reports and reports[0].ok
    assert session.context.fingerprint != fingerprint_before
    # build_messages 内部先跑指纹 + 预算两道校验——返回即重建自洽，
    # 且系统消息仍以常驻区文本开头。
    messages = session.context.build_messages()
    assert len(session.history()) == history_before
    # 压缩视图随迁：摘要在位，且发模型的历史以摘要开头（而不是原始全量）。
    assert session.context.summary is not None
    effective = session.context.effective_history()
    assert effective[0] is session.context.summary
    system_content = messages[0].message.content
    assert isinstance(system_content, str) and system_content.startswith("测试")


def test_session_reload_rebinds_persist_hook(tmp_path: Path) -> None:
    """persist 钩子原地 rebind 到新 context；同实例、新引用。"""
    _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    session, _registry = _make_session(tmp_path, enable_extensions=True)
    persist_before = session.persist_hook
    assert persist_before.context is session.context

    (tmp_path / "extensions" / "greet.py").write_text(EXT_GREET_V2, encoding="utf-8")
    session.reload_tools()

    assert session.persist_hook is persist_before  # 同一实例，原地 rebind
    assert session.persist_hook.context is session.context  # 接到了新 context


def test_session_reload_with_broken_extension_keeps_session_usable(
    tmp_path: Path,
) -> None:
    """重载失败（语法错误）→ 会话照常可用：build_messages 不炸、旧工具仍在。"""
    path = _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    session, registry = _make_session(tmp_path, enable_extensions=True)
    old = registry.get("greet")

    path.write_text(EXT_SYNTAX_ERROR, encoding="utf-8")
    reports = session.reload_tools()

    assert not reports[0].ok
    assert registry.get("greet") is old
    session.context.build_messages()  # 指纹校验通过（schema 未变）


# ---------------------------------------------------------------------------
# G-HR-4：启动装载
# ---------------------------------------------------------------------------


def test_startup_loads_good_and_reports_bad(tmp_path: Path) -> None:
    """坏扩展警告继续：好的注册上、坏的留在报告里，会话照常构造。"""
    _write_ext(tmp_path, "greet.py", EXT_GREET_V1)
    _write_ext(tmp_path, "broken.py", EXT_SYNTAX_ERROR)

    session, registry = _make_session(tmp_path, enable_extensions=True)

    reports = session.extension_reports
    assert len(reports) == 2
    by_name = {Path(r.source).name: r for r in reports}
    assert by_name["greet.py"].ok
    assert by_name["greet.py"].added == ("greet",)
    assert not by_name["broken.py"].ok
    assert "导入失败" in by_name["broken.py"].failed_reason
    assert registry.get("greet").version == "v1"  # type: ignore[attr-defined]


def test_startup_disabled_loads_nothing(tmp_path: Path) -> None:
    """enable_extensions=False：不扫描、不注册，报告为空。"""
    _write_ext(tmp_path, "greet.py", EXT_GREET_V1)

    session, registry = _make_session(tmp_path, enable_extensions=False)

    assert session.extension_reports == []
    with pytest.raises(KeyError):
        registry.get("greet")
