# P4-批次6:观测通道并入钩子 + CLI 面板渲染优化(rich)详规 v2

> 立项:2026-09-26 ｜ 需求方:星辰 ｜ 状态:**✅ 已落地(2026-09-26。观测通道并入钩子 + 全渲染面 rich 化)**
> 前置阅读:`core/sigma_agent/observe.py`(被本批并入的观测通道)、`P4-批次5` 详规(钩子通道)、`architecture.md` 2.2(不做 TUI 的决定)

## 0. 需求原话(星辰,2026-09-26)

> 1. cli 的消息流是如何被事件驱动的?
> 2. 这些事件有没有被放到 hooks 里面管理?
> 3. 当前 cli 的面板排版输出建议使用 rich 库进行优化一下。
> 4. (Q1 答复)我希望将这些日志信息也并入钩子进行管理,我认为钩子不仅仅有控制 loop 循环走向的作用,还有负责日志记录并告知的义务。

## 1. 前两问的回答(现状链路)

**CLI 消息流的事件驱动**:loop 在五个固定时机调用 `self._notify(event)`,事件沿 `LoopObserver.on_event` 单方法接口推给观察者;CLI 组装时把 `TerminalRenderer`(render.py)作为 observer 传进 `InteractiveSession(observer=...)` → `AgentLoop`。事件:`TextChunk`(流式逐块)/ `ThinkingChunk` / `ToolStart`(执行前)/ `ToolEnd`(✓/✗)/ `TurnEnd`(汇总)。

**截至 P4-批次5,这些事件没有放进 hooks**:批次 5 曾拍板"observer(旁听渲染)与 hooks(行为)两通道分离"。**本批按需求 4 推翻该拍板**。

### 1.1 决策变更记录(星辰,2026-09-26):观测并入钩子,单通道

- **新判断**:钩子是 harness 唯一的行为扩展面,**日志记录与告知也是钩子的义务**——"渲染"不过是一个订阅了日志事件的钩子,与持久化钩子平级。
- **统一后的事件全集**(hooks.py):`TextChunk` / `ThinkingChunk` / `ToolStart` / `ToolEnd`(吸收批次5 的 `ToolResultProduced`,新增 `message` 字段——持久化与渲染是**同一个时机**的两个订阅者)/ `TurnEnd` / `AssistantProduced` / `MessageInjected`。
- **observer 通道删除**:`observe.py` 整个移除,`LoopObserver`、loop 的 `observer` 参数与 `_notify` 一并删除;`observe.py` 的 W9 判据(单方法+事件对象)由钩子系统继承。
- **接受的代价**(写明不藏):CLI 渲染路径现在依赖钩子注册——不注册渲染钩子就没有输出。这是**特性不是缺陷**:评测/子 agent 测试不注册渲染钩子=天然静默,CLI 注册=有人看。子 agent 的渲染可见性由工厂把 `extra_hooks` 透传给子会话保证。
- `TurnResult` 契约不变;`interactive session` 的 `observer` 参数更名为 `extra_hooks: Sequence[BaseHook]`(批次5 刚加的 `hooks: HookManager | None` 参数一并收紧为内部自建 manager——注册动作只收"钩子列表",不收"总线",避免调用方把带持久化钩子的 manager 串场到别的会话)。

## 2. 目标与不翻案的既有决定

- **目标**:面板排版与可读性——横幅/汇总成块、工具行着色、列表成表格;Windows conhost 老终端自动降级。
- **"不做 TUI"(architecture 2.2)不翻案**:不用 `rich.Live` / `Progress` / Spinner 等重绘组件,不做交互组件,不引入备用屏。rich 只承担:**带终端能力检测的文本渲染**——render.py docstring"颜色等终端能力检测稳定后再加"的兑现。
- **G34 宽容纪律保留并升级(G99)**:渲染钩子在 loop 调用栈里,任何渲染异常降级成旧式裸文本输出,不许冒泡。

## 3. 方案

### 3.1 依赖(架构 0.3 第 2 条:新依赖先验 3.12+)

`rich>=13` 加入 `pyproject.toml` **运行期依赖**(Q2 拍板)。验证:venv Python 3.13.9 实测 rich 15.0.0 正常;rich 官方支持 ≥3.9。README 依赖说法同步更新。

### 3.2 分层

rich 与渲染钩子只进**产品壳**(sigma 层)。sigma_agent 的 hooks/loop 只有事件、没有 rich;lint-imports 三契约不动。

### 3.3 渲染面清单

| 面 | 现状 | 改后 |
| --- | --- | --- |
| 事件行(TerminalRenderHook) | 纯文本 ⏺/✓/✗ | 同形着色(结构行 `markup=False, highlight=False, soft_wrap=True`,仅 `style=` 上色;**符号与行形状逐字节不变**) |
| 模型流式文本 | 裸写 | 仍**绕过 rich 裸写**(Q3 拍板:纯文本逐字,零重排风险;G98 钉住) |
| CLI 启动横幅 | 逐行 print | `rich.Panel` + 对齐 |
| 一次性结果汇总 | `──` 分隔 | `rich.Panel` |
| `--list-checkpoints` / 回滚报告 | 逐行 print | `rich.Table` |
| REPL `/sessions` | 逐行 print | `rich.Table`;`/help` 轻改 |
| stderr `[harness 错误]` | print | `Console(stderr=True)` 红色 |

### 3.4 已识别的陷阱(逐条有对策)

1. **markup 吞字**:凡承载模型数据的行(参数预览/结果预览)一律 `markup=False, highlight=False`,颜色只走 `style=` 参数——模型文本里的 `[方括号]` 逐字保留(G98)。
2. **管道/重定向宽度**:rich 非 TTY 按 80 列折行;结构行用 `soft_wrap=True` 保持单行,面板内容窄于 80 列;测试注入固定宽度 Console。
3. **G99 兜底**:`on_event` 外层 try/except → 降级旧式裸 print,不冒泡(注入:让 Console 写入抛异常 → 断言循环不死)。
4. **与 `_configure_console` 叠加**(W13):UTF-8 reconfigure 保留;cmd/PowerShell/Windows Terminal 三环境冒烟。
5. **测试确定性**:渲染测试注入 `Console(file=StringIO)`(非 TTY 自动无色);既有 render 断言(⏺/✓/✗)保持通过。

## 4. 拍板结果(2026-09-26)

| # | 决定 |
| --- | --- |
| Q1 范围 | **观测通道并入钩子(星辰答复改写)+ 全渲染面 rich 化**(横幅/汇总/表格/事件行;`input()` 输入行不动) |
| Q2 依赖 | rich 进运行期依赖 |
| Q3 模型文本 | 纯文本逐字,不做 markdown 渲染 |

## 5. 改动清单

| 文件 | 改动 |
| --- | --- |
| `core/sigma_agent/hooks.py` | 并入五个观测事件;`ToolEnd` 加 `message` 字段(合并 `ToolResultProduced`);docstring 记录单通道决策 |
| `core/sigma_agent/observe.py` | **删除**(判据迁入 hooks.py docstring) |
| `core/sigma_agent/loop.py` | 删 `observer`/`_notify`;新增 `_emit()`;全部时机改发钩子事件 |
| `core/sigma/render.py` | `TerminalRenderer(BaseHook)` + rich(同形着色 + G99 兜底) |
| `core/sigma/sdk.py` | `observer` 参数 → `extra_hooks: Sequence[BaseHook]`;子 agent 工厂透传 |
| `core/sigma/cli.py` | `extra_hooks=[TerminalRenderer()]` 两处;横幅/汇总/checkpoints/回滚 Panel/Table;stderr 红色 |
| `core/sigma/repl.py` | `/sessions` 表格化 |
| `pyproject.toml` + `README.md` | rich 依赖 + 说法同步 |
| `tests/` | `test_agent_observe.py` → `test_agent_hook_events.py`(录制器改钩子);`test_hooks_persistence.py` 事件更名;新增 G98/G99 |

不动:`sigma_session`、evals 运行器、`TurnResult` 契约、`input()` 交互行。

## 6. 测试与门槛

| 编号 | 断言 | 注入 |
| --- | --- | --- |
| G98 | 模型文本含 `[xxx]` 逐字保留,不被 markup/highlight 吃掉 | 去掉 `markup=False` → 红 |
| G99 | 渲染内部抛异常 → 降级裸文本输出,run_turn 不死 | 让 Console 写入抛 → 红 |
| G34(保留) | 未知事件类型不崩(渲染钩子的宽容分支) | 不变 |
| 事件序列 | 一轮的完整事件序列与次序不变(TextChunk…ToolStart…ToolEnd…TurnEnd) | 录制钩子断言 |
| 回归 | 全套 pytest / mypy strict / lint-imports / 回放 6/6 | — |

## 7. 风险

| # | 风险 | 处置 |
| --- | --- | --- |
| R1 | 渲染依赖钩子注册:某调用方忘注册 → 无输出 | CLI 两处工厂统一经 SessionManager;文档写明"评测静默是特性" |
| R2 | rich 非 TTY 80 列折行 | 结构行 soft_wrap;面板窄于 80 列 |
| R3 | 解析 stdout 的老脚本破窗 | 事件行形状逐字节不变(仅加色);面板面只在横幅/汇总 |
| R4 | TextChunk 逐块 emit 的开销(每 delta 一次协程调度) | 无钩子时短路(hooks None);同步钩子无真实 await;CLI 规模可忽略 |
| R5 | legacy conhost + GBK 组合 | `_configure_console` 保留;三环境冒烟 |

## 8. 明确不做(本期边界)

- `rich.Live` / `Progress` / Spinner 等重绘组件(TUI 决定不翻案);
- 模型输出的 markdown 渲染与代码块语法高亮(与流式冲突);
- 鼠标/交互组件、备用屏;
- sigma_agent / sigma_session 内任何 rich 使用;
- 钩子优先级/异步订阅语义变更(维持 P4-批次5 的注册顺序 + sync/async 双支持)。


## 9. 实现记录(2026-09-26)

| 项 | 结果 |
| --- | --- |
| 通道统一 | `observe.py` 删除;事件全集入 `sigma_agent/hooks.py`;`loop.py` 的 `observer`/`_notify` 由 `hooks`/`_emit` 取代;`ToolEnd` 吸收 `ToolResultProduced`(加 `message` 字段——持久化与渲染是同一时机的两个订阅者) |
| 产品壳 | `InteractiveSession`/`run_task` 的 `observer` 参数 → `extra_hooks: Sequence[BaseHook]`(每会话自建总线:持久化钩子绑定本会话,共享钩子逐会话注册,子 agent 工厂透传);`TerminalRenderer` 改为 `BaseHook`,cli.py 两处 `extra_hooks=[TerminalRenderer()]` |
| rich 面 | 横幅/安全边界 → `Panel`;一次性汇总 → Panel + grid Table;`--list-checkpoints` → Table;REPL `/sessions` → Table(会话 id 列 `no_wrap`,Console 宽度下限 110——宁可超宽不截断);stderr 错误 → 红色 `_err()`;**模型流式文本绕过 rich 逐字直写**(Q3) |
| 依赖 | `rich>=13` 进运行期依赖;README 依赖说法同步;venv 3.13.9 + rich 15.0.0 实测 |
| 测试 | `test_agent_observe.py` → `test_agent_hook_events.py`(录制器换 BaseHook,G32 序列断言逐字未改);G33 改写为"hooks=None 短路";`test_hooks_persistence.py` 事件更名;新增 G98(markup 吞字)/G99(渲染异常降级);全套 **652 passed**、mypy --strict 58 文件零错误、lint-imports 三契约 KEPT、回放 **6/6** |
| 冒烟 | FakeProvider 驱动一轮:事件行(⏺/✓/汇总)与 P1 形态逐字一致;TTY 下自动着色、管道/CI 自动无色 |

**踩坑记录**:① 用 heredoc 写 Python 补丁时 `
` 被展开成真实换行,源码里出现"字符串内嵌真实换行"的语法错误(两处 Panel 与 join 字面量)——以 `chr(92)` 构造转义修复;教训:**给源码打补丁的脚本别用 heredoc 传转义**,或全部走 `chr()` 构造。② rich Table 在 80 列(非 TTY 默认)下会折行/截断 id 与摘要列——`no_wrap` + 宽度下限解决,判据是"完整标识符不许折行"。

**对批次5 的两处修订**(由本批承载,批次5 文档正文不改):事件名 `ToolResultProduced` → 并入 `ToolEnd`;`InteractiveSession` 的 `hooks: HookManager | None` 参数收紧为 `extra_hooks`(避免把绑定会话上下文的总线串场到别的会话)。
