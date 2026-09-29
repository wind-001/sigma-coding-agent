# P1-repo map 详规

> 版本:v1 ｜ 2026-09-29
> 定位:**P1 的最后一块**——常驻区"具名预留(repo map 等) 500"的具名消费(5.1.0 消费纪律:吃预留必须连实测数字进表)。
> 拍板:**星辰 2026-09-29 全权委托("一切你说了算,开启多个子 agent 继续工作")**,本详规按委托代行拍板;此前的调研锚点:aider 手法(repo map + 缓存 + 降频刷新)已在常驻区锁定调研中记录,本详规只取其"会话级快照"一半,刷新降频不取(见"不做")。

## 1. 问题与主张

跨文件任务("先定位、再修改")里,模型目前的唯一手段是 grep——大仓库下要盲搜多轮。
repo map 给模型一张**工作区结构地图**(文件 + Python 顶层符号),会话开始就进常驻区,
让第一轮 grep 就有方向。可证伪主张:有 repo map 的臂在"跨文件定位类"任务上
首轮工具调用更少——这是将来 ablation 的一行,本轮不跑(要真钱)。

## 2. 设计

**形态**:纯函数 `build_repo_map(workspace_root, *, max_tokens) -> str`,住
`sigma/sessions/repo_map.py`——它喂 SessionContext,与 `resources.py`(AGENTS.md)、
`memory`(索引)同族:**文本进上下文,策略归产品壳**。

**扫描规则**(全部确定性,同输入逐字节同输出):
- 跳过所有点开头目录(`.git/.sigma/.venv/...`一并覆盖)+ `node_modules/__pycache__/build/dist`;
- 文件按 posix 路径字符串排序;`.py` 用 **ast 提取顶层 class/def 名**(零新依赖,
  与"不引 tree-sitter"的极简判据一致),解析失败给空符号表不报错;
- >64KB 的文件只列名不提符号;非 Python 文件只列名。

**注入纪律**(逐条对齐既有门槛):
- **会话内冻结**(G884 同款):sdk 在 InteractiveSession 构造时扫一次,传
  `SessionContext(repo_map=...)`;文件中途变化下个会话可见;
- **空工作区零注入**(G881 同款):没有文件 → 返回 "" → 常驻区逐字节不变;
- **cap 500 且截断可见**(记忆索引同款):超限先降级(去符号明细),仍超则
  `truncate_to_tokens` 硬截,标记拼进正文;
- **进 resident 即被指纹+预算两道断言覆盖**(G26/G59 无需改)。

**预算表**(resident_caps.py):`"具名预留(repo map 等)"` 改名 `"repo map"`(500 不动,
分项和仍 5500,G885 守护);MEASURED 增加 sigma 仓库自样本实测数(实现后回填)。

**开关**:默认开,`--no-repo-map` 整层关(与 `--no-memory` 同款,连扫描都不做);
子 agent 构造时 `enable_repo_map=False`(与 `enable_memory=False` 同款——子会话
上下文是主会话的切片,不再付一份地图钱)。

## 3. 不做(写明,避免被读成"忘了")

- **不做符号级排序启发式**(aider 用图排序决定截断时谁存活):截断按路径序,
  这是 v1 显式取舍,登记技术债;
- **不做会话中刷新/降频**(D4:会话内常驻区不得变一个字节,刷新就是违约);
- **不做非 Python 语言的符号提取**(ast 只懂 Python;其他语言列文件名);
- **不做 .gitignore 解析**(隐藏目录+固定清单已覆盖 99% 噪声,解析 gitignore 是新依赖面)。

## 4. 门槛

| 编号 | 断言 |
| --- | --- |
| G-P1RM-1 | 同一工作区两次构建**逐字节相同**(确定性) |
| G-P1RM-2 | 点开头/排除目录不出现;.py 有顶层符号;非 py 只有文件名 |
| G-P1RM-3 | 超 cap:输出 `estimate_text ≤ 500` 且**截断标记在正文里可见** |
| G-P1RM-4 | 空工作区返回 "",SessionContext 常驻区与不传时**逐字节一致** |
| G-P1RM-5 | `--no-repo-map` 时连扫描都不发生(monkeypatch 计数,G884 同款) |
| G-P1RM-6 | CAPS 含 `"repo map"`=500 且 `caps_sum()==5500`(既有 G885 键名同步) |

回归门禁:pytest 全绿(724+新增)、mypy --strict 零错、lint-imports 2/2。
