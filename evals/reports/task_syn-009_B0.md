# 任务评测：syn-009 —— 保序去重：casefold 合并重复但要保留首现原样（档位 B0）

> 生成时间 2026-09-23T12:38:08 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 479 |
| completion token | 152 |
| wall-clock | 1.066s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-009\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": ".........                                                                [100%]\n9 passed in 0.04s"
}
```
