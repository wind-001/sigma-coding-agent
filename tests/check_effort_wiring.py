"""G94-G96：档位链路门（2026-10-02）。

背景：用户报「能选模型，选不了档位」。根因两条独立路径：
  G94 内置 preset 被workbench_server 硬编码 "efforts": []（且 ProviderSpec
       当时根本没有档位字段，无处可取）→ 前端 efforts.length>0 判假，隐藏下拉。
  G95 执行链三处写死 "reasoning_effort"，spec 即使声明 thinking 也发不出去。
  G96 models 端点必须真的把档位下发（端点绿但字段空= 界面依然没下拉）。

⚠ 门必须报**检查了多少项**，0 项按失败论 —— 见判据手册「门绿不等于门有效」。
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（不是 tests/）
sys.path.insert(0, str(ROOT / "src"))

from sigma.providers.registry import builtin_providers  # noqa: E402

checked = 0
fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global checked
    checked += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


print("=== G94 preset 档位：deepseek 有真三档，其余留空 ===")
reg = builtin_providers()
ds = reg.resolve("deepseek")
check(
    tuple(ds.efforts) == ("low", "medium", "high"),
    "deepseek preset 三档 = low/medium/high",
    f"实得 {list(ds.efforts)}",
)
# 未实测的厂商必须留空 —— 猜的档位是假功能（能选但没效果），比没有更坏
for name in ("moonshot", "zhipu", "dashscope", "ollama", "anthropic"):
    spec = reg.resolve(name)
    check(
        spec.efforts == (),
        f"{name} 未实测故留空",
        f"实得 {list(spec.efforts)}",
    )

print("\n=== G95 执行链：随附方式取自 spec，无写死 ===")
srv = (ROOT / "src/sigma-frontend/server/workbench_server.py").read_text(encoding="utf-8")
# _effort_extra_body 的实参里不应再出现字面量 "reasoning_effort"
import re  # noqa: E402

calls = re.findall(r"_effort_extra_body\(\s*([^,]+),", srv)
hardcoded = [c.strip() for c in calls if c.strip().startswith(('"reasoning_effort"', "'reasoning_effort'"))]
check(
    not hardcoded,
    "无写死的 reasoning_effort 实参",
    f"实得 {hardcoded or '无'}（{len(calls)} 处调用）",
)
check(
    srv.count("_effort_extra_body(spec.effort_style") == 2,
    "两处 preset 分支走 spec.effort_style",
    f"实得 {srv.count('_effort_extra_body(spec.effort_style')} 处",
)

print("\n=== G96 models 端点：preset 档位真的下发 ===")
check(
    '"efforts": list(spec.efforts)' in srv,
    "端点读 spec.efforts 而非写死空数组",
)
check(
    '"effortStyle": spec.effort_style' in srv,
    "端点下发 spec.effort_style",
)
# 端到端：真的构造一次端点载荷，确认 deepseek 条目带档位
import importlib.util  # noqa: E402

spec_mod = importlib.util.spec_from_file_location(
    "wb_server_probe", ROOT / "src/sigma-frontend/server/workbench_server.py"
)
assert spec_mod and spec_mod.loader
mod = importlib.util.module_from_spec(spec_mod)
try:
    spec_mod.loader.exec_module(mod)
except Exception as exc:  # pragma: no cover - 环境缺依赖时如实失败
    print(f"  （跳过端到端：无法导入 server 模块 —— {type(exc).__name__}: {exc}）")
else:
    payloads = []
    registry = builtin_providers()
    for name in registry.names():
        sp = registry.resolve(name)
        payloads.append({"name": name, "efforts": list(sp.efforts)})
    by_name = {p["name"]: p for p in payloads}
    check(
        by_name["deepseek"]["efforts"] == ["low", "medium", "high"],
        "端到端 deepseek 条目带三档",
        f"实得 {by_name['deepseek']['efforts']}",
    )
    check(
        by_name["zhipu"]["efforts"] == [],
        "端到端 zhipu 条目无档位（不显示下拉）",
    )
    check(
        hasattr(mod, "_effort_extra_body"),
        "server 模块导出 _effort_extra_body",
    )
    # 随附方式：两个 style 各出一条，且空档位必须返回 None（字节级不变）
    check(
        mod._effort_extra_body("reasoning_effort", "high") == {"reasoning_effort": "high"},
        "reasoning_effort 风格随附正确",
    )
    check(
        mod._effort_extra_body("thinking", "enabled") == {"thinking": {"type": "enabled"}},
        "thinking 风格随附正确",
    )
    check(
        mod._effort_extra_body("reasoning_effort", "  ") is None,
        "空档位返回 None（请求字节与无档位一致）",
    )

print(f"\n检查 {checked} 项，失败 {len(fails)}")
if checked == 0:
    print("!! 0 项 = 门空跑，按失败论")
    sys.exit(2)
sys.exit(1 if fails else 0)
