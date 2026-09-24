# P4-批次2：todo 消融开关 + sub_agent 回报增强 详规

> 立项：2026-09-23 ｜ 需求方：星辰（设计审查后拍板「修复上面的设计缺陷」）
> 状态：✅ 已落地（2026-09-23。563 单测绿 / mypy 改动四文件无告警。
> 注：`test_bash_timeout_kills_command` 在本机（Windows git-bash）挂起，
> 已在改动前的代码上复现——是预先存在的环境问题，与本次改动无关。）
> 前置阅读：`P4-任务清单工具-详规.md`（todo + steering）、`P4-task工具-sub_agent-详规.md`（task + 信箱）

## 0. 背景：设计审查发现的缺口

2026-09-23 对 todo 工具与 sub_agent（task 工具）做了一轮设计审查，结论：
**两个工具的本体设计合理**（状态机、锁、信箱、steering 的论证都闭合，测试在位），
但评测侧有四个缺口会让「派 2 个 sub_agent 并行跑 todo A/B 评测」的数据失真，外加两个小观察。

| # | 缺口 | 后果 | 处置 |
| --- | --- | --- | --- |
| 1 | 子 agent 的 `TurnResult.usage` 被 `_run_sub` 丢弃 | A/B 报告里的 token 只统计主 agent（编排者），两臂真实成本不可见 | **修**（D-A1） |
| 2 | todo 恒注册、提示词恒含 todo 行，无消融开关 | 「无 todo」对照臂拼不出来；漏一处对照臂即被污染 | **修**（D-A2） |
| 3 | `EvalProfile` 只有 compaction/checkpoint；`task_runner` 从不传 `enable_sub_agent` / `enable_todo` | 评测运行器无法声明带 sub_agent / 无 todo 的档位 | **修**（D-A3） |
| 4 | bash 超时上限 600 s | 真实模型跑一条评测任务超过 10 分钟是常态，单条 bash 必被砍 | **修**（D-A4，上限提至 1800 s，仍有界） |
| 5 | 子 agent 不知道总结会被截到 4000 字符 | 截断后主 agent 才看到「请写得更精炼」，子 agent 从不知情 | **修**（D-A5，子提示词写明上限） |
| 6 | `_pending_sub_tasks` 依赖 dispatch 时 details 里的 pending 计数（陈旧值） | 不会出错（`wait_and_drain` 只看真实在跑的），最多多一次空 drain | **不改**（留档，避免过度工程） |
| 7 | 两个 sub-agent 共享同一 `workspace_root`，锁只保写互斥不保语义 | 若两臂直接在同一工作区干活会互相覆盖 | **不改代码**：评测走 `task_runner.py`（任务拷进临时目录）天然规避；编排层须知写进本文 §5 |

## 1. 设计决策

### D-A1 子 agent 用量随回报回传（task.py）

- `_SubTaskState` 加 `usage: Usage | None`；`_run_sub` 里 `state.usage = result.usage`（completed 与 stopped 都记）。
- `_report_message` 的完成行从 `已完成（N 轮）` 变为 `已完成（N 轮，token P+C（cached K））`；usage 为 None 时保持原样（回放/FakeProvider 路径逐字节不变）。
- **为什么放在回报文本而不是另开通道**：主 agent 是评测数据的第一个消费者——它要在最终汇总里引用两臂成本；文本同行保证「成本与结论一起被看到」，与「结果可信度与结果同行」（stopped 前缀）同一条纪律。

### D-A2 todo 消融开关（sdk.py，三处同源）

「无 todo」臂要三条同时成立，漏一条即污染：

1. `default_registry(todo: bool = True)`——`todo=False` 时不注册 TodoTool；
2. `build_system_prompt(todo: bool = True)`——`todo=False` 时去掉工具行，**且**「工作方式」第 3 条从
   「先用 todo create 拆解成清单」换成「先拆解成小步骤，按步骤依次执行」（保留拆解建议、去掉工具引用；
   不换文案会让模型去找一个不存在的工具——批次 7 同源纪律）；
3. `InteractiveSession(enable_todo: bool = True)` / `run_task(enable_todo=…)`——`False` 时：
   默认注册表走 `default_registry(todo=False)`；调用方自带的 registry 里若仍有 todo →
   **构造时 `ValueError`**（与 `enable_sub_agent` 撞车检查同一条纪律：一致性在启动时炸，不留运行期断点）；
   传 AgentLoop 的 `todo_steer_interval` 置 0（registry 检查已是硬闸，置 0 是显式声明「这一档没有 todo 概念」）。

**逐字节承诺**：`SYSTEM_PROMPT` 拆成 `_SYSTEM_PROMPT_HEAD + TODO_TOOL_LINE + 规则段` 构造，
`build_system_prompt()`（全默认）的输出与重构前**逐字节一致**——现有测试
`test_system_prompt_is_byte_identical_when_disabled` 继续钉住这一点。

### D-A3 评测档位接线（eval_profile.py + task_runner.py）

- `EvalProfile` 加两个字段：`todo: bool = True`、`sub_agent: bool = False`。
  它们**一落地就真实接线**（满足该类型「字段最小化：只放当前真实接线的行为」的自我约束）。
  `b1()` / `b2()` 语义不变（继承默认值）。
- `task_runner.py` 加 `--no-todo` / `--sub-agent` 两个 CLI 开关：
  - 档位名加后缀（如 `B2-no-todo`、`B2-sub`）进**报告文件名**与每行数据——
    没有档位名，两个月后没人说得清那份 JSON 是在什么配置下产生的（运行器原有纪律的延伸）；
  - `_run_agent` 按 profile 传 `enable_todo` / `enable_sub_agent`，
    提示词用 `sdk.build_system_prompt(todo=profile.todo, task=profile.sub_agent)` 同源生成；
  - `--no-todo` / `--sub-agent` 搭配 `--profile B0` 直接报错（B0 无工具，消融开关对它无意义——
    宁可报错，不要静默跑一份名不副实的报告）。

### D-A4 bash 超时上限 600 → 1800（bash.py）

不设超时是正确性问题（挂起的命令让 loop 永久卡死），所以上限**保留但有界放大**：
真实模型跑一条评测任务（多轮 + 判定）超过 10 分钟是常态，600 s 会让「一条任务一次 bash 调用」
的编排方式必死。1800 s 仍是有界上限，不改变「默认 60 s、模型按需声明」的语义。

### D-A5 子提示词写明总结上限（sdk.py）

`SUB_SYSTEM_PROMPT_PREFIX` 加一句「总结控制在 {MAX_RESULT_CHARS} 字符以内，超出会被截断」
（引用常量而非抄写数字——两处同源，改上限不用记得改文案）。

## 2. 评测编排约定（交给执行 agent 的须知）

1. **一臂一进程**：两个 sub-agent 各自用 bash 调 `evals/task_runner.py`，
   不要直接在同一工作区里改文件（缺口 7）；task_runner 会把任务拷进临时目录，两臂天然隔离。
2. **每条任务一次 bash 调用**（`--task syn-00X`），不要一条命令跑全套——
   即使上限 1800 s，单命令跑全套仍会超时，且失败粒度太粗。
3. 对照组态：`--profile B2`（有 todo）对 `--profile B2 --no-todo`（无 todo），
   判定点是判定器通过率 + `TaskResult` 的轮数/工具失败/token。
4. 子任务回报文本里自带两臂 token（D-A1），主 agent 汇总时直接引用。

## 3. 测试与门槛

- 新增（`tests/test_task_tool.py`）：回报含 token 用量；usage=None 时文本逐字节不变。
- 新增（`tests/test_eval_profile.py`）：
  `default_registry(todo=False)` 无 todo；`build_system_prompt(todo=False)` 无 todo 行且无「todo create」；
  `enable_todo=False` + 含 todo 的 registry → ValueError；`run_task(enable_todo=False)` 通路正常；
  EvalProfile 新字段默认值。
- 既有继续钉住：`build_system_prompt() == SYSTEM_PROMPT`（逐字节）；task/todo/sub_agent 全部门禁。
- 全量 `pytest` 绿。

## 4. 改动文件

| 文件 | 改动 |
| --- | --- |
| `core/sigma_tools/task.py` | `_SubTaskState.usage` + 回报带 token（D-A1） |
| `core/sigma_tools/bash.py` | `MAX_TIMEOUT_S` 600→1800（D-A4） |
| `core/sigma/sdk.py` | `TODO_TOOL_LINE` + 提示词拆分、`build_system_prompt(todo=)`、`default_registry(todo=)`、`enable_todo` 接线、子提示词截断告知（D-A2/D-A5） |
| `core/sigma/eval_profile.py` | `todo` / `sub_agent` 字段（D-A3） |
| `evals/task_runner.py` | `--no-todo` / `--sub-agent` + 档位后缀 + 接线（D-A3） |
| `tests/test_task_tool.py` / `tests/test_eval_profile.py` | 上述门禁 |
| `docs/plans/P4-任务清单工具-详规.md` / `P4-task工具-sub_agent-详规.md` | 头部加修订指针到本文档 |
