# 任务评测：ax118-syn-multi —— [规格综合] 跨模块接口错位（档位 B2）

> 生成时间 2026-10-04T20:13:25 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 5 |
| 工具调用 | 7 |
| 工具失败 | 0 |
| prompt token | 17873 |
| completion token | 467 |
| wall-clock | 6.943s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax118-syn-multi\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 0,
  "summary": "......                                                                   [100%]\n6 passed in 0.07s"
}
```
