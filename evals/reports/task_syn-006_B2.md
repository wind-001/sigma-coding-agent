# 任务评测：syn-006 —— 闰年漏了整百年规则（1900 不是闰年）（档位 B2）

> 生成时间 2026-09-23T12:43:35 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 8 |
| 工具调用 | 8 |
| 工具失败 | 1 |
| prompt token | 24623 |
| completion token | 767 |
| wall-clock | 12.752s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-006\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": ".........                                                                [100%]\n9 passed in 0.03s"
}
```
