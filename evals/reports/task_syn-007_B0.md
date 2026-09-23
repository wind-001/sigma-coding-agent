# 任务评测：syn-007 —— 模板渲染：占位符互相独立 + 缺失 key 要炸出来（档位 B0）

> 生成时间 2026-09-23T12:38:00 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 563 |
| completion token | 236 |
| wall-clock | 1.22s |

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
