"""mdrender 渲染器的验收测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdrender import render_html  # noqa: E402


# ---------- 标题与段落 ----------

def test_h1_h2_h3():
    out = render_html("# 标题一\n## 标题二\n### 标题三")
    assert "<h1>标题一</h1>" in out
    assert "<h2>标题二</h2>" in out
    assert "<h3>标题三</h3>" in out


def test_paragraph():
    out = render_html("这是一段话。")
    assert "<p>这是一段话。</p>" in out


def test_paragraph_multiline_merges():
    out = render_html("第一行\n第二行")
    assert "<p>第一行 第二行</p>" in out


def test_paragraph_blank_line_separates():
    out = render_html("段落甲\n\n段落乙")
    assert "<p>段落甲</p>" in out
    assert "<p>段落乙</p>" in out


# ---------- 行内标记 ----------

def test_bold():
    assert "<strong>重要</strong>" in render_html("这是**重要**的")


def test_italic():
    assert "<em>强调</em>" in render_html("这是*强调*的")


def test_bold_and_italic_mixed():
    out = render_html("**粗体** 与 *斜体* 混排")
    assert "<strong>粗体</strong>" in out
    assert "<em>斜体</em>" in out
    assert "<em>" not in out.split("<strong>")[0]  # 粗体之前的文本不能被误解析


def test_inline_code():
    assert "<code>pip install</code>" in render_html("运行 `pip install` 即可")


def test_link():
    out = render_html("见 [文档](https://example.com/a)")
    assert '<a href="https://example.com/a">文档</a>' in out


# ---------- HTML 转义 ----------

def test_escape_ampersand():
    out = render_html("AT&T 与 R&D")
    assert "AT&amp;T" in out
    assert "R&amp;D" in out
    assert "AT&T" not in out.replace("&amp;", "")


def test_escape_angle_brackets():
    out = render_html("泛型 List<T> 说明")
    assert "List&lt;T&gt;" in out


def test_escape_inside_list_item():
    out = render_html("- 选项 A&B\n- 选项 C<D")
    assert "A&amp;B" in out
    assert "C&lt;D" in out


# ---------- 列表与嵌套 ----------

def test_simple_list():
    out = render_html("- 甲\n- 乙\n- 丙")
    assert out.count("<li>") == 3
    assert "<ul><li>甲</li><li>乙</li><li>丙</li></ul>" in out


def test_two_level_nesting():
    out = render_html("- 水果\n  - 苹果\n  - 香蕉\n- 蔬菜")
    assert "<li>水果<ul><li>苹果</li><li>香蕉</li></ul></li>" in out
    assert "<li>蔬菜</li>" in out


def test_three_level_nesting():
    out = render_html("- 一层\n  - 二层\n    - 三层")
    # 三层必须保持树形：三层的内容要嵌在二层的 ul 里，不能和二层同级
    assert "<li>一层<ul><li>二层<ul><li>三层</li></ul></li></ul></li>" in out


def test_list_item_with_inline_marks():
    out = render_html("- **加粗**项\n- `代码`项")
    assert "<li><strong>加粗</strong>项</li>" in out
    assert "<li><code>代码</code>项</li>" in out


# ---------- 综合文档 ----------

def test_full_document():
    doc = "# 计划\n\n先装依赖 `pip install x`，注意 AT&T 的坑。\n\n- 步骤一\n  - 细节 *甲*\n- 步骤二"
    out = render_html(doc)
    assert "<h1>计划</h1>" in out
    assert "<code>pip install x</code>" in out
    assert "AT&amp;T" in out
    assert "<li>步骤一<ul><li>细节 <em>甲</em></li></ul></li>" in out
    assert "<li>步骤二</li>" in out
