"""影子 git checkpoint：让破坏性操作**可整体回滚**（D5 的 L2）。

它解决什么问题
    `bash` 不做命令过滤（字符串匹配挡不住 `python -c` / base64 / 先写脚本再执行），
    所以"拦得住"这条路在本项目是明确放弃的。D5 换了一条路：**拦不住也能退回去**。
    每次**写批次之前**自动提交一次工作区快照，任何一次破坏都可以回滚到"那一批次之前"。

为什么它在 ``sigma_agent`` 而不是 ``sigma_tools``（architecture 4.x 落点表）
    它需要感知"**一个工具批次**"的边界，而这个边界只有 loop 知道。
    放进工具层会退化成"每个工具自己备份"，重复且不可控。

**绝不碰用户仓库自己的 ``.git``**
    全部命令显式带 ``--git-dir`` / ``--work-tree``，影子库放在会话目录下
    （由产品壳决定位置，本模块只收 ``root``——"谁决定策略，谁传参"）。
    `git stash` 或往用户历史里插 commit 都会污染真实项目，本模块一次都不做。

exclude 与大文件：**全走 git 原生机制，不自写匹配器**
    自写 .gitignore 匹配器是个兔子洞（转义、`**`、`!` 取反、目录 vs 文件）。做法是：

    1. 影子库的 excludes 文件 = **内置清单** + 工作区自己的 ``.gitignore`` 原样追加；
    2. 大文件（默认 > 5 MB）先用 ``status --untracked-files=all``（已认 exclude）拿候选，
       超限的追加进 excludes —— **不 hash 就不慢**；
    3. 回滚清理只处理 ``ls-files --others --exclude-standard`` 列出的文件，
       所以**被排除的文件（大产物、.env 之类）不会被回滚删掉**。

    第 3 条是这一层最危险的地方：清理逻辑写宽一点，L2 就从"可回滚"变成"数据销毁器"。

失败姿态：**保险丝，不是发动机**
    git 不在 PATH、初始化失败、命令报错——一律降级为"本次不 checkpoint"，
    **不抛异常、不阻断任务**（checkpoint 失败不该让用户丢会话）。
    可用性由 :attr:`available` 与调用方（CLI 横幅）如实展示，不静默变弱。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: 内置排除清单。它必须包含"体积大 / 与回滚无关"的那些目录，
#: 否则一次 checkpoint 会在 `.venv` / `node_modules` 上耗掉几秒——**性能就是可用性**。
BUILTIN_EXCLUDES: tuple[str, ...] = (
    ".git/",
    ".sigma/",
    ".venv/",
    "venv/",
    "env/",
    "node_modules/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".mypy_cache/",
    ".import_linter_cache/",
    ".ruff_cache/",
    "dist/",
    "build/",
    "target/",
    "*.egg-info/",
    ".idea/",
    ".vscode/",
    ".DS_Store",
    "Thumbs.db",
)

#: 单文件上限。>5 MB 不入快照（architecture 6.2 的建议值）。
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024

EXCLUDES_HEADER = (
    "# 本文件由 sigma 的影子 checkpoint 生成，每次 mark 前刷新，请勿手改。\n"
    "# 内容 = 内置排除清单 + 工作区 .gitignore + 自动排除的超大文件。\n"
)

OVERSIZE_HEADER = "# 以下路径因超过大小上限被自动排除（由 sigma 追加）"


@dataclass(frozen=True)
class CheckpointInfo:
    """一个快照。``label`` 是 mark 时给的名字（如 ``baseline`` / ``write-batch:write,edit``）。"""

    ref: str
    label: str


@dataclass(frozen=True)
class RestoreReport:
    """一次回滚的结果。**给人看，也给评测看**（回滚成功率这个指标就靠它）。"""

    ok: bool
    ref: str
    changed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    protected: int = 0
    """被 exclude 规则保护、因此**没有**参与回滚的文件数（大文件 / .env 之类）。"""

    pre_restore_ref: str | None = None
    """回滚前自动打的快照——**回滚本身也可回滚**。"""

    note: str = ""


class ShadowCheckpoint:
    """影子 git checkpoint。生命周期跟着一个会话（影子库由调用方给路径）。"""

    def __init__(
        self,
        *,
        root: Path,
        workspace: Path,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        git_bin: str = "git",
        timeout_s: float = 60.0,
    ) -> None:
        self._root = Path(root)
        self._workspace = Path(workspace)
        self._max_file_bytes = max_file_bytes
        self._git_bin = git_bin
        self._timeout_s = timeout_s
        self._ready: bool | None = None
        self._reason = ""
        self._oversize: set[str] = set()
        #: 最近一次 mark/restore 的失败原因（空串 = 没出错）。
        #: 它必须能被拿到：**静默降级的 checkpoint 等于没有 checkpoint**。
        self._last_error = ""

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """影子库可用（git 在、初始化成功）。不可用时 mark/restore 是安全空操作。"""
        self._ensure_ready()
        return self._ready is True

    @property
    def unavailable_reason(self) -> str:
        """不可用的原因（给横幅看）。可用时为空串。"""
        self._ensure_ready()
        return self._reason

    @property
    def root(self) -> Path:
        return self._root

    @property
    def last_error(self) -> str:
        """最近一次 mark/restore 的失败原因（空串表示没出错）。"""
        return self._last_error

    @property
    def _excludes_path(self) -> Path:
        return self._root / "info" / "exclude"

    @property
    def _workspace_marker(self) -> Path:
        """记录"这个影子库服务哪个工作区"的文件。

        **没有它就会发生灾难性回滚**：影子库给工作区 A 建的，却拿工作区 B 去 restore，
        `reset --hard` 会把 B 里"A 快照里没有"的文件**全删掉**。
        这不是假想——2026-09-22 的真模型冒烟里真发生了：CLI 回滚忘了传 `--workspace`，
        默认当前目录（仓库根），于是删掉仓库 171 个文件（幸而回滚前的自动快照把它救了回来）。

        教训的通用形式：**两份状态各自记录"我们配对"是不够的，必须有一处把配对写下来。**
        """
        return self._root / "sigma-workspace.txt"

    @property
    def recorded_workspace(self) -> Path | None:
        """创建时记下的工作区。旧版本建的影子库没有这一项 → None。"""
        self._ensure_ready()
        try:
            text = self._workspace_marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return Path(text) if text else None

    def workspace_mismatch(self) -> str:
        """工作区配对检查。返回空串 = 匹配；否则返回一句给人看的原因。"""
        recorded = self.recorded_workspace
        current = self._workspace.resolve()
        if recorded is None:
            return (
                f"影子库 {self._root} 没有记录它服务的工作区（旧版本创建的）。"
                "不能安全回滚——请手工核对后再决定怎么处理。"
            )
        if recorded.resolve() != current:
            return (
                f"工作区不匹配：影子库属于 {recorded}，当前给的是 {current}。\n"
                "**回滚会按 A 的快照去改 B，删掉 B 里那些文件**——所以这里直接拒绝。\n"
                f"请 cd 到 {recorded}，或显式传 --workspace {recorded}。"
            )
        return ""

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        """跑一条 git 命令（显式 GIT_DIR / WORK_TREE / excludes 文件）。

        ``-c core.excludesFile`` 显式指向我们的合并清单：既保证它生效，
        也**屏蔽用户全局 gitignore**——否则同一条命令在不同机器上纳入的文件不同，
        checkpoint 就不可比较了。
        """
        return subprocess.run(
            [
                self._git_bin,
                "-c",
                "user.name=sigma-checkpoint",
                "-c",
                "user.email=sigma@checkpoint.local",
                "-c",
                "core.excludesFile=" + str(self._excludes_path),
                "-c",
                "core.autocrlf=false",
                "--git-dir",
                str(self._root),
                "--work-tree",
                str(self._workspace),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self._timeout_s,
            check=False,
        )

    def _ensure_ready(self) -> None:
        """懒初始化。**任何失败都降级为不可用，不抛。**"""
        if self._ready is not None:
            return
        if shutil.which(self._git_bin) is None:
            self._ready = False
            self._reason = f"PATH 中找不到 {self._git_bin}，本次会话没有回滚保障"
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            if not (self._root / "HEAD").exists():
                # init **不能**带 --work-tree：仓库还不存在，git 会直接报
                # "GIT_WORK_TREE not allowed without specifying GIT_DIR"（实测）。
                # 先建库，之后所有命令再显式带 --work-tree。
                init = subprocess.run(
                    [self._git_bin, "--git-dir", str(self._root), "init", "--bare", "--quiet"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self._timeout_s,
                    check=False,
                )
                if init.returncode != 0:
                    self._ready = False
                    self._reason = f"影子库初始化失败：{init.stderr.strip()[:200]}"
                    return
                # 把"这个影子库服务哪个工作区"写下来（防灾难性回滚，见 _workspace_marker）
                self._workspace_marker.parent.mkdir(parents=True, exist_ok=True)
                self._workspace_marker.write_text(
                    str(self._workspace.resolve()), encoding="utf-8"
                )
            self._refresh_excludes()
        except (OSError, subprocess.SubprocessError) as exc:
            self._ready = False
            self._reason = f"影子库初始化异常：{type(exc).__name__}: {exc}"
            return
        self._ready = True

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    # ------------------------------------------------------------------
    # 写快照
    # ------------------------------------------------------------------

    def mark(self, *, label: str) -> str | None:
        """提交一次快照，返回 commit ref。

        **失败返回 ``None``，绝不抛**：checkpoint 是保险丝。它挂了不该让任务停下来
        （调用方在横幅 / 日志里如实展示 :attr:`last_error`）。
        ``--allow-empty`` 是有意的：没变化也留一条提交，
        这样"快照数 = 基线 + 写批次数"这条不变量成立（门槛 G66 就断言它）。
        """
        self._ensure_ready()
        if self._ready is not True:
            return None
        try:
            if self._collect_oversize():
                self._refresh_excludes()
            add = self._git("add", "-A")
            if add.returncode != 0:
                self._last_error = f"git add 失败：{add.stderr.strip()[:200]}"
                return None
            commit = self._git("commit", "--allow-empty", "--quiet", "-m", label)
            if commit.returncode != 0:
                self._last_error = f"git commit 失败：{commit.stderr.strip()[:200]}"
                return None
            head = self._git("rev-parse", "HEAD")
            if head.returncode != 0:
                self._last_error = "git rev-parse 失败（拿不到快照 ref）"
                return None
            self._last_error = ""
            return head.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            self._last_error = f"checkpoint 异常：{type(exc).__name__}: {exc}"
            return None

    def refs(self) -> list[CheckpointInfo]:
        """已有快照，**新 → 旧**（git log 的顺序）。"""
        self._ensure_ready()
        if self._ready is not True:
            return []
        log = self._git("log", "--format=%H%x1f%s")
        if log.returncode != 0:
            return []
        out: list[CheckpointInfo] = []
        for line in log.stdout.splitlines():
            if "\x1f" not in line:
                continue
            ref, label = line.split("\x1f", 1)
            out.append(CheckpointInfo(ref=ref.strip(), label=label.strip()))
        return out

    def _collect_oversize(self) -> bool:
        """把超过大小上限的未跟踪文件记进排除集。返回是否新增（新增才需刷新 excludes）。

        用 ``status --untracked-files=all`` 而不是自己走目录：**让 git 认 exclude**，
        我们只做"看大小"这一件 git 不做的事。
        """
        status = self._git("status", "--porcelain", "-z", "--untracked-files=all")
        if status.returncode != 0:
            return False
        changed = False
        for entry in status.stdout.split("\0"):
            if not entry.startswith("?? "):
                continue
            rel = entry[3:].strip()
            if not rel or rel in self._oversize:
                continue
            try:
                path = self._workspace / rel
                if path.is_file() and path.stat().st_size > self._max_file_bytes:
                    self._oversize.add(rel)
                    changed = True
            except OSError:
                continue
        return changed

    def _protected_count(self) -> int:
        """被 exclude 规则保护的文件数（回滚**不会**碰它们）。"""
        all_untracked = self._git("ls-files", "--others", "-z")
        visible = self._git("ls-files", "--others", "--exclude-standard", "-z")
        if all_untracked.returncode != 0 or visible.returncode != 0:
            return 0
        total = len([p for p in all_untracked.stdout.split("\0") if p.strip()])
        shown = len([p for p in visible.stdout.split("\0") if p.strip()])
        return max(0, total - shown)

    def _refresh_excludes(self) -> None:
        """重建 excludes 文件：内置清单 + 工作区 .gitignore + 已发现的超大文件。"""
        lines = [EXCLUDES_HEADER, *BUILTIN_EXCLUDES, ""]
        ignore = self._workspace / ".gitignore"
        if ignore.is_file():
            lines.append("# ↓ 以下来自工作区的 .gitignore")
            lines.append(self._read_text(ignore).rstrip())
            lines.append("")
        if self._oversize:
            lines.append(OVERSIZE_HEADER)
            lines.extend(sorted(self._oversize))
            lines.append("")
        self._excludes_path.parent.mkdir(parents=True, exist_ok=True)
        self._excludes_path.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # 回滚
    # ------------------------------------------------------------------

    def restore(self, ref: str) -> RestoreReport:
        """回到 ``ref``：恢复被改的文件、**删除 ref 之后新增的文件**。

        为什么是 ``reset --hard`` 而不是 ``checkout <ref> -- .``：
            ``checkout`` **不会删除新增的文件**（architecture 6.2 点名的坑），
            而"新建一个文件再把仓库搞坏"恰恰是最常见的破坏形态。
            ``reset --hard`` 会把"HEAD 里有、ref 里没有"的文件一并删掉。

        为什么先 ``mark`` 再 reset：
            工作区里可能还有"最后一次 mark 之后新增、尚未进快照"的文件。
            不先快照一次，``reset --hard`` 就删不掉它们（它们还是未跟踪状态），
            而且**回滚本身也就不可回滚了**。
        """
        self._ensure_ready()
        if self._ready is not True:
            return RestoreReport(ok=False, ref=ref, note=f"影子库不可用：{self._reason}")

        # **最后一道闸**：工作区配对不匹配就绝不 reset（见 workspace_mismatch）。
        # 放在这里而不是只放在 CLI：调用方可以有很多个，闸门只能有一个。
        mismatch = self.workspace_mismatch()
        if mismatch:
            return RestoreReport(ok=False, ref=ref, note=mismatch)

        head = self._git("rev-parse", "HEAD")
        if head.returncode != 0:
            return RestoreReport(ok=False, ref=ref, note="影子库还没有任何快照")

        # 回滚前先自保：把**当前状态**存一分。
        # 这一步同时也是"差异统计"的数据源——见下面那行注释（踩过一次）。
        pre = self.mark(label=f"pre-restore:{ref[:8]}")
        if pre is None:
            # mark 的保险丝语义（失败返回 None 不阻断）在**回滚**场景是错的
            # （2026-09-24 review 修复）：自保快照没收进去的新增文件，
            # ``reset --hard`` 删不掉（它不碰未跟踪文件）——回滚实际不完整，
            # 而报告却是 ok=True，L2 从"可回滚"变成"看似回滚了"。
            # **回滚的优先级高于"不阻断"**：快照坏了就拒绝回滚，工作区原样不动。
            return RestoreReport(
                ok=False,
                ref=ref,
                note=f"回滚前自保快照失败，拒绝回滚：{self.last_error or '未知原因'}",
            )
        current = pre

        # 差异要相对 **pre-restore 快照** 算，不能相对 head_ref：
        # "最后一次 mark 之后新增/改动的文件"此刻只存在于 pre-restore 快照里，
        # 拿旧 HEAD 去 diff 会得到空列表——**回滚照常发生，但报告成了假的**
        # （文件删了却说没删）。这个错误第一版真踩到了，单测当场抓住。
        changed, deleted = self._diff_names(ref, current)
        reset = self._git("reset", "--hard", "--quiet", ref)
        if reset.returncode != 0:
            return RestoreReport(
                ok=False,
                ref=ref,
                note=f"reset --hard 失败：{reset.stderr.strip()[:200]}",
                pre_restore_ref=pre,
            )
        return RestoreReport(
            ok=True,
            ref=ref,
            changed=changed,
            deleted=deleted,
            protected=self._protected_count(),
            pre_restore_ref=pre,
        )

    def _diff_names(self, from_ref: str, to_ref: str) -> tuple[list[str], list[str]]:
        """``to_ref`` 相对 ``from_ref`` 的变更：返回（会被改回的, 会被删掉的）。

        状态字母（相对 ``from_ref`` 看）：``A`` = from_ref 之后新增 → 回滚时**删除**；
        ``M`` = 改过 → 恢复；``D`` = from_ref 之后被删 → 重建。
        ``R`` / ``C``（重命名/复制）记录有**三个字段**，步长必须不同——
        按两个字段硬走会让后面所有记录错位，症状是"文件名串行"。
        """
        out = self._git("diff", "--name-status", "-z", from_ref, to_ref)
        if out.returncode != 0:
            return [], []
        parts = out.stdout.split("\0")
        changed: list[str] = []
        deleted: list[str] = []
        index = 0
        while index < len(parts):
            status = parts[index].strip()
            index += 1
            if not status:
                continue
            take = 2 if status[0] in ("R", "C") else 1
            names = [p for p in parts[index : index + take] if p]
            index += take
            if not names:
                continue
            if status[0] == "A":
                deleted.append(names[-1])
            else:
                changed.append(names[-1])
        return changed, deleted
