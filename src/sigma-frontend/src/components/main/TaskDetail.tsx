import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, Send } from 'lucide-react'
import {
  apiClient,
  STATUS_META,
  type Task,
  type TaskEvent,
  type TaskTimeline,
} from '../../api'
import { formatDateTime } from '../../lib/time'
import { useAppActions, useAppState } from '../../store/appStore'

/** 十六进制颜色 → rgba 字符串(用于状态徽章底色的透明度) */
function hexToRgba(hex: string, alpha: number): string {
  const body: string = hex.replace('#', '')
  const r: number = parseInt(body.slice(0, 2), 16)
  const g: number = parseInt(body.slice(2, 4), 16)
  const b: number = parseInt(body.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
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
 * 会话工作区:**这里只出现对话**。
 * - 气泡流(用户右 / 助手左,工具调用 chip 行)占满主区,自动滚底;
 * - 顶部只有返回 + 标题 + 状态小徽章 + 会话 id;
 * - 运行指标(轮数/token/缓存/耗时)以**小字**放在底部输入框提示行的右端——
 *   不再占独立看板(星辰 2026-10-01:图二信息小字放图三位置)。
 *
 * 数据源 = 真实 sigma(会话 JSONL / build_timeline),与 CLI 同源。
 */
export default function TaskDetail({ task }: TaskDetailProps): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [timeline, setTimeline] = useState<TaskTimeline | null>(null)
  const [composeText, setComposeText] = useState<string>('')
  const [sending, setSending] = useState<boolean>(false)
  const composeReady: boolean = composeText.trim().length > 0 && !sending
  const threadRef = useRef<HTMLDivElement | null>(null)

  const isRunning: boolean = task.status === 'running'
  const canCompose: boolean = task.status !== 'archived' && !(sending || isRunning)

  // 运行时间线(底部小字指标的数据源;可选方法,mock 未实现时静默跳过)。
  // ⚠ 必须以 `apiClient.xxx?.()` 形式调用:取出来再调会丢 this(实测白屏)。
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
    return (): void => {
      cancelled = true
    }
    // 依赖 updatedAt:每轮执行完(载荷替换)自动重取,底部小字指标跟着翻新。
  }, [task.id, task.updatedAt])

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

  // 底部小字指标(图二信息 → 图三位置):有 timeline 才显示,没有不占位。
  const metrics: string | null =
    timeline === null
      ? null
      : [
          `轮数 ${timeline.rounds.length}`,
          `token ${timeline.totalPrompt}/${timeline.totalCompletion}`,
          timeline.cacheRate !== null ? `缓存 ${Math.round(timeline.cacheRate * 100)}%` : null,
          `工具错误 ${timeline.toolErrors}`,
          timeline.wallSeconds !== null
            ? `耗时 ${timeline.wallSeconds.toFixed(1)}s${timeline.wallApprox ? '≈' : ''}`
            : null,
        ]
          .filter((part): part is string => part !== null)
          .join(' · ')

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
          <span className="task-detail__crumb">{projectName} / {task.title}</span>
          <span
            className="status-badge"
            style={{
              backgroundColor: hexToRgba(statusMeta.color, 0.12),
              color: statusMeta.color,
            }}
          >
            <span className="status-badge__dot" style={{ backgroundColor: statusMeta.color }} />
            <span className="status-badge__text">{statusMeta.label}</span>
          </span>
          <span className="task-detail__meta-id" title={task.id}>
            {task.id}
          </span>
        </div>

        {/* ============ 气泡流(主区只有对话) ============ */}
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
              正在执行,回复流式输出中…
            </div>
          ) : null}
        </div>

        {/* ============ 底部输入(指标以小字在提示行右端) ============ */}
        <div className="wb-composer">
          <textarea
            className="wb-composer__input"
            value={composeText}
            rows={2}
            disabled={!canCompose}
            placeholder={
              canCompose
                ? '输入提示词,Enter 发送(流式输出,Shift+Enter 换行)'
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
            {metrics !== null ? (
              <span className="wb-composer__metrics" title="本会话运行指标(观测层 timeline)">
                {metrics}
              </span>
            ) : null}
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
