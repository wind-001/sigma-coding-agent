# 任务评测：ax112-orch-topo —— [多步编排] 依赖拓扑构建顺序（档位 B0）

> 生成时间 2026-10-04T20:12:47 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

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
| wall-clock | 0.001s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax112-orch-topo\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 1,
  "summary": "        the built-in open() function does.\n        \"\"\"\n        if \"b\" not in mode:\n            encoding = io.text_encoding(encoding)\n>       return io.open(self, mode, buffering, encoding, errors, newline)\n               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\nE       FileNotFoundError: [Errno 2] No such file or directory: 'C:\\\\Users\\\\刘康鑫\\\\AppData\\\\Local\\\\Temp\\\\sigma-eval-3hvp68gg\\\\workspace\\\\build_order.txt'\nD:\\Myapp\\anaconda\\Lib\\pathlib\\_local.py:537: FileNotFoundError\n=========================== short test summary info ===========================\nFAILED tests/test_all.py::test_exact_deterministic_order - FileNotFoundError:...\nFAILED tests/test_all.py::test_all_modules_present - FileNotFoundError: [Errn...\n2 failed in 0.22s"
}
```
