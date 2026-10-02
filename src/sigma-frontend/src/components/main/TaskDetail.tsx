import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowUp, ChevronDown, Pencil, Send, ShieldCheck, Square, X } from 'lucide-react'
import {
  ACCESS_OPTIONS,
  apiClient,
  STATUS_META,
  type Task,
  type TaskEvent,
  type TaskQueues,
  type TaskTimeline,
} from '../../api'
import { buildMetrics, wallSummary } from '../../lib/metrics'
import { formatDateTime } from '../../lib/time'
import { useAppActions, useAppState } from '../../store/appStore'
import Dropdown from './Dropdown'
import FullAccessWarning from './FullAccessWarning'
import RichText from './RichText'

/** 十六进制颜色 → rgba 字符串(用于状态徽章底色的透明度) */
function hexToRgba(hex: string, alpha: number): string {
  const body: string = hex.replace('#', '')
  const r: number = parseInt(body.slice(0, 2), 16)
  const g: number = parseInt(body.slice(2, 4), 16)
  const b: number = parseInt(body.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** 工具调用行(图二语义):工具名 + 结果状态 + 耗时,与正文/思考有区分度。
 * 点击展开/收起完整调用参数(默认单行截断,星辰 2026-10-02 折叠诉求)。 */
function ToolChip({ event }: { event: TaskEvent }): JSX.Element {
  const [open, setOpen] = useState<boolean>(false)
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
    <button
      type="button"
      className={`wb-toolrow${open ? ' wb-toolrow--open' : ''}`}
      title={open ? '点击收起' : '点击展开完整调用'}
      onClick={(): void => setOpen((prev) => !prev)}
    >
      <span className="wb-toolrow__dot" />
      <span className="wb-toolrow__name">{event.tool ?? 'tool'}</span>
      <span className={`wb-toolrow__status ${statusClass}`}>{statusLabel}</span>
      <span className="wb-toolrow__args">{event.text}</span>
      <span className="wb-toolrow__dur">{duration}</span>
    </button>
  )
}

/** 思考行:模型思考增量,弱化展示(图二的"思考 · 持续了 N 秒"位)。 */
function ThinkingRow({ event }: { event: TaskEvent }): JSX.Element {
  return (
    <div className="wb-thinking" title={event.text}>
      <span className="wb-thinking__label">深度思考</span>
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
  /** 切到「完全访问」前必须过风险知情确认(共享组件,星辰 2026-10-02)。 */
  const [showFullWarn, setShowFullWarn] = useState<boolean>(false)
  /** 已请求强制中断:协作式停止有窗口期(当前工具跑完才停),期间指示条
   * 要如实显示"等待工具完成"而不是继续假装在流式输出(星辰 2026-10-02)。 */
  const [stopRequested, setStopRequested] = useState<boolean>(false)
  /** 排队项行内编辑(星辰 2026-10-02"排队文本可重新编辑")。 */
  const [editTarget, setEditTarget] = useState<{ kind: 'steering' | 'followup'; index: number } | null>(null)
  const [editText, setEditText] = useState<string>('')

  const handleSaveEdit = (): void => {
    if (editTarget === null || editText.trim() === '') return
    void actions.editQueued(task.id, editTarget.kind, editTarget.index, editText)
    setEditTarget(null)
    setEditText('')
  }
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
    if (!isRunning || apiClient.getTaskQueues === undefined) {
      setQueues(null)
      return undefined
    }
    // 收窄到局部再调:守卫后跨闭包 narrow 不成立;提取安全(原型方法已在
    // HttpSigmaClient 构造器绑定)。
    const poller = apiClient.getTaskQueues
    let cancelled = false
    const poll = (): void => {
      poller(task.id)
        .then((info: TaskQueues): void => {
          if (!cancelled) setQueues(info)
        })
        .catch((): void => undefined)
    }
    poll()
    const timer = setInterval(poll, 500)
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

  // 轮结束(无论成败)后复位中断请求标记,下一轮从头开始。
  useEffect((): void => {
    if (task.status !== 'running') setStopRequested(false)
  }, [task.status])

  // `sending` 只该覆盖「POST 已发出、载荷未到」这一瞬(星辰 2026-10-02
  // 实测"停止和发送按钮全部失效")。send() 内含 deltas 轮询直到**整轮结束**,
  // 此前若让 sending 一直为 true:
  //   - 停止按钮 disabled={sending} → 整轮执行期点不动(实测 166 轮/3.7 小时
  //     全程无效,用户唯一的中断手段被焊死);
  //   - 发送按钮 composeReady 含 !sending → 运行中无法排队。
  // 载荷一到(task.status 变 running)就交棒给 isRunning —— 按钮启用与否
  // 该由"后端真的在跑"决定,而不是由"本地 promise 还没落地"决定。
  useEffect((): void => {
    if (task.status === 'running') setSending(false)
  }, [task.status])

  const project = state.projects.find((p): boolean => p.id === task.projectId)
  const projectName: string = project?.name ?? '未知工作区'
  const statusMeta = STATUS_META[task.status]
  // 权限档位:空 = 未显式设置,执行链的默认即「完全访问」(如实显示)。
  const accessLabel: string =
    ACCESS_OPTIONS.find((option): boolean => option.id === task.access)?.label ?? '完全访问'
  const isFullAccess: boolean = accessLabel === '完全访问'

  const handleAccessSelect = (label: string): void => {
    const option = ACCESS_OPTIONS.find((o): boolean => o.label === label)
    if (option === undefined || option.label === accessLabel) return
    if (option.id === 'full' && !isFullAccess) {
      // 启用完全访问前必须过风险知情确认(与输入卡同一规则)
      setShowFullWarn(true)
      return
    }
    void actions.setTaskAccess(task.id, option.id)
  }

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
  // 结构化指标 + 口径见 lib/metrics.ts(抽出来是为了与 endcap 同口径、可单测)。
  const metricItems = buildMetrics(timeline)

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
          {/* 权限模式显示与切换统一收在下方输入区左下角(星辰 2026-10-02),
              顶栏不再重复提供入口。 */}
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
              if (event.kind === 'note') {
                // 系统注入(如 todo 防跑偏提醒):不是用户说的话,灰条呈现
                return (
                  <div key={event.id} className="wb-sysnote">
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
                const live = event.id.startsWith('__live')
                return (
                  <div key={event.id} className="wb-row wb-row--assistant">
                    <div
                      className={`wb-bubble wb-bubble--assistant${live ? ' wb-bubble--live' : ''}`}
                      title={
                        live
                          ? '流式直播:分块弱化显示,完成后以正式气泡沉淀'
                          : formatDateTime(event.at)
                      }
                    >
                      <RichText text={event.text} />
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
          {/* 收尾标记(星辰 2026-10-02:完成后戛然而止太突兀)。
              完成给绿色收尾行带指标摘要;失败给红色行指路。 */}
          {task.status === 'completed' ? (
            <div className="wb-endcap wb-endcap--ok">
              <span className="wb-endcap__rule" />
              <span className="wb-endcap__text">
                ✓ 本轮执行完成
                {timeline !== null ? ` · ${wallSummary(timeline)}` : ''}
                ,可继续输入追问
              </span>
            </div>
          ) : null}
          {task.status === 'failed' ? (
            <div className="wb-endcap wb-endcap--bad">
              <span className="wb-endcap__rule" />
              <span className="wb-endcap__text">
                ✕ 本轮未跑完{task.statusDetail ? `:${task.statusDetail}` : ',可重试或换个模型/档位再试'}
                ,直接输入"继续"可接续执行
              </span>
            </div>
          ) : null}
          {isRunning || sending ? (
            <div className="wb-typing">
              <span className="wb-typing__dot" />
              <span className="wb-typing__dot" />
              <span className="wb-typing__dot" />
              {stopRequested
                ? '已请求中断,等待当前工具完成后停止…'
                : task.events.some(
                      (event) => event.kind === 'thinking' && event.id.startsWith('__live'),
                    )
                  ? '深度思考中…'
                  : '正在执行,回复流式输出中…'}
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
            {queuedItems.map((item) => {
              const editingThis =
                editTarget !== null &&
                editTarget.kind === item.kind &&
                editTarget.index === item.index
              return (
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
                        // 乐观移除(星辰 2026-10-02"点立即后队列项要消失"):
                        // 不等 1s 队列轮询,本地立即摘掉这一条(按 index,
                        // 与后端 drop_queued 同一下标);注入的消息经 deltas
                        // 的 user 块即时上屏。
                        setQueues((prev) =>
                          prev === null
                            ? prev
                            : {
                                ...prev,
                                followups: prev.followups.filter(
                                  (_: string, i: number): boolean => i !== item.index,
                                ),
                              },
                        )
                        void actions.steerTask(task.id, item.text)
                        void actions.removeQueued(task.id, 'followup', item.index)
                      }}
                    >
                      <ArrowUp size={13} />
                      立即
                    </button>
                    <button
                      type="button"
                      className="wb-queue__edit-btn"
                      aria-label="编辑排队文本"
                      title="编辑排队文本"
                      onClick={(): void => {
                        setEditTarget({ kind: item.kind, index: item.index })
                        setEditText(item.text)
                      }}
                    >
                      <Pencil size={13} aria-hidden="true" />
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
                {editingThis ? (
                  <div className="wb-queue__edit">
                    <textarea
                      className="wb-queue__edit-input"
                      value={editText}
                      rows={2}
                      autoFocus
                      onChange={(event: React.ChangeEvent<HTMLTextAreaElement>): void =>
                        setEditText(event.target.value)
                      }
                      onKeyDown={(event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault()
                          handleSaveEdit()
                        }
                        if (event.key === 'Escape') setEditTarget(null)
                      }}
                    />
                    <div className="wb-queue__edit-actions">
                      <button
                        type="button"
                        className="wb-queue__edit-save"
                        disabled={editText.trim() === ''}
                        onClick={handleSaveEdit}
                      >
                        保存
                      </button>
                      <button
                        type="button"
                        className="wb-queue__edit-cancel"
                        onClick={(): void => setEditTarget(null)}
                      >
                        取消
                      </button>
                    </div>
                  </div>
                ) : (
                  <span className="wb-queue__text">{item.text}</span>
                )}
              </div>
              )
            })}
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
            {/* 权限模式(human-in-the-loop):左下角随时切换,热替换审批闸,
                下一声工具调用生效;切到「完全访问」先过风险知情确认。 */}
            <Dropdown
              trigger={
                <span
                  className={`wb-composer__access${isFullAccess ? ' wb-composer__access--full' : ''}`}
                  title="权限模式:点击切换,立即生效"
                >
                  <ShieldCheck size={13} aria-hidden="true" />
                  {accessLabel}
                  <ChevronDown size={12} aria-hidden="true" />
                </span>
              }
              items={ACCESS_OPTIONS.map((option): string => option.label)}
              value={accessLabel}
              onSelect={handleAccessSelect}
              menuWidth={128}
            />
            {metricItems.length > 0 ? (
              <div className="wb-metrics-bar" title="本会话运行指标(观测层 timeline)">
                {metricItems.map((item) => (
                  <span key={item.key} className="wb-metric" title={item.title}>
                    <span className="wb-metric__label">{item.label}</span>
                    <span
                      className={`wb-metric__value${item.tone === 'bad' ? ' wb-metric__value--bad' : ''}`}
                    >
                      {item.value}
                    </span>
                  </span>
                ))}
              </div>
            ) : null}
            {isRunning ? (
              <button
                type="button"
                className="wb-composer__stop"
                disabled={stopRequested}
                onClick={(): void => {
                  setStopRequested(true)
                  void actions.stopTask(task.id)
                }}
                aria-label="强制中断"
                title={
                  stopRequested
                    ? '已请求中断,等待当前工具完成'
                    : '强制中断:当前工具完成后在块边界停止,状态已保存可续跑'
                }
              >
                <Square size={14} />
              </button>
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

        {/* 完全访问的严厉警告(共享组件,勾选知情才可启用) */}
        {showFullWarn ? (
          <FullAccessWarning
            onCancel={(): void => setShowFullWarn(false)}
            onEnable={(): void => {
              setShowFullWarn(false)
              void actions.setTaskAccess(task.id, 'full')
            }}
          />
        ) : null}
      </div>
    </div>
  )
}
