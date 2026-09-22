"""批次 2–4 门槛注入实验：逐条证伪 G22–G31。

为什么必须做这个
    本项目的核心教训（P0 起即固化为验收门）：

        **「配置跑绿」不等于「约束生效」。**

    批次 2–4 一共定了十条门槛（G22–G31）。在写本脚本之前，
    先做了一次覆盖核查，结果是**其中五条连测试都没有**
    （已在同一天补齐，见 `tests/` 下新增的三个文件）。
    也就是说，那五条门槛当时既"不能被证伪"，也"根本没被测"。

    本脚本的每一条注入都同时检查**基线绿**与**注入后红**：
    - 少了前者，"注入后红"可能只是因为环境本来就是红的；
    - 少了后者，说明门槛没有在防它声称防的东西。

**为什么在真实仓库上做**
    批次 1 踩过的坑：`.venv` 里 `pip install -e .` 生成的 `.pth`
    把真实仓库的绝对路径写死，所以"复制到临时目录再注入"是**假实验**
    （沙箱里的改动完全不参与测试，症状是全员"仍然全绿"）。
    正确做法是在真实仓库上改、跑、还原，**还原放 `finally`**。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch24.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import ClassVar

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

# 开头打印解析出的仓库根——这条纪律来自详规 6.2 节：
# 解析错时的症状（FileNotFoundError）很容易被误判成"文件真被删了"。
print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "core" / "sigma_agent" / "loop.py").exists(), "仓库根解析错了"


class Repo:
    """在真实仓库上做受控破坏，并保证还原。"""

    #: 未还原的文件（路径 → 写入前的**原始字节**）。
    #:
    #: **为什么是类级而不是实例级**：终止信号处理器够不到某个实例，
    #: 但它必须能还原——否则中断一次就把仓库留成脏的（实测发生过一次）。
    #:
    #: **为什么存原始字节而不是归一化后的文本**：还原必须**逐字节精确**。
    #: 文本化再写回要走 `_write` 的行尾还原逻辑，而那多一个出错的机会；
    #: 直接写回原字节则与行尾无关。
    _pending: ClassVar[list[tuple[Path, bytes]]] = []

    def __init__(self) -> None:
        self._crlf: dict[Path, bool] = {}

    # ------------------------------------------------------------------
    # 行尾：读时归一化为 LF，写时还原成文件原本的风格
    # ------------------------------------------------------------------
    # 本脚本在这里连踩了两次坑，两次症状相同（锚点找不到 / 满屏 M），
    # 但方向相反：
    #
    #   1. 用 `read_text()`：它默认启用 universal newlines，
    #      把 CRLF 转成 LF——**写回后整文件的行尾都变了**，
    #      git 于是把 7 个文件标成 M（内容其实没变）。
    #   2. 改成二进制读写：行尾不再被改写，但**锚点里的 `\n` 匹配不上
    #      工作区里的 `\r\n`**，于是第一条注入就抛"锚点没找到"。
    #
    # 所以正确做法是**两头都处理**：读进来归一化为 LF 以便匹配锚点，
    # 写回去时还原成这个文件原本的行尾风格。
    #
    # **注入脚本的副作用必须为零**——否则下一个人看到满屏 M，
    # 只会以为"注入没还原"，白白排查一轮。
    def _read(self, path: Path) -> str:
        raw = path.read_bytes().decode("utf-8")
        self._crlf[path] = "\r\n" in raw
        return raw.replace("\r\n", "\n")

    def _write(self, path: Path, text: str) -> None:
        if self._crlf.get(path, False):
            text = text.replace("\n", "\r\n")
        path.write_bytes(text.encode("utf-8"))

    def patch(self, rel_path: str, old: str, new: str) -> None:
        """替换，并登记原始内容以便还原。

        **锚点不中就直接抛错**——静默不匹配会让注入实验假绿，
        那正是本脚本要防的那类问题。
        """
        path = REPO / rel_path
        text = self._read(path)

        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, path.read_bytes()))

        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到：{old[:70]!r}")
        self._write(path, text.replace(old, new, 1))

    def append(self, rel_path: str, extra: str) -> None:
        """在文件末尾追加内容（用于"新增一个类"这类注入）。"""
        path = REPO / rel_path
        text = self._read(path)
        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, path.read_bytes()))
        self._write(path, text + extra)

    @classmethod
    def restore_all(cls) -> None:
        """把登记过的文件**逐字节**写回原样。

        写成 classmethod 是为了让终止信号处理器也能调它——
        它拿不到实例，但**必须能还原**（见 `_pending` 的说明）。
        """
        for path, raw in reversed(cls._pending):
            path.write_bytes(raw)
        cls._pending.clear()

    def restore(self) -> None:
        self.restore_all()

    @classmethod
    def has_pending(cls) -> bool:
        return bool(cls._pending)

    def run_pytest(self, target: str | list[str]) -> tuple[int, str]:
        targets = [target] if isinstance(target, str) else list(target)
        proc = subprocess.run(
            [str(PYTHON), "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider"],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


RESULTS: list[tuple[str, str, bool, str]] = []

#: 注入标记。所有注入都用这个前缀注释（`# 注入：…`），
#: 于是"有没有残留"可以用**一次扫描**回答。
INJECTION_MARKER = "# 注入："


def find_residue() -> list[str]:
    """扫描 ``core/`` 下的注入残留，返回 ``["相对路径:行号: 内容", …]``。

    **为什么需要它（这是踩过的坑）**

        注入实验被**硬中断**（SIGTERM / 关窗口 / 杀进程）时，
        ``finally`` 里的还原不会执行——SIGTERM 的默认处置是立即终止，
        Python 不会跑 ``finally``。源码于是留在被改坏的状态。

        后果**不是报错**，而是**基线变红**：脚本把每一条门槛都报成
        "基线不是绿的，实验无效"，看起来像环境噪声，**实际是源码坏了**。

        实测发生过一次：`web_search.py` 的额度判定被顶成 `if False`，
        两个额度用例因此失败，而第一轮排查方向指向了别处。
    """
    hits: list[str] = []
    for path in sorted((REPO / "core").rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if INJECTION_MARKER in line:
                hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    return hits


def assert_no_residue() -> None:
    """开工前先确认上一次的注入已经还原。**发现残留就直接退出。**

    这是本脚本的"配置跑绿 ≠ 约束生效"的又一次应用：
    **还原成功**这件事本身也需要被检查，不能假设它发生了。
    """
    hits = find_residue()
    if hits:
        raise SystemExit(
            "检测到**上一次注入实验的残留**（还原没有执行）：\n  "
            + "\n  ".join(hits)
            + "\n\n先还原再跑：git checkout -- <上面列出的文件>\n"
            "理由：残渣会让基线变红，而脚本会把它报成"
            "「基线不是绿的，实验无效」——看起来像环境噪声，实际是源码被改坏了。"
        )


def _install_restore_on_termination() -> None:
    """被硬中断时也还原。

    ``finally`` 挡不住 SIGTERM，所以额外挂一个处理器。
    非主线程或平台不支持时静默跳过——那只是少一道保险，
    还有 :func:`assert_no_residue` 在下次开工时兜住。
    """
    import signal

    def _handler(signum: int, _frame: object) -> None:
        if Repo.has_pending():
            Repo.restore_all()
            print(f"\n[警告] 收到信号 {signum}，已还原注入改动。", file=sys.stderr)
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # 非主线程 / 平台不支持
            pass


_install_restore_on_termination()


def experiment(gate: str, what: str, target: str | list[str], inject) -> None:
    """跑一次注入实验：基线绿 → 注入 → 必须红 → 还原。

    ``target`` 可以是单个 pytest 目标，也可以是**多个**——当一条门槛由
    多条断言共同支撑时（任一为红即算门槛生效），必须一次传进来：
    分两次跑会让"基线绿"这个前提各自成立，却测不出注入的影响面。
    """
    assert_no_residue()
    repo = Repo()
    try:
        code, out = repo.run_pytest(target)
        if code != 0:
            RESULTS.append(
                (gate, what, False, f"基线不是绿的，实验无效：{out.strip()[-200:]}")
            )
            return

        inject(repo)

        code, out = repo.run_pytest(target)
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

        failing = [
            line
            for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR")
        ]
        detail = failing[0][:150] if failing else out.strip().splitlines()[-1][:150]
        RESULTS.append((gate, what, True, detail))
    finally:
        repo.restore()


# ---------------------------------------------------------------------------
# 各门槛的注入
# ---------------------------------------------------------------------------

TOOL_BASE = "core/sigma_agent/base.py"
TOOL_REGISTRY = "core/sigma_agent/registry.py"
MSG = "core/sigma_agent/agent_messages.py"
LOOP = "core/sigma_agent/loop.py"
CONTEXT = "core/sigma_session/context.py"
TRUNCATE = "core/sigma_tools/truncate.py"
CALLS = "core/sigma_ai/tool_calls.py"

LOOP_TESTS = "tests/test_agent_loop.py"
CONTEXT_TESTS = "tests/test_session_context.py"
TRUNCATE_TESTS = "tests/test_tools_truncate.py"
FROM_RESULT_TESTS = "tests/test_agent_messages_from_result.py"


def _inject_e20(repo: Repo) -> None:
    """E20 / G22：`BaseTool` 直接实例化必须抛 `TypeError`。

    去掉 `@abstractmethod` 之后，`BaseTool()` 变成一个普通类——
    而"忘了实现 `run`"会从**实例化时报错**退化成**调用时才报错**。

    **两个 abstract 都要去掉**：只去一个，另一个仍然拦着，
    注入就不生效（那样会得到一条假绿）。
    """
    repo.patch(
        TOOL_BASE,
        "    @property\n    @abstractmethod\n    def params(self)",
        "    @property\n    def params(self)",
    )
    repo.patch(
        TOOL_BASE,
        "    @abstractmethod\n    async def run(self, args: BaseModel, ctx: ToolContext)",
        "    async def run(self, args: BaseModel, ctx: ToolContext)",
    )


def _inject_e21(repo: Repo) -> None:
    """E21 / G23：内置工具必须是 `BaseTool` 的子类。

    把 `ReadTool` 的继承去掉——它仍然"能跑"（鸭子类型），
    所以这条门槛拦的是**类型层的约束**，不是运行期行为。
    """
    repo.patch(TRUNCATE.replace("truncate.py", "read.py"), "class ReadTool(BaseTool):", "class ReadTool:")


def _inject_e22(repo: Repo) -> None:
    """E22 / G24：重名注册失败后，注册表必须**保持旧状态**。

    改成"先删后插"的写法——抛错之前已经把旧的删了，
    于是留下一个**半更新状态**：工具永久消失。
    """
    repo.patch(
        TOOL_REGISTRY,
        "        if name in self._definitions:\n            existing = self._definitions[name]\n",
        "        if name in self._definitions:\n"
        "            existing = self._definitions[name]\n"
        "            del self._definitions[name]  # 注入：先删后插\n",
    )


def _inject_e23(repo: Repo) -> None:
    """E23 / G25：`from_result` 必须传递 `details`。

    漏传 `details` **不会报错**（Pydantic 有默认值），
    只会让"给程序看的信息"静默消失——它本该进审计与评测。
    这正是这条门槛真正在防的东西。
    """
    repo.patch(MSG, "            details=result.details,\n", "")


def _inject_e24(repo: Repo) -> None:
    """E24 / G26：常驻区变化必须抛。

    去掉 `verify_resident_region()` 的调用——此后"按任务动态改系统提示词"
    不再有任何东西拦着，而后果（prompt cache 全失效）是**静默**的。

    **锚点于 2026-09-21（P2-3）移动过一次**，原因是在 `build_messages` 里
    插入了预算校验（`verify_resident_budget()`）与时钟读取，
    原来的"校验紧跟 system 构造"这个上下文不再成立。

    这正是元纪律②要防的形态：**锚点与源码硬耦合，换代码必须重跑注入**。
    当时的失败方式是**响亮地抛 `AssertionError`**（而不是静默跳过）——
    这是设计好的失败姿态，见 `Repo.patch` 的 docstring。

    现在只锚 `verify_resident_region()` 那一行（8 空格缩进的那次调用是文件内唯一一处）。
    注意**不要**连 `verify_resident_budget()` 一起删掉：
    那属于另一条门槛（G59），混进来会让这条实验同时测两件事，
    红了也说不清是哪条失效。
    """
    repo.patch(
        CONTEXT,
        "        self.verify_resident_region()\n",
        "",
    )


def _inject_e25(repo: Repo) -> None:
    """E25 / G27：结果顺序必须与调用顺序一致。

    把 `sorted()` 换成 `reversed(sorted())`。

    **注意**：换成"dict 插入顺序"（`list(self._slots)`）是**不够的**——
    在测试里 index 0 恰好先到达，顺序不变，于是注入不生效、得到假绿。
    必须用能真正打乱顺序的注入。
    """
    repo.patch(
        CALLS,
        "        return [self._finalize(index) for index in sorted(self._slots)]",
        "        # 注入：反转顺序\n"
        "        return [self._finalize(index) for index in reversed(sorted(self._slots))]",
    )


def _inject_e26(repo: Repo) -> None:
    """E26 / G28：工具失败后 loop 必须**继续**。

    加上"一失败就 return"的分支。它看起来像防御性编程
    （"都失败了还跑什么"），实际会**静默关掉整个纠错能力**——
    而"纠错增益 = 最终成功率 − 首次成功率"这个核心指标会因此永远是 0。
    """
    repo.patch(
        LOOP,
        "                produced.append(\n"
        "                    ToolResultAgentMessage.from_result(\n"
        "                        item.to_block(), result, timestamp=self._clock()\n"
        "                    )\n"
        "                )",
        "                # 注入：工具一失败就结束整轮（关掉纠错能力）\n"
        "                if result.is_error:\n"
        "                    produced.append(\n"
        "                        ToolResultAgentMessage.from_result(\n"
        "                            item.to_block(), result, timestamp=self._clock()\n"
        "                        )\n"
        "                    )\n"
        "                    return TurnResult(\n"
        '                        status="completed",\n'
        "                        messages=produced,\n"
        "                        text=_text_of(assistant),\n"
        "                        rounds=round_index,\n"
        "                    )\n"
        "                produced.append(\n"
        "                    ToolResultAgentMessage.from_result(\n"
        "                        item.to_block(), result, timestamp=self._clock()\n"
        "                    )\n"
        "                )",
    )


def _inject_e27(repo: Repo) -> None:
    """E27 / G29：超过阈值的输出必须被截断，且文案含行动指引。

    把阈值判断短路成"永远不截断"——此后一次大输出就能炸掉上下文预算。
    """
    repo.patch(
        TRUNCATE,
        "    if total_bytes <= max_bytes:\n        return Truncated(",
        "    if True:  # 注入：从不截断\n        return Truncated(",
    )


def _inject_e28(repo: Repo) -> None:
    """E28 / G30：全项目只能有一个类定义 `run_turn`。

    新增一个**不继承任何基类**的循环实现——
    这正是旧断言（`BaseLoop.__subclasses__()`）拦不住的形态，
    也是门槛改写的理由。
    """
    repo.append(
        LOOP,
        "\n\nclass SimpleLoop:\n"
        '    """注入：第二套循环实现（不继承任何基类）。"""\n\n'
        "    async def run_turn(self, messages):\n"
        "        raise NotImplementedError\n",
    )


def _inject_e29(repo: Repo) -> None:
    """E29 / G31：工具参数必须通过 schema 校验。

    跳过 `model_validate`——此后"JSON 合法但字段不对"的参数会直接
    送进工具，而工具拿到一个字段缺失的参数模型。
    """
    repo.patch(
        LOOP,
        "                validated = tool.params.model_validate(arguments)",
        "                validated = arguments  # 注入：跳过 schema 校验",
    )


EXPERIMENTS = [
    ("E20", "G22  BaseTool 去掉 @abstractmethod → 基类变得可实例化", f"{LOOP_TESTS}::test_base_tool_cannot_be_instantiated", _inject_e20),
    ("E21", "G23  ReadTool 去掉 BaseTool 继承 → 不再是子类", f"{LOOP_TESTS}::test_builtin_tools_are_subclasses_with_matching_name", _inject_e21),
    ("E22", "G24  注册表改成先删后插 → 失败后留下半更新状态", f"{LOOP_TESTS}::test_duplicate_tool_registration_keeps_registry_intact", _inject_e22),
    ("E23", "G25  from_result 漏传 details → 审计数据静默消失", f"{FROM_RESULT_TESTS}::test_from_result_propagates_every_field", _inject_e23),
    ("E24", "G26  去掉常驻区校验 → 动态改提示词无人拦", f"{CONTEXT_TESTS}::test_resident_region_change_raises", _inject_e24),
    ("E25", "G27  结果顺序反转 → tool_call_id 与结果错配", f"{LOOP_TESTS}::test_two_tool_calls_in_one_round_keep_order", _inject_e25),
    ("E26", "G28  工具失败即 return → 纠错能力被关掉", f"{LOOP_TESTS}::test_tool_error_is_visible_and_loop_continues", _inject_e26),
    ("E27", "G29  短路阈值判断 → 大输出不再截断", f"{TRUNCATE_TESTS}::test_long_output_keeps_head_and_tail", _inject_e27),
    ("E28", "G30  新增第二个 loop 类（不继承基类）", f"{LOOP_TESTS}::test_only_one_class_defines_run_turn", _inject_e28),
    ("E29", "G31  跳过 model_validate → 参数不符 schema 也能执行", f"{LOOP_TESTS}::test_schema_violation_becomes_visible_error", _inject_e29),
]


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入：{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 78)
    print("批次 2–4 门槛注入实验结果")
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
