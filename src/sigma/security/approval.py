"""审批拦截层(L3):决策型审批钩子的产品壳实现(P3-批次2)。

三层拦截在这里补完最后一块:
    L1 写路径约束(security/path_sandbox.py)——越界**拒绝**,审批豁免后放行;
    L2 影子 git checkpoint(sigma.agent/checkpoint)——可回滚兜底;
    L3 审批(**本模块**)——危险/越界的动作在执行前交给人确认。

**它是什么、不是什么(与架构 6.3 同一条诚实声明)**
    危险指令匹配是**软边界**:字符串与启发式只能减少、不能消除危险
    (``python -c``、base64、先写脚本再执行都能绕过)。它的价值是
    "把看得见的危险明示出来,让用户决定",不是安全边界本身;
    真正的兜底是 L2 的可回滚。提示文案里会把这个后果说给用户听。

链路(CliApprovalGate.approve):
    allowlist 精确命中 → 放行(不打扰)
    → 分析(越界写 / 危险指令)→ 无标记 → 放行(日常操作零打扰)
    → 有标记 → confirmer(y 允许一次 / a 总是允许并记入 allowlist / n 拒绝)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sigma.events.lifecycle import (
    ApprovalDecision,
)
from sigma.hooks.base import (
    ApprovalHook,
)

#: chooser:决策型交互的统一协议(P3-批次2 追加,按键化)。
#: 参数 = (标题行[调用预览/标记/后果], 选项文本, 推荐下标);
#: 返回选中下标;None = 取消(Esc / Ctrl+C / EOF)→ 按拒绝处理。
#: 产品壳实现(sigma/_selector.choose_option)负责按键选择与编号降级。
Chooser = Callable[[list[str], list[str], int | None], Awaitable[int | None]]

#: 审批三选项。**次序与语义固定**,chooser 的默认光标由推荐下标表达
#(星辰拍板:默认停在"允许一次"——常见路径直接回车;误回车兜底是 L2 回滚)。
APPROVAL_OPTIONS: tuple[str, str, str] = (
    "允许一次",
    "总是允许(写入 .sigma/allowlist.json)",
    "拒绝",
)


# ---------------------------------------------------------------------------
# 危险指令清单(内置,软边界)
# ---------------------------------------------------------------------------

#: (编译正则, 名称, 后果说明)。匹配不区分大小写;只收"无歧义危险"的模式,
#: 目标在工作区内外的细分判断在 analyze_call 里做。
DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"\bsudo\b", re.I),
        "sudo 提权执行",
        "以系统管理员权限运行,影响范围超出当前用户;任何写操作都不再受工作区约束",
    ),
    (
        re.compile(r"\bdd\b[^;|&]*\bof=", re.I),
        "dd 直接写块设备",
        "低层写入磁盘/设备,目标设备上的数据会被整体覆盖",
    ),
    (
        re.compile(r"\bmkfs\b|\bformat\b(?!ed\b)", re.I),
        "格式化文件系统",
        "整卷格式化,卷上数据全部丢失",
    ),
    (
        re.compile(r"(curl|wget)\b[^;|&]*\|\s*(ba|z)?sh\b", re.I),
        "下载脚本直接执行",
        "执行来历不明的远程代码,等价于把本机控制权交给脚本作者",
    ),
    (
        re.compile(r"\bgit\s+push\b[^;|&]*(--force\b|-f\b)", re.I),
        "git push --force",
        "强制覆盖远端分支历史,他人基于原历史的提交会丢失",
    ),
    (
        re.compile(r"\bchmod\s+(-R\s+)?777\b"),
        "chmod 777 开放全部权限",
        "任何用户/进程都可读写执行目标文件,是常见提权入口",
    ),
    (
        re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I),
        "关机/重启",
        "立即终止本机全部进程,包括 harness 自身与未保存状态",
    ),
    (
        re.compile(r"\bdel\s+/[sq]", re.I),
        "del /s /q 递归静默删除",
        "Windows 递归删除且不进回收站,删除后不可恢复",
    ),
    (
        re.compile(r"\brd\s+/s\b", re.I),
        "rd /s 递归删除目录树",
        "Windows 递归删除目录树且不进回收站",
    ),
    (
        re.compile(r"\breg\s+(delete|add)\b", re.I),
        "写注册表",
        "修改系统注册表,影响全局配置且难以回退",
    ),
    (
        re.compile(r">\s*/dev/(sd|nvme|hd)", re.I),
        "重定向写块设备",
        "把数据直接写进物理设备,设备上原有数据被覆盖",
    ),
)

#: bash 命令里"递归删除且目标疑似工作区之外"的启发式。
_RECURSIVE_DELETE = re.compile(r"\brm\s+(-[a-z]*r[a-z]*f?|-[a-z]*f[a-z]*r)\b", re.I)

#: 绝对路径/Home/父目录记号——出现在写向动作之后才算"疑似越界写"。
_ABS_PATH_TOKEN = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s;|&\"']*|/(?:home|Users|root|etc|var|usr|opt)[^\s;|&\"']*|~[^\s;|&\"']*)"
)
_WRITE_VERBS = re.compile(r"\b(rm|del|rd|mv|move|cp|copy|tee|touch|mkdir)\b|>{1,2}", re.I)

#: 诚实声明常量:每条确认提示都会带上(架构 6.3 的要求——写清楚挡不住什么)。
HONESTY_NOTE = (
    "提示:命令匹配是软边界,python -c / base64 / 先写脚本再执行都能绕过;"
    "真正的兜底是 L2 影子快照(只覆盖工作区内文件)。"
)


@dataclass(frozen=True)
class Finding:
    """一条审批标记:为什么这次调用需要人看。"""

    kind: str  # "danger" | "outside"
    detail: str  # 展示给用户的说明(含后果)


def analyze_call(
    name: str, arguments: dict[str, Any], workspace: Path
) -> list[Finding]:
    """分析一个即将执行的调用,返回审批标记(空 = 无标记,直接放行)。"""
    findings: list[Finding] = []

    if name == "bash":
        command = str(arguments.get("command", ""))
        lowered = command.lower()
        for pattern, label, consequence in DANGEROUS_PATTERNS:
            if pattern.search(lowered if pattern.flags & re.I else command):
                findings.append(
                    Finding("danger", f"命中危险模式[{label}];后果:{consequence}")
                )
        # 递归删除 + 目标疑似工作区之外(rm -rf build 在工作区内不标记,L2 兜底)
        if _RECURSIVE_DELETE.search(command):
            for token in _ABS_PATH_TOKEN.findall(command):
                if not _under_workspace(token, workspace):
                    findings.append(
                        Finding(
                            "danger",
                            f"递归删除目标疑似在工作区之外:{token};"
                            "删除后无法用 sigma --rollback 找回",
                        )
                    )
                    break
        # 写向动词 + 工作区之外的绝对路径(启发式;漏报有 L1/L2 兜底)
        if _WRITE_VERBS.search(command):
            for token in _ABS_PATH_TOKEN.findall(command):
                if not _under_workspace(token, workspace):
                    findings.append(
                        Finding(
                            "outside",
                            f"命令疑似写向工作区之外:{token}(启发式,以实际执行为准)",
                        )
                    )
                    break
        cwd = arguments.get("cwd")
        if isinstance(cwd, str) and cwd:
            # 相对 cwd **按工作区根解析**——与 bash 工具的 L1 语义
            # (resolve_write_path) 同源。2026-09-28 对抗集 a08 实测:
            # 此前按进程 cwd 解析,模型传 cwd="." 时被误判越界(误拦),
            # 而工具真实执行落点却是工作区——判定与执行必须看同一个基准。
            candidate = Path(cwd)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            if not _under_workspace(str(candidate), workspace):
                findings.append(
                    Finding("outside", f"bash 的工作目录越出工作区:{cwd}")
                )

    elif name in ("write", "edit"):
        raw = arguments.get("path")
        if isinstance(raw, str) and raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(workspace.resolve(strict=False)):
                findings.append(
                    Finding(
                        "outside",
                        f"写路径越出工作区:{resolved};"
                        "批准后本次放行(L1 校验照常,回滚依赖 L2 对区外文件不生效)",
                    )
                )

    return findings


def _under_workspace(token: str, workspace: Path) -> bool:
    """路径记号是否落在工作区内(启发式;不 resolve 符号链接)。"""
    candidate = Path(token)
    try:
        if candidate.is_absolute():
            resolved = candidate.resolve(strict=False)
        else:
            expanded = candidate.expanduser()
            resolved = expanded.resolve(strict=False)
    except (OSError, ValueError, RuntimeError):
        return False
    return resolved.is_relative_to(workspace.resolve(strict=False))


def allowlist_key(name: str, arguments: dict[str, Any], workspace: Path) -> str:
    """allowlist 条目的键:**精确匹配**的已批准动作。

    bash = 空白规范化后的完整命令;write/edit = resolve 后的绝对路径。
    保守判据:能进清单的都是被明确批准过的具体动作(批次 Q4 拍板)。
    """
    if name == "bash":
        command = str(arguments.get("command", ""))
        return f"bash|{' '.join(command.split())}"
    if name in ("write", "edit"):
        raw = arguments.get("path")
        if isinstance(raw, str) and raw:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            return f"{name}|{candidate.resolve(strict=False)}"
    return f"{name}|{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"


# ---------------------------------------------------------------------------
# allowlist 存储
# ---------------------------------------------------------------------------


class Allowlist:
    """允许清单:精确匹配的已批准动作,落 ``<workspace>/.sigma/allowlist.json``。

    为什么放 ``.sigma/``:它在影子 checkpoint 的内置排除清单里——
    ``reset --hard`` 碰不到它,回滚不会把用户批过的动作"滚丢"
    (与 todo 账本同一条判据)。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[str] = []
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self._entries = [str(item) for item in loaded]
            except (OSError, ValueError):
                # 坏文件当空清单:审批流不因清单损坏而瘫痪,下次 add 会重写
                self._entries = []

    def contains(self, key: str) -> bool:
        return key in self._entries

    def add(self, key: str) -> str | None:
        """加入清单并落盘。返回 None = 成功;返回字符串 = 落盘失败原因
        (内存已生效,审批照常放行——保障类动作不成为失败源)。"""
        if key not in self._entries:
            self._entries.append(key)
        return self._save()

    def remove(self, key: str) -> bool:
        if key not in self._entries:
            return False
        self._entries.remove(key)
        self._save()
        return True

    def items(self) -> list[str]:
        return list(self._entries)

    def _save(self) -> str | None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            return str(exc)
        return None


# ---------------------------------------------------------------------------
# 审批钩子(产品壳组装)
# ---------------------------------------------------------------------------


class CliApprovalGate(ApprovalHook):
    """终端审批门:allowlist → 分析 → 有标记才打扰用户。

    ``chooser`` 可注入:REPL/一次性 = 按键选择器(prompt_toolkit 后端);
    测试 = 桩。stdin 不可交互时 chooser 走编号输入降级;EOF/取消返回
    None → 按拒绝处理(批次 Q2 拍板:一次性也弹确认,但无交互通道
    只能拒绝,并如实告知)。
    """

    name = "cli-approval"

    def __init__(
        self,
        workspace: Path,
        chooser: Chooser,
        allowlist: Allowlist,
    ) -> None:
        self._workspace = workspace
        self._chooser = chooser
        self._allowlist = allowlist

    @property
    def allowlist(self) -> Allowlist:
        """/allowlist 斜杠命令与横幅统计用。"""
        return self._allowlist

    async def approve(
        self, name: str, arguments: dict[str, Any], call_id: str
    ) -> ApprovalDecision:
        findings = analyze_call(name, arguments, self._workspace)
        if not findings:
            return ApprovalDecision(allowed=True)

        key = allowlist_key(name, arguments, self._workspace)
        if self._allowlist.contains(key):
            return ApprovalDecision(allowed=True, note="allowlist 命中,免确认")

        title_lines = _render_title(name, arguments, findings)
        chosen = await self._chooser(title_lines, list(APPROVAL_OPTIONS), 0)
        if chosen == 0:
            return ApprovalDecision(allowed=True)
        if chosen == 1:
            error = self._allowlist.add(key)
            note = "已加入 allowlist,后续相同动作不再询问"
            if error:
                note += f"(⚠ 清单落盘失败:{error};本次内存生效)"
            return ApprovalDecision(allowed=True, note=note)
        # 拒绝 / 取消(Esc / Ctrl+C / EOF)
        return ApprovalDecision(
            allowed=False,
            reason=";".join(f.detail for f in findings) or "用户拒绝",
            note="用户在终端拒绝了这次调用",
        )


def _render_title(
    name: str, arguments: dict[str, Any], findings: list[Finding]
) -> list[str]:
    """确认标题行:调用了什么、为什么拦、后果。选项区由选择器渲染。"""
    preview = json.dumps(arguments, ensure_ascii=False)
    if len(preview) > 160:
        preview = preview[:160] + " …"
    lines = [f"⏺ {name} 想要执行:{preview}"]
    for finding in findings:
        lines.append(f"  ⚠ {finding.detail}")
    lines.append(f"  {HONESTY_NOTE}")
    return lines
