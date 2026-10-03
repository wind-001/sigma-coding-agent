import type { TaskTimeline } from '../api/client'

/**
 * 底部运行指标的口径与格式化（星辰 2026-10-02）。
 *
 * 抽出来有两个原因：
 * 1. **口径单一来源**——指标串与 endcap 收尾行都报「轮数/耗时」，
 *    之前两处各写一份字面量，已经出现过术语不一致（底栏写「轮数 189」、
 *    endcap 写「189 轮」）。现在都从这里取。
 * 2. **可测**——纯函数，能直接单测；留在组件里就只能靠肉眼看。
 */

/** 时长:秒 → 人类可读。阈值取「读起来最短且不丢精度」的一档。 */
export function formatWall(seconds: number): string {
  if (seconds === 0) return '0s'
  // ⚠ 关键：**先四舍五入到整秒,再选档**,顺序不能反。
  // 反了的实测后果(踩过):
  //   - 先选档:if(seconds < 60) 内 toFixed(0) → 59.9s 打成 "60s",
  //     而 60s 该显示 "1m0s",同一时长两种写法;
  //   - 阈值改 59.5 同样错:59.5/60 向下取整 = 0 → 输出 "0m59s"(荒谬)。
  // 整秒化之后 59.5→60,自然落进分钟档,m 至少是 1。
  // 不保留亚秒:真实耗时最少是秒级(一次 LLM 调用),0.4s 档位是过度设计
  // ——实测它还会因为 toFixed 缩放把 1s 打成 "0.1s"。
  const whole = Math.round(seconds)
  if (whole < 60) return `${whole}s`
  const m = Math.floor(whole / 60)
  if (m < 60) return `${m}m${whole % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h${m % 60}m`
  return `${Math.floor(h / 24)}d${h % 24}h`
}

/**
 * token 量级缩写。8 位数裸排（8907093）在 10px 小字里没法扫读，
 * 眼睛只会看到一串黑块。→ 8.9M / 118.3K。
 * 小值不缩写：四位数以内原样显示，缩写反而增加解码成本。
 *
 * ⚠ 两条硬约束（都是实测踩出来的）：
 * 1. **进位不能丢档**：999999 走 K 档会打成 "1000K"（= 1M），比不缩写
 *    还难读。选档必须按**进位后**的量级判断。
 * 2. **同量级格式必须一致**：99999 要和 100000 长得一样（都 "100K"），
 *    999999 要和 1000000 长得一样（都 "1.0M"）。toFixed 位数由
 *    **进位后的值**决定,不由原值决定,否则 99999→"100.0K"
 *    而 100000→"100K",眼睛要重新适应。
 */
export function formatTokens(n: number): string {
  if (n < 10_000) return String(n)
  const render = (value: number, suffix: string): string =>
    `${value < 100 ? value.toFixed(1) : value}${suffix}`
  if (n < 1_000_000) {
    const k = Number((n / 1_000).toFixed(1))
    return k >= 1000 ? render(Number((n / 1_000_000).toFixed(1)), 'M') : render(k, 'K')
  }
  return render(Number((n / 1_000_000).toFixed(1)), 'M')
}

export interface MetricItem {
  key: string
  label: string
  value: string
  /**
   * 语义色:
   * - 'bad'  真正需要报警（工具错误非零）——全站唯一的红。
   * - 'weak' 次要读数（tok/s 这类长任务下必然很小的数）——弱化不隐藏。
   * - 不设   = 常态信息。
   * 滥用颜色 = 没有颜色。
   */
  tone?: 'plain' | 'weak' | 'bad'
  /** tooltip：说明这个指标的口径，别让数字自解释。 */
  title: string
}

/** 一组指标：对应参考设计里「带一个图标 + 若干数值」的一块。 */
export interface MetricGroup {
  key: string
  /** 组图标（lucide 组件名，由渲染层映射，这里只给语义 key）。 */
  icon: 'activity' | 'database'
  items: MetricItem[]
}

/** 工具调用总次数 = 各轮 tools 长度之和（"步"是用户能理解的单位）。 */
function countSteps(timeline: TaskTimeline): number {
  return timeline.rounds.reduce((sum: number, round) => sum + round.tools.length, 0)
}

/**
 * 输出速度 = completion token / 墙钟秒数。
 * 用 completion 而非总量：入向 token 是被缓存喂进去的，不反映"生成速度"。
 * 无耗时数据时返回 null（不编造 0）。
 */
function tokensPerSecond(timeline: TaskTimeline): number | null {
  if (timeline.wallSeconds === null || timeline.wallSeconds <= 0) return null
  if (timeline.totalCompletion <= 0) return null
  return timeline.totalCompletion / timeline.wallSeconds
}

/**
 * 指标分组（参考 DeepSeek 式底栏：两组，各带一个图标）。
 *
 * 分组依据是**读的时候想知道什么**，不是字段类型：
 * - activity「执行过程」：跑了多久、跑了几步、出得多快 —— 回答"刚才干了什么"
 * - database「token 消耗」：烧了多少 token、缓存省了多少 —— 回答"代价多大"
 *
 * ⚠ 单行排版（星辰 2026-10-02 定稿，判"两排丑"）：
 * 同一组内用 `标签 值` 同行连读，**不人为换行**。层次靠字重和颜色，
 * 不靠行数——底栏是横向扫描区，拆行会让视线横扫再竖扫。
 */
export function buildMetricGroups(timeline: TaskTimeline | null): MetricGroup[] {
  if (timeline === null) return []
  const cacheNote =
    timeline.cacheRate !== null
      ? `　缓存命中 ${Math.round(timeline.cacheRate * 100)}%（${timeline.totalCached}）`
      : ''

  const activity: MetricItem[] = [
    {
      key: 'rounds',
      label: '轮',
      value: String(timeline.rounds.length),
      title: 'LLM 调用轮数(与 sigma 观测层 RoundView 同口径)',
    },
  ]
  const steps = countSteps(timeline)
  if (steps > 0) {
    activity.push({
      key: 'steps',
      label: '步',
      value: String(steps),
      title: '工具调用总次数（各轮 tools 累加）',
    })
  }
  const tps = tokensPerSecond(timeline)
  if (tps !== null) {
    // wallSeconds 在 tps !== null 时必非 null(tps 的定义即含该前提),
    // 但 TS 的收窄不跨函数边界。与其写非空断言(会被 lint 挡)或 `?? 0`
    // (那是**编造一个数字**),不如让 tooltip 自己按可用字段拼。
    const wallText =
      timeline.wallSeconds !== null ? ` ÷ 执行 ${timeline.wallSeconds.toFixed(1)}s` : ''
    activity.push({
      key: 'tps',
      label: 'tok/s',
      value: tps >= 100 ? String(Math.round(tps)) : tps.toFixed(1),
      tone: 'weak',
      // ⚠ 口径提醒（实测踩过）：这个数**看起来总是很小**——参考设计
      // 显示 269 tok/s，那是因为它的任务是几分钟的；sigma 长任务
      // 跑到 5 小时 / 118K completion 就是个位数。**这不是渲染错误**。
      // 若哪天在短任务上还是 5 以下，才说明分母口径错了。
      // 用 completion 而非总量：入向 token 是被缓存喂进去的,不代表生成速度。
      title: `输出速度 = completion ${timeline.totalCompletion}${wallText}。长任务摊薄后会显著偏低,不代表卡住`,
    })
  }
  if (timeline.wallSeconds !== null) {
    const wall = timeline.wallSeconds
    // ⚠ 「≈」只在 wallApprox 时出现。以前这里恒定写 `≈`,又按
    // wallApprox 追加「(估算值)」——不估算的时候也在说"约等于",
    // 那是**把不确定伪装成确定**,与"丢弃必须可见"同源。
    activity.push({
      key: 'wall',
      label: '耗时',
      value: `${formatWall(wall)}${timeline.wallApprox ? '≈' : ''}`,
      title: `执行耗时${timeline.wallApprox ? '（估算值，由消息时间戳推算，非精确计时）' : '：累计 LLM 与工具执行时间，不含轮间等待'} ${wall.toFixed(1)}s`,
    })
  }

  const cost: MetricItem[] = [
    {
      key: 'tokens',
      label: 'token',
      value: formatTokens(timeline.totalPrompt + timeline.totalCompletion),
      title: `入 / 出累计　prompt ${timeline.totalPrompt} · completion ${timeline.totalCompletion}${cacheNote}`,
    },
  ]
  if (timeline.cacheRate !== null) {
    cost.push({
      key: 'cache',
      label: '缓存命中',
      value: `${Math.round(timeline.cacheRate * 100)}%`,
      title: `缓存命中率 = cached / prompt（${timeline.totalCached} / ${timeline.totalPrompt}）`,
    })
  }
  if (timeline.toolErrors > 0) {
    // 只有非零才出现：0 是常态，满屏红色只会让人麻木。
    cost.push({
      key: 'toolErrors',
      label: '工具错误',
      value: String(timeline.toolErrors),
      tone: 'bad',
      title: '本会话工具调用失败次数（重试也计入）',
    })
  }

  return [
    { key: 'activity', icon: 'activity', items: activity },
    { key: 'cost', icon: 'database', items: cost },
  ]
}

/** endcap 收尾行的「N 轮 · 耗时 X」——与底栏同口径，避免两处说法不一。 */
export function wallSummary(timeline: TaskTimeline | null): string {
  if (timeline === null) return ''
  const parts = [`${timeline.rounds.length} 轮`]
  if (timeline.wallSeconds !== null) {
    parts.push(`耗时 ${formatWall(timeline.wallSeconds)}${timeline.wallApprox ? '≈' : ''}`)
  }
  return parts.join(' · ')
}
