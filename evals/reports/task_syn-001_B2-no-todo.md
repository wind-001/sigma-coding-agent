# 任务评测：syn-001 —— 按显示宽度折行（东亚宽度）（档位 B2-no-todo）

> 生成时间 2026-09-23T17:27:00 ｜ 判定器 `PytestJudge` ｜ 档位 **B2-no-todo**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 9 |
| 工具调用 | 9 |
| 工具失败 | 0 |
| prompt token | 29944 |
| completion token | 1250 |
| wall-clock | 24.628s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-001\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": ".........                                                                [100%]\n9 passed in 0.04s"
}
```
