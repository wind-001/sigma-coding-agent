# P5 — REPL 斜杠命令与会话切换 详规

> 立项：2026-09-23 ｜ 需求方：星辰（「支持用户终端通过 "/" 相关命令选择之前的会话继续对话，提出如何实现的计划即可」）
> 状态：✅ 已实现（2026-09-23）。本文件第 4 节的三个待拍板项**按第 4 节的建议口径落地**（星辰指示「就按照上面说的来实现就行」）。
> 落地结果：584 单测绿 / mypy 56 files 干净 / G87 两条注入路径均证伪（`scripts/gate_injection_p5.py`）。
> 回执文案与详规的差异只有措辞（"已切换到会话 X（续上 N 条消息）"），语义一致。
> 前置阅读：`P2-会话树与上下文-详规.md`（树/存储）、`P4-task工具-sub_agent-详规.md`（loop 信箱）

## 0. 需求原话（防漂移）

> 支持用户终端通过 "/" 相关命令选择之前的会话继续对话。

## 1. 目标 / 非目标

**目标**：
1. REPL 里输入 `/sessions` 列出历史会话（可辨认：时间、大小、首条用户消息摘要、消息数）；
2. `/switch <序号|id>` 切换到任一历史会话继续对话，历史与 checkpoint 都接上；
3. `/new` 开新会话、`/help` 列出命令；
4. 未知 `/` 命令**报错并打印帮助**——不静默发给模型（「有歧义宁可报错」；注意这是一次行为变更：之前以 `/` 开头的输入会原样发给模型）。

**非目标（v1 不做）**：`/delete` 与会话 GC；会话重命名；跨机器同步；切换时的分支可视化（树的分支能力是 P2 的，本批次只切整会话）；模糊搜索。

## 2. 关键设计决策

### D-S1 切换 = 重建 InteractiveSession（方案 A），不做就地切换（方案 B）

候选方案 B（`InteractiveSession.switch_session()` 就地换 tree/context/checkpoint）被否：

- 就地切换要枚举"哪些状态属于会话"：`_context`（树 + 压缩视图）、`_checkpoint`、
  `_loop` 的 session_id 与 todo steering 计数、TaskTool 的信箱……**枚举会漏**，
  漏一个就是"新会话带着旧会话的 steering 计数/压缩视图"这种离根因很远的症状；
- 重建的答案是**全部重置**——不需要枚举，就不可能漏；
- 组装知识集中在 `InteractiveSession.__init__` 一处（sdk 的既有纪律：「只有一处组装，不会漂移」），
  方案 B 等于在会话中途再开一条半个组装的路径；
- **安全性已论证**：REPL 逐轮执行，命令只在轮间处理；loop 的收尾兜底保证
  轮结束时信箱排空、无在跑子任务——切换点没有悬空的异步状态；
- 成本可忽略：重扫 skills / 重读 AGENTS.md 是启动时本来就做的事；
  checkpoint 的 `mark(baseline)` 在已存在的影子库上多打一个 commit，无害，
  且与现有 `--continue` 路径行为**完全一致**（--continue 本来就是这么续的）。

### D-S2 分层：解析在 repl、组装在 cli、预览在 sigma_session

| 层 | 新增 | 职责 |
| --- | --- | --- |
| `sigma_session/sessions.py` | `SessionPreview`（dataclass：id / modified / size / message_count / first_user_text）+ `session_previews(root, limit)` | 「会话文件长什么样」的知识只在这层；读每个文件**前 4 KB** 找第一条 user 消息，坏文件跳过（与 `list_sessions` 的"能救多少救多少"同源） |
| `sigma/cli.py` | `SessionManager`：持有组装参数（provider/registry/prompt/workspace/skills/shadow 规则），`current` / `switch_to(id)` / `new()` / `list()` | 产品壳决定"换会话怎么重组装"；复用 `resolve_session` 已有的树加载路径 |
| `sigma/repl.py` | `/` 前缀分派 + 命令表 | REPL 只管输入解析与生命周期（既有职责不扩大）；命令回调由 cli 注入 |

`SessionPreview` 用 dataclass 不用 BaseModel——不落盘不过网，判据与 `SessionInfo` / `LoadResult` 一致。

### D-S3 列表可辨认性：首条用户消息 + 工作区标注

光给会话 id 用户认不出哪个是哪个（`SessionBinding.previous_messages` 的 docstring 已说过这个理）。
每行显示：`[序号] id  时间  大小  消息数  "首条用户消息前 40 字…"`。

跨工作区：会话存在用户级 `~/.sigma/sessions`（刻意设计），但影子库记着创建时的工作区
（`sigma-workspace.txt`）。列表里若该会话的工作区 ≠ 当前工作区，标注 `[其他工作区: <路径>]`——
**不禁止切换**（用户可能就是要搬上下文），但必须可见（「丢弃/偏差必须可见」纪律）。

### D-S4 切换语义与 `--continue` 对齐

`switch_to(id)` 内部就是 `SessionTree.from_store(JsonlStore(root, id))` + 重建会话 +
banner 打印「已切换到会话 X（续上 N 条消息）」——与启动期 `resolve_session` 的
「--session ID 指向已存在会话」分支同一条路径，不发明第二种"续接"。

todo 账本**不换**：它在工作区 `.sigma/todo.json`（工作区级，不是会话级）——
换会话后 steering 计数随新 loop 清零，清单本身延续。这是 todo 工具的既有设计，写明即可。

### D-S5 命令表 v1

| 命令 | 行为 |
| --- | --- |
| `/sessions`（别名 `/list`） | 列最近 20 个会话（倒序），当前会话打 `*` |
| `/switch <n\|id>`（别名 `/resume`） | 按序号（本次 `/sessions` 的输出）或完整 id 切换；找不到 → 报错不崩 |
| `/new` | 开新会话（新 id，立即生效） |
| `/help`（`/h`、`/?`） | 命令清单 |

序号引用**只在最近一次 `/sessions` 输出后有效**（序号→id 的映射缓存在 REPL 侧；
切换前用 id 重新校验文件存在）——否则"列表后又有新会话写入"会让序号指错会话，
而那是静默切错上下文。

## 3. 改动文件与测试

| 文件 | 改动 |
| --- | --- |
| `core/sigma_session/sessions.py` | `SessionPreview` + `session_previews()`；顺带修 `new_session_id` 的 `clock: object` 注解（→ `Callable[[], float] \| None`，AGENTS.md 第 3 条） |
| `core/sigma/cli.py` | `SessionManager`；`_run_interactive` 改走 manager；横幅命令提示 |
| `core/sigma/repl.py` | `/` 分派 + 命令表 + HELLO 文案更新 |
| `tests/test_sigma_repl.py` | 脚本化输入：`/sessions` 输出、`/switch` 后历史可见、`/new` 生效、未知命令报错不崩、空目录 `/sessions` |
| `tests/test_sigma_cli_session.py` | manager 切换后消息进新文件、旧文件不再增长；preview 的坏文件跳过 |
| 门槛候选 | G87：切换后 send 的消息落在新会话文件（注入靶：switch 只换 id 不换树 → 历史丢失，测试必须红） |

## 4. 待拍板

1. **D-S1 方案 A（重建）确认**——若你希望保留"切换不重扫资源"，请说明，方案 B 的枚举清单我可以列全再评；
2. `-p` 一次性模式产生的短会话**是否进 `/sessions` 列表**（建议：进，按 mtime 排即可；它们也是"可续的会话"，过滤反而制造"我的会话呢"的困惑）；
3. 命令名是否就用 `/sessions` `/switch` `/new`（还是要 `/resume` 为主名）。
