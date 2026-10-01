import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, Send } from 'lucide-react'
import {
  apiClient,
  STATUS_META,
  type Task,
  type TaskEvent,
  type TaskTimeline,
  type WorkbenchBudget,
} from '../../api'
import { formatDateTime, formatRelative } from '../../lib/time'
import { useAppActions, useAppState } from '../../store/appStore'

/** 十六进制颜色 → rgba 字符串(用于状态徽章底色的透明度) */
function hexToRgba(hex: string, alpha: number): string {
  const body: string = hex.replace('#', '')
  const r: number = parseInt(body.slice(0, 2), 16)
  const g: number = parseInt(body.slice(2, 4), 16)
  const b: number = parseInt(body.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** 看板一枚指标卡:label + 大数字 + 说明。 */
function BoardCard({
  label,
  value,
  sub,
  title,
}: {
  label: string
  value: string
  sub?: string
  title?: string
}): JSX.Element {
  return (
    <div className="wb-card" title={title}>
      <span className="wb-card__label">{label}</span>
      <span className="wb-card__value">{value}</span>
      {sub !== undefined && sub !== '' ? <span className="wb-card__sub">{sub}</span> : null}
    </div>
  )
}

/** 气泡流里的工具调用 chip。 */
function ToolChip({ event }: { event: TaskEvent }): JSX.Element {
  return (
    <div className="wb-toolrow" title={event.at}>
      <span className="wb-toolrow__dot" />
      <span className="wb-toolrow__name">{event.tool ?? 'tool'}</span>
      <span className="wb-toolrow__args">{event.text}</span>
    </div>
  )
}

interface TaskDetailProps {
  /** 当前展示的任务(由 MainArea 从 state.tasks 解析后传入) */
  task: Task
}

/**
 * 会话工作区(以 sigma 现有功能为核心):
 * - **看板行** = 上下文信息:运行指标(观测层 timeline)+ 常驻区预算构成(D4,表即常量);
 * - **气泡流** = 会话回放(用户右 / 助手左,工具调用 chip 行);
 * - **底部输入** = 提示词框,Enter 同步执行一轮(执行中禁用)。
 *
 * 数据源 = 真实 sigma(会话 JSONL / build_timeline / resident_caps),与 CLI 同源。
 */
export default function TaskDetail({ task }: TaskDetailProps): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [timeline, setTimeline] = useState<TaskTimeline | null>(null)
  const [budget, setBudget] = useState<WorkbenchBudget | null>(null)
  const [composeText, setComposeText] = useState<string>('')
  const [sending, setSending] = useState<boolean>(false)
  const composeReady: boolean = composeText.trim().length > 0 && !sending
  const threadRef = useRef<HTMLDivElement | null>(null)

  const isRunning: boolean = task.status === 'running'
  const canCompose: boolean = task.status !== 'archived' && !(sending || isRunning)

  // 运行时间线 + 常驻预算(看板数据源;可选方法,mock 未实现时静默跳过)。
  // ⚠ 必须以 `apiClient.xxx?.()` 形式调用:取出来再调(`const f = apiClient.f; f()`)
  // 会丢 this——HttpSigmaClient 的方法依赖 this.request,实测炸出白屏。
  useEffect((): (() => void) => {
    let cancelled = false
    if (apiClient.getTaskTimeline !== undefined) {
      apiClient
        .getTaskTimeline(task.id)
        .then((report: TaskTimeline | null): void => {
          if (!cancelled) setTimeline(report)
        })
        .catch((): void => {
          if (!cancelled) setTimeline(null)
        })
    }
    if (apiClient.getBudget !== undefined && budget === null) {
      apiClient
        .getBudget()
        .then((info: WorkbenchBudget): void => {
          if (!cancelled) setBudget(info)
        })
        .catch((): void => undefined)
    }
    return (): void => {
      cancelled = true
    }
  }, [task.id, budget])

  // 新回放到达(轮次变化)时滚到底部。
  useEffect((): void => {
    const node = threadRef.current
    if (node !== null) {
      node.scrollTop = node.scrollHeight
    }
  }, [task.events.length, task.status])

  const project = state.projects.find((p): boolean => p.id === task.projectId)
  const projectName: string = project?.name ?? '未知工作区'
  const statusMeta = STATUS_META[task.status]
  const engineLabel: string = [task.model, task.effort].filter((part) => part !== '').join(' · ')

  const handleSendCompose = (): void => {
    if (!composeReady) return
    const text = composeText
    setSending(true)
    void (async (): Promise<void> => {
      try {
        await actions.sendMessage(task.id, text)
        setComposeText('')
      } finally {
        setSending(false)
      }
    })()
  }

  const cachePercent: string =
    timeline !== null && timeline.cacheRate !== null
      ? `${Math.round(timeline.cacheRate * 100)}%`
      : '—'
  const wallLabel: string =
    timeline !== null && timeline.wallSeconds !== null
      ? `${timeline.wallSeconds.toFixed(1)}s${timeline.wallApprox ? ' ≈' : ''}`
      : '—'

  return (
    <div className="task-detail">
      <div className="task-detail__inner">
        <div className="task-detail__topbar">
          <button
            type="button"
            className="task-detail__back"
            aria-label="返回首页"
            onClick={(): void => actions.goHome()}
          >
            <ArrowLeft size={18} />
          </button>
          <span className="task-detail__crumb">
            {projectName} / {task.title}
          </span>
          <span className="task-detail__meta-id" title={task.id}>
            {task.id}
          </span>
        </div>

        {/* ============ 看板:上下文信息(运行指标 + 常驻预算) ============ */}
        <div className="wb-board">
          <div className="wb-card">
            <span className="wb-card__label">状态</span>
            <span
              className="status-badge"
              style={{
                backgroundColor: hexToRgba(statusMeta.color, 0.12),
                color: statusMeta.color,
              }}
            >
              <span
                className="status-badge__dot"
                style={{ backgroundColor: statusMeta.color }}
              />
              <span className="status-badge__text">{statusMeta.label}</span>
            </span>
            <span className="wb-card__sub">{engineLabel !== '' ? engineLabel : '模型未知'}</span>
          </div>
          <BoardCard
            label="轮数"
            value={timeline !== null ? String(timeline.rounds.length) : '—'}
            sub={`工具错误 ${timeline?.toolErrors ?? '—'} · 截断 ${timeline?.truncated ?? '—'}`}
            title="LLM 调用轮次(与 AgentLoop 的 round 同口径)"
          />
          <BoardCard
            label="token(入/出)"
            value={
              timeline !== null ? `${timeline.totalPrompt} / ${timeline.totalCompletion}` : '—'
            }
            sub={`缓存命中 ${cachePercent}`}
            title="prompt / completion(provider 返回的真实用量);缓存命中 = cached/prompt"
          />
          <BoardCard
            label="耗时"
            value={wallLabel}
            sub={`更新于 ${formatRelative(task.updatedAt)} · ${formatDateTime(task.createdAt)}`}
            title="端到端墙钟(≈ = 由消息时间戳差近似)"
          />
          <div className="wb-card wb-card--budget" title="常驻区预算 D4 v3(表即常量):系统提示词+工具 schema+AGENTS.md+技能索引+记忆索引+repo map,逐字节稳定是 prompt cache 的前提">
            <span className="wb-card__label">
              上下文构成 · 常驻区预算 {budget?.residentBudgetTokens ?? 5750} tok
            </span>
            {budget !== null ? (
              <div className="wb-budget">
                {Object.entries(budget.caps).map(([name, cap]) => {
                  const measured = budget.measured[name]
                  const percent: number = Math.min(100, Math.round((measured ?? 0) / cap * 100))
                  return (
                    <div key={name} className="wb-budget__row" title={`${name}:实测 ${measured ?? '—'} / cap ${cap}`}>
                      <span className="wb-budget__name">{name}</span>
                      <span className="wb-budget__bar">
                        <span className="wb-budget__fill" style={{ width: `${percent}%` }} />
                      </span>
                      <span className="wb-budget__num">{measured ?? '—'}/{cap}</span>
                    </div>
                  )
                })}
              </div>
            ) : (
              <span className="wb-card__sub">预算表加载中…</span>
            )}
          </div>
        </div>

        {/* ============ 气泡流 ============ */}
        <div className="wb-thread" ref={threadRef}>
          {task.events.length === 0 ? (
            <p className="wb-thread__empty">
              {task.status === 'draft'
                ? '在下方输入第一条消息,会话开始执行'
                : '暂无回放'}
            </p>
          ) : (
            task.events.map((event: TaskEvent): JSX.Element => {
              if (event.kind === 'tool_call') {
                return <ToolChip key={event.id} event={event} />
              }
              if (event.kind === 'note') {
                return (
                  <div key={event.id} className="wb-bubble wb-bubble--error">
                    {event.text}
                  </div>
                )
              }
              if (event.role === 'user') {
                return (
                  <div key={event.id} className="wb-row wb-row--user">
                    <div className="wb-bubble wb-bubble--user" title={formatDateTime(event.at)}>
                      {event.text}
                    </div>
                  </div>
                )
              }
              if (event.role === 'assistant') {
                return (
                  <div key={event.id} className="wb-row wb-row--assistant">
                    <div className="wb-bubble wb-bubble--assistant" title={formatDateTime(event.at)}>
                      {event.text}
                    </div>
                  </div>
                )
              }
              return (
                <div key={event.id} className="wb-toolrow">
                  <span className="wb-toolrow__args">{event.text}</span>
                </div>
              )
            })
          )}
          {isRunning || sending ? (
            <div className="wb-typing">
              <span className="wb-typing__dot" />
              <span className="wb-typing__dot" />
              <span className="wb-typing__dot" />
              正在执行(sigma 同步一轮,完成后回放自动更新)
            </div>
          ) : null}
        </div>

        {/* ============ 底部输入 ============ */}
        <div className="wb-composer">
          <textarea
            className="wb-composer__input"
            value={composeText}
            rows={2}
            disabled={!canCompose}
            placeholder={
              canCompose
                ? '输入提示词,Enter 发送(同步执行一轮,Shift+Enter 换行)'
                : '正在执行,请等当前轮完成…'
            }
            onChange={(event: React.ChangeEvent<HTMLTextAreaElement>): void =>
              setComposeText(event.target.value)
            }
            onKeyDown={(event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                handleSendCompose()
              }
            }}
          />
          <div className="wb-composer__foot">
            <span className="wb-composer__hint">
              工具调用自动放行(L1 路径沙箱 + L2 影子快照在岗)· 审批确认 / 打断待接入 σ-server
              M2 · 数据与 CLI 同一份(~/.sigma/sessions)
            </span>
            <button
              type="button"
              className="wb-composer__send"
              disabled={!canCompose}
              onClick={handleSendCompose}
              aria-label="发送"
            >
              <Send size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
