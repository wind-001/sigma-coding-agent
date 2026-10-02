import type { TaskDeltaPiece, TaskEvent } from '../api'

/**
 * 把流式**结构化块**折叠成直播事件序列——与回放气泡流同一套 kind
 * (message/thinking/tool_call),渲染层零特判(星辰 2026-10-02
 * "参考成熟 agent:文本块/思考块/工具调用块分开"):
 * - 连续 text / thinking 增量各自聚成一个块(跨工具调用必然分块);
 * - tool_start 开一个"未完成"工具行,同名 tool_end 就地补状态;
 * - id 统一 `__live` 前缀,渲染层据此做弱化显示,轮结束后被回放
 *   事件整体替换(正式沉淀)。
 */
export function piecesToLiveEvents(pieces: TaskDeltaPiece[]): TaskEvent[] {
  const events: TaskEvent[] = []
  let text = ''
  let thinking = ''
  let n = 0
  const flush = (): void => {
    if (text !== '') {
      events.push({ id: `__live-t${n++}`, kind: 'message', role: 'assistant', text, at: '' })
      text = ''
    }
    if (thinking !== '') {
      events.push({ id: `__live-k${n++}`, kind: 'thinking', text: thinking, at: '' })
      thinking = ''
    }
  }
  for (const piece of pieces) {
    if (piece.k === 'text') {
      if (thinking !== '') flush()
      text += piece.t ?? ''
    } else if (piece.k === 'thinking') {
      if (text !== '') flush()
      thinking += piece.t ?? ''
    } else if (piece.k === 'user') {
      // 注入即时上屏(星辰 2026-10-02):「立即」/排队的用户消息,一注入
      // 就以用户气泡出现在直播区,不等轮结束的最终载荷。
      flush()
      events.push({
        id: `__live-u${n++}`,
        kind: 'message',
        role: 'user',
        text: piece.t ?? '',
        at: '',
      })
    } else if (piece.k === 'note') {
      // 系统条(任务清单提醒/子任务回报/停滞提醒)——与回放的 note 同款。
      flush()
      events.push({ id: `__live-n${n++}`, kind: 'note', text: piece.t ?? '', at: '' })
    } else if (piece.k === 'tool_start') {
      flush()
      events.push({
        id: `__live-s${n++}`,
        kind: 'tool_call',
        role: 'assistant',
        tool: piece.name ?? 'tool',
        text: piece.args ?? '',
        at: '',
      })
    } else if (piece.k === 'tool_end') {
      flush()
      for (let i = events.length - 1; i >= 0; i -= 1) {
        const candidate = events[i]
        if (
          candidate.kind === 'tool_call' &&
          candidate.tool === piece.name &&
          candidate.status === undefined
        ) {
          events[i] = { ...candidate, status: piece.ok ? 'ok' : 'error' }
          break
        }
      }
    }
  }
  flush()
  return events
}
