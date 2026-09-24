# 任务评测：syn-011 —— Markdown 渲染器的三处跨模块缺陷（inline / blocks / renderer）（档位 B2-r20）

> 生成时间 2026-09-23T19:30:41 ｜ 判定器 `PytestJudge` ｜ 档位 **B2-r20**

## 判定

- **未通过** —— 判定命令 exit 1

## 指标（口径与 runner.py 对齐）

| 指标 | 值 |
| --- | --- |
| status | `stopped` |
| 轮数 | 20 |
| 工具调用 | 26 |
| 工具失败 | 0 |
| prompt token | 156082 |
| completion token | 3012 |
| wall-clock | 88.115s |

## 证据（为什么这么判）

```json
{
  "command": "python -m pytest tests/ -q",
  "tests_restored_from": "C:\\Users\\刘康鑫\\Desktop\\sigma\\evals\\datasets\\synthetic\\syn-011\\judge_tests",
  "tests_restored_to": "tests",
  "tests_were_modified": false,
  "exit_code": 1,
  "summary": "        doc = \"# 计划\\n\\n先装依赖 `pip install x`，注意 AT&T 的坑。\\n\\n- 步骤一\\n  - 细节 *甲*\\n- 步骤二\"\n        out = render_html(doc)\n        assert \"<h1>计划</h1>\" in out\n        assert \"<code>pip install x</code>\" in out\n>       assert \"AT&amp;T\" in out\nE       AssertionError: assert 'AT&amp;T' in '<h1>计划</h1>\\n<p>先装依赖 <code>pip install x</code>，注意 AT&T 的坑。</p>\\n<ul><li>步骤一<ul><li>细节 <em>甲</em></li></ul></li><li>步骤二</li></ul>'\ntests\\test_mdrender.py:115: AssertionError\n=========================== short test summary info ===========================\nFAILED tests\\test_mdrender.py::test_escape_ampersand - AssertionError: assert...\nFAILED tests\\test_mdrender.py::test_escape_inside_list_item - AssertionError:...\nFAILED tests\\test_mdrender.py::test_full_document - AssertionError: assert 'A...\n3 failed, 14 passed in 0.51s"
}
```
