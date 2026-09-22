"""批次 1 门槛注入实验：逐条证伪 G1 / G2 / G3 / G4 / G6 / G9 / G10。

为什么必须做这个
    P0 的核心教训（已固化为验收门）：

        **「配置跑绿」不等于「约束生效」。**

    一条门槛如果从来没有红过，就不知道它到底在不在工作。
    全绿的测试套件可能只是"什么都没检查"。

    所以每条门槛都要做一次**注入实验**：
    故意把被防的问题造出来，确认对应的断言**真的红**，
    然后还原、确认**真的绿**。

**为什么在真实仓库上做，而不是复制一份到临时目录**

    第一版实现是"复制到临时目录再改"，**它是错的，而且错得很隐蔽**：

    ``.venv`` 里 `pip install -e .` 生成的 `.pth` 文件把
    **真实仓库的绝对路径**写死了。所以在临时目录里跑 pytest，
    导入到的仍然是真实仓库的 `sigma_ai`——沙箱里的改动
    **完全不参与测试**，于是 7 条注入实验全部报"注入后仍然全绿"。

    这个假绿的形状值得记住：**它看起来像"门槛失灵"，
    实际是"疫苗打在别人身上"。** 定位它的唯一办法是先手查
    `sigma_ai.__file__` 指向哪里。

    正确做法是在真实仓库上改、跑、还原。
    **还原必须放在 ``finally`` 里**——注入中途抛错却留下破坏，
    比不做实验更糟。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch1.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"


class Repo:
    """在真实仓库上做受控破坏，并保证还原。"""

    def __init__(self) -> None:
        self._pending: list[tuple[Path, str]] = []  # (文件, 原始内容)

    def patch(self, rel_path: str, old: str, new: str) -> None:
        """替换，并登记原始内容以便还原。

        锚点不中就直接抛错——静默不匹配会让注入实验假绿，
        那正是本脚本要防的那类问题。
        """
        path = REPO / rel_path
        text = path.read_text(encoding="utf-8")

        # 第一次碰这个文件时记下原始内容
        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, text))

        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到：{old[:70]!r}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def restore(self) -> None:
        """还原全部改动。失败也必须执行。"""
        for path, original in reversed(self._pending):
            path.write_text(original, encoding="utf-8")
        self._pending.clear()

    def run(self, target: str) -> tuple[int, str]:
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


RESULTS: list[tuple[str, str, bool, str]] = []


def experiment(gate: str, what: str, target: str, inject) -> None:
    repo = Repo()
    try:
        # 1. 基线必须绿——否则"注入后变红"说明不了任何事
        code, out = repo.run(target)
        if code != 0:
            RESULTS.append(
                (gate, what, False, f"基线不是绿的，实验无效：{out.strip()[-200:]}")
            )
            return

        # 2. 注入
        inject(repo)

        # 3. 必须变红
        code, out = repo.run(target)
        if code == 0:
            RESULTS.append(
                (
                    gate,
                    what,
                    False,
                    "注入后**仍然全绿** → 门槛没有在防它声称防的东西",
                )
            )
            return

        # 4. 顺手记录"红在哪"，作为门槛确实起作用的证据
        failing = [
            line
            for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        ]
        detail = failing[0][:140] if failing else out.strip().splitlines()[-1][:140]
        RESULTS.append((gate, what, True, detail))
    finally:
        # 5. 无论成败都必须还原
        repo.restore()


# ---------------------------------------------------------------------------
# 各门槛的注入
# ---------------------------------------------------------------------------


def _inject_g1(repo: Repo) -> None:
    """去掉 ``BaseProvider`` 上**全部** ``@abstractmethod`` 标记。

    第一版只去掉了 ``estimate_tokens`` 的标记，**实验无效**——
    ``HalfDone`` 只实现 ``estimate_tokens``、不实现 ``stream``，
    于是 ``stream`` 的抽象标记仍然把它拦在外面，测试照样绿。

    **这是注入实验自身的一个坑**：注入必须足以让"被防的问题真的发生"，
    否则得到的"仍然全绿"是在冤枉门槛。
    所以这里把两个标记都去掉，让半实现子类**真的**能够被实例化。
    """
    repo.patch(
        "core/sigma_ai/base.py",
        "    @abstractmethod\n    def estimate_tokens(self, messages: list[LlmMessage]) -> int:",
        "    def estimate_tokens(self, messages: list[LlmMessage]) -> int:",
    )
    repo.patch(
        "core/sigma_ai/base.py",
        "    @abstractmethod\n    def stream(",
        "    def stream(",
    )


def _inject_g2(repo: Repo) -> None:
    """把 ``text_signature`` 排除出序列化——签名往返丢失。"""
    repo.patch(
        "core/sigma_ai/messages.py",
        "from pydantic import BaseModel",
        "from pydantic import BaseModel, Field",
    )
    repo.patch(
        "core/sigma_ai/messages.py",
        "    # provider 返回的不透明串，必须原样回传。见模块 docstring。\n    text_signature: str | None = None",
        "    # 注入：字段被排除出序列化\n    text_signature: str | None = Field(default=None, exclude=True)",
    )


def _inject_g3(repo: Repo) -> None:
    """把 ``ToolCallDelta.index`` 排除出序列化——事件往返丢字段。"""
    repo.patch(
        "core/sigma_ai/events.py",
        "from pydantic import BaseModel",
        "from pydantic import BaseModel, Field",
    )
    repo.patch(
        "core/sigma_ai/events.py",
        '    type: Literal["tool_call_delta"] = "tool_call_delta"\n    index: int',
        '    type: Literal["tool_call_delta"] = "tool_call_delta"\n    index: int = Field(exclude=True)',
    )


def _inject_g4(repo: Repo) -> None:
    """让 ``FakeProvider`` 每次构造多吐一个事件——两次回放不一致。

    真实的"不确定性来源"（时间戳、随机 ID）在这个最简回放器里不存在，
    所以用"构造序号"模拟。它要防的是同一类东西：
    **同一输入两次跑出不同结果**。
    """
    repo.patch(
        "core/sigma_ai/fake.py",
        "    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:\n        self._rounds = rounds\n        self._cursor = 0",
        "    _INSTANCE_SEQ = 0\n\n"
        "    def __init__(self, rounds: list[list[dict[str, Any]]]) -> None:\n"
        "        type(self)._INSTANCE_SEQ += 1\n"
        "        self._seq = type(self)._INSTANCE_SEQ\n"
        "        # 注入：每次构造多吐一个事件，破坏两次回放的一致性\n"
        '        suffix = [{"type": "text_delta", "text": f"#{self._seq}"}]\n'
        "        self._rounds = [r + suffix for r in rounds]\n"
        "        self._cursor = 0",
    )


def _inject_g6(repo: Repo) -> None:
    """让 overflow 判定**永远不成立**——超限被误判成普通非法请求。

    第一版只把 ``"context length"`` 替换成哨兵串，**实验无效**：
    三条测试样例其实分别命中 ``"too long"`` / ``"maximum context"`` /
    ``"reduce the length"`` 这几个 marker，我改的那条根本不参与判定，
    于是三条全绿。

    这次直接把整个判断短路掉——**这才是"识别逻辑失效"的准确注入**。
    """
    repo.patch(
        "core/sigma_ai/errors.py",
        "        if any(marker in lowered for marker in overflow_markers):\n            return ErrorCode.CONTEXT_OVERFLOW\n        return ErrorCode.INVALID_REQUEST",
        "        # 注入：overflow 判定被短路，永远不会识别出上下文超限\n"
        "        return ErrorCode.INVALID_REQUEST",
    )


def _inject_g9(repo: Repo) -> None:
    """给 ``SystemMessage`` 加 ``summary``——正是 G9 要挡的那个改动。"""
    repo.patch(
        "core/sigma_ai/messages.py",
        "    tools_removed: list[str] | None = None  # 仅工具名，对应 Pi 的 ToolReference\n    timestamp: str",
        "    tools_removed: list[str] | None = None  # 仅工具名，对应 Pi 的 ToolReference\n"
        "    # 注入：把压缩摘要塞进 system——G9 要挡的正是这个\n"
        "    summary: str | None = None\n"
        "    timestamp: str",
    )


def _inject_g10(repo: Repo) -> None:
    """从 ``BaseProvider.stream`` 删掉 ``sampling`` / ``options``。

    这就是 B1.3 讨论里"方案丙"的状态：抽象签名只被回放器适配过。

    **第一版这条实验也是无效的**——目标测试是"参数能传进请求体"，
    而那个测试调的是 ``OpenAICompatProvider``，它自己保留了这三个参数，
    所以抽象层退化后照样绿。

    **这正是 G10 要防的问题本身**，只是发生在比预期更深的地方。
    修法不是换一个测试，是**补一条真正的断言**：
    ``tests/test_sigma_ai_provider_signature.py`` 用自省直接比对
    抽象层与实现层的签名。目标测试也随之改到那里。
    """
    repo.patch(
        "core/sigma_ai/base.py",
        "        sampling: SamplingParams | None = None,\n"
        "        options: StreamOptions | None = None,\n"
        "        timeout_s: float | None = None,\n"
        "    ) -> AsyncIterator[StreamEvent]:",
        "    ) -> AsyncIterator[StreamEvent]:",
    )


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------

EXPERIMENTS = [
    (
        "G1",
        "去掉 estimate_tokens 的 @abstractmethod → 半实现子类可实例化",
        "tests/test_sigma_ai_types.py::test_incomplete_subclass_still_fails",
        _inject_g1,
    ),
    (
        "G2",
        "把 text_signature 排除出序列化 → 签名往返丢失",
        "tests/test_sigma_ai_types.py::test_signature_fields_survive_json_roundtrip",
        _inject_g2,
    ),
    (
        "G3",
        "把 ToolCallDelta.index 排除出序列化 → 事件往返丢字段",
        "tests/test_sigma_ai_events.py::test_stream_event_roundtrip_by_class",
        _inject_g3,
    ),
    (
        "G4",
        "让 FakeProvider 每次构造多吐一个事件 → 两次回放不一致",
        "tests/test_sigma_ai_fake.py::test_same_transcript_replays_identically",
        _inject_g4,
    ),
    (
        "G6",
        "清空 overflow 文案表 → 超限被误判成普通非法请求",
        "tests/test_sigma_ai_types.py::test_context_overflow_is_detected_from_message",
        _inject_g6,
    ),
    (
        "G9",
        "给 SystemMessage 加 summary 字段 → 摘要能塞进常驻区",
        "tests/test_sigma_ai_types.py::test_system_message_field_set_is_frozen",
        _inject_g9,
    ),
    (
        "G10",
        "删掉 stream 的 sampling/options → 抽象层退化，实现者需要而抽象层没有",
        "tests/test_sigma_ai_provider_signature.py::test_abstract_signature_accepts_both_implementers",
        _inject_g10,
    ),
]


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入：{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 76)
    print("批次 1 门槛注入实验结果")
    print("=" * 76)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 76)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪（注入后确实变红）")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
