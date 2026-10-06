# 任务评测：ax110-orch-organize —— [多步编排] 按扩展名归档目录（档位 B2）

> 生成时间 2026-10-04T20:13:02 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 12 |
| 工具调用 | 13 |
| 工具失败 | 3 |
| prompt token | 68575 |
| completion token | 2250 |
| wall-clock | 19.919s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax110-orch-organize\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 0,
  "summary": "....                                                                     [100%]\n4 passed in 0.06s"
}
```
