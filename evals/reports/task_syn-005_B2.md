# 任务评测：syn-005 —— LRU 缓存：淘汰的是「最久未使用」而不是「最先写入」（档位 B2）

> 生成时间 2026-09-23T12:43:19 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 8 |
| 工具调用 | 8 |
| 工具失败 | 1 |
| prompt token | 28936 |
| completion token | 1479 |
| wall-clock | 16.599s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-005\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": "........                                                                 [100%]\n8 passed in 0.04s"
}
```
