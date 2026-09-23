# 任务评测：syn-008 —— 通配符匹配：* 是「零个或多个」不是「一个或多个」（档位 B0）

> 生成时间 2026-09-23T12:38:04 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 483 |
| completion token | 191 |
| wall-clock | 1.086s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-008\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": ".............                                                            [100%]\n13 passed in 0.04s"
}
```
