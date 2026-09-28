# P4-批次7:影子 checkpoint 优化(工作区级共享库 + 懒基线 + 快照纪律)详规

> 立项:2026-09-26 ｜ 需求方:星辰 ｜ 状态:**✅ 已落地(2026-09-27,G857–G864 全绿,691 passed + mypy 干净)**
> 前置阅读:`architecture.md` 6.2(影子 checkpoint)、`checkpoint.py` 模块 docstring、`P3-批次1-安全边界-详规.md`(L2 由来)

## 0. 需求原话(星辰,2026-09-26)

> 我现在的git checkpoint设计逻辑是否有缺陷,工具每次写都建立快照是否会导致快照爆炸,建议优化一下
> 另外项目启动的时候会建立当前目录的 baseline 快照,市面上其他的coding agent是如何实现的,有何优化空间
> 清理旧的影子快照

三条中第三条(清理)**已执行完毕**:删除 139 个旧影子库,`~/.sigma/sessions` 从 804MB 降到 3.9MB,jsonl 转录全保留。本详规覆盖前两条的代码层优化。

## 1. 现状与差距(2026-09-26 实测)

| 问题 | 实测证据 | 根因位置 |
| --- | --- | --- |
| 会话级影子库跨会话零去重 | 151 会话 = 803MB;家目录启动 3 次 = 3 个 274/169/104MB 的库,内容几乎相同 | `shadow_git_dir_for` 每会话一个新裸库 + baseline `add -A` 全量入库 |
| 启动同步建基线 | 家目录一次 `status` 扫描 **65 秒**;项目目录启动到横幅 ~3s | `sdk.py:567` 构造时立即 `mark("baseline")` |
| 空批次也提交 | `--allow-empty` 有意保留(G66:快照数=基线+写批次数) | `checkpoint.py` mark |
| 每次 mark 3~5 个 git 进程 | Windows 每进程 ~120ms → 每写批次 ~0.5s 纯开销 | `_git add`/`commit`/`rev-parse` + `_collect_oversize` 的 `status` |
| 无 GC/保留 | 全库搜不到清理逻辑;loose objects 永不打包 | — |

**市面对照**(2026-09-26 调研):Claude Code 影子 git + **每个用户 prompt 前**一个快照 + **只跟踪被改文件**;Cline/Roo 影子 git + 每工具操作一 commit(正在用选择性 staging 降开销);Aider 不建影子库,直接 auto-commit 用户自己的仓库。共同点:**成本随"改动量"走,不随"工作区体积"走**。sigma 的独特价值是 bash 副作用也在回滚面内——本批保留它,不学"只跟踪被改文件"(那需要触及跟踪模式,见 §6)。

**判据**:sigma 现在的成本模型是 O(工作区 × 会话数),要改成 O(工作区 × 1) + O(改动 × 批次数)。

## 2. 方案

### 2.1 影子库按工作区共享,会话=分支(主体)

影子库从"每会话一个库"改为"**每工作区一个库,每会话一个分支**":

- 落点:`<workspace>/.sigma/shadow.git`(bare)。选工作区级而非用户级 hash 目录的理由:① 与 todo/allowlist 同判据——**工作区级状态落工作区 `.sigma/`**;② 删项目即删库,不产生孤儿存储;③ 路径即语义,人可发现可手删。
- `.sigma/` 已在 `BUILTIN_EXCLUDES`,影子库不会被自己快照(自指问题现有设计已解)。
- 分支:`refs/heads/<session_id>`。会话切换(/switch、/new)= 换分支,对象全库共享——**同工作区第二次启动 baseline 几乎零成本**(对象已在库,`add -A` 只建索引)。
- mark 改 plumbing,**不碰 HEAD**(多会话分支并存的前提):

```
add -A  →  write-tree  →  commit-tree -p <parent> -m <label>  →  update-ref refs/heads/<sid> <new>
```

- `commit-tree` 的 stdout 就是新 ref,**`rev-parse` 进程直接省掉**(4→4,但 parent 与 ref 查询走文件/git 命令合并,见 2.4);
- 空批次跳过:`ShadowCheckpoint` 增加实例字段 `_last_tree`,`write-tree` 输出与上次相同 → 跳过 commit-tree/update-ref(每会话内存态,重启后首次 mark 必提交——语义正确)。**G66 修订**:"快照数=基线+写批次数" → "**每个写批次执行前,会话分支的 tree 与工作区一致**;工作区无变化时不新增提交"。
- restore 改 `git read-tree --reset -u <ref>`(等价 reset --hard 的树部分,含删除多余文件;bare + 显式 `--work-tree` 形态与现状命令同款,已验证可行);`pre-restore` 快照逻辑不变。
- 竞态边界(记录不解决):两个 sigma 进程同工作区并发 mark → commit-tree 的 parent 可能过期,分支 ref 被后写者覆盖,**丢一条快照不崩进程**。同进程内主/子 agent 已被 `tool_lock` 罩住。影子库是软保障,这个窗口可接受,写入 checkpoint.py docstring。

### 2.2 懒基线(删,不是挪)

`_mark_before_writes` 本来就在**写批次执行前**打快照(loop.py:795)——所以**首个写批次快照天然就是"任何写之前的干净状态"**,启动时那记 baseline 是纯冗余:

- **动作 = 删除** `sdk.py:565-570` 的构造期 `mark("baseline")`,`ShadowCheckpoint` 对象照常构造(懒初始化,零 git 调用);
- G65 语义从"启动基线必须在任何写之前"改为"**首个写批次前必有快照**"——由 `_mark_before_writes` 结构性保证,不需要新守卫方法;
- 回滚语义零损失:没有写就没有需要回滚的东西;
- 收益:启动路径 git 调用 = 0,家目录 65s 归零,横幅秒出。

### 2.3 家目录守门

`main()` 里 workspace 解析后:`workspace.resolve() == Path.home().resolve()` →

- `--no-checkpoint` 强制生效(影子库不构造),横幅"拦截"行如实显示;
- 追加一行黄色提示:"工作区是家目录,L2 快照已禁用(全量快照会复制 AppData);需要回滚请 cd 到项目目录或 --workspace 指向它"。

不做触及跟踪降级档(§6)。

### 2.4 进程瘦身

- `rev-parse`:已由 commit-tree stdout 吸收(2.1);
- parent 查询:`refs/heads/<sid>` 裸库直接读文件(不存在=无 parent),零进程;
- `_collect_oversize` 降频:实例计数器,每 `OVERSIZE_SCAN_INTERVAL=20` 次 mark 跑一次(会话首次必跑)。代价:超 5MB 的新文件最多延迟 20 个批次才被排除——5MB 上限从硬变软,记入风险。

### 2.5 会话收尾 GC

`SessionManager.aclose()` 末尾:影子库可用且存在 pack 收益时跑 `git -c gc.autoDetach=false gc --quiet`,timeout 30s,失败静默(last_error 记录)。同库其他分支的快照不受影响(gc 只打包不删引用可达对象)。

## 3. 改动清单

| 文件 | 改动 |
| --- | --- |
| `core/sigma_agent/checkpoint.py` | mark 改 plumbing 四段;`_last_tree` 缓存与空跳过;`refs()`/`restore`/`workspace_mismatch` 改分支读法;parent 走文件读;`OVERSIZE_SCAN_INTERVAL` 降频;docstring 记录竞态边界 |
| `core/sigma/sdk.py` | 删构造期 baseline(565-570);`shadow_git_dir_for` 改返回 `<workspace>/.sigma/shadow.git`(签名不变,session_id 参数弃用并在调用侧清理) |
| `core/sigma/cli.py` | `shadow_git_dir_for` 调用处(883/1069)核对;家目录守门 + 横幅文案;`SessionManager.aclose` 收尾 GC |
| `core/sigma_agent/loop.py` | 无(795 处 `_mark_before_writes` 结构不变,补注释说明"首个写批次快照即基线") |
| `tests/test_agent_checkpoint.py` 及相关 | G857–G864(§4) |
| `docs/architecture.md` | 6.2 节同步:落点、分支模型、G65/G66 新表述 |

## 4. 测试与门槛(可证伪,G857 起)

- **G857 共享去重**:同一工作区 /new 两个会话各打一次快照,第二个会话 `objects/` 无新增对象(内容级共享);
- **G858 懒基线**:构造 InteractiveSession 后影子库无 ref;首次写批次前 `refs/heads/<sid>` 存在;
- **G859 空跳过**:无变化的连续两次 mark,ref 不增、objects 不增;
- **G860 回滚语义回归**:写→写→回滚到首快照,工作区逐文件等于首快照;含"回滚删除新增文件"路径(read-tree --reset -u);
- **G861 家目录守门**:workspace=home → checkpoint 为 None + 横幅提示行出现;
- **G862 降频**:spy `_collect_oversize`,30 次 mark 内调用 ≤2 次;
- **G863 GC**:aclose 后 pack 文件出现,快照 ref 全部可达;
- **G864 分支并发**:同库两分支交替 mark,ref 互不覆盖;pre-restore 快照落在当前会话分支;
- **性能门槛**:项目目录启动到横幅 <600ms(现 ~3s);小项目单次 mark <150ms(现 ~0.5s)。

## 5. 风险

| 风险 | 缓解 |
| --- | --- |
| plumbing restore 漏删多余文件(数据不回退) | 用 `read-tree --reset -u`(含删除);G860 专门断言"回滚删除新增文件"路径 |
| bare 库 + work-tree 的 git 行为差异 | 沿用现状已验证的 `--git-dir + --work-tree` 显式形态;不加 core.worktree 配置 |
| 跨进程 parent 竞态丢快照 | 软保障语义内可接受,docstring 写明;同进程已有 tool_lock |
| 5MB 上限软化(降频间隙) | 间隔 20 批次 ≈ 一次任务内最多一批次超大文件入库;可调 |
| `.sigma/shadow.git` 所在目录只读/满盘 | `_ensure_ready` 失败降级路径已有,横幅 last_error 如实显示 |
| gc 卡收尾 | 30s 超时 + 失败静默 |

## 6. 明确不做(本批边界)

- **触及跟踪模式**(只快照将被写的文件,Claude Code 式)——家目录守门(2.3)已堵最险场景,触及跟踪是独立大改,留后续批次详规;
- 旧路径 `sessions_root/<id>.shadow.git` 的读取兼容(数据已于 2026-09-26 清理,无迁移对象);
- 快照加密、跨机器同步、快照上限条数策略;
- Aider 式"写用户自己的仓库"模式。

## 7. 拍板记录(2026-09-26,星辰)

1. **影子库落点**:A. 工作区级 `<workspace>/.sigma/shadow.git`——与 todo/allowlist 同判据,删项目即删库;
2. **家目录策略**:A. 禁用 L2 + 横幅提示;触及跟踪留后续批次;
3. **oversize 扫描间隔**:默认 20 批次;
4. **aclose GC**:做(30s 超时,失败静默)。

> 拍板后本节转为记录(日期+结论),按 §3 清单开工。

## 8. 实现记录(2026-09-27)

**落地与 §2 方案的差异**(两处,均为实现中发现的事实修正):

1. **懒基线 = 纯删除,连守卫方法都不需要**(§2.2 预估正确):实现时确认 `_mark_before_writes` 本就在写批次**执行前**跑,首个写批次快照天然是基线,`sdk.py` 那记 baseline 直接删,零新增代码。
2. **`gc()` 的空库检查必须是文件级的**(§2.5 补丁):首版 gc 直接 `_ensure_ready`,冒烟发现 `/exit` 退出时会**凭空建库**(没写过文件的会话也被 init)。改为先查 `HEAD` 文件存在与否——懒基线"启动零写入"才真正成立(冒烟:临时目录 `/exit` 后工作区零新增)。

**门槛结果**:

- G857 共享去重:第二会话 baseline 只新增 1 个 commit 对象(blob/tree 全复用) ✅
- G858 懒基线:构造后 refs 为空;启动/退出全程零 git 写入 ✅
- G859 空跳过:无变化 mark 返回现有 tip、refs 不增;有变化必提交(两面对照) ✅
- G860 回滚语义:`read-tree --reset -u` 删除新增文件、恢复改写/删除,与旧 `reset --hard` 等价 ✅(既有 4 条 restore 用例 + loop 集成用例全部走新路径通过)
- G861 家目录守门:`checkpoint_disabled_reason` 四分支断言 ✅
- G862 降频:30 次 mark 扫描恰好 2 次 ✅
- G863 GC:打包出 pack 文件且松散 ref 仍可读 ✅
- G864 分支隔离:A 回滚后 B 分支 tip 逐字节不变 ✅

**性能**(门槛:项目目录启动 <600ms;实测说明见下):

- 项目目录启动到横幅+退出:~1.0s(改造前 ~3s;其中 ~0.9s 是 Python import 底座,httpx/pydantic/asyncio——import 懒加载不在本批范围)
- 家目录启动:65 秒的全工作区扫描**归零**(横幅即出,快照只在首个写批次前打)
- 小项目单次 mark:4 个 git 进程(与旧持平;`rev-parse` 被 commit-tree stdout 吸收、parent 改文件零进程读);**内容无变化的 mark 降到 2 个**(add+write-tree 后即返回);`_collect_oversize` 的 status 从每次 mark 降到每 20 次

**测试**:691 passed(含 9 条新增/改写门槛用例);mypy 61 文件零问题;`gate_injection_p5.py` 的字面锚点(`switch_to` 三行)未动,注入实验不受影响。
