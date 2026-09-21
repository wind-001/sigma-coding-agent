# 回放场景评测报告

- 生成时间戳（脚本注入，非挂钟）：2026-09-21 23:46:38
- 场景数：6
- 通过：6 / 6

> 本报告由 `evals/runner.py` 生成。**不要手改**——
> 手改的数字会与代码脱节，那正是这份报告要防的事。

## 汇总

| 场景 | 状态 | 轮数 | 工具调用 | 工具失败 | prompt tok | completion tok | 耗时 ms | 结果 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| read_then_edit | completed | 4 | 3 | 0 | 3740 | 203 | 1032 | PASS |
| bash_fail_then_retry | completed | 6 | 5 | 1 | 6150 | 261 | 2109 | PASS |
| context_overflow | completed | 4 | 3 | 0 | 18600 | 245 | 2400 | PASS |
| compact_then_continue | completed | 4 | 3 | 0 | 9200 | 152 | 5 | PASS |
| tool_error_recovery | completed | 5 | 4 | 1 | 5000 | 202 | 1620 | PASS |
| branch_and_resume | completed | 3 | 2 | 0 | 4800 | 100 | 2 | PASS |

## 偏差明细

无。全部场景与清单里的期望值一致。

## 怎么读这份报告

1. **`prompt tok` 是整个 turn 的累计**（逐轮相加），不是最后一轮的。
   这不是回放器的属性，是 `TurnResult.usage` 的口径——
   它曾写成「只取最后一轮」，症状是「多轮任务更贵」这个事实在报告里消失，
   而**不报任何错**。门槛 G58 现在钉住它（注入退回旧写法必须变红）。
2. **轮数对不上是最有信息量的失败**：loop 提前退出（少）通常是「一失败就 return」这类分支混进来了；轮数多出来则是 transcript 被改过。
3. **`transcript 完整消费` 是个独立信号**：状态全绿但没消费完，说明 loop 在有剩余输入时自己停了——那是个真 bug，不是噪声。
4. **耗时（wall_clock_ms）在本机是噪声**：它主要反映的是**工具是否起了子进程**（`bash` 起 bash.exe ≈ 1–2 s，`read`/`grep` 纯文件 IO ≈ 个位数 ms），
   不是 harness 的开销。真实性能基线见架构 7.3 节的 B0–B3。
5. **本报告不含「任务成功率」类指标**——那需要 `evals/datasets/` 的任务集与判定脚本，
   尚未落地。**不要拿本报告的 PASS 当成功率。**
