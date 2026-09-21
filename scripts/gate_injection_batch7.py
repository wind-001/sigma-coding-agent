"""批次 7 门槛注入实验：逐条证伪 G37–G41。

沿用批次 2–4 的框架（在真实仓库上改、跑、finally 还原），**不另写一份 Repo**——
验证工具自身会腐化，两份实现意味着修一个忘另一个。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E35 | G37 额度判定在发请求之前 | 判定恒为真（不拦） | 已用尽仍发出 /search 请求 |
| E36 | G38 记账口径按 credits | advanced 也记 1 | 额度只扣 1，口径错 |
| E37 | G39 校准与预留原子 | 去掉 asyncio.Lock | 并发下 /usage 被校准两次 |
| E38 | G40 无 key 不注册 | 无条件注册 web_search | 默认注册表里多出工具 |
| E39 | G41 错误不穿透 | except 里直接 raise | 失败变成异常而不是 is_error 结果 |

用法
    ./.venv/Scripts/python.exe scripts/gate_injection_batch7.py

本机注意：若 pytest 的 tmp_path 在当前沙箱下不可用（WinError 5），
基线就会是红的——实验会如实报"基线不是绿的，实验无效"，
**不会**把这种情况当成"门槛通过"。CI 上没有这个问题。
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

WEB_SEARCH = "core/sigma_tools/web_search.py"
QUOTA = "core/sigma_tools/_tavily_quota.py"
SDK = "core/sigma/sdk.py"

WS_TESTS = "tests/test_tools_web_search.py"


def _inject_e35(repo: Repo) -> None:
    """E35 / G37：忽略额度判定。

    这是"先花钱再发现没钱"的形态——闸门形同不存在，
    而模型与用户都不会看到任何异常，只是账一直在涨。
    """
    repo.patch(
        WEB_SEARCH,
        "        if not decision.allowed:\n            return self._refusal(params, decision)",
        "        if False:  # 注入：忽略额度判定\n"
        "            return self._refusal(params, decision)",
    )


def _inject_e36(repo: Repo) -> None:
    """E36 / G38：把口径从 credits 改回"次数"。

    症状是"额度明明没到上限却用完了"——而日志上一切正常。
    """
    repo.patch(
        WEB_SEARCH,
        '        return ADVANCED_COST if self.search_depth == "advanced" else DEFAULT_COST',
        "        return DEFAULT_COST  # 注入：advanced 也按 1 credit 记",
    )


def _inject_e37(repo: Repo) -> None:
    """E37 / G39：去掉锁。

    reserve 里的"校准 + 判定 + 预留"跨了 await，没有锁就不再原子：
    两个并发调用会在同一份过期快照上各校准一次。
    """
    repo.patch(
        QUOTA,
        "        async with self._lock:\n            state = self._load()\n            synced = False",
        "        if True:  # 注入：去掉锁\n            state = self._load()\n            synced = False",
    )


def _inject_e38(repo: Repo) -> None:
    """E38 / G40：无条件注册 web_search。

    这一条的代价是**常驻成本**：没配 key 的机器白付约 150 token 的工具 schema，
    而且系统提示词会告诉模型有一个它其实调不动的工具。
    """
    repo.patch(SDK, "    if web_search:", "    if True:  # 注入：无条件注册")
    repo.patch(
        SDK,
        "        if not tavily_api_key:",
        "        if False:  # 注入：没有 key 也注册",
    )


def _inject_e39(repo: Repo) -> None:
    """E39 / G41：让错误穿透。

    工具抛异常等于把纠错能力关掉（架构 4.3 节第 3 点）：
    模型看不到失败原因，只能重试同一个调用。
    """
    repo.patch(
        WEB_SEARCH,
        "        except SearchFailure as exc:\n            if exc.quota_exhausted:",
        "        except SearchFailure as exc:\n"
        "            raise  # 注入：错误直接穿透\n"
        "            if exc.quota_exhausted:",
    )


def main() -> int:
    experiment("G37", "额度判定发生在发 /search 之前", WS_TESTS, _inject_e35)
    experiment("G38", "记账口径按 credits（advanced=2）", WS_TESTS, _inject_e36)
    experiment("G39", "并发下校准 + 判定 + 预留原子", WS_TESTS, _inject_e37)
    experiment("G40", "没有 key 就不注册 web_search", WS_TESTS, _inject_e38)
    experiment("G41", "失败转成 is_error 结果而不抛异常", WS_TESTS, _inject_e39)

    print()
    print("=" * 78)
    ok = True
    for gate, what, passed, detail in RESULTS:
        flag = "PASS" if passed else "FAIL"
        ok = ok and passed
        print(f"[{flag}] {gate}  {what}")
        if not passed:
            print(f"        {detail}")
    proven = sum(1 for row in RESULTS if row[2])
    print("=" * 78)
    print(f"共 {len(RESULTS)} 条注入实验，{proven} 条被成功证伪")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
