# 任务评测：syn-007 —— 模板渲染：占位符互相独立 + 缺失 key 要炸出来（档位 B1）

> 生成时间 2026-09-23T12:40:30 ｜ 判定器 `PytestJudge` ｜ 档位 **B1**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 7 |
| 工具调用 | 7 |
| 工具失败 | 1 |
| prompt token | 21908 |
| completion token | 1067 |
| wall-clock | 13.456s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-007\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": "..........                                                               [100%]\n10 passed in 0.04s"
}
```
