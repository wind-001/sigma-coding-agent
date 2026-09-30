"""P4 团队任务 v3 门槛注入实验：逐条证伪 G-TEAM-14/15/16。

**为什么另起一个脚本，而不是 import batch24 的框架**

    ``gate_injection_batch24.py`` 在 **import 时**就断言
    ``core/sigma.agent/loop.py`` 存在——那是 P6 目录重构**之前**的布局，
    现在这个文件已经不在了。历史脚本是当轮的证据，不该被改；
    所以这里把框架**自包含**地重写一遍（还原、残留扫描、信号兜底都照旧），
    而不是让新实验挂在一个跑不起来的 import 上。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E77 | G-TEAM-14 装配级角色面 | ``sdk`` 里子会话那份 team_board 的 ``role`` 退回 ``"lead"`` | 子会话的 ``create`` 在板上建出了任务 |
| E78 | G-TEAM-15 幂等收尾（门面） | ``WorkerBoard._conclude`` 去掉"任务还在 running 吗"的判断 | ``try_finish`` 对已 fail 的任务抛非法迁移 |
| E79 | G-TEAM-15 幂等收尾（引擎） | 引擎退回 ``wb.finish`` / ``wb.fail``（无条件收尾） | worker 协程带着 ``ValueError`` 死掉 |
| E80 | G-TEAM-16 失败必须通知 lead | 删掉 poll 里的 ``_notify_failures`` 调用 | lead 收到 0 封"[失败待决]" |
| E81 | G-TEAM-16 通知去重 | ``_unnotified_failures`` 去掉 (id, attempts) 去重 | lead 每轮都收到一封，收到十几封 |
| E82 | G-TEAM-16 收摊遇 fail 不崩 | ``stop()`` 对 fail 任务退回 ``cancel`` | stop 抛非法迁移（迁移表没有 fail+cancel） |


### D 组（Review-2026-09-30 §5 的门禁补强）

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E83 | D1 心跳真跑（门面） | ``WorkerBoard.heartbeat`` 退化成只读 | 租约不前进 → 续租断言红 |
| E84 | D1 心跳真跑（引擎） | worker loop 不起 ``_heartbeat_loop`` | 长任务被 lease_tick 误回收 → 状态/attempts 红 |
| E85 | D4 空转不退出 | 空闲分支 ``return`` | worker 协程结束 → 空转断言红 |
| E86 | D5 表漂移守卫 | 往 ``TRANSITIONS`` 偷加一行 | 手写清单等式红 |
| E87 | D3 异常路径经引擎 | 去掉 ``_notify_failures`` 调用 | lead 信箱无待决提醒 → 断言红 |

**E79 的观测点为什么是"协程异常"**

    修复前：runner 返回后引擎无条件 ``wb.finish`` → 撞非法迁移 →``except`` 里再
    ``wb.fail`` → 再撞 → 异常从 ``_worker_loop`` 逃出去，**worker 协程带异常结束**
    而任务停在非终态的 fail 上。任务状态、结论文本都**看不出差别**，唯一能把
    两者分开的就是"这个协程有没有异常"——所以靶测试断言的是它。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_team_v3.py
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

# 开头打印解析出的仓库根：解析错时的症状（FileNotFoundError）很容易被误判成
# "文件真被删了"——这条纪律来自详规 6.2 节。
print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "src" / "sigma" / "team" / "engine.py").exists(), "仓库根解析错了"

INJECTION_MARKER = "# 注入："


class Repo:
    """在真实仓库上做受控破坏，并保证逐字节还原。"""

    #: 未还原的文件（路径 → 写入前的原始字节）。**类级**：终止信号处理器
    #: 够不到实例，但它必须能还原——否则中断一次就把仓库留成脏的。
    _pending: ClassVar[list[tuple[Path, bytes]]] = []

    def __init__(self) -> None:
        self._crlf: dict[Path, bool] = {}

    # ---- 行尾：读时归一化为 LF（好匹配锚点），写时还原成本文件的风格 ----
    def _read(self, path: Path) -> str:
        raw = path.read_bytes().decode("utf-8")
        self._crlf[path] = "\r\n" in raw
        return raw.replace("\r\n", "\n")

    def _write(self, path: Path, text: str) -> None:
        if self._crlf.get(path, False):
            text = text.replace("\n", "\r\n")
        path.write_bytes(text.encode("utf-8"))

    def patch(self, rel_path: str, old: str, new: str) -> None:
        """替换并登记原始内容。**锚点不中直接抛错**——静默不匹配会让注入假绿。"""
        path = REPO / rel_path
        text = self._read(path)
        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, path.read_bytes()))
        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到：{old[:70]!r}")
        self._write(path, text.replace(old, new, 1))

    @classmethod
    def restore_all(cls) -> None:
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


def find_residue() -> list[str]:
    """扫描 ``src/`` 与 ``tests/`` 下的注入残留。

    硬中断（SIGTERM / 关窗口）时 ``finally`` 不会执行，源码会留在被改坏的
    状态；而它的后果**不是报错**，是"基线变红"——看起来像环境噪声。
    """
    hits: list[str] = []
    for root in ("src", "tests"):
        for path in sorted((REPO / root).rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if INJECTION_MARKER in line:
                    hits.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    return hits


def assert_no_residue() -> None:
    hits = find_residue()
    if hits:
        raise SystemExit(
            "检测到**上一次注入实验的残留**（还原没有执行）：\n  "
            + "\n  ".join(hits)
            + "\n\n先还原再跑：git checkout -- <上面列出的文件>\n"
        )


def _install_restore_on_termination() -> None:
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
    """基线绿 → 注入 → **必须红** → 还原 → **必须再绿**。

    最后一步是比 batch24 多出来的：还原本身也该被检查，
    否则"还原失败"会以"下一条门槛基线莫名变红"的形状出现。
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
                (gate, what, False, "注入后**仍然全绿** → 门槛没在防它声称防的东西")
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

    code, out = repo.run_pytest(target)
    if code != 0:
        RESULTS.append(
            (gate, what, False, f"还原后基线没有回到绿：{out.strip()[-200:]}")
        )


# ---------------------------------------------------------------------------
# 各门槛的注入
# ---------------------------------------------------------------------------

D1_FACADE_TEST = (
    "tests/test_multi_agent.py::test_worker_heartbeat_really_extends_lease"
)
D1_ENGINE_TEST = (
    "tests/test_multi_agent.py::test_engine_heartbeat_keeps_long_task_alive"
)
D3_TEST = (
    "tests/test_multi_agent.py::test_engine_failure_path_records_fail_and_notifies"
)
D4_TEST = "tests/test_multi_agent.py::test_idle_workers_keep_polling"
D5_TEST = "tests/test_team_board.py::test_g_team1_legal_rows_match_transitions"

SDK = "src/sigma/sdk.py"
WORKER_BOARD = "src/sigma/team/worker_board.py"
ENGINE = "src/sigma/team/engine.py"

G14_TEST = "tests/test_team_board.py::test_g_team14_sub_sessions_get_worker_face"
G15_FACADE_TEST = "tests/test_multi_agent.py::test_g_team15_try_conclude_is_idempotent"
G15_ENGINE_TEST = (
    "tests/test_multi_agent.py::test_g_team15_engine_skips_when_worker_concluded"
)
G16_TEST = (
    "tests/test_multi_agent.py::test_g_team16_failed_task_notifies_lead_exactly_once"
)


def _inject_e77(repo: Repo) -> None:
    """E77 / G-TEAM-14：子会话退回 lead 面（①-c 之前的状态）。

    这正是装配层的原始形状：类支持 ``role``，但没有任何地方下发 worker 面。
    """
    repo.patch(
        SDK,
        "                        lead_session_id=self._session_id,\n"
        '                        role="worker",\n',
        "                        lead_session_id=self._session_id,\n"
        '                        role="lead",  # 注入：子会话退回 lead 面\n',
    )


def _inject_e78(repo: Repo) -> None:
    """E78 / G-TEAM-15：幂等收尾不再判断"任务还在 running 吗"。"""
    repo.patch(
        WORKER_BOARD,
        '            if task is None or task.state != "running":',
        "            if task is None:  # 注入：不做幂等判断",
    )


def _inject_e79(repo: Repo) -> None:
    """E79 / G-TEAM-15：引擎退回无条件收尾（①-c 之前的状态）。"""
    repo.patch(
        ENGINE,
        "                await wb.try_finish(claimed.id, result)",
        "                await wb.finish(claimed.id, result)  # 注入：无条件收尾",
    )
    repo.patch(
        ENGINE,
        '                await wb.try_fail(claimed.id, f"{type(exc).__name__}: {exc}")',
        '                await wb.fail(claimed.id, f"{type(exc).__name__}: {exc}")'
        "  # 注入：无条件认输",
    )


def _inject_e80(repo: Repo) -> None:
    """E80 / G-TEAM-16：失败不再通知 lead（②-b 之前的状态）。"""
    repo.patch(
        ENGINE,
        "            if fresh_failures:\n"
        "                await self._notify_failures(fresh_failures)",
        "            if fresh_failures:\n                pass  # 注入：不通知 lead",
    )


def _inject_e81(repo: Repo) -> None:
    """E81 / G-TEAM-16：通知去掉去重（同一次失败每轮都发一封）。"""
    repo.patch(
        ENGINE,
        "            and (task.id, task.attempts) not in self._notified_failures",
        "            and True  # 注入：去掉去重",
    )


def _inject_e82(repo: Repo) -> None:
    """E82 / G-TEAM-16：``stop()`` 对 fail 任务退回 ``cancel``。

    迁移表里没有 ``fail+cancel`` 这一行 ⇒ stop 抛非法迁移，想收摊反而收不掉。
    """
    repo.patch(
        ENGINE,
        "                    apply(\n"
        "                        board,\n"
        "                        ABANDON,\n",
        "                    apply(\n"
        "                        board,\n"
        "                        CANCEL,  # 注入：fail 任务退回 cancel\n",
    )


def _inject_e83(repo: Repo) -> None:
    """E83 / D1（门面）：``heartbeat`` 退化成只读——租约不再前进。

    这正是"忘了续租"的真实形态：方法还在、被调用、不报错，只是什么都没改。
    """
    repo.patch(
        WORKER_BOARD,
        "        async with self._store.transaction(self._root) as board:\n"
        "            return apply(\n"
        "                board,\n"
        "                HEARTBEAT,\n"
        "                task_id=task_id,\n"
        "                caller=self._worker_id,\n"
        "                now=self._clock(),\n"
        "                lease_ttl=self._lease_ttl,\n"
        "            )\n",
        "        async with self._store.transaction(self._root) as board:\n"
        "            task = board.find(task_id)\n"
        "            assert task is not None\n"
        "            return task  # 注入：心跳退化成只读，租约不再前进\n",
    )


def _inject_e84(repo: Repo) -> None:
    """E84 / D1（引擎）：worker loop 不再起心跳协程。"""
    repo.patch(
        ENGINE,
        "            lease = asyncio.create_task(self._heartbeat_loop(wb, claimed.id))",
        "            lease = asyncio.create_task(asyncio.sleep(3600))  # 注入：不起心跳",
    )


def _inject_e85(repo: Repo) -> None:
    """E85 / D4：空闲 worker 直接退出（空转变罢工）。"""
    repo.patch(
        ENGINE,
        "            if claimed is None:\n"
        "                if self._converged:\n"
        "                    return\n"
        "                await asyncio.sleep(self._config.poll_s)\n"
        "                continue\n",
        "            if claimed is None:\n"
        "                return  # 注入：空转即退出\n",
    )


def _inject_e86(repo: Repo) -> None:
    """E86 / D5：表里偷偷多一行（不更新手写清单）。"""
    repo.patch(
        "src/sigma/team/board.py",
        '    ("blocked", CANCEL): "cancelled",\n',
        '    ("blocked", CANCEL): "cancelled",\n'
        '    ("blocked", CLAIM): "running",  # 注入：表里偷偷多一行\n',
    )


def _inject_e87(repo: Repo) -> None:
    """E87 / D3：引擎不再通知 lead（②-b 的兜底被拿掉）。"""
    repo.patch(
        ENGINE,
        "            if fresh_failures:\n"
        "                await self._notify_failures(fresh_failures)\n",
        "            if False:  # 注入：不再通知 lead\n"
        "                await self._notify_failures(fresh_failures)\n",
    )


def main() -> int:
    experiment("G-TEAM-14", "装配级角色面：子会话拿到的确实是 worker 面", G14_TEST, _inject_e77)
    experiment(
        "G-TEAM-15", "幂等收尾（门面）：已收尾的任务不再动手", G15_FACADE_TEST, _inject_e78
    )
    experiment(
        "G-TEAM-15",
        "幂等收尾（引擎）：worker 自收尾后引擎不重复收尾",
        G15_ENGINE_TEST,
        _inject_e79,
    )
    experiment("G-TEAM-16", "失败必须让 lead 看见", G16_TEST, _inject_e80)
    experiment("G-TEAM-16", "失败通知去重：同一次失败只发一封", G16_TEST, _inject_e81)
    experiment(
        "G-TEAM-16", "收摊遇 fail 不崩（fail 非终态且不可 cancel）", G16_TEST, _inject_e82
    )

    experiment(
        "D1", "心跳真跑（门面）：heartbeat 必须实测续上租约", D1_FACADE_TEST, _inject_e83
    )
    experiment(
        "D1",
        "心跳真跑（引擎）：长任务靠 _heartbeat_loop 不被误回收",
        D1_ENGINE_TEST,
        _inject_e84,
    )
    experiment(
        "D3", "异常路径经引擎取证：fail + attempts+1 + 通知 lead", D3_TEST, _inject_e87
    )
    experiment("D4", "空转不退出：空闲 worker 继续轮询并认领新任务", D4_TEST, _inject_e85)
    experiment("D5", "表漂移守卫：加迁移不补清单必须红", D5_TEST, _inject_e86)

    print()
    print("=" * 78)
    print("P4 团队任务 v3 角色面与失败兜底门槛注入实验结果")
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
