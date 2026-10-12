# 验证收尾 A/B：ax107 × B2 vs B2-verify（推进机制批次 2）

> 生成：2026-10-13 ｜ 模型 LongCat-2.5-Preview @ api.longcat.chat ｜ N=1 采样
> 任务：ax107-fix-jsonlog（**B1 基线上唯一一条 M1' 谎报完成**：completed、
> reason 空、判定 exit 1——见 `agency_baseline_20261013.md`）
> 落盘：`evals/reports/sessions/agency-ab-verify-20261013/`（两臂 trace 全文）

## 两臂配置

| 臂 | 档位 | 验证收尾 |
| --- | --- | --- |
| 对照 | B2（完整 harness） | 无（不注册 VerificationGate） |
| 实验 | B2-verify | 有（任务的判定命令 `python -m pytest tests/ -q` 当验收命令） |

其余逐项相同：同模型、同端点、同 `--sessions-dir`、同任务初始状态。

## 结果

| 臂 | status | 轮数 | 工具 | prompt+completion tok | wall-clock | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| B2 | completed | 6 | 6（失败 0） | 21284+499 | 31.3s | 通过 |
| B2-verify | completed | 4 | 4（失败 0） | 14050+4170 | 126.6s | 通过 |

**verify 臂的 trace 证据**（`ax107-fix-jsonlog_B2-verify.trace.jsonl`）：

```json
{"kind": "advance", "round": 4, "continue_round": false,
 "force_stop": false, "injected": false, "reason": ""}
```

第 4 轮模型收手时，gate 询问过一次、跑了验收命令、命令已过 → 不干预、
放行 completed。**"注册了且验收已过"与"没注册"在 trace 上分得清**
（这一条是批次 2 中途补的：第一版把"问了说不用管"折叠进 None，
两种静默混为一种，审计链缺环——协议改为有钩子时永不返回 None）。

## 怎么读这份 A/B（不许过度解读）

1. **N=1，两臂都过**。ax107 在 B1 基线上的谎报是**上一次采样**的结果；
   本次 B2 对照臂模型自己就做对了（6 轮）。所以本 A/B **不能**宣称
   "验证收尾把谎报率从 X 降到 Y"——采样方差覆盖了效应量。
2. **机制链路本身的证据在注入实验，不在这份 A/B**：GA-1a（不跑命令恒放行
   → 测试红）与 GA-1b（连续不过也永不 stopped → 测试红）证伪了
   "验证收尾在干活"的两条核心承诺；确定性回放（test_advance_gate.py 8 条）
   覆盖了 通过/不过/超时/命令缺失/预算内恢复 五种序列。
3. **方向性结论**：gate 在真实任务上按设计工作（问过 → 跑命令 → 放行），
   零注册时行为不变（GA-4a/4b）。要拿到"减少谎报"的效应量数字，需要
   N≥3 复跑 ax107 两臂 + 在 syn-011/012 复杂任务上同款对照——
   列入后续（见详规 §9 风险③）。

## 产物

- 报告：`evals/reports/task_ax107-fix-jsonlog_B2{,-verify}.{json,md}`
- trace/会话树：`evals/reports/sessions/agency-ab-verify-20261013/`
