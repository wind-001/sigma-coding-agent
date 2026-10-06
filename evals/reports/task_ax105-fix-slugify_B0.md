# 任务评测：ax105-fix-slugify —— [缺陷修复] slugify 分隔符折叠（档位 B0）

> 生成时间 2026-10-04T20:12:22 ｜ 判定器 `PytestJudge` ｜ 档位 **B0**

## 判定

- **未通过** —— 判定命令 exit 1

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `completed` |
| 轮数 | 1 |
| 工具调用 | 0 |
| 工具失败 | 0 |
| prompt token | 277 |
| completion token | 51 |
| wall-clock | 1.05s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\ax105-fix-slugify\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 1,
  "summary": "..F...                                                                   [100%]\n================================== FAILURES ===================================\n________________________________ test_cjk_kept ________________________________\n    def test_cjk_kept() -> None:\n>       assert slugify('  中文 标题! ') == '中文-标题'\nE       AssertionError: assert '' == '中文-标题'\nE         \nE         - 中文-标题\ntests\\test_all.py:13: AssertionError\n=========================== short test summary info ===========================\nFAILED tests/test_all.py::test_cjk_kept - AssertionError: assert '' == '中文-...\n1 failed, 5 passed in 0.12s"
}
```
