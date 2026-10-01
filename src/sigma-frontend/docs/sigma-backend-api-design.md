# sigma 后端设计方案(σ-server)

> 为 `sigma-frontend`(React 前端,已就绪)提供任务/项目管理 REST + 事件流服务。
> 设计原则:**零侵入 sigma 核心** —— 只新增独立服务包,全部通过 sigma 既有扩展点接入。
> 前端接口契约已固定:见 `sigma-frontend/src/api/client.ts`(SigmaApiClient),HTTP 实现已预留在 `src/api/httpClient.ts`,配置 `VITE_SIGMA_API_BASE` 即切换。
> 状态:详规待星辰拍板(文末 5 个决策点)。按工作流约定,拍板后才进入实现。

## 1. 背景与目标

- 前端已完成真实项目管理交互(任务 CRUD、状态机、搜索、自动化、插件市场),当前数据走浏览器 localStorage(mock)。
- 目标:σ-server 把这些操作映射到 sigma 的真实执行内核(InteractiveSession / HookManager / SessionManager),让前端创建的任务真正由 sigma 跑起来。
- 非目标:多用户/公网部署、团队协作、移动端;本机单用户优先。

## 2. 总体架构

```
浏览器 sigma-frontend (React)
   │  REST(控制)+ SSE(事件流)
   ▼
σ-server(FastAPI + uvicorn,新包,与 sigma 同 venv)
   │  service 层:TaskService(任务生命周期/状态机)
   │  adapter 层:SigmaRunAdapter(把 sigma 的会话/事件映射为前端事件协议)
   ▼
sigma SDK(不改核心,只用扩展点)
   ├─ InteractiveSession / SessionManager   ← 任务执行、会话持久化、断点续跑(批次5/6)
   ├─ HookManager 决策型 ApprovalHook       ← HttpApprovalHook:审批请求经 SSE 推前端(P3-批次2 接口)
   ├─ HookManager TraceHook                 ← 观测数据复用(P5-批次1,--timeline 同源)
   ├─ InterruptToken / TurnCancelled        ← 打断(P3-批次2)
   └─ .sigma/session/*.jsonl + shadow.git   ← 对话事实源与回滚(P4-批次7/8)
```

关键映射:**一个前端 Task = 一个 sigma 会话(session)**。对话内容不二次存储——σ-server 只存任务元数据,回放直接解析 sigma 会话 JSONL。

## 3. 技术选型(含理由)

| 项 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI + uvicorn | 异步原生;pydantic v2 模型与 sigma 既有 BaseModel 约定一致(AGENTS.md #3);自带 OpenAPI 文档 |
| 事件流 | **SSE**(`GET /tasks/{id}/events`) | 单向推送足够(控制走 POST);浏览器原生 EventSource;`Last-Event-ID` 天然支持断线续传。WebSocket 列为 M3 备选(若要做前端→后端的实时 steering) |
| 任务元数据 | SQLite(stdlib sqlite3,单文件 `.sigma/server/tasks.db`) | 零运维、事务安全;对话不进库 |
| 审批持久化 | 复用 `.sigma/allowlist.json` | 与 CliApprovalGate 同一份 allowlist,前端和 CLI 行为一致 |
| 打包 | sigma 仓库内新增 `server/` 目录(包名 `sigma_server`) | 同 venv 直接 import sigma;`python -m sigma_server` 启动 |

## 4. 任务-会话映射与状态机

```
draft ──开始──▶ running ⇄ waiting_approval ──完成──▶ completed
                   │  ▲              │超时/拒绝
                   ▼  │重启           ▼
               cancelled(InterruptToken)          failed(错误/上下文溢出恢复失败)
```

- 前端「开始任务」= 用任务里保存的 access/model/effort 构造 session 配置并启动首轮 run。
- `running` 期间前端可发:`POST /tasks/{id}/interrupt`(映射 InterruptToken,对应 CLI 的 `!!`/`/stop`)、`POST /tasks/{id}/steer`(`!text` 语义,steering_drain 注入)。
- `waiting_approval` 由 HttpApprovalHook 进入,见 §6。
- failed 分支沿用 sigma 的 context_overflow 恢复(P0):恢复成功回 running,恢复失败才落 failed。

## 5. REST 端点表(与 SigmaApiClient 一一对应)

前缀 `/api/v1`。✅ = 前端 httpClient.ts 已实现,后端按表落位即可。

| 方法 | 路径 | 用途 | 前端方法 |
|---|---|---|---|
| GET | /system/ping | 健康检查 `{ok, mode:'http', version}` | ping ✅ |
| GET | /projects | 项目列表(sigma 工作区) | listProjects ✅ |
| POST | /projects | 注册工作区 `{name, repoPath?, branch?}` | createProject ✅ |
| GET | /tasks?project=&status=&q= | 任务列表(过滤) | listTasks ✅ |
| POST | /tasks | 创建任务(= 建会话,状态 draft) | createTask ✅ |
| GET | /tasks/{id} | 任务详情 | getTask ✅ |
| PATCH | /tasks/{id} | 改标题/描述/状态 | updateTask ✅ |
| DELETE | /tasks/{id} | 删除任务(联动:仅停引用,不删 sigma 会话档案) | deleteTask ✅ |
| POST | /tasks/{id}/start | 开始执行(draft→running,发起首轮) | — M2 |
| POST | /tasks/{id}/messages | 追加用户消息并推进一轮 | — M2 |
| POST | /tasks/{id}/interrupt | 打断(InterruptToken) | — M2 |
| POST | /tasks/{id}/steer | `!text` 注入指导 | — M2 |
| POST | /tasks/{id}/approvals/{req} | 审批决策 `{decision:'approve'\|'deny', persist?:boolean}` | — M2 |
| GET | /tasks/{id}/messages | 会话回放(解析 sigma JSONL → 渲染视图) | — M2 |
| GET | /tasks/{id}/events | **SSE 事件流** | — M2 |
| GET | /tasks/{id}/timeline | 时间线(trace 数据,复用 --timeline 渲染逻辑) | — M3 |
| GET/PATCH | /automations[/{id}] | 自动化列表/启停/新建 | 已实现 ✅(M3 接真调度) |
| GET/PATCH | /plugins[/{id}] | 插件列表/装卸 | 已实现 ✅(M3 接 skills 目录) |
| GET | /models | 可用模型与推理力度档 | listModels ✅ |

核心请求/响应示例:

```jsonc
// POST /api/v1/tasks
{ "projectId": "proj-sigma", "title": "优化git影子库", "description": "…",
  "access": "full", "model": "GLM-5.3-Flash", "effort": "最高" }
// 201 →
{ "id": "task-1001", "projectId": "proj-sigma", "status": "draft",
  "createdAt": "2026-09-28T21:03:00+08:00", "events": [ … ], … }
```

## 6. 关键机制对接(全部走既有扩展点,不改核心)

### 6.1 审批环(最关键)
`HttpApprovalHook implements 决策型 ApprovalHook`(P3-批次2 定义的接口,HookManager 直接纳):

1. 拦截到危险操作 → 构造 `approval_request` 事件帧(request_id、工具、参数摘要、危险等级)推 SSE;任务状态 → waiting_approval;**asyncio.Future 挂起**。
2. 前端弹审批卡 → `POST /tasks/{id}/approvals/{request_id}` `{decision, persist?}` → resolve Future。
3. `persist=true` 写 `.sigma/allowlist.json`(与 CLI /allowlist 同一份)。
4. 超时(默认 120s,可配)自动 deny,事件帧带 `reason: "timeout"`。
5. `access: readonly` 的任务 = 挂载只读 Hook 集合(等价 CLI --no-approval 的反面,全部 deny 写类)。

### 6.2 事件帧协议(SSE)

```
event: agent_event
id: <seq>                      // 断线重连:Last-Event-ID 之后续传(内存 ring buffer + 重放)
data: { "taskId": "...", "seq": 42, "ts": "...", "kind": "...", "payload": { … } }
```

kind 取值:`status`(状态机迁移)/ `message`(assistant 消息,首版整段)/ `message_delta`(token 级,待 §8-④)/ `tool_call` / `tool_result` / `approval_request` / `approval_decided` / `error`。kind 与 TraceHook 事件对齐(P5-批次1),时间线页直接吃 trace.jsonl。

### 6.3 回放与续跑
- 回放:`GET /tasks/{id}/messages` 读 `.sigma/session/<id>.jsonl`(唯一事实源),σ-server 只做 JSONL→前端消息视图的转换。
- 续跑:沿用 SessionManager 增量持久化;影子 checkpoint(批次7/8)提供回滚;水位治理在 server 侧同样生效。

### 6.4 并发与资源
- 每 task 一个 InteractiveSession;全局信号量(默认 3)限制并行执行轮。
- uvicorn 单进程 + asyncio 足够(本机单用户);每 20 次 mark 的水位治理钩子照常触发。

### 6.5 安全
- 默认绑 `127.0.0.1:8300`;启动生成随机 token 写 `.sigma/server-token`,请求头 `X-Sigma-Token` 校验。
- 开发期前端用 vite proxy(`/api` → 127.0.0.1:8300)免 CORS;生产(若打包)同源。

## 7. 目录结构提案(新增,不触碰现有代码)

```
sigma/
└── server/                      # 新包 sigma_server
    ├── __main__.py              # python -m sigma_server
    ├── app.py                   # FastAPI 装配 + token 中间件
    ├── schemas.py               # pydantic 模型(Task/Event/Approval…,镜像前端 client.ts)
    ├── routes/                  # system / projects / tasks / automations / plugins
    ├── services/task_service.py # 状态机 + Task↔Session 生命周期
    ├── adapters/sigma_run.py    # InteractiveSession 驱动 + 事件映射
    ├── adapters/approval.py     # HttpApprovalHook
    └── store.py                 # SQLite(任务元数据/审批记录)
```

## 8. 里程碑与验收门(可证伪)

| 里程碑 | 内容 | 验收门 |
|---|---|---|
| M1 | 任务 CRUD + 列表/详情回放(只读,不执行) | 前端 `.env` 设 `VITE_SIGMA_API_BASE` 后,全部现有交互走真接口绿;curl 全端点 200;mock/http 双实现接口一致性由同一套契约类型保证 |
| M2 | 真执行 + SSE + 审批环 + 打断 | e2e:前端创建任务→开始→观察流式动态→触发危险命令→前端弹审批→deny→任务回 running 并收到拒绝说明→interrupt 生效 |
| M3 | 自动化调度(APScheduler)+ 插件市场接真数据(skills 目录) | 定时任务真实触发一轮执行;插件装卸反映到 sigma 工具表 |

## 9. 待拍板问题(AGENTS.md #5,请星辰定夺)

1. **服务位置**:`sigma/server/`(同仓库新包,推荐——同 venv、同提交历史)还是独立仓库 `sigma-server`?
2. **任务元数据存储**:SQLite(推荐)vs JSON 文件?会话对话一律仍以 JSONL 为事实源,此点固定不争。
3. **事件通道**:SSE(推荐)vs WebSocket?若 M3 要做前端实时 steering(输入即注入),WS 价值才出现。
4. **流式粒度**:首版整段 message(零核心改动,推荐)vs token 级 delta(需要 sigma SDK 增加流式回调扩展点——这是唯一一处需要动 `sigma_ai` 的扩展,请单独批准)。
5. **鉴权强度**:本机单用户 token(推荐)是否足够;是否需要预留多用户(不影响 M1/M2 结构)。

## 10. 风险与既有事实对照

- 审批接口:决策型 ApprovalHook 已在 P3-批次2 落地并有回归,G103–G107——HttpApprovalHook 是第 3 个实现(CLI/Gate 之后),风险低。
- 打断:InterruptToken/TurnCancelled 已落地(G108–G110),server 侧只是远程触发。
- 回放:--timeline 已验证"旧会话可查"(P5-批次1),messages 回放同源。
- 已知独立缺陷(全失败且无文本时 assistant content 为空)会出现在回放视图,建议随 M2 顺手修。
- 预算提醒:常驻区现 3408/5500(批次3 D4 v2 后),server 不进常驻区,无 token 代价。
