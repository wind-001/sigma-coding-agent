# 任务评测：syn-012 —— 任务队列的四处缺陷（状态机 / 重试计数 / 出队方向 / id 恢复）（档位 B2-r30）

> 生成时间 2026-09-23T19:31:58 ｜ 判定器 `PytestJudge` ｜ 档位 **B2-r30**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 23 |
| 工具调用 | 31 |
| 工具失败 | 0 |
| prompt token | 183178 |
| completion token | 2953 |
| wall-clock | 66.651s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-012\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 0,
  "summary": ".............                                                            [100%]\n13 passed in 2.47s"
}
```
