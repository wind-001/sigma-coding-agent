# sigma-frontend —— sigma 工作台(只读)

React + TypeScript + Vite 的桌面壳工作台,**数据源 = sigma 的真实本机数据**:
会话(`~/.sigma/sessions/*.jsonl`)、观测时间线(`--timeline` 同源)、内置工具清单、
技能(`extensions/skills/`)、跨会话记忆(`.sigma/memory/`)。与 CLI 共享同一批事实文件,
没有第二份存储。

> 数据通路:浏览器 → **只读桥接服务**(`server/workbench_server.py`,stdlib 零依赖)
> → sigma 既有模块(`session_previews` / `build_timeline` / `discover_skills` /
> `scan_memory` / `default_registry`)。σ 核心零改动。

## 运行

```bash
# 方式一(零 node):直接跑桥接服务,浏览器开 http://127.0.0.1:8301
./.venv/Scripts/python.exe src/sigma-frontend/server/workbench_server.py

# 方式二(开发):桥接 + vite 热更(改前端即时生效)
./.venv/Scripts/python.exe src/sigma-frontend/server/workbench_server.py --port 8301 &
cd src/sigma-frontend && npm ci && npm run dev   # http://localhost:5173
```

可选参数:`--sessions-dir` / `--workspace` / `--dist` / `--host` / `--port`。
`VITE_SIGMA_API_BASE=mock` 可回到纯前端演示数据(localStorage)。

## 功能与真实度对照(以 sigma 当前能力为准)

| 前端功能 | 状态 | 数据来源 |
| --- | --- | --- |
| 会话列表(标题/状态/时间) | ✅ 真实 | `session_previews` + 整树解析;状态由 trace 末轮落点/悬空工具调用推导 |
| 会话详情:动态回放 | ✅ 真实 | 会话 JSONL 逐条映射(message / tool_call / 错误 note) |
| 会话详情:运行时间线 | ✅ 真实 | `build_timeline`(轮次/token/缓存命中率/工具错误/审批/耗时) |
| 插件市场(清单) | ✅ 真实 | 内置工具(`default_registry`)+ 技能扫描 |
| 状态筛选 / 分组 / 搜索(Ctrl+K) | ✅ 真实 | 前端本地过滤(数据面单项目、量级小) |
| 断线容错 | ✅ | 桥接未启动 → 空态 + 常驻提示,界面不停在"加载中" |
| 任务创建 / 发消息 / 状态流转 | ⏳ 待接入 | σ-server M2(执行/审批/SSE)——UI 已标"待接入",发送禁用 |
| 自动化(定时执行) | ⏳ 待接入 | σ-server M3(sigma 目前没有调度器) |
| 插件装卸 | ⏳ 待接入 | σ-server M3;扩展工具(\*.py)装载即执行代码,只读服务刻意不列出 |
| 模型选择 / 推理力度 | ⏳ 装饰 | 只读回放无"下次执行用什么"的语义,控件保留但仅用于展示 |

**写操作的三道边界**(桥接服务 docstring 同款):不执行扩展代码、不写任何文件
(PATCH/DELETE/POST 一律 501 + 指路)、只绑 127.0.0.1。
执行链路(σ-server:FastAPI + SSE + 审批环)的完整设计见
`docs/sigma-backend-api-design.md`——它是**待拍板的 M1/M2/M3**,本工作台不抢它的活。

## 目录

```
src/sigma-frontend/
  server/workbench_server.py   # 只读桥接(stdlib + import sigma;mypy strict 覆盖)
  src/                         # React 应用(契约层 api/ · 状态 store/ · 组件 components/)
  dist/                        # 构建产物(已提交,零 node 即可由桥接直接服务)
  docs/sigma-backend-api-design.md  # σ-server 完整设计(待拍板)
```

修改前端后:`cd src/sigma-frontend && npm run build`(tsc strict + vite)。
