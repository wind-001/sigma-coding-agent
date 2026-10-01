import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowUp, Send, X } from 'lucide-react'
import {
  ACCESS_OPTIONS,
  apiClient,
  STATUS_META,
  type Task,
  type TaskEvent,
  type TaskQueues,
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

/** 工具调用行(图二语义):工具名 + 结果状态 + 耗时,与正文/思考有区分度。 */
function ToolChip({ event }: { event: TaskEvent }): JSX.Element {
  const statusLabel =
    event.status === 'ok' ? '已完成' : event.status === 'error' ? '失败' : '未完成'
  const statusClass =
    event.status === 'ok'
      ? 'wb-toolrow__status--ok'
      : event.status === 'error'
        ? 'wb-toolrow__status--bad'
        : 'wb-toolrow__status--pending'
  const duration =
    event.durationMs !== null && event.durationMs !== undefined
      ? ` · ${(event.durationMs / 1000).toFixed(1)}s`
      : ''
  return (
    <div className="wb-toolrow" title={event.at}>
      <span className="wb-toolrow__dot" />
      <span className="wb-toolrow__name">{event.tool ?? 'tool'}</span>
      <span className={`wb-toolrow__status ${statusClass}`}>{statusLabel}</span>
      <span className="wb-toolrow__args">{event.text}</span>
      <span className="wb-toolrow__dur">{duration}</span>
    </div>
  )
}

/** 思考行:模型思考增量,弱化展示(图二的"思考 · 持续了 N 秒"位)。 */
function ThinkingRow({ event }: { event: TaskEvent }): JSX.Element {
  return (
    <div className="wb-thinking" title={event.text}>
      <span className="wb-thinking__label">思考</span>
      <span className="wb-thinking__text">{event.text}</span>
    </div>
  )
}

interface TaskDetailProps {
  /** 当前展示的任务(由 MainArea 从 state.tasks 解析后传入) */
  task: Task
}

/**
 * 会话工作区:这里只出现对话。
 * - 气泡流(用户右 / 助手左 / 工具行带状态耗时 / 思考弱化行)占满主区;
 * - 执行中:**排队输入**(默认 follow-up,完成后自动执行)+ 队列管理
 *   (「立即」= 升级为 steering 打断注入,图一)+ **审批卡**(变更前确认,图三);
 * - 运行指标以小字放输入框提示行右端。
 * 数据源 = 真实 sigma(会话 JSONL / build_timeline / steer & follow-up 队列)。
 */
export default function TaskDetail({ task }: TaskDetailProps): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [timeline, setTimeline] = useState<TaskTimeline | null>(null)
  const [queues, setQueues] = useState<TaskQueues | null>(null)
  const [composeText, setComposeText] = useState<string>('')
  const [sending, setSending] = useState<boolean>(false)
  const composeReady: boolean = composeText.trim().length > 0 && !sending
  const threadRef = useRef<HTMLDivElement | null>(null)

  const isRunning: boolean = task.status === 'running'
  const canCompose: boolean = task.status !== 'archived' && !sending

  // 时间线(底部小字指标)+ 队列/审批快照(执行中管理)。
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
  }, [task.id, task.updatedAt])

  useEffect((): void | (() => void) => {
    const poller = apiClient.getTaskQueues
    if (!isRunning || poller === undefined) {
      setQueues(null)
      return undefined
    }
    let cancelled = false
    const poll = (): void => {
      poller(task.id)
        .then((info: TaskQueues): void => {
          if (!cancelled) setQueues(info)
        })
        .catch((): void => undefined)
    }
    poll()
    const timer = setInterval(poll, 1000)
    return (): void => {
      cancelled = true
      clearInterval(timer)
    }
  }, [task.id, isRunning, task.updatedAt])

  // 新回放到达(轮次变化)时滚到底部。
  useEffect((): void => {
    const node = threadRef.current
    if (node !== null) {
      node.scrollTop = node.scrollHeight
    }
  }, [task.events.length, task.status, queues?.followups.length, queues?.approvals.length])

  const project = state.projects.find((p): boolean => p.id === task.projectId)
  const projectName: string = project?.name ?? '未知工作区'
  const statusMeta = STATUS_META[task.status]
  const accessLabel: string =
    ACCESS_OPTIONS.find((option): boolean => option.id === task.access)?.label ?? ''

  const handleSendCompose = (): void => {
    if (!composeReady) return
    const text = composeText
    setComposeText('')
    if (isRunning) {
      // 执行中:默认排队(图一),完成后自动执行;「立即」在队列行上。
      void actions.queueMessage(task.id, text)
      return
    }
    setSending(true)
    void (async (): Promise<void> => {
      try {
        await actions.sendMessage(task.id, text)
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

  const queuedItems: { kind: 'followup' | 'steering'; index: number; text: string }[] = [
    ...(queues?.steering ?? []).map(
      (text: string, index: number): { kind: 'followup' | 'steering'; index: number; text: string } => ({
        kind: 'steering',
        index,
        text,
      }),
    ),
    ...(queues?.followups ?? []).map(
      (text: string, index: number): { kind: 'followup' | 'steering'; index: number; text: string } => ({
        kind: 'followup',
        index,
        text,
      }),
    ),
  ]
  const pendingApprovals = queues?.approvals ?? []

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
          {accessLabel !== '' ? (
            <span className="task-detail__access">{accessLabel}</span>
          ) : null}
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
              if (event.kind === 'thinking') {
                return <ThinkingRow key={event.id} event={event} />
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

        {/* ============ 审批卡(变更前确认环,图三) ============ */}
        {pendingApprovals.length > 0 ? (
          <div className="wb-approvals">
            {pendingApprovals.map((item) => (
              <div key={item.id} className="wb-approval">
                <div className="wb-approval__main">
                  <span className="wb-approval__tool">{item.tool}</span>
                  <span className="wb-approval__summary" title={item.summary}>{item.summary}</span>
                </div>
                <div className="wb-approval__actions">
                  <button
                    type="button"
                    className="wb-approval__btn wb-approval__btn--deny"
                    onClick={(): void => void actions.decideApproval(task.id, item.id, 'deny')}
                  >
                    拒绝
                  </button>
                  <button
                    type="button"
                    className="wb-approval__btn wb-approval__btn--approve"
                    onClick={(): void => void actions.decideApproval(task.id, item.id, 'approve')}
                  >
                    批准
                  </button>
                </div>
              </div>
            ))}
          </div>
        ) : null}

        {/* ============ 排队区(图一:默认排队,「立即」打断注入) ============ */}
        {isRunning && queuedItems.length > 0 ? (
          <div className="wb-queue">
            {queuedItems.map((item) => (
              <div key={`${item.kind}-${item.index}`} className="wb-queue__row">
                {item.kind === 'steering' ? (
                  <span className="wb-queue__tag">已注入</span>
                ) : (
                  <>
                    <button
                      type="button"
                      className="wb-queue__now"
                      title="立即打断注入(下一轮模型调用前生效)"
                      onClick={(): void => {
                        void actions.steerTask(task.id, item.text)
                        void actions.removeQueued(task.id, 'followup', item.index)
                      }}
                    >
                      <ArrowUp size={13} />
                      立即
                    </button>
                    <button
                      type="button"
                      className="wb-queue__del"
                      aria-label="删除排队项"
                      onClick={(): void => void actions.removeQueued(task.id, 'followup', item.index)}
                    >
                      <X size={13} />
                    </button>
                  </>
                )}
                <span className="wb-queue__text">{item.text}</span>
              </div>
            ))}
          </div>
        ) : null}

        {/* ============ 底部输入(指标小字在提示行右端) ============ */}
        <div className="wb-composer">
          <textarea
            className="wb-composer__input"
            value={composeText}
            rows={2}
            disabled={task.status === 'archived'}
            placeholder={
              canCompose
                ? isRunning
                  ? '继续输入以排队后续修改(Enter 排队,完成后自动执行)'
                  : '输入提示词,Enter 发送(流式输出,Shift+Enter 换行)'
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
              工具调用受权限模式约束(默认完全访问)· 审批确认 / 打断 / 排队经真实 sigma
              队列 · 数据与 CLI 同一份(~/.sigma/sessions)
            </span>
            {metrics !== null ? (
              <span className="wb-composer__metrics" title="本会话运行指标(观测层 timeline)">
                {metrics}
              </span>
            ) : null}
            <button
              type="button"
              className="wb-composer__send"
              disabled={!composeReady && !isRunning}
              onClick={handleSendCompose}
              aria-label={isRunning ? '排队' : '发送'}
            >
              <Send size={15} />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
