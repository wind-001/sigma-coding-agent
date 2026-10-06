# 任务评测：ax117-syn-spec —— [规格综合] 按 SPEC 生成报告（档位 B2）

> 生成时间 2026-10-04T20:13:21 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 7 |
| 工具调用 | 7 |
| 工具失败 | 1 |
| prompt token | 24583 |
| completion token | 797 |
| wall-clock | 8.224s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax117-syn-spec\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 0,
  "summary": "..                                                                       [100%]\n2 passed in 0.02s"
}
```
