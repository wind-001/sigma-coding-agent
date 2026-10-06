# 任务评测：ax101-impl-roman —— [从零实现] 罗马数字互转（档位 B0）

> 生成时间 2026-10-04T20:12:05 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **未通过** —— 判定命令 exit 2

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `error` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 0 |
| completion token | 0 |
| wall-clock | 0.001s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax101-impl-roman\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 2,
  "summary": "Hint: make sure your test modules/packages have valid Python names.\nTraceback:\nD:\\Myapp\\anaconda\\Lib\\importlib\\__init__.py:88: in import_module\n    return _bootstrap._gcd_import(name[level:], package, level)\n           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\ntests\\test_all.py:3: in <module>\n    from roman import from_roman, to_roman\nE   ModuleNotFoundError: No module named 'roman'\n=========================== short test summary info ===========================\nERROR tests/test_all.py\n!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n1 error in 0.38s"
}
```
