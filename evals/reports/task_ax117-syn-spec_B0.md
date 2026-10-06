# 任务评测：ax117-syn-spec —— [规格综合] 按 SPEC 生成报告（档位 B0）

> 生成时间 2026-10-04T20:13:14 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **未通过** —— 判定命令 exit 1

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 232 |
| completion token | 722 |
| wall-clock | 2.609s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax117-syn-spec\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": true,
  "exit_code": 1,
  "summary": "        Open the file pointed to by this path and return a file object, as\n        the built-in open() function does.\n        \"\"\"\n        if \"b\" not in mode:\n            encoding = io.text_encoding(encoding)\n>       return io.open(self, mode, buffering, encoding, errors, newline)\n               ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\nE       FileNotFoundError: [Errno 2] No such file or directory: 'C:\\\\Users\\\\刘康鑫\\\\AppData\\\\Local\\\\Temp\\\\sigma-eval-fgtgcsuz\\\\workspace\\\\report.json'\nD:\\Myapp\\anaconda\\Lib\\pathlib\\_local.py:537: FileNotFoundError\n=========================== short test summary info ===========================\nFAILED tests/test_all.py::test_report_exact - FileNotFoundError: [Errno 2] No...\n1 failed, 1 passed in 0.20s"
}
```
