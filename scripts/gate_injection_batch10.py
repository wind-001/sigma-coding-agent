"""P2-3 门槛注入实验：逐条证伪上下文改造门槛（G50 扩展 / G54 / G59）。

沿用批次 2–4 / 7 / 8 / P2-1 / P2-2 的框架（在真实仓库上改、跑、finally 还原），
**不另写一份 Repo**——验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E62 | G50 扩展：项目说明也是常驻区 | 指纹只用系统提示词（漏掉 AGENTS.md）| 改 AGENTS.md 后不抛 |
| E63 | G54 `AGENTS.md` 硬截断 | 短路阈值判断（从不截断）| 截断后 token 超上限 |
| E64 | G59 常驻区预算硬闸 | 去掉 `verify_resident_budget()` 调用 | 超预算不再抛 |

**为什么 G54 的注入必须"从头到尾不截断"**

    只把上限调大是不够的——那测的是"数字配置"，不是"截断逻辑还在"。
    真正的失败模式是**有人把截断整段删掉**（觉得"用户的文件凭什么被砍"），
    那时 `truncated` 恒为 False、`tokens` 等于原文长度，
    于是常驻区被用户的一篇长文顶爆。

**为什么 G59 的断言要带上"用了多少 / 上限多少"**

    只断言"抛了"的话，任何异常都能让它绿（比如 KeyError）。
    断言异常文案里带着数字，才证明是**这条**校验在报错。

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch10.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

CONTEXT = "core/sigma_session/context.py"
RESOURCES = "core/sigma_session/resources.py"

CONTEXT_TESTS = "tests/test_session_context.py"
RESOURCES_TESTS = "tests/test_session_resources.py"

FINGERPRINT_TEST = (
    f"{CONTEXT_TESTS}::test_project_instructions_participate_in_fingerprint"
)
TRUNCATE_TEST = f"{RESOURCES_TESTS}::test_long_file_is_truncated_within_budget"
TRUNCATE_SCAN_TEST = f"{RESOURCES_TESTS}::test_budget_is_never_exceeded"
BUDGET_TEST = f"{CONTEXT_TESTS}::test_resident_budget_exceeded_raises"


def _inject_e62(repo: Repo) -> None:
    """E62 / G50 扩展：指纹漏掉项目说明。

    这是最容易发生的疏漏——写指纹时只想着"系统提示词 + 工具 schema"
    （P1 时代常驻区确实只有这两样），于是 AGENTS.md 被当成
    "每轮重新读进来的普通资源"。

    后果：用户改一次 AGENTS.md，prompt cache 从改动点起全部失效，
    而**没有任何东西会拦**——失效只是变慢变贵。
    """
    repo.patch(
        CONTEXT,
        '            {"system": self._resident_text(), "tools": self._tools_schema},',
        '            # 注入：指纹漏掉项目说明\n'
        '            {"system": self._system_prompt, "tools": self._tools_schema},',
    )


def _inject_e63(repo: Repo) -> None:
    """E63 / G54：从不截断。

    模拟"有人觉得砍用户文件不妥，于是把截断删掉"。
    ``AGENTS.md`` 是唯一一个**长度由用户决定**的常驻项，
    所以这条截断是常驻区里最脆弱的一环。
    """
    repo.patch(
        RESOURCES,
        "    original_tokens = estimate_text(raw)\n"
        "    if original_tokens <= max_tokens:",
        "    original_tokens = estimate_text(raw)\n"
        "    if True:  # 注入：从不截断\n",
    )


def _inject_e64(repo: Repo) -> None:
    """E64 / G59：去掉预算校验。

    去掉之后，"常驻区超限"就再也没有执行点了——而它**不会报错**，
    只会让每一次请求都更贵。与 E24（去掉指纹校验）是同一种形态：
    **断言唯一的价值就是它真的会红**，否则它会被当成死代码删掉。
    """
    repo.patch(
        CONTEXT,
        "        self.verify_resident_region()\n"
        "        self.verify_resident_budget()\n"
        "        now = self._clock()",
        "        self.verify_resident_region()\n"
        "        now = self._clock()  # 注入：去掉预算校验\n",
    )


def main() -> int:
    experiment("G50+", "项目说明也是常驻区（改它必须抛）", FINGERPRINT_TEST, _inject_e62)
    experiment("G54", "AGENTS.md 硬截断（截断后不超上限）", TRUNCATE_TEST, _inject_e63)
    experiment("G54b", "任何上限下都不得超（参数化扫描）", TRUNCATE_SCAN_TEST, _inject_e63)
    experiment("G59", "常驻区预算硬闸（超限必须抛）", BUDGET_TEST, _inject_e64)

    print()
    print("=" * 78)
    print("P2-3 上下文改造门槛注入实验结果")
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
