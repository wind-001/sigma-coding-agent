# 任务评测：ax112-orch-topo —— [多步编排] 依赖拓扑构建顺序（档位 B2）

> 生成时间 2026-10-04T20:12:58 ｜ 判定器 `PytestJudge` ｜ 档位 **B2**

## 判定

- **通过** —— 判定命令 exit 0

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 6 |
| 工具调用 | 6 |
| 工具失败 | 1 |
| prompt token | 21015 |
| completion token | 1204 |
| wall-clock | 9.518s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax112-orch-topo\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 0,
  "summary": "..                                                                       [100%]\n2 passed in 0.02s"
}
```
