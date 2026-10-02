"""G97：首页联网开关链路（2026-10-02）。

起因：用户问「为什么新任务刚开始没显示联网开关按钮」。根因两层：
  ① 前端：联网按钮只加在 TaskDetail（会话页），首页 Composer 完全没有 ——
     而首页才是**建任务的地方**，用户在那里无法表达意图。
  ② 后端：createTask 的 record **不写 web 字段**，而 _draft_payload 会读它
     ⇒ 即使前端传了也被"缺字段=False"吃掉 ⇒「开关能点、开了没效果」假功能。

⚠ 本门刻意覆盖**建任务路径**而不只是热切路径：
   上一批的门（test_web_set_task_web_roundtrip 等）全走_set_task_web，
   也就是**建完任务之后**改 —— 恰好漏掉了「首页建任务时能不能带上」这一环。
   同族教训：门覆盖了同模块的另一条路径，不等于覆盖了本路径。

⚠ 本门第一版在**前端断言**上栽了一次：断言 `"Globe" in composer`，
   而 import 行里就有 Globe —— 注入删掉整块 JSX 后该断言照样绿，
   门「红在后端、前端假绿」。修正见 G97a：改查**出现次数**（import + 渲染 = 2）。
"""

import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SERVER_PATH = ROOT / "src/sigma-frontend/server/workbench_server.py"
FRONTEND = ROOT / "src/sigma-frontend/src"

checked = 0
fails: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    global checked
    checked += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail else ''}")
    if not ok:
        fails.append(label)


spec = importlib.util.spec_from_file_location("wb_g97", SERVER_PATH)
assert spec and spec.loader
srv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(srv)
srv_src = SERVER_PATH.read_text(encoding="utf-8")


print("=== G97a 前端：首页 Composer 必须有联网按钮（原来只有会话页有） ===")
composer = (FRONTEND / "components/main/Composer.tsx").read_text(encoding="utf-8")
task_detail = (FRONTEND / "components/main/TaskDetail.tsx").read_text(encoding="utf-8")
# ⚠ **不能只查 "Globe" in composer**：import 行里就有 Globe，
#   删掉整块 JSX 后 import 仍在 → 字符串存在性检查照样绿。
#   本门第一版就栽在这：注入删了 JSX，四项全绿。
#   → 真正能判别的是**出现次数**：import 1 次 + 真正渲染 1 次 = 2。
#   （一般形式：断言「能力存在」时，要挑那个**只有真正实现才满足**的量。）
check(composer.count("Globe") >= 2,
      "Composer 既 import 了 Globe 也真的渲染了 <Globe>",
      f"实得 {composer.count('Globe')} 次（1=只有 import，按钮没渲染）")
check("<Globe" in composer, "存在真正的 <Globe ... /> 渲染（不是只有 import）")
check("WEB_OPTIONS" in composer, "Composer 引用了 WEB_OPTIONS")
# 按钮必须在 spacer 之前（spacer 之后是右对齐的模型/档位区）
spacer_at = composer.find('className="composer__spacer"')
web_at = composer.find("composer__web-trigger")
check(web_at != -1 and spacer_at != -1 and web_at < spacer_at,
      "联网按钮在 spacer 左侧（与权限按钮同排）",
      f"web@{web_at} spacer@{spacer_at}")
check("composerWeb" in composer, "Composer 读/写 composerWeb 暂存态")
check("Globe" in task_detail, "TaskDetail 仍有（对照，防我误删）")

print("\n=== G97b 前端：composerWeb 必须进 createTask 载荷 ===")
store = (FRONTEND / "store/appStore.tsx").read_text(encoding="utf-8")
create_blocks = [
ln
 for ln in store.split("\n")
 if "apiClient.createTask({" in ln
]
check(len(create_blocks) >= 1, f"找得到 createTask 调用（{len(create_blocks)} 处）")
# 两处 createTask 都要带 web
check(store.count("web: s.composerWeb") == len(create_blocks),
       "每一处 createTask 都带 web: s.composerWeb",
       f"实得 {store.count('web: s.composerWeb')} / {len(create_blocks)} 处")
client = (FRONTEND / "api/client.ts").read_text(encoding="utf-8")
check("web?: boolean" in client, "CreateTaskInput 声明了 web?: boolean")
# mock 客户端必须同口径，否则 mock 模式与真后端分叉
mock = (FRONTEND / "api/mockClient.ts").read_text(encoding="utf-8")
check("web: input.web ?? false" in mock,
      "mockClient 用 input.web（不写死 false —— 否则两客户端行为分叉）")

print("\n=== G97c 后端：createTask 必须真的落 web 字段 ===")
check('"web": bool(body.get("web") or False)' in srv_src,
      "createTask 落 web 字段（原缺陷：只读不回写）")
# 端到端：真的调创建函数，确认 web=true 能落进 record
# _create_task 的签名是 (body) -> dict（**不是** (code, payload) 元组）
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    srv._MODELS_REGISTRY_PATH = pathlib.Path(td) / "models.json"
    payload = srv._create_task({"title": "T", "description": "d",
                                "access": "full", "model": "m", "web": True})
    check(isinstance(payload, dict) and payload.get("id") is not None,
          "创建成功并回 id", f"id={payload.get('id')!r}")
    check(payload.get("web") is True,
          "web=true 回读到 payload", f"实得 {payload.get('web')!r}")
    # record 内部也要真的落住（payload 只读不回写是原缺陷）
    rid = payload["id"]
    check(srv._TASKS[rid].get("web") is True,
          "record 里确实落了 web=true",
          f"实得 {srv._TASKS[rid].get('web')!r}")
    srv._TASKS.pop(rid, None)

print("\n=== G97d 后端：缺字段仍默认关（不能因为补了写入就变成默认开） ===")
with tempfile.TemporaryDirectory() as td:
    srv._MODELS_REGISTRY_PATH = pathlib.Path(td) / "models.json"
    payload2 = srv._create_task({"title": "T2", "description": "d",
                                 "access": "full", "model": "m"})
    check(payload2.get("web") is False,
          "不传 web 时默认 False（默认关是拍板口径）",
          f"实得 {payload2.get('web')!r}")
    srv._TASKS.pop(payload2["id"], None)

print(f"\n检查 {checked} 项，失败 {len(fails)}")
if checked == 0:
    print("!! 0 项 = 门空跑，按失败论")
    sys.exit(2)
sys.exit(1 if fails else 0)
