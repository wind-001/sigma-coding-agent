# 任务评测：syn-011 —— Markdown 渲染器的三处跨模块缺陷（inline / blocks / renderer）（档位 B2-r10）

> 生成时间 2026-09-24T00:57:14 ｜ 判定器 `PytestJudge` ｜ 档位 **B2-r10**

## 判定

- **未通过** —— 判定命令 exit 1

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `stopped` |
| 轮数 | 10 |
| 工具调用 | 13 |
| 工具失败 | 0 |
| prompt token | 54896 |
| completion token | 872 |
| wall-clock | 27.677s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-011\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 1,
  "summary": ">       assert \"AT&amp;T\" in out\nE       AssertionError: assert 'AT&amp;T' in '<h1>计划</h1>\\n<p>先装依赖 <code>pip install x</code>，注意 AT&T 的坑。</p>\\n<ul><li>步骤一<ul><li>细节 <em>甲</em></li></ul></li><li>步骤二</li></ul>'\ntests\\test_mdrender.py:115: AssertionError\n=========================== short test summary info ===========================\nFAILED tests\\test_mdrender.py::test_bold - AssertionError: assert '<strong>重...\nFAILED tests\\test_mdrender.py::test_bold_and_italic_mixed - AssertionError: a...\nFAILED tests\\test_mdrender.py::test_escape_ampersand - AssertionError: assert...\nFAILED tests\\test_mdrender.py::test_escape_inside_list_item - AssertionError:...\nFAILED tests\\test_mdrender.py::test_three_level_nesting - AssertionError: ass...\nFAILED tests\\test_mdrender.py::test_list_item_with_inline_marks - AssertionEr...\nFAILED tests\\test_mdrender.py::test_full_document - AssertionError: assert 'A...\n7 failed, 10 passed in 0.31s"
}
```
