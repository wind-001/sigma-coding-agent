# 任务评测：syn-012 —— 任务队列的四处缺陷（状态机 / 重试计数 / 出队方向 / id 恢复）（档位 B2-r20）

> 生成时间 2026-09-23T19:31:33 ｜ 判定器 `PytestJudge` ｜ 档位 **B2-r20**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `stopped` |
| 轮数 | 20 |
| 工具调用 | 29 |
| 工具失败 | 0 |
| prompt token | 150133 |
| completion token | 2583 |
| wall-clock | 41.508s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-012\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": ".............                                                            [100%]\n13 passed in 2.74s"
}
```
