# 任务评测：syn-012 —— 任务队列的四处缺陷（状态机 / 重试计数 / 出队方向 / id 恢复）（档位 B2-r10）

> 生成时间 2026-09-24T00:57:32 ｜ 判定器 `PytestJudge` ｜ 档位 **B2-r10**

## 判定

- **未通过** —— 判定命令 exit 1

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `stopped` |
| 轮数 | 10 |
| 工具调用 | 16 |
| 工具失败 | 3 |
| prompt token | 56079 |
| completion token | 1146 |
| wall-clock | 9.398s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-012\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 1,
  "summary": "        q = TaskQueue.load(Storage(path))\n        fresh = q.add(\"新任务\")\n>       assert fresh.id not in {\"t1\", \"t3\"}\nE       AssertionError: assert 't1' not in {'t1', 't3'}\nE        +  where 't1' = Task(id='t1', title='新任务', priority=0, state='pending', attempts=0).id\ntests\\test_taskq.py:143: AssertionError\n=========================== short test summary info ===========================\nFAILED tests\\test_taskq.py::test_claim_picks_highest_priority - AssertionErro...\nFAILED tests\\test_taskq.py::test_claim_same_priority_fifo_numeric - Assertion...\nFAILED tests\\test_taskq.py::test_save_then_load_roundtrip - AssertionError: a...\nFAILED tests\\test_taskq.py::test_load_then_add_no_id_collision - AssertionErr...\n4 failed, 9 passed in 3.89s"
}
```
