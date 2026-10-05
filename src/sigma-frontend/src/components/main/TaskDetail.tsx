import { useEffect, useRef, useState } from 'react'
import { Activity, ArrowLeft, ArrowUp, ChevronDown, Database, Gauge, Globe, HelpCircle, Pencil, Send, ShieldCheck, Square, X } from 'lucide-react'
import {
  ACCESS_OPTIONS,
  apiClient,
  STATUS_META,
  WEB_OPTIONS,
  type ModelInfo,
  type Task,
  type TaskEvent,
  type TaskQueues,
  type TaskQuestion,
  type TaskTimeline,
} from '../../api'
import { buildMetricGroups, wallSummary } from '../../lib/metrics'
import { formatDateTime } from '../../lib/time'
import { maybeNotify } from '../../notify'
import { useAppActions, useAppState } from '../../store/appStore'
import Dropdown from './Dropdown'
import FullAccessWarning from './FullAccessWarning'
import RichText from './RichText'
import SlashPalette, { useSlashItems, useSlashNav } from './SlashPalette'

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
/**
 * 深度思考块(2026-10-04 星辰:可折叠+上下可滑动+蓝玻璃):
 * 流式直播中默认展开,轮次结束后自动收起成一行摘要;头部点击随时展开/收起,
 * 展开态正文区限高可滚动。默认展开与否由 live 驱动——收起是"完成态"。
 */
function ThinkingRow({ event, live = false }: { event: TaskEvent; live?: boolean }): JSX.Element {
  const [open, setOpen] = useState<boolean>(live)
  useEffect((): void => {
    // 直播结束 → 自动收起;用户手动展开过就不再抢(以最后一次状态为准会
    // 打架,这里取最简单也最符合"完成即收起"预期的语义)
    if (!live) setOpen(false)
  }, [live])
  return (
    <div className={`wb-thinking${open ? ' wb-thinking--open' : ''}`}>
      <button
        type="button"
        className="wb-thinking__head"
        title={open ? '点击收起' : '点击展开'}
        onClick={(): void => setOpen((prev: boolean) => !prev)}
      >
        <span className="wb-thinking__label">深度思考</span>
        {!open ? <span className="wb-thinking__preview">{event.text}</span> : null}
        <ChevronDown
          size={13}
          className={`wb-thinking__chevron${open ? ' wb-thinking__chevron--open' : ''}`}
          aria-hidden="true"
        />
      </button>
      {open ? <div className="wb-thinking__body">{event.text}</div> : null}
    </div>
  )
}

/**
 * 安全审核卡(审批):头部标明"安全审核"+工具名,摘要单行截断,
 * 完整调用参数可展开(human-in-the-loop 要让人看清楚批的是什么);
 * 决策已发出时按钮禁用,防重复点击,决策落地靠队列轮询摘卡。
 */
function ApprovalCard({
  item,
  deciding,
  onDecide,
}: {
  item: TaskQueues['approvals'][number]
  deciding: boolean
  onDecide: (decision: 'approve' | 'deny') => void
}): JSX.Element {
  const [open, setOpen] = useState<boolean>(false)
  const hasArgs = item.args !== undefined && Object.keys(item.args).length > 0
  return (
    <div className="wb-approval">
      <ShieldCheck size={16} className="wb-approval__icon" aria-hidden="true" />
      <div className="wb-approval__main">
        <div className="wb-approval__head">
          <span className="wb-approval__label">安全审核</span>
          <span className="wb-approval__tool">{item.tool}</span>
        </div>
        <span className="wb-approval__summary" title={item.summary}>{item.summary}</span>
        {hasArgs ? (
          <>
            <button
              type="button"
              className="wb-approval__toggle"
              onClick={(): void => setOpen((prev: boolean) => !prev)}
            >
              <ChevronDown size={12} className={open ? 'wb-approval__chevron--open' : ''} />
              {open ? '收起参数' : '展开完整参数'}
            </button>
            {open ? (
              <pre className="wb-approval__args">{JSON.stringify(item.args, null, 2)}</pre>
            ) : null}
          </>
        ) : null}
      </div>
      <div className="wb-approval__actions">
        <button
          type="button"
          className="wb-approval__btn wb-approval__btn--deny"
          disabled={deciding}
          onClick={(): void => onDecide('deny')}
        >
          拒绝
        </button>
        <button
          type="button"
          className="wb-approval__btn wb-approval__btn--approve"
          disabled={deciding}
          onClick={(): void => onDecide('approve')}
        >
          {deciding ? '已决策…' : '批准'}
        </button>
      </div>
    </div>
  )
}

/**
 * ask_user 问题卡(方向决策交还用户,样式参照市面成熟问询 UI):
 * **两段式防误触**(星辰 2026-10-04)——点选项只是"选中"(圆点填充、边框
 * 高亮),可随时改选;点「确认提交」才真正回填执行线程。自定义文本与
 * 选项互斥:输入文字即改走自由输入,选中项被清掉,反之亦然。
 * 「忽略本次」是显式动作,仍直接生效(告诉模型"用户看到了但不想选")。
 * 多个问题排队时**一次只出一张**(星辰 2026-10-04"一个一个选,避免上下
 * 对齐"),答完由队列轮询顶上下一张;queued=排在其后的数量。
 * 已应答后整卡禁用置灰,由队列轮询摘除。
 */
function QuestionCard({
  item,
  onAnswer,
  onDismiss,
  queued = 0,
}: {
  item: TaskQuestion
  onAnswer: (option: string) => void
  onDismiss: () => void
  queued?: number
}): JSX.Element {
  const [customText, setCustomText] = useState<string>('')
  const [selected, setSelected] = useState<string | null>(null)
  const [answered, setAnswered] = useState<boolean>(false)
  const customReady: boolean = customText.trim() !== ''
  const canConfirm: boolean = !answered && (customReady || selected !== null)

  const confirm = (): void => {
    if (!canConfirm) return
    setAnswered(true)
    if (customReady) {
      onAnswer(customText.trim())
    } else {
      onAnswer(selected as string)
    }
  }
  return (
    <div className={`wb-question${answered ? ' wb-question--answered' : ''}`}>
      <div className="wb-question__head">
        <HelpCircle size={16} className="wb-question__icon" aria-hidden="true" />
        <span className="wb-question__label">需要你决定</span>
        {queued > 0 ? (
          <span className="wb-question__queued" title="答完这张,下一张自动出现">
            还有 {queued} 个问题排队
          </span>
        ) : null}
      </div>
      <div className="wb-question__text">{item.question}</div>
      <div className="wb-question__options">
        {item.options.map((option: string, index: number): JSX.Element => (
          <button
            key={option}
            type="button"
            disabled={answered}
            className={`wb-question__option${item.recommended === index ? ' wb-question__option--recommended' : ''}${selected === option && !customReady ? ' wb-question__option--selected' : ''}`}
            onClick={(): void => {
              setSelected(option)
              setCustomText('')
            }}
          >
            <span className="wb-question__dot" aria-hidden="true" />
            {item.recommended === index ? (
              <span className="wb-question__badge">推荐</span>
            ) : null}
            <span className="wb-question__option-text">{option}</span>
          </button>
        ))}
      </div>
      <div className="wb-question__custom">
        <input
          type="text"
          className="wb-question__custom-input"
          value={customText}
          disabled={answered}
          placeholder="都不合适？直接输入你的想法…"
          onFocus={(): void => setSelected(null)}
          onChange={(e: React.ChangeEvent<HTMLInputElement>): void => {
            setCustomText(e.target.value)
            if (e.target.value.trim() !== '') setSelected(null)
          }}
          onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>): void => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              confirm()
            }
          }}
        />
      </div>
      <div className="wb-question__foot">
        <button
          type="button"
          className="wb-question__ignore"
          disabled={answered}
          onClick={(): void => {
            setAnswered(true)
            onDismiss()
          }}
        >
          忽略本次（由模型自行决定）
        </button>
        <button
          type="button"
          className="wb-question__confirm"
          disabled={!canConfirm}
          onClick={confirm}
        >
          {answered ? '已提交…' : '确认提交'}
        </button>
      </div>
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
  // ===== 斜杠命令面板(2026-10-03):数据源与键盘导航逻辑在 SlashPalette.tsx =====
  const slashItems = useSlashItems()
  const slash = useSlashNav(composeText, setComposeText, slashItems)
  const [sending, setSending] = useState<boolean>(false)
  /** 切到「完全访问」前必须过风险知情确认(共享组件,星辰 2026-10-02)。 */
  const [showFullWarn, setShowFullWarn] = useState<boolean>(false)
  /** 已请求强制中断:协作式停止有窗口期(当前工具跑完才停),期间指示条
   * 要如实显示"等待工具完成"而不是继续假装在流式输出(星辰 2026-10-02)。 */
  const [stopRequested, setStopRequested] = useState<boolean>(false)
  /** 排队项行内编辑(星辰 2026-10-02"排队文本可重新编辑")。 */
  const [editTarget, setEditTarget] = useState<{ kind: 'steering' | 'followup'; index: number } | null>(null)
  const [editText, setEditText] = useState<string>('')
  /** 已发出决策的审批卡(按钮禁用防重复点击);卡片由队列轮询摘除。 */
  const [decidingIds, setDecidingIds] = useState<ReadonlySet<string>>(new Set())

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

  // ===== 系统通知(human-in-the-loop,星辰 2026-10-03 拍板 Web 通知 API):
  // 用户没在看面板(最小化/面板不在最上方)时,审批待确认与任务终态要发
  // 系统通知追人;正看着就只走面板内 UI。变化检测完全架在既有轮询上,
  // 不发任何新请求;判定与去重逻辑都在 notify.ts。 =====
  const prevStatusRef = useRef<Task['status']>(task.status)
  const prevApprovalsRef = useRef<TaskQueues['approvals']>([])
  const prevQuestionsRef = useRef<TaskQuestion[]>([])
  // 切换任务时先对齐基线:上一个任务的状态/审批数不能算成这个任务的变化。
  // (声明在检测 effect 之前,同一轮渲染里基线先重置、检测后执行。)
  useEffect((): void => {
    prevStatusRef.current = task.status
    prevApprovalsRef.current = queues?.approvals ?? []
    prevQuestionsRef.current = queues?.questions ?? []
    // 依赖刻意只有 task.id——状态/队列的变化由下面几个检测 effect 处理
  }, [task.id])
  useEffect((): void => {
    const prev = prevStatusRef.current
    prevStatusRef.current = task.status
    if (prev === task.status) return
    if (task.status === 'completed') {
      maybeNotify(`「${task.title}」已完成`, '任务已执行完成,请返回工作台查看结果。', {
        tag: `sigma-task-${task.id}-done`,
      })
    } else if (task.status === 'failed') {
      maybeNotify(`「${task.title}」失败`, '任务执行失败,请返回工作台查看原因。', {
        tag: `sigma-task-${task.id}-done`,
      })
    } else if (task.status === 'waiting_approval') {
      // 服务端当前不发这个状态(审批期仍是 running,靠 queues 轮询发现),
      // 分支留作状态机演进后的兜底。
      maybeNotify(`「${task.title}」等待审批`, '您有任务需要审批,请返回工作台批准。', {
        tag: `sigma-task-${task.id}-approval`,
      })
    }
  }, [task.status, task.title, task.id])
  useEffect((): void => {
    const current = queues?.approvals ?? []
    const prev = prevApprovalsRef.current
    prevApprovalsRef.current = current
    if (current.length === 0 || current.length <= prev.length) return
    // 通知文案只说"有事等你批"(星辰 2026-10-04):细节(工具名/参数)
    // 回面板看审批卡,通知不当明细单用。
    maybeNotify(`「${task.title}」等待审批`, '您有任务需要审批,请返回工作台批准。', {
      tag: `sigma-task-${task.id}-approval`,
    })
  }, [queues, task.title, task.id])
  useEffect((): void => {
    // ask_user 问题增量:模型把方向决策交还用户了,同样要弹通知追人。
    const current = queues?.questions ?? []
    const prev = prevQuestionsRef.current
    prevQuestionsRef.current = current
    if (current.length === 0 || current.length <= prev.length) return
    maybeNotify(
      `「${task.title}」等待你的选择`,
      '有任务等待您做出选择,请返回工作台处理。',
      { tag: `sigma-task-${task.id}-ask` },
    )
  }, [queues, task.title, task.id])

  // 新回放到达(轮次变化)时滚到底部。
  useEffect((): void => {
    const node = threadRef.current
    if (node !== null) {
      node.scrollTop = node.scrollHeight
    }
  }, [task.events.length, task.status, queues?.followups.length, queues?.approvals.length, queues?.questions.length])

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
  // 联网档位(星辰 2026-10-02,默认关)。tooltip 要说清三件事:
  // 默认关、要 TAVILY_API_KEY、**下次执行才生效**(不是立即)。
  // 少一条就会有人以为"点了没反应"是坏了。
  const webOn: boolean = task.web === true
  const webLabel: string = webOn ? '联网搜索' : '联网关闭'

  const handleWebSelect = (label: string): void => {
    const option = WEB_OPTIONS.find((o): boolean => o.label === label)
    if (option === undefined) return
    const next: boolean = option.id === 'on'
    if (next === webOn) return
    void actions.setTaskWeb(task.id, next)
  }

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
    // /new 本地拦截:建会话是纯 UI 导航(服务端同名命令只是 API 对等面),
    // 直走 store 的 createSessionInProject,建完即切换(2026-10-03)。
    if (text.trim() === '/new') {
      void actions.createSessionInProject(task.projectId)
      return
    }
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
  // 分组结构 + 口径见 lib/metrics.ts(抽出来是为了与 endcap 同口径、可单测)。
  const metricGroups = buildMetricGroups(timeline)

  // 模型 / 档位(参考设计右侧)。口径与首页 Composer 完全一致:
  // 当前选择不在 models 里时回落到首项(与 Composer.tsx 同一写法)。
  const modelValue: string = state.models.some((m: ModelInfo): boolean => m.name === state.composerModel)
    ? state.composerModel
    : state.models[0]?.name ?? state.composerModel
  // 档位取值由所选模型条目自己声明;条目无档位时为空 → 隐藏该下拉。
  const effortOptions: string[] =
    state.models.find((m: ModelInfo): boolean => m.name === modelValue)?.efforts ?? []

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
              // 流式直播判定提到最前:thinking 与 message 两个分支都要用
              const live = event.id.startsWith('__live')
              if (event.kind === 'tool_call') {
                return <ToolChip key={event.id} event={event} />
              }
              if (event.kind === 'thinking') {
                return <ThinkingRow key={event.id} event={event} live={live} />
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

        {/* ============ 审批卡(安全审核)+ ask_user 问题卡 ============ */}
        {pendingApprovals.length > 0 || (queues?.questions.length ?? 0) > 0 ? (
          <div className="wb-approvals">
            {pendingApprovals.map((item) => (
              <ApprovalCard
                key={item.id}
                item={item}
                deciding={decidingIds.has(item.id)}
                onDecide={(decision: 'approve' | 'deny'): void => {
                  // 决策中禁用防重复点击;promise 落定后解除(卡片由队列
                  // 轮询摘除;失败时 toast 已报错,按钮恢复可重试)。
                  setDecidingIds((prev): Set<string> => new Set(prev).add(item.id))
                  void actions
                    .decideApproval(task.id, item.id, decision)
                    .finally((): void => {
                      setDecidingIds((prev): Set<string> => {
                        const next = new Set(prev)
                        next.delete(item.id)
                        return next
                      })
                    })
                }}
              />
            ))}
            {(queues?.questions ?? []).slice(0, 1).map((item: TaskQuestion): JSX.Element => (
              <QuestionCard
                key={item.id}
                item={item}
                queued={(queues?.questions.length ?? 0) - 1}
                onAnswer={(option: string): void =>
                  void actions.answerQuestion(task.id, item.id, option)
                }
                onDismiss={(): void =>
                  void actions.answerQuestion(task.id, item.id, '', true)
                }
              />
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
          <div className="wb-composer__inputwrap">
          {slash.open ? (
            <SlashPalette
              items={slash.filtered}
              activeIndex={slash.index}
              onPick={slash.pick}
              onHover={slash.setIndex}
            />
          ) : null}
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
              // 斜杠面板键盘面前置(2026-10-03):消费返回 true;否则放行发送分支。
              if (slash.handleKeyDown(event, (): void => handleSendCompose())) return
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                handleSendCompose()
              }
            }}
          />
          </div>
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
            {/* 联网搜索(星辰 2026-10-02,默认关)。与权限按钮同一形状、紧挨着放——
                两个都是"这声消息用什么口径跑"的开关,放在一处比分散两处好记。
                刻意用 Dropdown 而不是勾选框:勾选框表达"我勾了=已生效",
                而这个**下次执行才生效**(服务端要重建工具表与 loop),
                下拉式的"切到某一档"不含"已即时生效"的暗示。 */}
            <Dropdown
              trigger={
                <span
                  className={`wb-composer__web${webOn ? ' wb-composer__web--on' : ''}`}
                  title="联网搜索:默认关闭,需 TAVILY_API_KEY。切换在下次执行时生效"
                >
                  <Globe size={13} aria-hidden="true" />
                  {webLabel}
                  <ChevronDown size={12} aria-hidden="true" />
                </span>
              }
              items={WEB_OPTIONS.map((option): string => option.label)}
              value={webLabel}
              onSelect={handleWebSelect}
              menuWidth={112}
            />
            <div className="wb-composer__spacer" aria-hidden="true" />
            {/* 模型 / 档位(参考设计右侧)。复用 Composer 的既有做法:
                composerModel + composerEffort 已在 store,setComposerOpt
                会带着"换模型时档位跟随"的口径,这里不重造。
                与首页 Composer 共用同一份选择——两处是同一个状态。 */}
            {state.models.length > 0 ? (
              <Dropdown
                trigger={
                  <span className="wb-composer__opt" title="本会话使用的模型">
                    <span className="wb-composer__opt-value">{modelValue}</span>
                    <ChevronDown size={12} aria-hidden="true" />
                  </span>
                }
                items={state.models.map((model: ModelInfo): string => model.name)}
                value={modelValue}
                onSelect={(name: string): void => actions.setComposerOpt({ model: name })}
                align="right"
                menuWidth={220}
              />
            ) : null}
            {effortOptions.length > 0 ? (
              <Dropdown
                trigger={
                  <span className="wb-composer__opt" title="思考档位">
                    <Gauge size={13} aria-hidden="true" />
                    <span className="wb-composer__opt-value">{state.composerEffort}</span>
                    <ChevronDown size={12} aria-hidden="true" />
                  </span>
                }
                items={[...effortOptions]}
                value={state.composerEffort}
                onSelect={(effort: string): void => actions.setComposerOpt({ effort })}
                align="right"
                menuWidth={96}
              />
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

        {/* 运行指标:独立成行,在输入框**外面**(参考设计)。
            放框内的教训:框内底栏要塞权限芯片/模型/档位/停止/发送,
            再塞一串指标就变回"报销单"了——挤在边框里,人也分不清
            哪些是控件、哪些是读数。挪到框外,输入框只留控件,
            指标单独一行,分组带图标(参考设计的两组式)。 */}
        {metricGroups.length > 0 ? (
          <div className="wb-runstats" title="本会话运行指标(观测层 timeline)">
            {metricGroups.map((group) => {
              const Icon = group.icon === 'activity' ? Activity : Database
              return (
                <span key={group.key} className="wb-runstats__group">
                  <Icon size={12.5} className="wb-runstats__icon" aria-hidden="true" />
                  {group.items.map((item) => (
                    <span
                      key={item.key}
                      className={`wb-metric${
                        item.tone === 'bad'
                          ? ' wb-metric--bad'
                          : item.tone === 'weak'
                            ? ' wb-metric--weak'
                            : ''
                      }`}
                      title={item.title}
                    >
                      <span className="wb-metric__label">{item.label}</span>{' '}
                      <span className="wb-metric__value">{item.value}</span>
                    </span>
                  ))}
                </span>
              )
            })}
          </div>
        ) : null}

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
