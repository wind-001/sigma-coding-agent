"""影子 git checkpoint：让破坏性操作**可整体回滚**（D5 的 L2）。

它解决什么问题
    `bash` 不做命令过滤（字符串匹配挡不住 `python -c` / base64 / 先写脚本再执行），
    所以"拦得住"这条路在本项目是明确放弃的。D5 换了一条路：**拦不住也能退回去**。
    每次**写批次之前**自动提交一次工作区快照，任何一次破坏都可以回滚到"那一批次之前"。

为什么它住 ``security/`` 而不是 ``runtime/``（P6 归位;原论证见 architecture 4.x）
    它需要感知"**一个工具批次**"的边界，而这个边界只有 loop 知道——
    但那是**调用方**的知识,经 ``mark`` 接口进入;它本质是 L2 安全边界（D5）,
    不是循环机制。放 runtime 会让"安全层"这个简历级概念无处安放,
    放工具层会退化成"每个工具自己备份",重复且不可控。

**绝不碰用户仓库自己的 ``.git``**
    全部命令显式带 ``--git-dir`` / ``--work-tree``，影子库放在**工作区**的
    ``<workspace>/.sigma/session/shadow.git``（P4-批次8 起挪到 session/ 子目录,
    由产品壳决定位置,
    本模块只收 ``root``——"谁决定策略，谁传参"）。
    `git stash` 或往用户历史里插 commit 都会污染真实项目，本模块一次都不做。

**一个工作区一个库，一个会话一个分支**（P4-批次7，星辰拍板 2026-09-26）
    旧设计每会话一个全新裸库：同一工作区被 151 个会话全量复制了 151 遍
    （实测 803MB，三个家目录会话各 274/169/104MB）。新设计库里所有会话共享
    对象——同工作区第二次启动的 baseline 几乎零成本（对象已在库，``add`` 只建索引），
    成本模型从 O(工作区 × 会话数) 降到 O(工作区 × 1)。
    会话隔离靠分支：``refs/heads/<session_id>``。mark/restore 全走 plumbing
    （``write-tree`` / ``commit-tree`` / ``update-ref`` / ``read-tree``），
    **绝不碰 HEAD**——``reset --hard`` 会移动 HEAD 所指的分支，共享库里那等于
    踩坏别的会话的分支。分支名由调用方给（``branch`` 参数，产品壳传 session_id）。

    竞态边界（记录，不解决）：两个 sigma 进程同工作区并发 mark 时，
    ``commit-tree`` 的 parent 可能过期，分支 ref 被后写者覆盖——**丢一条快照，
    不崩进程**。同进程内主/子 agent 的并发已被 ``tool_lock`` 罩住（loop 层）。
    影子库是软保障，这个窗口可接受。

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

import os
import shutil
import subprocess
import time
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

#: 超大文件扫描的降频间隔：每 N 次 mark 全扫一次工作区（P4-批次7 拍板：20）。
#: 首次 mark（计数 0）必扫。代价：超 5MB 的新文件最多延迟 N 个批次才被排除——
#: 5MB 上限从硬变软，详规 §5 风险表已记录。
OVERSIZE_SCAN_INTERVAL = 20

# ----------------------------------------------------------------------
# 影子库水位治理(P4-批次8,星辰拍板 2026-09-27)
# ----------------------------------------------------------------------

#: 砍尾后当前分支保留的快照数:**基线 + 最近 K 个**。
#: 再多就失去"防止工作区被影子库撑爆"的本意,再少就丢掉回滚粒度。
WATERMARK_KEPT_SNAPSHOTS = 20

#: 非当前分支的**最短闲置时间**:最后提交距今不足它的分支不参与清理。
#: 单机同时开两个 sigma 会话是完全正常的用法——LRU 只看"最后提交时间",
#: 没有这道闸,清理会把另一个**正在跑**的会话的分支当"过时"删掉。
WATERMARK_MIN_IDLE_S = 3600.0

#: 水位检查的降频间隔:每 N 次 mark 用纯文件遍历 du 一次库目录。
#: du 不贵,但它不该出现在**每个写批次**的热路径上——降频到 1/N。
WATERMARK_SCAN_INTERVAL = 20

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
        branch: str = "main",
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        watermark_bytes: int | None = None,
        watermark_scan_interval: int = WATERMARK_SCAN_INTERVAL,
        watermark_min_idle_s: float = WATERMARK_MIN_IDLE_S,
        git_bin: str = "git",
        timeout_s: float = 60.0,
    ) -> None:
        self._root = Path(root)
        self._workspace = Path(workspace)
        #: 本会话在共享库里的分支名（``refs/heads/<branch>``）。
        #: 产品壳传 session_id；同一库服务多个会话，分支是唯一的隔离面。
        self._branch = branch
        self._max_file_bytes = max_file_bytes
        #: 库目录总大小水位(字节)。``None`` = 不治理(测试/回滚专用路径的默认)。
        #: 默认值是**产品壳的决定**(cli 从 --checkpoint-watermark-mb 换算传入)——
        #: "影子库可以占多大"是策略,本层只收数。
        self._watermark_bytes = watermark_bytes
        self._watermark_scan_interval = max(1, watermark_scan_interval)
        self._watermark_min_idle_s = watermark_min_idle_s
        self._git_bin = git_bin
        self._timeout_s = timeout_s
        self._ready: bool | None = None
        self._reason = ""
        self._oversize: set[str] = set()
        #: 空批次跳过（P4-批次7）：上次 mark 的 tree id。``write-tree`` 结果与它相同
        #: 就跳过提交——内容没变化，提交只是噪声（G66 已随之修订为
        #: "每个写批次执行前分支 tree 与工作区一致；无变化不新增提交"）。
        #: **进程内缓存**：重启后首次 mark 必提交（即基线），语义正确。
        self._last_tree: str | None = None
        #: 上次 mark 得到的 commit ref（与 ``_last_tree`` 配对更新）。
        self._last_ref: str | None = None
        self._mark_count = 0
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
    def _branch_ref(self) -> Path:
        """本会话分支的松散 ref 文件（``refs/heads/<branch>``）。

        读它拿 parent/HEAD **零进程**。前提是 ref 必须是松散文件——
        所以本库的 gc 一律带 ``gc.packRefs=false``（见 :meth:`gc`），
        这是"松散 ref 恒成立"这条不变量的执行点。
        """
        return self._root / "refs" / "heads" / self._branch

    def _head_ref(self) -> str | None:
        """本会话分支当前的 commit id；还没有任何快照时返回 ``None``。"""
        try:
            text = self._branch_ref.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

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
                "工作区级影子库随工作区走（P4-批次7）：若本项目被移动或复制过，"
                f"删除 {self._root} 可重建（会失去旧的回滚点）。"
            )
        return ""

    def _git(
        self, *args: str, timeout_s: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        """跑一条 git 命令（显式 GIT_DIR / WORK_TREE / excludes 文件）。

        ``-c core.excludesFile`` 显式指向我们的合并清单：既保证它生效，
        也**屏蔽用户全局 gitignore**——否则同一条命令在不同机器上纳入的文件不同，
        checkpoint 就不可比较了。

        传给 ``args`` 的内容都落在 ``--work-tree`` 之后、子命令之前——
        所以像 ``"-c gc.packRefs=false"`` 这种**全局配置**也能从这里传进去。
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
            timeout=self._timeout_s if timeout_s is None else timeout_s,
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
        """提交一次快照到**本会话分支**，返回 commit ref。

        **失败返回 ``None``，绝不抛**：checkpoint 是保险丝。它挂了不该让任务停下来
        （调用方在横幅 / 日志里如实展示 :attr:`last_error`）。

        四段 plumbing（P4-批次7），不碰 HEAD（共享库多分支并存的前提）：

            add -A → write-tree → commit-tree [-p parent] → update-ref

        ``commit-tree`` 的 stdout 就是新 ref，不再需要 ``rev-parse`` 进程；
        parent 从松散 ref 文件零进程读（见 :meth:`_head_ref`）。

        **空批次跳过**（G66 修订：P4-批次7）：``write-tree`` 的结果与上次相同
        就直接返回旧 ref——内容没变化，再提交只是噪声。旧版 ``--allow-empty``
        换来的"快照数 = 基线 + 写批次数"不变量随之改成
        "每个写批次执行前，分支 tree 与工作区一致；无变化不新增提交"。
        """
        self._ensure_ready()
        if self._ready is not True:
            return None
        try:
            if self._mark_count % OVERSIZE_SCAN_INTERVAL == 0 and self._collect_oversize():
                self._refresh_excludes()
            add = self._git("add", "-A")
            if add.returncode != 0:
                self._last_error = f"git add 失败：{add.stderr.strip()[:200]}"
                return None
            tree_out = self._git("write-tree")
            if tree_out.returncode != 0:
                self._last_error = f"git write-tree 失败：{tree_out.stderr.strip()[:200]}"
                return None
            tree_id = tree_out.stdout.strip()
            if tree_id and tree_id == self._last_tree:
                # 内容与上次快照一致：不新增提交，返回现有 tip。
                self._mark_count += 1
                return self._last_ref
            commit_args = ["commit-tree", tree_id, "-m", label]
            parent = self._head_ref()
            if parent is not None:
                commit_args += ["-p", parent]
            commit = self._git(*commit_args)
            if commit.returncode != 0:
                self._last_error = f"git commit-tree 失败：{commit.stderr.strip()[:200]}"
                return None
            new_ref = commit.stdout.strip()
            update = self._git("update-ref", f"refs/heads/{self._branch}", new_ref)
            if update.returncode != 0:
                self._last_error = f"git update-ref 失败：{update.stderr.strip()[:200]}"
                return None
            self._last_tree = tree_id
            self._last_ref = new_ref
            self._mark_count += 1
            self._last_error = ""
            # 水位治理(P4-批次8):降频挂在 mark 的成功路径上。
            # 它自己的 try 兜住一切——**治理失败绝不能把已经成功的快照变成 None**
            # (mark 外层的 except 会把任何异常翻译成"本次快照失败",那对水位是错的)。
            try:
                self._maybe_enforce_watermark()
            except Exception:  # noqa: BLE001 — 保险丝语义,见上
                pass
            return new_ref
        except (OSError, subprocess.SubprocessError) as exc:
            self._last_error = f"checkpoint 异常：{type(exc).__name__}: {exc}"
            return None

    def refs(self) -> list[CheckpointInfo]:
        """本会话分支已有的快照，**新 → 旧**（git log 的顺序）。

        分支不存在（还没有任何快照）时**不需要**先查松散 ref：
        ``git log`` 对不存在的分支本来就返回非 0，下面照常落到 ``[]``——
        先读一次 ref 文件是纯冗余（restore/gc 里的同款判据不删，
        那两处需要区分"没有快照"与"命令失败"两种回执）。
        """
        self._ensure_ready()
        if self._ready is not True:
            return []
        log = self._git("log", f"refs/heads/{self._branch}", "--format=%H%x1f%s")
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

        为什么是 ``read-tree --reset -u`` 而不是 ``reset --hard``（P4-批次7）：
            共享库里 HEAD 不属于任何一个会话——``reset --hard`` 会移动
            HEAD 所指的**分支**，那等于踩坏别的会话的快照链。
            ``read-tree --reset -u <ref>`` 是 ``reset --hard`` 的 plumbing 孪生
            （git 内部同一套 unpack_trees 路径）：更新 index 与工作树、
            **删掉"index 里有、ref 里没有"的文件**，但不移动任何 ref。
            "会删新增文件"这个 architecture 6.2 点名的坑由 ``-u`` + ``--reset``
            承接（``checkout <ref> -- .`` 不会删除，仍然不能用）。

        为什么先 ``mark`` 再回滚：
            工作区里可能还有"最后一次 mark 之后新增、尚未进快照"的文件。
            不先快照一次，回滚就删不掉它们（它们还是未跟踪状态），
            而且**回滚本身也就不可回滚了**。
        """
        self._ensure_ready()
        if self._ready is not True:
            return RestoreReport(ok=False, ref=ref, note=f"影子库不可用：{self._reason}")

        # **最后一道闸**：工作区配对不匹配就绝不回滚（见 workspace_mismatch）。
        # 放在这里而不是只放在 CLI：调用方可以有很多个，闸门只能有一个。
        mismatch = self.workspace_mismatch()
        if mismatch:
            return RestoreReport(ok=False, ref=ref, note=mismatch)

        if self._head_ref() is None:
            return RestoreReport(ok=False, ref=ref, note="影子库还没有任何快照")

        # 回滚前先自保：把**当前状态**存一分。
        # 这一步同时也是"差异统计"的数据源——见下面那行注释（踩过一次）。
        pre = self.mark(label=f"pre-restore:{ref[:8]}")
        if pre is None:
            # mark 的保险丝语义（失败返回 None 不阻断）在**回滚**场景是错的
            # （2026-09-24 review 修复）：自保快照没收进去的新增文件，
            # 回滚删不掉——回滚实际不完整，而报告却是 ok=True，
            # L2 从"可回滚"变成"看似回滚了"。
            # **回滚的优先级高于"不阻断"**：快照坏了就拒绝回滚，工作区原样不动。
            return RestoreReport(
                ok=False,
                ref=ref,
                note=f"回滚前自保快照失败，拒绝回滚：{self.last_error or '未知原因'}",
            )
        current = pre

        # 差异要相对 **pre-restore 快照** 算，不能相对旧 tip：
        # "最后一次 mark 之后新增/改动的文件"此刻只存在于 pre-restore 快照里，
        # 拿旧 tip 去 diff 会得到空列表——**回滚照常发生，但报告成了假的**
        # （文件删了却说没删）。这个错误第一版真踩到了，单测当场抓住。
        changed, deleted = self._diff_names(ref, current)
        reset = self._git("read-tree", "--reset", "-u", ref)
        if reset.returncode != 0:
            return RestoreReport(
                ok=False,
                ref=ref,
                note=f"read-tree --reset 失败：{reset.stderr.strip()[:200]}",
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

    def gc(self, *, timeout_s: float = 30.0) -> bool:
        """收尾打包（P4-批次7 §2.5）。失败返回 ``False``——GC 是优化，不是正确性。

        两个 ``-c`` 各管一件事：
        ``gc.autoDetach=false``——后台 gc 会与本进程后续 mark 竞态；
        ``gc.packRefs=false``——**松散 ref 是 :meth:`_head_ref` 零进程读的前提**，
        打包进 packed-refs 这条不变量就断了。
        空库（无任何快照）不跑：没有可打包的东西。
        先做**文件级**存在性检查而不是 ``_ensure_ready``——懒基线（G65 修订）意味着
        没写过文件的会话根本没有库，收尾 gc 不许凭空把它造出来。
        """
        if not (self._root / "HEAD").exists():
            return False
        self._ensure_ready()
        if self._ready is not True:
            return False
        if self._head_ref() is None:
            return False
        out = self._git(
            "-c",
            "gc.autoDetach=false",
            "-c",
            "gc.packRefs=false",
            "gc",
            "--quiet",
            timeout_s=timeout_s,
        )
        return out.returncode == 0

    # ------------------------------------------------------------------
    # 水位治理(P4-批次8:防止工作区被影子库撑爆——批次7 实测过 803MB 单库)
    # ------------------------------------------------------------------

    def enforce_watermark(self) -> int | None:
        """水位治理入口:库目录超过水位就按 LRU 清理,返回**治理后**的库大小。

        返回 ``None`` = 没查(未设水位 / 影子库不可用)——调用方据此区分
        "没查"与"0 字节"。清理失败不抛:水位是**优化**,正确性由快照与
        回滚承载;清完仍超限的事实走 :attr:`last_error` 如实说出。
        """
        self._ensure_ready()
        if self._ready is not True or self._watermark_bytes is None:
            return None
        size = self._dir_bytes()
        if size <= self._watermark_bytes:
            return size
        size = self._cleanup_over_watermark(size)
        if size > self._watermark_bytes:
            self._last_error = (
                f"影子库 {size / 1048576:.0f}MB 超过水位 "
                f"{self._watermark_bytes / 1048576:.0f}MB,按最旧优先清理后仍超——"
                "工作区排除清单之外的内容本身太大,考虑调高 --checkpoint-watermark-mb"
            )
        else:
            self._last_error = ""
        return size

    def _maybe_enforce_watermark(self) -> None:
        """mark 的节奏钩子:每 N 次快照查一次水位,平时一帧都不付。"""
        if self._watermark_bytes is None:
            return
        if self._mark_count % self._watermark_scan_interval != 0:
            return
        self.enforce_watermark()

    def _dir_bytes(self) -> int:
        """影子库目录总大小(纯文件遍历,不开子进程)。

        遍历期间文件消失(并发 gc)→ 跳过:**宁小勿崩**,这只是个治理信号。
        """
        total = 0
        for dirpath, _dirs, files in os.walk(self._root):
            for name in files:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    continue
        return total

    def _branches_by_age(self) -> list[tuple[str, float]]:
        """全部分支(按最后提交时间**升序**)。单进程 ``for-each-ref``。

        时间取不到的分支(畸形记录)按"最旧"处理——它们本来就是清理候选。
        """
        out = self._git(
            "for-each-ref",
            "--sort=committerdate",
            "--format=%(refname:short)%00%(committerdate:unix)",
            "refs/heads",
        )
        if out.returncode != 0:
            return []
        branches: list[tuple[str, float]] = []
        for line in out.stdout.splitlines():
            name, sep, ts_text = line.partition("\x00")
            if not sep:
                continue
            try:
                branches.append((name.strip(), float(ts_text)))
            except ValueError:
                branches.append((name.strip(), 0.0))
        return branches

    def _gc_prune(self) -> None:
        """删 ref 之后**真正释放对象**:reflog 清零 + ``gc --prune=now``。

        与收尾 :meth:`gc` 的差别就在 ``--prune=now``——治理要的是"马上变小",
        默认的 2 周宽限会把对象留在盘上,水位永远下不来(G121 的注入靶)。
        ``gc.packRefs=false`` 延续松散 ref 不变量(见 :meth:`gc`)。
        """
        self._git("reflog", "expire", "--expire=now", "--all")
        self._git(
            "-c",
            "gc.autoDetach=false",
            "-c",
            "gc.packRefs=false",
            "gc",
            "--quiet",
            "--prune=now",
        )

    def _cleanup_over_watermark(self, size: int) -> int:
        """LRU 清理梯子,返回治理后的库大小。**当前会话的分支绝不删。**

        第一级:非当前、闲置超过最短时间的旧分支,最旧优先(删一个量一次,
        到水位即停)——别的会话的旧回滚点先牺牲,本会话的保障分毫不动;
        第二级:仍超 → 砍当前分支老快照(基线 + 最近 K,见
        :meth:`_truncate_current_branch`)。
        """
        watermark = self._watermark_bytes
        if watermark is None:  # mypy 收窄:实例属性跨方法调用不保窄,落局部
            return size
        now = time.time()
        for branch, last_ts in self._branches_by_age():
            if size <= watermark:
                return size
            if branch == self._branch:
                continue
            if now - last_ts < self._watermark_min_idle_s:
                continue  # 可能是另一个正在跑的会话(详规 R4),跳过
            deleted = self._git("update-ref", "-d", f"refs/heads/{branch}")
            if deleted.returncode == 0:
                self._gc_prune()
                size = self._dir_bytes()
        if size > watermark:
            self._truncate_current_branch()
            self._gc_prune()
            size = self._dir_bytes()
        return size

    def _truncate_current_branch(self) -> bool:
        """当前分支重写为"基线 + 最近 K 个快照"(链式 orphan 重放)。

        基线提交(分支上第一个、无 parent)**原样保留**,SHA 不变;它之上
        最近 K 个快照逐个 ``commit-tree`` 重放——tree 与 label 原样,SHA 必然
        变了。更早的提交从此不可达,由调用方的 ``gc --prune=now`` 释放。

        快照数 ≤ K+1 时无事可做,返回 ``False``。
        """
        head = self._head_ref()
        if head is None:
            return False
        log = self._git(
            "log",
            f"refs/heads/{self._branch}",
            "--reverse",
            "--format=%H%x00%T%x00%s",
        )
        if log.returncode != 0:
            return False
        entries: list[tuple[str, str, str]] = []
        for line in log.stdout.splitlines():
            parts = line.split("\x00")
            if len(parts) == 3:
                entries.append((parts[0].strip(), parts[1].strip(), parts[2]))
        if len(entries) <= WATERMARK_KEPT_SNAPSHOTS + 1:
            return False
        baseline_sha = entries[0][0]
        kept = entries[-WATERMARK_KEPT_SNAPSHOTS:]
        parent = baseline_sha
        new_ref = baseline_sha
        for _sha, tree, label in kept:
            commit = self._git("commit-tree", tree, "-p", parent, "-m", label)
            if commit.returncode != 0:
                return False
            parent = commit.stdout.strip()
            new_ref = parent
        update = self._git("update-ref", f"refs/heads/{self._branch}", new_ref)
        if update.returncode != 0:
            return False
        # 进程内缓存同步:tip 的 **SHA 变了但 tree 没变**——空批次跳过的判据
        # (tree 相同)仍成立,配对的 ref 必须跟着换,否则下一次"无变化"的
        # mark 会返回一个马上要被 gc 掉的旧 SHA,调用方拿着它 restore 必失败。
        self._last_ref = new_ref
        self._last_tree = kept[-1][1]
        return True

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
