# 任务评测：ax116-rule-stdlib —— [指令遵循与边界] 补全选择器且仅标准库（档位 B0）

> 生成时间 2026-10-04T20:20:04 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 436 |
| completion token | 293 |
| wall-clock | 1.236s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax116-rule-stdlib\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 0,
  "summary": ".......                                                                  [100%]\n7 passed in 0.03s"
}
```
