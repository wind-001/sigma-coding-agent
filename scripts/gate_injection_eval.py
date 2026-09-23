"""EvalProfile 门槛注入实验：逐条证伪消融开关的门槛（G80–G81）。

沿用既有框架（在真实仓库上改、跑、finally 还原），不另写 Repo。

| 编号 | 门槛 | 注入 | 注入后应该红在哪 |
| --- | --- | --- | --- |
| E68 | G80 显式关断压过默认替换 | sdk 的关断分支改成 ``if False:`` | "compaction_policy is None"红 |
| E69 | G81 档位声明不是名义开关 | ``EvalProfile.b1`` 返回全开档 | b1 的 flags 断言红 |

**为什么 G80 的注入"确定性会红"**

    被注入的测试同时给了"必然触发"的策略（窗口 2000、ratio 0）并断言
    ``session.compaction_policy is None``。注入后 ``enable_compaction=False``
    被忽略 → 走 ``elif compaction_policy is not None`` 分支 → 策略被保留 →
    断言立即失败。**不依赖文件系统顺序、不依赖路径长度、不依赖环境。**

**为什么需要 G81**

    档位声明（``EvalProfile``）最大的风险是变成**名义开关**：名字叫 B1、
    字段全开——报告里每一行都写着 B1，实际跑的是 B2。那条红很便宜，
    但它钉住的是"名字必须等于配置"这件事本身。

用法
    export PYTHONPATH=scripts
    ./.venv/Scripts/python.exe scripts/gate_injection_eval.py
"""

from __future__ import annotations

import sys

from gate_injection_batch24 import RESULTS, Repo, experiment

SDK = "core/sigma/sdk.py"
PROFILE = "core/sigma/eval_profile.py"

OFF_TEST = "tests/test_eval_profile.py::test_interactive_session_explicit_compaction_off"
B1_TEST = "tests/test_eval_profile.py::test_b1_disables_every_intervention"


def _inject_e68(repo: Repo) -> None:
    """E68 / G80：显式关断被忽略。

    后果：B1 档"名存实亡"——评测报告里写着 B1，实际跑的是带压缩（或带
    调用方策略）的配置。对照实验从此测的不是"无干预"，而是"另一种干预"，
    **而报告上完全看不出来**。这类缺陷不会崩、不会报错，只会让结论作废。
    """
    repo.patch(
        SDK,
        "        if not enable_compaction:\n            self._compaction_policy = None",
        "        if False:  # 注入：显式关断被忽略\n"
        "            self._compaction_policy = None",
    )


def _inject_e69(repo: Repo) -> None:
    """E69 / G81：档位声明与配置脱钩。

    ``b1()`` 名义上关掉干预、实际全开——这就是"名义开关"的形状。
    """
    repo.patch(
        PROFILE,
        '        return cls(name="B1", compaction=False, checkpoint=False)',
        '        return cls(name="B1")  # 注入：名字叫 B1，配置是全开',
    )


def main() -> int:
    experiment("G80", "显式关断压过默认替换与显式策略", OFF_TEST, _inject_e68)
    experiment("G81", "档位声明不是名义开关（b1 必须真关）", B1_TEST, _inject_e69)

    print()
    print("=" * 78)
    print("EvalProfile 门槛注入实验结果")
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
