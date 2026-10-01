const MIN = 60_000
const HOUR = 3_600_000
const DAY = 86_400_000

/** 相对时间:刚刚 / N分 / N小时 / N天(与参考截图口径一致) */
export function formatRelative(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < MIN) return '刚刚'
  if (diff < HOUR) return `${Math.floor(diff / MIN)}分`
  if (diff < DAY) return `${Math.floor(diff / HOUR)}小时`
  return `${Math.floor(diff / DAY)}天`
}

/** 绝对时间:2026-09-28 20:31 */
export function formatDateTime(iso: string): string {
  const d = new Date(iso)
  const pad = (x: number): string => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
