# 任务评测：ax120-syn-regression —— [规格综合] 修折扣不破坏退费与批量价（档位 B2）

> 生成时间 2026-10-04T20:20:24 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 6 |
| 工具调用 | 6 |
| 工具失败 | 0 |
| prompt token | 22816 |
| completion token | 642 |
| wall-clock | 7.013s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax120-syn-regression\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 0,
  "summary": "...........                                                              [100%]\n11 passed in 0.05s"
}
```
