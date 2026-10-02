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
   * 语义色:'bad' 只给真正需要报警的指标（当前只有工具错误）。
   * 滥用颜色=没有颜色，所以默认一律 plain。
   */
  tone?: 'plain' | 'bad'
  /** tooltip：说明这个指标的口径，别让数字自解释。 */
  title: string
}

/**
 * 底部指标列表。字段顺序 = 视觉顺序，按「规模 → 质量 → 时间」排，
 * 让人从左到右读下来是「跑了多少 → 有没有出错 → 花了多久」。
 *
 * ⚠ 单行排版（星辰 2026-10-02 定稿）：我第一版把标签和数值拆成
 * **上下两行**（标签在上、数值在下），想用行数做层次——结果视觉上
 * 像报销单，标签悬空、数值落地，读的时候视线要横扫再竖扫，很别扭。
 * **层次靠字重和颜色，不靠行数**：一行内 `标签 数值`，标签浅灰、
 * 数值半粗深色即可。底栏是横向扫描区，视线不会纵向移动。
 *
 * ⚠ 缓存命中率收进 tooltip（星辰定稿）：底栏只留「轮次 / token /
 * 工具错误 / 耗时」四个常看项。缓存命中率是**实现效率**指标，
 * 排查时要看，日常扫读时不需要占底栏一个位置。
 */
export function buildMetrics(timeline: TaskTimeline | null): MetricItem[] {
  if (timeline === null) return []
  const cacheNote =
    timeline.cacheRate !== null
      ? `　缓存命中 ${Math.round(timeline.cacheRate * 100)}%（${timeline.totalCached}）`
      : ''
  const items: MetricItem[] = [
    {
      key: 'rounds',
      label: '轮次',
      value: String(timeline.rounds.length),
      title: 'LLM 调用轮数(与 sigma 观测层 RoundView 同口径)',
    },
    {
      key: 'tokens',
      label: 'token',
      value: `${formatTokens(timeline.totalPrompt)}↑ ${formatTokens(timeline.totalCompletion)}↓`,
      title: `入 / 出 token 累计　prompt ${timeline.totalPrompt} · completion ${timeline.totalCompletion}${cacheNote}`,
    },
  ]
  items.push({
    key: 'toolErrors',
    label: '工具错误',
    value: String(timeline.toolErrors),
    // 只有非零才是错误；0 是常态，不必标红——满屏红色会让人麻木。
    tone: timeline.toolErrors > 0 ? 'bad' : 'plain',
    title: '本会话工具调用失败次数（重试也计入）',
  })
  if (timeline.wallSeconds !== null) {
    items.push({
      key: 'wall',
      label: '耗时',
      value: `${formatWall(timeline.wallSeconds)}${timeline.wallApprox ? '≈' : ''}`,
      title: `墙钟耗时 ≈ ${timeline.wallSeconds.toFixed(1)}s${timeline.wallApprox ? '（估算值，非精确计时）' : ''}`,
    })
  }
  return items
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
