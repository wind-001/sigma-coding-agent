"""P3-扩展热重载 门槛注入实验:逐条证伪 G-HR-1..5(详规 §5)。

为什么必须做这个
    P0 固化的教训:**「配置跑绿」不等于「约束生效」**。热重载的高危面:
    ①get() 若被调用方缓存,替换实现永远不生效——G-HR-1 的全部内容;
    ②三个失败场景只要有一个"报了没做"(导入失败清表 / 空 TOOLS 残留 /
    重名放行),半更新或静默覆盖就回来了——G-HR-2/5;
    ③重建路径丢压缩视图或忘记 rebind persist 钩子,症状都离根因极远
    ——G-HR-3;④启动装载与 --no-extensions 开关——G-HR-4。
    每条都要基线绿 → 注入红,还原后由测试自身保证复绿。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_hot_reload.py
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = REPO / ".venv" / "Scripts" / "python.exe"

print(f"[env] 仓库根 = {REPO}")
print(f"[env] python  = {PYTHON}")
assert (REPO / "src" / "sigma" / "tools" / "registry.py").exists(), "仓库根解析错了"


class Repo:
    def __init__(self) -> None:
        self._pending: list[tuple[Path, str]] = []

    def patch(self, rel_path: str, old: str, new: str) -> None:
        path = REPO / rel_path
        text = path.read_text(encoding="utf-8")
        if not any(p == path for p, _ in self._pending):
            self._pending.append((path, text))
        if old not in text:
            raise AssertionError(f"{rel_path}: 注入锚点没找到:{old[:70]!r}")
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


RESULTS: list[tuple[str, str, bool, str]] = []


def experiment(
    gate: str, what: str, target: str, inject: Callable[[Repo], None]
) -> None:
    """基线必须绿 → 注入 → 必须红。还原在 finally,失败也不留改动。"""
    repo = Repo()
    try:
        code, out = repo.run_pytest(target)
        if code != 0:
            RESULTS.append((gate, what, False, f"基线不是绿的,实验无效:{out.strip()[-200:]}"))
            return
        inject(repo)
        code, out = repo.run_pytest(target)
        if code == 0:
            RESULTS.append((gate, what, False, "注入后**仍然全绿** → 门槛没有在防它声称防的东西"))
            return
        failing = [
            line for line in out.splitlines()
            if line.startswith("FAILED") or line.startswith("ERROR") or "BROKEN" in line
        ]
        detail = failing[0][:150] if failing else out.strip().splitlines()[-1][:150]
        RESULTS.append((gate, what, True, detail))
    finally:
        repo.restore()


REGISTRY = "src/sigma/tools/registry.py"
CONTEXT = "src/sigma/sessions/context.py"
SDK = "src/sigma/sdk.py"
HR_TESTS = "tests/test_hot_reload.py"


def _inject_ehr1(repo: Repo) -> None:
    """E-HR-1 / G-HR-1:get() 缓存实例 → 替换实现永不生效(调用方违约的形状)。"""
    repo.patch(
        REGISTRY,
        """        try:
            return self._definitions[name].tool
        except KeyError:""",
        """        try:
            cache = getattr(self, "_get_cache", None)
            if cache is None:
                cache = self._get_cache = {}
            if name not in cache:
                cache[name] = self._definitions[name].tool
            return cache[name]  # 注入:调用方缓存实例 → 热重载当轮失效
        except KeyError:""",
    )


def _inject_ehr2(repo: Repo) -> None:
    """E-HR-2 / G-HR-2 场景1:导入失败也清旧项 → 半更新。"""
    repo.patch(
        REGISTRY,
        """        except BaseException as exc:  # 扩展代码什么都可能抛，全接
            return ReloadReport(""",
        """        except BaseException as exc:  # 扩展代码什么都可能抛，全接
            for _name in old_names:
                self._definitions.pop(_name, None)  # 注入:导入失败也清旧项 → 工具凭空消失
            return ReloadReport(""",
    )


def _inject_ehr3(repo: Repo) -> None:
    """E-HR-3 / G-HR-2 场景2:空 TOOLS 只报不删 → "已移除"变成谎言。"""
    repo.patch(
        REGISTRY,
        """        if tools is None or (isinstance(tools, list) and not tools):
            for name in old_names:
                del self._definitions[name]
            return ReloadReport(source=source, removed=tuple(old_names))""",
        """        if tools is None or (isinstance(tools, list) and not tools):
            return ReloadReport(source=source, removed=tuple(old_names))  # 注入:声称移除,旧项残留""",
    )


def _inject_ehr4(repo: Repo) -> None:
    """E-HR-4 / G-HR-2 场景3:重名检查放行 → 静默覆盖内置工具。"""
    repo.patch(
        REGISTRY,
        """        if intra or external:
            clash = sorted(intra | external)[0]""",
        """        if False and (intra or external):  # 注入:重名放行 → 静默覆盖
            clash = sorted(intra | external)[0]""",
    )


def _inject_ehr5(repo: Repo) -> None:
    """E-HR-5 / G-HR-3:rebuild 丢压缩视图 → 摘要凭空消失。"""
    repo.patch(
        CONTEXT,
        """        if self._summary is not None and self._keep_from is not None:
            rebuilt.apply_compaction(self._summary, keep_from=self._keep_from)""",
        """        if False and self._summary is not None and self._keep_from is not None:  # 注入:压缩视图丢失
            rebuilt.apply_compaction(self._summary, keep_from=self._keep_from)""",
    )


def _inject_ehr6(repo: Repo) -> None:
    """E-HR-6 / G-HR-3:reload 忘记 rebind persist 钩子 → 钩子持旧 context。"""
    repo.patch(
        SDK,
        """        self._context = self._context.rebuild_with_tools(self._registry.schemas())
        self._persist_hook.rebind(self._context)
        return reports""",
        """        self._context = self._context.rebuild_with_tools(self._registry.schemas())
        return reports  # 注入:忘记 rebind → 持久化钩子仍持被废弃的旧 context""",
    )


def _inject_ehr7(repo: Repo) -> None:
    """E-HR-7 / G-HR-4:坏扩展拖死启动 → 一个语法错误终结整个会话。"""
    repo.patch(
        REGISTRY,
        """        if not directory.is_dir():
            return []
        return [self._install_extension(path) for path in sorted(directory.glob("*.py"))]""",
        """        if not directory.is_dir():
            return []
        _reports = []
        for _path in sorted(directory.glob("*.py")):
            _report = self._install_extension(_path)
            if not _report.ok:
                raise RuntimeError(f"注入:坏扩展拖死启动 {_report.failed_reason}")
            _reports.append(_report)
        return _reports""",
    )


def _inject_ehr8(repo: Repo) -> None:
    """E-HR-8 / G-HR-4:enable_extensions 开关失效 → --no-extensions 也装载。"""
    repo.patch(
        SDK,
        """        self._extension_reports: list[ReloadReport] = (
            list(self._registry.load_extensions(workspace_root / "extensions"))
            if enable_extensions
            else []
        )""",
        """        self._extension_reports: list[ReloadReport] = (
            list(self._registry.load_extensions(workspace_root / "extensions"))
            if True  # 注入:开关失效,--no-extensions 照样装载
            else []
        )""",
    )


def _inject_ehr9(repo: Repo) -> None:
    """E-HR-9 / G-HR-5:报告字段失真 → added/removed 与实际改动不符。"""
    repo.patch(
        REGISTRY,
        """        return ReloadReport(
            source=source,
            added=tuple(sorted(incoming)),
            removed=tuple(old_names),
        )""",
        """        return ReloadReport(
            source=source,
            added=(),  # 注入:报告失真,动了什么不说
            removed=(),
        )""",
    )


EXPERIMENTS: list[tuple[str, str, str, Callable[[Repo], None]]] = [
    ("E-HR-1", "G-HR-1  get() 缓存实例 → 替换永不生效", f"{HR_TESTS}::test_reload_takes_effect_get_returns_new_instance", _inject_ehr1),
    ("E-HR-2", "G-HR-2  导入失败清旧项 → 半更新", f"{HR_TESTS}::test_import_error_keeps_old_table", _inject_ehr2),
    ("E-HR-3", "G-HR-2  空 TOOLS 只报不删 → 假移除", f"{HR_TESTS}::test_empty_tools_removes_old_entries", _inject_ehr3),
    ("E-HR-4", "G-HR-2  重名放行 → 静默覆盖内置", f"{HR_TESTS}::test_duplicate_with_builtin_rejected_and_builtin_intact", _inject_ehr4),
    ("E-HR-5", "G-HR-3  重建丢压缩视图 → 摘要凭空消失", f"{HR_TESTS}::test_session_reload_rebuilds_context_keeps_history", _inject_ehr5),
    ("E-HR-6", "G-HR-3  忘记 rebind persist → 钩子持旧 context", f"{HR_TESTS}::test_session_reload_rebinds_persist_hook", _inject_ehr6),
    ("E-HR-7", "G-HR-4  坏扩展拖死启动", f"{HR_TESTS}::test_startup_loads_good_and_reports_bad", _inject_ehr7),
    ("E-HR-8", "G-HR-4  开关失效 → --no-extensions 照装", f"{HR_TESTS}::test_startup_disabled_loads_nothing", _inject_ehr8),
    ("E-HR-9", "G-HR-5  报告字段失真", f"{HR_TESTS}::test_reload_takes_effect_get_returns_new_instance", _inject_ehr9),
]


def main() -> int:
    for gate, what, target, inject in EXPERIMENTS:
        print(f"[{gate}] 注入:{what}")
        experiment(gate, what, target, inject)

    print()
    print("=" * 78)
    print("P3-扩展热重载 门槛注入实验结果")
    print("=" * 78)
    ok = 0
    for gate, what, passed, detail in RESULTS:
        mark = "PASS" if passed else "FAIL"
        if passed:
            ok += 1
        print(f"[{mark}] {gate}  {what}")
        print(f"       → {detail}")
    print("=" * 78)
    print(f"{ok}/{len(RESULTS)} 条门槛被成功证伪(注入后确实变红)")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
