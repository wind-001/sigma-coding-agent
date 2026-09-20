"""批次 1.5 门槛注入实验：逐条证伪 G11–G18。

为什么必须做这个
    P0 的核心教训（已固化为验收门）：

        **「配置跑绿」不等于「约束生效」。**

    批次 1 已经证明这条教训**不是一次性的**：那次的 7 条注入里，
    有 3 条第一版是"无效注入"——注入做了、测试仍然绿，
    但真因是"注入不足以让被防的问题发生"，**不是门槛失灵**。
    详见 `docs/plans/P1-批次1-详规.md` 第 10.4 节。

    所以本脚本的每一条注入，都要同时检查**基线绿**与**注入后红**：
    少了前者，"注入后红"可能只是因为环境本来就是红的；
    少了后者，说明门槛没在防它声称防的东西。

**为什么在真实仓库上做**
    批次 1 踩过的坑：`.venv` 里 `pip install -e .` 生成的 `.pth`
    把真实仓库的绝对路径写死，所以"复制到临时目录再注入"是**假实验**
    （沙箱里的改动完全不参与测试，症状是 7 条全报"仍然全绿"）。

    正确做法是在真实仓库上改、跑、还原，**还原放 `finally`**。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch15.py

**E18 的例外**
    它是"往 `sigma_ai` 注入 `import sigma_agent`，看 `lint-imports` 是否变红"，
    目标不是 pytest。本脚本对它的处理是跑 `lint-imports` 而不是 pytest
    （见 `run_lint_imports`）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

# 批次 1 的脚本开头就打印解析出的仓库根——这条纪律来自详规 6.2 节：
# `Path(__file__).resolve().parent` 的层数取决于脚本放在哪，
# 而解析错时的症状（FileNotFoundError）很容易被误判成"文件真被删了"。
print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "core" / "sigma_ai" / "messages.py").exists(), "仓库根解析错了"


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

        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, text))

        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到：{old[:70]!r}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def restore(self) -> None:
        for path, original in reversed(self._pending):
            path.write_text(original, encoding="utf-8")
        self._pending.clear()

    def run_pytest(self, target: str) -> tuple[int, str]:
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", target, "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def run_lint_imports(self) -> tuple[int, str]:
        suffix = ".exe" if os.name == "nt" else ""
        exe = REPO / ".venv" / "Scripts" / f"lint-imports{suffix}"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO)
        proc = subprocess.run(
            [str(exe)],
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


RESULTS: list[tuple[str, str, bool, str]] = []


def experiment(gate: str, what: str, target: str, inject, *, tool: str = "pytest") -> None:
    """跑一次注入实验：基线绿 → 注入 → 必须红 → 还原。"""
    repo = Repo()

    def run() -> tuple[int, str]:
        return repo.run_pytest(target) if tool == "pytest" else repo.run_lint_imports()

    try:
        # 1. 基线必须绿——否则"注入后变红"说明不了任何事
        code, out = run()
        if code != 0:
            RESULTS.append(
                (gate, what, False, f"基线不是绿的，实验无效：{out.strip()[-200:]}")
            )
            return

        # 2. 注入
        inject(repo)

        # 3. 必须变红
        code, out = run()
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

        # 4. 记录"红在哪"，作为门槛确实起作用的证据
        failing = [
            line
            for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR") or "BROKEN" in line
        ]
        detail = failing[0][:150] if failing else out.strip().splitlines()[-1][:150]
        RESULTS.append((gate, what, True, detail))
    finally:
        # 5. 无论成败都必须还原
        repo.restore()


# ---------------------------------------------------------------------------
# 各门槛的注入
# ---------------------------------------------------------------------------

MSG = "core/sigma_agent/agent_messages.py"
CONVERT_TESTS = "tests/test_agent_messages_convert.py"
CONTRACT_TESTS = "tests/test_agent_messages_contracts.py"
REGISTRY_TESTS = "tests/test_agent_messages_registry.py"


def _inject_e11(repo: Repo) -> None:
    """E11 / G11：把 `to_llm` 改成**重新构造** `AssistantMessage`。

    **这是本批次最该跑的一条**，因为 G11 要防的失败模式
    （"重新构造导致丢字段"）**写出来是完全合法的代码**——
    `mypy` 与 `ruff` 都不报，批次 1 的 G2 也拦不住
    （G2 测的是 Pydantic 自己的序列化，不是这段代码）。

    漏掉的字段是 `api` / `provider` / `model` / `response_id` /
    `error_message`——四个 provider 元数据 + 一个错误信息。
    它们不显眼，所以"顺手重建"时最容易漏。
    """
    repo.patch(
        MSG,
        "from sigma_ai.messages import (\n    ContentBlock,",
        "from sigma_ai.messages import (\n    AssistantMessage,\n    ContentBlock,",
    )
    repo.patch(
        MSG,
        "        return self.message\n\n\n@register_message_type(role=\"tool_result\")",
        "        m = self.message\n"
        "        if isinstance(m, AssistantMessage):\n"
        "            # 注入：重新构造，漏掉 api / provider / model / response_id\n"
        "            return AssistantMessage(\n"
        "                content=m.content,\n"
        "                usage=m.usage,\n"
        "                stop_reason=m.stop_reason,\n"
        "                timestamp=m.timestamp,\n"
        "            )\n"
        "        return m\n\n\n@register_message_type(role=\"tool_result\")",
    )


def _inject_e12(repo: Repo) -> None:
    """E12 / G12：`to_llm` 忽略 `exclude_from_context`——显式丢弃失效。

    注入方式是删掉那个 `if`，让它总是构造 `ToolResultMessage`。
    后果是"UI 专属通知也会进模型上下文"——不报错，只是模型看见了
    不该看见的东西，且 prompt cache 命中率下降。
    """
    repo.patch(
        MSG,
        "        if self.exclude_from_context:\n            return None\n        return ToolResultMessage(",
        "        # 注入：exclude_from_context 被忽略\n        return ToolResultMessage(",
    )


def _inject_e13(repo: Repo) -> None:
    """E13 / G13：自定义消息降级时造 `SystemMessage` 而非 `UserMessage`。

    这条注入模拟的是"顺手把摘要放进 system 消息"——
    一个看起来更"正式"的选择。后果是摘要进常驻区、
    prompt cache 从插入点起全部失效（D4）。
    """
    repo.patch(
        MSG,
        "    return UserMessage(\n        content=message.model_dump_json(exclude={\"role\"}),\n        timestamp=message.timestamp,\n    )",
        "    # 注入：造 SystemMessage 而不是 UserMessage\n"
        "    return SystemMessage(\n"
        "        content=message.model_dump_json(exclude={\"role\"}),\n"
        "        timestamp=message.timestamp,\n"
        "    )",
    )
    repo.patch(
        MSG,
        "from sigma_ai.messages import (\n    ContentBlock,",
        "from sigma_ai.messages import (\n    ContentBlock,\n    SystemMessage,",
    )
    repo.patch(
        MSG,
        "    LlmMessage,\n    ToolResultMessage,\n    UserMessage,\n)",
        "    LlmMessage,\n    ToolResultMessage,\n    UserMessage,\n)  # noqa: E501",
    )


def _inject_e14(repo: Repo) -> None:
    """E14 / G14：`convert_to_llm` 改为静默 `continue`（不发 warning）。

    这就是 Pi 的做法（`default → undefined → filter`）。
    Pi 敢这么做是因为 TypeScript 的联合类型在编译期已封闭，
    `default` 分支理论上不可达。**Python 没有编译期穷尽性检查**——
    静默丢弃会变成"消息莫名消失"且无从排查。
    """
    repo.patch(
        MSG,
        "            _warn_unconvertible(message, role)\n            continue",
        "            # 注入：静默丢弃，不发 warning\n            continue",
    )


def _inject_e15(repo: Repo) -> None:
    """E15 / G15：去掉 `to_llm` 的 `@abstractmethod`。

    半实现子类（只提供字段、不实现 `to_llm`）将能实例化，
    于是降级时调用到 `AgentMessage.to_llm` 的 `raise NotImplementedError`
    ——或者更糟，如果将来有人在基类里给了默认实现，就是静默错误。
    """
    repo.patch(
        MSG,
        "    @abstractmethod\n    def to_llm(self) -> LlmMessage | None:",
        "    def to_llm(self) -> LlmMessage | None:",
    )


def _inject_e16(repo: Repo) -> None:
    """E16 / G16：`register_message_type` 改为静默覆盖（去掉 `raise`）。

    后果是扩展 B 的类型悄悄顶掉扩展 A 的，而 A 的代码仍在构造
    自己的类型——落到盘上的是"role 对但字段不对"的消息，
    反序列化时才炸。
    """
    repo.patch(
        MSG,
        "        if role in _MESSAGE_TYPES:\n            raise DuplicateMessageType(role)\n",
        "        # 注入：静默覆盖\n",
    )


def _inject_e17(repo: Repo) -> None:
    """E17 / G17：`message_from_dict` 遇未知 role 改为**静默放行**。

    **第一版这条注入是无效的**（本脚本第一轮跑出 8/9，就是它）。
    当时只改了 `not isinstance(role, str)` 那条分支，而目标测试用的是
    **合法字符串** `"test_codec_never_registered"`——它走的是
    `get_message_type(role)` 抛错那条路径，**我改的分支根本不参与判定**。

    这是批次 1 同型坑的第二次出现（当时是改 `"context length"` 而
    样例命中 `"too long"`）：**注入必须足以让被防的问题真的发生**，
    而不是"改了一处看起来相关的地方"。

    正确做法是短路**整个未知 role 判定**，让 `get_message_type` 抛的
    `UnknownMessageType` 被吞掉、替换成一个默认类型。
    这才是 G17 要防的那个失败模式："数据坏了但代码假装没事"。
    """
    repo.patch(
        MSG,
        "    cls = get_message_type(role)\n    try:\n        return cls.model_validate(payload)",
        "    # 注入：未知 role 不再抛错，替换成一个字段碰巧对得上的默认类型\n"
        "    try:\n"
        "        cls = get_message_type(role)\n"
        "    except UnknownMessageType:\n"
        "        cls = LlmMessageWrapper\n"
        "    try:\n        return cls.model_validate(payload)",
    )


def _inject_e18(repo: Repo) -> None:
    """E18 / G18：往 `sigma_ai` 注入 `import sigma_agent`。

    注入点在 `messages.py`——它是真正的"协议对齐层"里最不可能
    引入 agent 概念的文件，正因如此，它上面的违规最像"顺手 import"。

    **注意 `lint-imports` 有缓存**（`tests/fixtures/arch/*/.import_linter_cache`）：
    若结果看起来"没变化"，先怀疑缓存而不是契约。
    """
    repo.patch(
        "core/sigma_ai/messages.py",
        "from pydantic import BaseModel",
        "from pydantic import BaseModel\n\n"
        "# 注入：下层引用上层\n"
        "from sigma_agent.agent_messages import AgentMessage  # noqa: F401",
    )


def _inject_e19(repo: Repo) -> None:
    """E19 / G21：把 `convert_to_llm` 的判据改回**类相等性**。

    这就是 2026-09-20 修复的那个缺陷的原形状：

        if role is None or _MESSAGE_TYPES.get(role) is not type(message):

    它让**已注册类型的子类实例**被判成"未知类型"而丢弃，
    `to_llm()` 一次都没被调用——**绕过抽象方法 + 替子类做丢弃决定**，
    同时违反本模块自己写下的两条规则。

    这条注入之所以值得补：原实现的 9 条注入（E11–E18）**全部不覆盖
    子类化路径**，所以"9/9 全绿"与"这个缺陷存在"可以同时成立。
    **又一次「配置全绿但约束有洞」**——与 P0 的 `layers` 兄弟层缺陷、
    批次 1 的 G10 同型。注入的价值就在这里：它能把"门槛没覆盖的方向"
    逼出来。
    """
    repo.patch(
        MSG,
        "        role = getattr(message, \"role\", None)\n"
        "        if role is None or _MESSAGE_TYPES.get(role) is None:",
        "        role = getattr(message, \"role\", None)\n"
        "        # 注入：改回类相等性判据（子类实例会被当作未知类型丢弃）\n"
        "        if role is None or _MESSAGE_TYPES.get(role) is not type(message):",
    )


# ---------------------------------------------------------------------------
# 跑
# ---------------------------------------------------------------------------

EXPERIMENTS = [
    (
        "E11",
        "G11  to_llm 改为重新构造 AssistantMessage（漏 api/provider/model/response_id）",
        f"{CONVERT_TESTS}::test_llm_message_passes_through_as_same_object",
        _inject_e11,
        "pytest",
    ),
    (
        "E11b",
        "G11  同上注入，看 provider 元数据断言是否也红",
        f"{CONVERT_TESTS}::test_provider_metadata_survives_downgrade",
        _inject_e11,
        "pytest",
    ),
    (
        "E12",
        "G12  to_llm 忽略 exclude_from_context",
        f"{CONVERT_TESTS}::test_excluded_message_is_absent_from_result",
        _inject_e12,
        "pytest",
    ),
    (
        "E13",
        "G13  自定义消息降级成 SystemMessage 而非 UserMessage",
        f"{CONVERT_TESTS}::test_custom_message_degrades_to_user_not_system",
        _inject_e13,
        "pytest",
    ),
    (
        "E14",
        "G14  convert_to_llm 改为静默丢弃（不发 warning）",
        f"{CONVERT_TESTS}::test_unknown_message_type_warns",
        _inject_e14,
        "pytest",
    ),
    (
        "E15",
        "G15  去掉 to_llm 的 @abstractmethod",
        f"{REGISTRY_TESTS}::test_half_done_subclass_still_fails",
        _inject_e15,
        "pytest",
    ),
    (
        "E16",
        "G16  register_message_type 改为静默覆盖",
        f"{REGISTRY_TESTS}::test_duplicate_role_raises",
        _inject_e16,
        "pytest",
    ),
    (
        "E17",
        "G17  message_from_dict 遇未知 role 改为返回默认类型",
        f"{CONTRACT_TESTS}::test_unknown_role_raises_on_decode",
        _inject_e17,
        "pytest",
    ),
    (
        "E18",
        "G18  往 sigma_ai 注入 import sigma_agent → lint-imports 必须变红",
        "",  # 不用 pytest target
        _inject_e18,
        "lint-imports",
    ),
    (
        "E19",
        "G21  convert_to_llm 判据改回类相等性 → 已注册类型的子类实例被丢弃",
        f"{CONVERT_TESTS}::test_registered_subclass_instance_still_converts",
        _inject_e19,
        "pytest",
    ),
]


def main() -> int:
    for gate, what, target, inject, tool in EXPERIMENTS:
        print(f"[{gate}] 注入：{what}")
        experiment(gate, what, target, inject, tool=tool)

    print()
    print("=" * 78)
    print("批次 1.5 门槛注入实验结果")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 78)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪（注入后确实变红）")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
