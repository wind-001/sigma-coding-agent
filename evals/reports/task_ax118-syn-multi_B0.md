# 任务评测：ax118-syn-multi —— [规格综合] 跨模块接口错位（档位 B0）

> 生成时间 2026-10-04T20:13:16 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **未通过** —— 判定命令 exit 1

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `error` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 0 |
| completion token | 0 |
| wall-clock | 0.002s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax118-syn-multi\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 1,
  "summary": "    def test_store_has_name_and_username() -> None:\n        from store import get_user\n        user = get_user('u1')\n>       assert user['name'] == 'alice' and user['username'] == 'alice'\n               ^^^^^^^^^^^^\nE       KeyError: 'name'\ntests\\test_all.py:20: KeyError\n=========================== short test summary info ===========================\nFAILED tests/test_all.py::test_describe_u1 - KeyError: 'name'\nFAILED tests/test_all.py::test_describe_u2 - KeyError: 'name'\nFAILED tests/test_all.py::test_store_has_name_and_username - KeyError: 'name'\n3 failed, 3 passed in 0.18s"
}
```
