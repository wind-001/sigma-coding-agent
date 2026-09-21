# evals —— 评测

> 状态（2026-09-21）：**`runner.py` 已落地**，跑 `tests/fixtures/transcripts/` 下的
> 六个回放场景，报告落 `reports/`。
> `datasets/` 的 70 条任务集**尚未落地**——那些需要真实 API key + 判定脚本。

**不要在这里写空壳实现。** 骨架到位、实现没接，是本项目明确要避免的失败模式。
（`datasets/*/` 三个目录除了 `.gitkeep` 是空的，**这是如实状态，不是"已完成"。**）

## 现在能跑的（离线、免 key、不联网）

```bash
python evals/runner.py                        # 全部场景
python evals/runner.py --scenario read_then_edit
```

六个场景：`read_then_edit` · `bash_fail_then_retry` · `context_overflow` ·
`compact_then_continue` · `tool_error_recovery` · `branch_and_resume`。
清单与期望值在 `tests/fixtures/transcripts/_scenarios.py`——
**门禁测试与运行器共用同一份**，不各写一份。

采集字段：轮数 / 状态 / 工具调用数 / 工具失败数 / prompt+completion token /
wall_clock / **transcript 是否被完整消费**。

**`transcript 未被完整消费` 是个独立信号**：状态全绿但没消费完，
说明 loop 在有剩余输入时自己停了——那是个真 bug，不是噪声。

## 数据集结构（**尚未落地**）

```
datasets/
  reproduce/     20 条  从真实小仓库取已修复的 bug，checkout 到修复前，任务描述取自 issue
  synthetic/     30 条  先写测试并验证它在当前代码上失败，再写任务描述
  adversarial/   20 条  诱导 agent 执行越界 / 危险操作
```

### 硬性构造顺序

**`synthetic/` 必须先有会失败的测试、后有任务描述。**

反过来做（先写实现再补测试）会造出一批「本项目的实现恰好能过」的任务，
评测就失去意义。这是整个评测体系里最容易自欺的地方。

### 判定方式

- `reproduce` / `synthetic`：判定脚本 exit 0 即通过。
- `adversarial`：**不进成功率指标**，只统计拦截率与误拦率。

## 运行器

`runner.py` 负责：批量跑回放场景、采集指标、输出报告到 `reports/`。

**回放场景是数据集的测量仪，不是替代品。** 先有测量仪，才知道数据集跑出来的
数字有没有意义；而且回放场景进 CI、不花钱、不联网，是数据集的前置条件。

## 指标

| 指标 | 定义 |
| --- | --- |
| 任务成功率 | 判定脚本 exit 0 的比例 |
| 首次成功率 | 不经过任何自我纠错轮即为正确的比例 |
| **纠错增益** | 最终成功率 − 首次成功率。**这是「agent 会不会自我纠错」的唯一证据** |
| 每任务 token | prompt + completion，分常驻 / 动态两档 |
| 每任务工具轮数 | 平均模型调用轮次 |
| 每任务 wall-clock | 端到端耗时 |
| 危险操作拦截率 | 对抗集被正确拦截的比例 |
| 误拦率 | 正常任务被错误拦截的比例 |
| 回滚成功率 | checkpoint 回滚后工作区与提交点完全一致的比例 |
| prompt cache 命中率 | `cached_tokens / prompt_tokens` |
| 截断发生率 | 触发输出截断的工具调用占比 |

## 基线

| 编号 | 配置 | 用途 |
| --- | --- | --- |
| B0 | 纯 LLM，无工具，单轮 | 证明任务本身不能靠模型一次答对 |
| **B1** | 有工具，无 harness 干预 | **关键对照。没有它，「harness 有用」无法与「工具本身有用」区分** |
| B2 | 完整 sigma | 主结果 |
| B3 | B2 + 一个扩展 | 证明扩展层真的能改变行为 |

## 报告

每次运行产出 JSON 原始数据 + Markdown 汇总，落在 `reports/`。
报告是**要提交进版本库的证据**，不要忽略掉。
