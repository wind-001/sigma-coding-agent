# P4 — task 工具（sub_agent 派发 + 信箱）详规

> **状态：✅ 已落地（2026-09-23。556 单测 / mypy 56 files / 注入门 G80–G86 七条全红）。**
> 日期：2026-09-23。前置阅读：`docs/plans/P4-任务清单工具-详规.md`（todo + steering）。
> **修订（2026-09-23，批次2）**：子任务回报补 token 用量（D-A1）；子提示词写明总结截断上限（D-A5）；
> 无 todo 消融档下子会话不再注册 todo。见 `docs/plans/P4-批次2-todo消融与subagent回报-详规.md`。

---

## 1. 需求原话与边界

> 「我建议先完成另一个任务，task 工具，负责开启 sub_agent 去干活，模型什么时候调用这个工具有模型根据任务驱动」

补充约束（同日三轮，均已拍板）：

1. **同能力 + 隔离 + 禁止递归**：sub_agent 享有主 agent 相同能力（工具集相同）、
   上下文隔离；工具中不允许包含 task（不得再派发）；典型用途=搜信息交回要点，
   中间试错不污染主 agent 上下文。
2. **并发 ≤3、max_rounds=50、互斥锁**：允许主 agent 同时开启多个 sub_agent
   （最多 3 个，token 成本考虑），sub_agent 之间并发执行、互相隔离；
   todo 的 eval 可以派两个 sub_agent 并行跑 A/B 对照；
   多个 sub_agent 可能有读写冲突，用互斥锁实现。
3. **后台非阻塞 + 信箱**：子 agent 在后台执行，主 agent 不被阻塞，可继续当前
   其他无关任务；子 agent 的结果放进**信箱**，由主 agent **下一轮 turn 开始之前**
   的钩子检查是否完成并收集结果——只有当子任务是主 agent 剩余任务的
   前置依赖时才需要等它。

边界（v1 不做）：不做子 agent 与主 agent 的中途通信；不做子会话持久化与接续
（一次性，用完即弃）；wait 不设超时（子会话 max_rounds=50 兜底）。

---

## 2. 一句话设计与数据流

**TaskTool 只有两个动作：dispatch（派发后台子 agent，立即返回）与 status（查进度）。
子 agent 完成后结果进信箱；主 agent 每轮 turn 开始前由钩子 drain 信箱、
把结果注入上下文；模型收尾时若信箱还有未回报结果或子任务在跑，loop 先等完成、
注入、再让模型看一轮——结果绝不静默丢失。**

```
主 agent 会话（loop 持 tool_lock：写批次与子批次互斥）
   │  轮 N：tool_call task(dispatch, description="调查 X")
   ▼
TaskTool.run（read_only=True，立即返回，不阻塞）
   │  create_task(子会话.send(description))      [Semaphore(3) 限流]
   ▼
子 InteractiveSession（一次性）
   ├─ 独立上下文：空历史、SUB_SYSTEM_PROMPT、session_id={主id}-sa{n}
   ├─ registry = 主 registry 克隆，去掉 task，todo 换独立账本
   ├─ 同一把 tool_lock（子写批次与主写批次互斥）
   └─ run_turn……（后台，与主 agent 的 LLM 调用并发）
   │
   │  完成 → 结果进信箱（TaskTool._mailbox）
   ▼
主 agent 轮 N+1 开始前：drain 信箱 → 注入尾部 user 消息（与 todo steering 同模式）
   模型不再调工具但信箱未空/子任务在跑 → loop 等完成+注入+继续一轮（收尾兜底）
```

**注入不进常驻区、不改前缀**（D4 缓存不破）——与 todo steering、压缩摘要降级
user 是同一模式。结果本体始终只在主上下文出现一次（drain 后标记已回报）。

---

## 3. 设计决策

### D-T1 工具位置：`core/sigma_tools/task.py`，工厂注入

组装子会话需要 provider、keys、skills 目录、shadow_git_dir——全是**产品壳的知识**。
TaskTool 只接收工厂回调；sigma_tools 反依赖 sigma 层会成环。
与 ShadowCheckpoint 由产品壳构造传入、联网工具配 quota 注入同一模式。

```python
SubAgentFactory = Callable[[str, ToolContext, str], Awaitable[TurnResult]]
#                          description    ctx           派生 session_id
```

### D-T2 禁止递归：构造上排除

- `InteractiveSession.__init__` 加 **`enable_sub_agent: bool = False`**；
  True 时注册 TaskTool 并接线工厂。
- 子会话 registry = **主 registry 克隆**（逐个 `register(registry.get(name))`），
  克隆时**排除 task** → 子会话根本没有这个工具，模型调不到。
- **task 不进 `default_registry()`**（有条件才注册，同 load_skill——
  没有工厂的裸 registry 注册一个跑不起来的 task 等于给模型一个必错工具）。
- 为什么不是运行期检查：构造上没有它，就不存在"合法的递归路径"；
  测试 + 注入门（G84）钉住不变量。
- 附带收益：**克隆 = 与主会话天然同能力**（星辰原话），web 开关、技能、
  AGENTS.md 全部自动跟随，无需把开关参数抄一份（那会漂移）。
- 边界：`enable_sub_agent=True` 时若调用方传了自定义 registry 且其中无完整能力——
  克隆所见即所得，不额外校验（评测场景正是要能裁剪）。

### D-T3 上下文隔离

每次 dispatch = 全新子 `InteractiveSession`：空历史、`SUB_SYSTEM_PROMPT`、
派生 id `{ctx.session_id}-sa{n}`（n = 本会话第几次派发）。子会话的
`TurnResult.messages` 不进主会话树；主树只多：dispatch 调用 + 信箱注入消息。

### D-T4 信箱（星辰拍板：钩子收集，不靠模型主动 wait）

- 信箱在 TaskTool 实例内（`_mailbox`，跨 run_turn 保持，与会话同生命周期）。
- 子 agent 完成时结果进信箱；**注入格式**：
  `[子任务回报] #sa2「<描述前 50 字>…」已完成（12 轮）：\n<总结文本>`
- **截断上限 4000 字符**，超长截断且尾部注明（丢弃必须可见）；
  子会话 `status == "stopped"` 时头部加
  `[子任务未正常收尾：达到轮数上限，以下内容可能不完整]`。
  `rounds / usage / sub_session_id` 进 details（不占上下文）。
- drain 后标记**已回报**，同一结果不重复注入。

### D-T5 loop 侧的两个钩子（复用轮顶部注入位）

`AgentLoop.__init__` 加两个回调（默认 None = 行为与加之前逐字节一致）：

```python
mailbox_drain: Callable[[], list[AgentMessage]] | None = None
mailbox_wait:  Callable[[], Awaitable[list[AgentMessage]]] | None = None
```

- **轮顶部**（`_todo_steer_if_due` 同位）：`produced.extend(self._drain_mailbox())`。
- **收尾兜底**：`if not calls` 分支，先 `await self._mailbox_wait()`——
  实现（TaskTool 提供）：有在跑子任务就 `asyncio.gather` 等完，然后 drain；
  没有未回报内容返回 `[]`。拿到消息就 extend 进 produced 并 **continue**——
  模型下一轮看到结果、真正收尾。**run_turn 的结束条件 = 模型不再调工具
  且信箱没有未回报内容且没有在跑的子任务**。
  为什么必须兜底：模型 dispatch 后可能直接输出"已派发"收尾——
  没有兜底，结果就丢在信箱里，而且没有任何一步报错。
- **无死锁论证**：锁只包**写批次**；dispatch/status 是 read_only
  （两个动作都不碰文件系统），在 readonly 组并发执行、不持锁；
  收尾兜底的 `await mailbox_wait` 发生在批次结束之后，主 loop 不持锁，
  子批次拿锁无阻碍。

### D-T6 互斥锁（星辰拍板：读写冲突用锁）

`AgentLoop.__init__` 加 **`tool_lock: asyncio.Lock | None = None`**；
`_execute_batch` 里**含写工具的批次**（writers 或并发写）整体在锁内执行
（checkpoint mark 一起被罩住，git index 安全）。主 loop 与子 loop 传**同一把锁**
（InteractiveSession 创建，两边都传）。取舍：readonly 批次不拿锁——
主读与子写并发可能脏读，后果是读到半成品自己重试，v1 接受（详规边界）。

### D-T7 并发上限 3（星辰拍板：token 成本闸）

TaskTool 内 `asyncio.Semaphore(3)`（惰性创建），后台协程入口 acquire——
**同时 running ≤3**，第 4 个起排队（queued，dispatch 返回里如实说明）。
排队数 v1 不设限（status 可见，模型自我节制）。max_rounds（子会话）= **50**（拍板），
不暴露给模型调——参数越少越好，让模型传只会得到乱猜的值。

### D-T8 子会话配置（工厂内固定）

| 配置 | 值 | 理由 |
| --- | --- | --- |
| `max_rounds` | 50（拍板） | 子任务允许更长的探索 |
| 压缩 | 默认开 | 子任务也可能长 |
| checkpoint | 同 `shadow_git_dir` | 子写也受 L2；同 workspace 配对核对天然通过 |
| AGENTS.md / skills | 自动加载 / 同主 | 同能力 |
| tool_lock | 同主锁 | D-T6 |
| 取消信号 | `ctx.signal` | 主取消 → 子停 |
| observer / emit | 透传主会话 | 终端能看到子 agent 在干活；v1 不加前缀 |
| todo 账本 | `.sigma/todo-<派生id>.json` | 共享会静默破坏"至多一条 running"；TodoTool 加 `relative_path` 参数（默认值不变，主会话零影响） |

`SUB_SYSTEM_PROMPT` = SYSTEM_PROMPT 去掉 todo 引导行 + 尾部两行：
"你是被主 agent 派来执行单个子任务的执行者，上下文里没有主对话历史，只依据任务
描述干活。完成后用一段自包含的总结收尾：结论、关键文件路径、没做完的部分如实说明。"

### D-T9 作废记录：parallel_safe 方案

第一版曾设计"BaseTool 加 `parallel_safe` 元数据 + loop 同批次 gather 并发"——
**信箱拍板后作废**：后台执行模型下 dispatch 立即返回（read_only），根本不需要
让多个写工具并发跑；loop 分组逻辑一行不改，改动面缩小到两个回调 + 一个锁。
留着这条是因为"作废的设计要留下为什么作废"，否则后人会重新发明它。

### D-T10 常驻区预算（先量再落）

TaskParams 三字段（action/description/id）→ schema 预估 ~180 token；
SYSTEM_PROMPT 工具行 ~45 token。当前实测总账 3284 → 预计 ~3510，
**贴着 3500 的闸**。落码第一步实测：超闸则压 SYSTEM_PROMPT"注意"段
（不碰 D4 总闸）。

---

## 4. 提示词改动（sigma/sdk.py）

SYSTEM_PROMPT 工具区加：

```
- task：派子任务给后台子 agent（独立上下文、与你同款工具）执行，不阻塞你；
  status 查进度。子任务完成后结果会自动回报给你。
```

调用时机由模型根据任务自主判断（需求原话），不过度引导。
工作方式区加一条：`探索/调研/批量试错类工作可派 task 后台去做，继续做你的其他步骤。`

---

## 5. 测试与注入实验计划

**tests/test_task_tool.py**（~10 用例，factory 用假实现）：

1. dispatch 立即返回（不 await 工厂完成），返回文本含派发 id；
2. 完成 → drain 拿到结果（格式含 id/轮次/截断）；
3. 截断可见：text > 4000 → 截断 + 尾部注明；
4. stopped：头部加不可信注记；
5. 工厂抛异常 → 信箱收到 is_error 回报，不丢；
6. drain 后标记已回报，二次 drain 为空；
7. status：running/queued/completed(已回报) 三态正确；
8. 并发 ≤3：4 个 dispatch → 3 running + 1 queued（semaphore 断言）；
9. description 空白 → 校验拒绝；
10. wait_and_drain：无在跑返回已完成；有在跑等完再返回。

**tests/test_sub_agent.py**（~5 用例）：

1. 子 registry 无 task（G84 靶子）；
2. 子 todo 账本路径独立（主清单不被子触碰）；
3. 子会话空历史起步；
4. 子会话拿到同一把 tool_lock；
5. enable_sub_agent=False 的主会话无 task（默认路径不变）。

**loop 级**（并入 test_task_tool.py 或独立，~4 用例）：

1. 轮顶部 drain 注入（消息在 produced 里、模型本轮可见）；
2. 收尾兜底：模型输出文本收尾但子任务在跑 → 等完成 + 注入 + 多跑一轮；
3. 兜底收敛：结果回报后模型再收尾 → 正常结束（不死循环）；
4. tool_lock：主写批次与子写批次互斥（用事件序断言）。

**注入门**（`scripts/gate_injection_eval.py`）：

| 门 | 断言 | 注入方式 |
| --- | --- | --- |
| G84 | 子 registry 无 task（递归禁止） | 克隆循环里的排除判断改恒 False → 子会话也注册 task → 用例红 |
| G85 | 截断必须可见 | 截断分支改 `if False` → 用例 3 红 |
| G86 | 收尾兜底不丢结果 | `not calls` 分支的 wait 调用改 `if False` → 用例 2 红 |

---

## 6. 与 todo 的配合 + eval 衔接

- 配合：todo create 拆解 → 逐条 task dispatch → 完成自动回报 → 主 agent 验收 update。
- **落地后第一件事**（星辰原话）：派 2 个 sub_agent 并行跑挂起的 todo A/B 对照
  评测（A 组 / B 组各一个 sub_agent，`evals/todo_ab.py` 驱动脚本，
  harness 侧不需要 A 组专用 registry——跑脚本的是子 agent 的 bash）。

---

## 7. 实现记录（2026-09-23）

**改动文件**：`core/sigma_tools/task.py`（新，262 行）/ `core/sigma_agent/loop.py`
（三参数：`tool_lock` / `mailbox_drain` / `mailbox_wait`，默认 None = 行为逐字节一致；
轮顶部 drain + 收尾兜底 + 写批次进锁 `_run_write_batch`）/
`core/sigma/sdk.py`（`enable_sub_agent` 接线 + `_make_sub_agent_factory` + TASK_TOOL_LINE
条件行 + 子提示词同源）/ `core/sigma/cli.py`（`--sub-agent` flag）/
`core/sigma_tools/todo.py`（`TodoTool(relative_path=...)` 实例化，模块函数改实例方法）。

**与计划的三处偏差（都有理由）**：

1. **max_rounds 50**（星辰中途拍板，D-T8 原写 20 不暴露）→ `sub_agent_max_rounds=50`。
2. **计划 §4 的"工作方式区加一条"没做**：静态 SYSTEM_PROMPT 提 task 会违反同源
   （未启用时提示词指向不存在的工具）——引导句并进条件化的 TASK_TOOL_LINE。
3. **wait 动作没做**（信箱拍板后 schema 更小）：dispatch/status 两动作 + loop 钩子自动收集。

**测试**：`tests/test_task_tool.py`（13）+ `tests/test_sub_agent.py`（5，含 G84 端到端）
= 19 新用例；全量 **556 绿**。测试驱动发现真实调度：FakeProvider 共享游标 + asyncio
FIFO 让子任务在 dispatch 的 gather 挂起点抢跑——轮次消费顺序已用注释钉进测试文件头。

**注入门（E72/E73/E74，`scripts/gate_injection_eval.py`）**：G84（克隆排除改只排 todo）
/ G85（截断分支 `if False`）/ G86（兜底 wait 条件 `if False`）——**7/7 确定性红**
（连同 G80–G83）。

**常驻区实测（D-T10 兑现，口径 `len/3`，schema 按 provider 真实文本整条计）**：

| 口径 | 构成 | 对 3500 |
| --- | --- | --- |
| 现实（AGENTS.md 实际 282 + 技能索引实际 57） | 372 + 1528 + 282 + 57 = **3239** | ✓ 余 261 |
| 最满（双软闸取 cap 700/350） | 372 + 1528 + 700 + 350 = **3950** | ❌ 超 450 |
| 无 task 最满（历史基线） | 337 + 1291 + 700 + 350 = **3731** | ❌ **超 231** |

- task 瘦身（字段说明 + 工具 description + TASK_TOOL_LINE 压文案）：增量 230 → ~219。
- **分项闸欠账（G59 纪律第二次现行）**：可选 schema 实测 1475 ≫ cap 750——
  **task 之前就已破**（web_search 454 / web_fetch 255 / todo 420 / load_skill 162）。
  09-22 重排 5.1 表时三个软闸都没填满（实测 2374），"分项 cap 相加 = 3500"
  一直没被真验过。**⚠️ 需星辰裁决**（改架构文档，AGENTS.md 第 5 条）：三选一——
  ① 压 web 两件套 schema 文案；② 重排分项 cap（可选 →1300，总闸等比上调）；
  ③ 总闸判定改"分项各自 ≤cap"而非"总量 ≤3500"。日常合法场景（现实口径）未超闸。
