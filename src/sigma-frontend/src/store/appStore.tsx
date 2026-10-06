import { createContext, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from 'react'
import { apiClient, ACCESS_OPTIONS, STATUS_META, type Automation, type ModelInfo, type ModelSaveInput, type Plugin, type Project, type Task, type TaskDeltaPiece, type TaskEvent, type TaskStatus } from '../api'
import { piecesToLiveEvents } from '../lib/liveBlocks'
import { requestNotificationPermission } from '../notify'

export type StatusFilter = 'all' | 'active' | 'completed'
export type OverlayKind =
  | 'automations'
  | 'plugins'
  | 'help'
  | 'fs-picker'
  | 'fs-file'
  | 'model-settings'
  | 'trash'
  | 'phone-access'
  | null

export interface AppState {
  ready: boolean
  projects: Project[]
  tasks: Task[]
  automations: Automation[]
  plugins: Plugin[]
  models: ModelInfo[]
  /** 当前激活的工作区(切换语义):侧栏过滤、新任务目标、composer 显示共用这一个源 */
  activeProjectId: string
  selectedTaskId: string | null
  /** home=首页 / task=会话详情 / mail=自动化·邮件功能详情(主区展示) */
  view: 'home' | 'task' | 'mail'
  statusFilter: StatusFilter
  draft: string
  composerAccess: string
  composerModel: string
  /** 档位取值由所选模型条目声明;条目无档位时为 ''(前端隐藏下拉) */
  composerEffort: string
  /**
   * 联网搜索开关（首页 Composer 的暂存态，默认关）。
   *
   * ⚠ 与 ``Task.web`` 同一语义、不同环节：这里是「**还没建任务**时用户
   * 的选择」，随 createTask 提交；建好后走 ``setTaskWeb(taskId)`` 热切。
   * 少了这个字段，首页就**没有**联网开关可用——用户无法在建任务前表达意图，
   * 只能建完任务再去会话页切（多数人不会想到）。
   */
  composerWeb: boolean
  paletteOpen: boolean
  overlay: OverlayKind
  /** 递增触发 Composer 聚焦 */
  focusComposerSignal: number
  toast: string | null
}

const initialState: AppState = {
  ready: false,
  projects: [],
  tasks: [],
  automations: [],
  plugins: [],
  models: [],
  activeProjectId: 'proj-sigma',
  selectedTaskId: null,
  view: 'home',
  statusFilter: 'all',
  draft: '',
  composerAccess: 'full',
  composerModel: '',
  composerEffort: '',
  composerWeb: false,
  paletteOpen: false,
  overlay: null,
  focusComposerSignal: 0,
  toast: null,
}

type Action =
  | { type: 'loaded'; projects: Project[]; tasks: Task[]; automations: Automation[]; plugins: Plugin[]; models: ModelInfo[] }
  | { type: 'patch'; patch: Partial<AppState> }
  | { type: 'taskReplaced'; task: Task }
  | { type: 'taskAdded'; task: Task }
  | { type: 'taskRemoved'; taskId: string }
  | { type: 'automationReplaced'; automation: Automation }
  | { type: 'automationAdded'; automation: Automation }
  | { type: 'pluginReplaced'; plugin: Plugin }

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case 'loaded':
      return { ...state, ready: true, projects: action.projects, tasks: action.tasks, automations: action.automations, plugins: action.plugins, models: action.models }
    case 'patch':
      return { ...state, ...action.patch }
    case 'taskReplaced':
      return { ...state, tasks: state.tasks.map((t) => (t.id === action.task.id ? action.task : t)) }
    case 'taskAdded':
      return { ...state, tasks: [action.task, ...state.tasks] }
    case 'taskRemoved':
      return { ...state, tasks: state.tasks.filter((t) => t.id !== action.taskId) }
    case 'automationReplaced':
      return { ...state, automations: state.automations.map((a) => (a.id === action.automation.id ? action.automation : a)) }
    case 'automationAdded':
      return { ...state, automations: [...state.automations, action.automation] }
    case 'pluginReplaced':
      return { ...state, plugins: state.plugins.map((p) => (p.id === action.plugin.id ? action.plugin : p)) }
    default:
      return state
  }
}

/** 侧栏状态筛选 → 允许的任务状态集合 */
export function matchStatusFilter(task: Task, filter: StatusFilter): boolean {
  if (filter === 'active') return task.status === 'draft' || task.status === 'running' || task.status === 'waiting_approval'
  if (filter === 'completed') return task.status === 'completed'
  return true
}

export interface AppActions {
  setDraft(value: string): void
  /** 在现有草稿末尾追加一段文本(引用文件路径用),空草稿直接作为开头。 */
  appendToDraft(text: string): void
  newTaskDraft(): void
  goHome(): void
  selectTask(taskId: string): void
  /** 打开自动化·邮件功能详情(主区展示,星辰 2026-10-06:邮件是自动化的
   *  一个功能,面板里单列一条,点击后详情在主区,不塞在侧栏面板里) */
  openMailFeature(): void
  submitDraft(): Promise<void>
  setTaskStatus(taskId: string, status: TaskStatus): Promise<void>
  deleteTaskById(taskId: string): Promise<void>
  /** 批量删除:循环走删除端点,执行中的会话服务端 409,结果 toast 如实汇报 */
  deleteTasksBatch(taskIds: string[]): Promise<void>
  /** 在指定项目下立即创建一个新会话(draft)并打开 */
  createSessionInProject(projectId: string): Promise<void>
  /** 发送会话首条消息:写入描述、标题取首行(原为「新会话」时)并开始执行 */
  sendFirstMessage(taskId: string, text: string): Promise<void>
  /** 向已有会话追加一条消息并同步执行一轮(草稿首条 / 已完成续聊同一条路) */
  sendMessage(taskId: string, text: string): Promise<void>
  /** 执行中排队(follow-up):当前任务完成后自动执行——图一「默认排队」 */
  queueMessage(taskId: string, text: string): Promise<void>
  /** 「立即」:把排队指导升级为 steering(打断注入,下一轮前生效) */
  steerTask(taskId: string, text: string): Promise<void>
  /** 删除排队项 */
  removeQueued(taskId: string, kind: 'steering' | 'followup', index: number): Promise<void>
  /** 改写排队文本(排队项可编辑) */
  editQueued(taskId: string, kind: 'steering' | 'followup', index: number, text: string): Promise<void>
  /** 审批决策(变更前确认环) */
  decideApproval(taskId: string, requestId: string, decision: 'approve' | 'deny'): Promise<void>
  /** ask_user 选择回填:把用户的选择交回执行线程;dismiss=忽略本次 */
  answerQuestion(taskId: string, questionId: string, option: string, dismiss?: boolean): Promise<void>
  /** 权限模式中途切换:热替换审批闸,下一声工具调用生效 */
  setTaskAccess(taskId: string, access: string): Promise<void>
  /** 联网开关(默认关):要重建工具表与 loop,**下次执行生效** */
  setTaskWeb(taskId: string, enabled: boolean): Promise<void>
  /** 外部强制中断:协作式停止,块边界生效,状态可续跑 */
  stopTask(taskId: string): Promise<void>
  /** 导入工作区(选择目录) */
  importWorkspace(repoPath: string, name?: string): Promise<void>
  /** 切换激活的工作区 */
  switchProject(projectId: string): Promise<void>
  /** 移除导入的工作区(主工作区不可移) */
  removeWorkspace(projectId: string): Promise<void>
  /** 重新拉取全部列表(导入工作区/项目变更后) */
  refreshAll(): Promise<void>
  setStatusFilter(filter: StatusFilter): void
  setActiveProjectId(projectId: string): void
  setComposerOpt(patch: {
    access?: string
    model?: string
    effort?: string
    /** 联网开关（首页 Composer 暂存态，随 createTask 提交） */
    web?: boolean
  }): void
  /** 模型设置:新增/更新自定义条目(apiKey 留空 = 保留原值) */
  saveModelConfig(input: ModelSaveInput): Promise<void>
  /** 模型设置:移除自定义条目 */
  removeModelConfig(id: string): Promise<void>
  togglePalette(): void
  closePalette(): void
  setOverlay(overlay: OverlayKind): void
  focusComposer(): void
  toggleAutomation(automationId: string): Promise<void>
  createAutomation(input: { name: string; schedule: string }): Promise<void>
  setPluginInstalledById(pluginId: string, installed: boolean): Promise<void>
}

const StateContext = createContext<AppState | null>(null)
const ActionsContext = createContext<AppActions | null>(null)

export function AppProvider({ children }: { children: ReactNode }): JSX.Element {
  const [state, dispatch] = useReducer(reducer, initialState)
  const stateRef = useRef<AppState>(state)
  stateRef.current = state
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const actionsRef = useRef<AppActions | null>(null)

  useEffect(() => {
    void actionsRef.current?.refreshAll()
  }, [])

  const actions = useMemo<AppActions>(() => {
    const patch = (p: Partial<AppState>): void => {
      dispatch({ type: 'patch', patch: p })
    }
    const showToast = (message: string): void => {
      if (toastTimer.current !== null) clearTimeout(toastTimer.current)
      patch({ toast: message })
      toastTimer.current = setTimeout(() => patch({ toast: null }), 2600)
    }
    const runOrToast = async (work: () => Promise<string>): Promise<void> => {
      try {
        showToast(await work())
      } catch (error) {
        showToast(error instanceof Error ? error.message : String(error))
      }
    }
    /** 最小执行环(流式):发消息启动一轮 → 轮询 deltas 取**结构化块** →
     * 折叠成分块直播事件(文本/思考/工具,参考成熟 agent)→ 完成后取最终
     * 载荷替换。⚠ 以 `apiClient.xxx(...)` 方法调用形式执行——取出来调会
     * 丢 this(实测白屏)。 */
    const send = async (taskId: string, text: string): Promise<string> => {
      // 发消息必然来自点击/回车 = 合法的用户手势:系统通知的授权请求
      // 挂在这里(浏览器要求手势内发起;未授权时静默跳过,不影响主路径)。
      requestNotificationPermission()
      if (apiClient.addTaskMessage === undefined) {
        throw new Error('当前后端不支持执行(桥接服务过旧或处于 mock 模式)')
      }
      const trimmed: string = text.trim()
      if (trimmed === '') return '消息为空'
      const existing = stateRef.current.tasks.find((t) => t.id === taskId)
      const existingEvents: TaskEvent[] = existing?.events ?? []
      const userBubble: TaskEvent = {
        id: '__user-pending',
        kind: 'message',
        role: 'user',
        text: trimmed,
        at: '',
      }
      // 第一时间渲染(星辰 2026-10-02):提问气泡在 **按 Enter 的瞬间** 上屏,
      // 不等任何网络往返。submitDraft 已预插则跳过(id 相同去重)。
      // ⚠ 任务已在 running → 本条会走**排队**分支(后端 queued)——不插
      // 乐观气泡:它的显示位是排队区,注入后经 deltas 的 user 块上屏;
      // 两处都插会重复两条一模一样的提问(实测)。
      const willQueue = existing?.status === 'running'
      const alreadyOptimistic = existingEvents.some(
        (e) => e.id === '__user-pending' && e.text === trimmed,
      )
      if (existing !== undefined && !alreadyOptimistic && !willQueue) {
        dispatch({
          type: 'taskReplaced',
          task: { ...existing, status: 'running', events: [...existingEvents, userBubble] },
        })
      }
      let started: Task
      try {
        // model/effort 随消息提交:TaskDetail 切换器写的是全局 composer 状态,
        // 这里把它带到任务记录,服务端会话层发现变化就重建(切模型下轮生效)。
        const s0 = stateRef.current
        started = await apiClient.addTaskMessage(taskId, trimmed, {
          model: s0.composerModel,
          effort: s0.composerEffort,
        })
      } catch (error) {
        // 发送失败要回滚乐观气泡,否则界面停在一个假 running 态
        const landed = await apiClient.getTask(taskId).catch((): null => null)
        if (landed !== null) {
          dispatch({ type: 'taskReplaced', task: landed })
        } else if (existing !== undefined) {
          dispatch({
            type: 'taskReplaced',
            task: { ...existing, status: existing.status, events: existingEvents },
          })
        }
        throw error
      }
      // POST 载荷里还没有刚发的提问(要等执行链持久化)——乐观气泡补上,
      // 与落盘真身靠完成时整体替换去重。
      const startedEvents: TaskEvent[] = started.events ?? []
      const hasPendingUser = startedEvents.some(
        (e) => e.role === 'user' && e.text === trimmed,
      )
      const eventsWithUser: TaskEvent[] =
        hasPendingUser || willQueue
          ? startedEvents
          : [...startedEvents, userBubble]
      dispatch({ type: 'taskReplaced', task: { ...started, events: eventsWithUser } })
      // 斜杠命令分支(2026-10-03):服务端拦截执行、不开轮——把输出作为
      // **单条**多行 note 上屏后直接返回。不走 deltas 轮询与最终替换(那会
      // 把命令输出冲掉;命令本就不产生轮,也不会进会话树)。
      // ⚠ 状态必须取 started.status(服务端真值),不能用 current——
      //   stateRef 在 dispatch 后有渲染延迟,命令分支若回写 current,
      //   会把乐观阶段的 running 原样落库,任务永远"正在执行"(实测卡死)。
      if ((started as unknown as { command?: boolean }).command === true) {
        const output = (started as unknown as { output?: string[] }).output ?? []
        const baseEvents: TaskEvent[] = started.events ?? []
        const hasUser = baseEvents.some((e) => e.role === 'user' && e.text === trimmed)
        const note: TaskEvent = {
          id: `__cmd-${Date.now()}`,
          kind: 'note',
          text: output.join('\n'),
          at: '',
        }
        dispatch({
          type: 'taskReplaced',
          task: {
            ...started,
            events: [...(hasUser ? baseEvents : [...baseEvents, userBubble]), note],
          },
        })
        return '命令已执行'
      }
      // 排队分支(星辰 2026-10-02):任务运行中发消息不再 409,后端直接进
      // followup 队列(queued=true)。当前轮的流式轮询已在别处进行,这里
      // 不再重复轮询——排队区(队列轮询)可见,本轮结束后自动接跑。
      if ((started as unknown as { queued?: boolean }).queued === true) {
        return '已排队:当前轮结束后自动执行,可在队列区管理'
      }
      if (apiClient.getTaskDeltas !== undefined) {
        let offset = 0
        let livePieces: TaskDeltaPiece[] = []
        let streamError: string | null = null
        for (;;) {
          await new Promise<void>((resolve): void => {
            setTimeout(resolve, 100)  // 本地回环很便宜,细粒度才丝滑(星辰 2026-10-02)
          })
          const d = await apiClient.getTaskDeltas(taskId, offset)
          if (d.error !== null && d.error !== undefined) {
            streamError = d.error
            break
          }
          if (d.pieces.length > 0) {
            livePieces = livePieces.concat(d.pieces)
            offset = d.seq
            const current = stateRef.current.tasks.find((t) => t.id === taskId)
            if (current !== undefined) {
              const settled = current.events.filter((e) => !e.id.startsWith('__live'))
              dispatch({
                type: 'taskReplaced',
                task: {
                  ...current,
                  events: [...settled, ...piecesToLiveEvents(livePieces)],
                },
              })
            }
          }
          if (!d.running) break
        }
        if (streamError !== null) {
          // 出错也要取回真实状态再抛:否则任务永远停在 running(执行指示不会消失)。
          const landed = await apiClient.getTask(taskId)
          if (landed !== null) {
            dispatch({ type: 'taskReplaced', task: landed })
          }
          throw new Error(streamError)
        }
      }
      const final = await apiClient.getTask(taskId)
      if (final === null) throw new Error('执行结束但会话不存在(未执行的草稿已随重启消失?)')
      dispatch({ type: 'taskReplaced', task: final })
      return final.status === 'completed' ? '本轮执行完成' : `本轮结束:${final.status}`
    }
    return {
      setDraft(value: string): void {
        patch({ draft: value })
      },
      appendToDraft(text: string): void {
        const base = stateRef.current.draft
        patch({ draft: base === '' ? text : `${base}\n${text}` })
      },
      newTaskDraft(): void {
        patch({ view: 'home', selectedTaskId: null, draft: '', focusComposerSignal: stateRef.current.focusComposerSignal + 1 })
      },
      goHome(): void {
        patch({ view: 'home', selectedTaskId: null })
      },
      selectTask(taskId: string): void {
        patch({ view: 'task', selectedTaskId: taskId })
      },
      openMailFeature(): void {
        patch({ view: 'mail', selectedTaskId: null })
      },
      async submitDraft(): Promise<void> {
        const s = stateRef.current
        const text = s.draft.trim()
        if (text === '') {
          showToast('请输入任务描述')
          return
        }
        await runOrToast(async () => {
          const title = text.split('\n')[0].slice(0, 40)
          const created = await apiClient.createTask({
            projectId: s.activeProjectId,
            title,
            description: text,
            access: s.composerAccess,
            model: s.composerModel,
            effort: s.composerEffort,
            // 首页 Composer 的联网选择随建任务一起提交（此处还没有 taskId，
            // 无法走 setTaskWeb 热切）。漏这一行 ⇒ 首页开关怎么点都没用。
            web: s.composerWeb,
          })
          // 提问气泡第一时间上屏:创建瞬间预插(send 里按 id 去重不会重复)
          dispatch({
            type: 'taskAdded',
            task: {
              ...created,
              events: [
                { id: '__user-pending', kind: 'message', role: 'user', text, at: '' },
              ],
            },
          })
          patch({ view: 'task', selectedTaskId: created.id, draft: '' })
          // 最小执行环:首条消息直接开跑(同步一轮,完成即替换载荷)。
          return await send(created.id, text)
        })
      },
      async setTaskStatus(taskId: string, status: TaskStatus): Promise<void> {
        await runOrToast(async () => {
          const task = await apiClient.updateTask(taskId, { status })
          dispatch({ type: 'taskReplaced', task })
          return `已${STATUS_META[status].label}`
        })
      },
      async deleteTaskById(taskId: string): Promise<void> {
        await runOrToast(async () => {
          await apiClient.deleteTask(taskId)
          dispatch({ type: 'taskRemoved', taskId })
          if (stateRef.current.selectedTaskId === taskId) {
            patch({ view: 'home', selectedTaskId: null })
          }
          return '任务已删除'
        })
      },
      async deleteTasksBatch(taskIds: string[]): Promise<void> {
        await runOrToast(async () => {
          // 先读快照再动 store:dispatch 之后 stateRef 有渲染延迟(命令分支踩过同款坑)
          const selectedId = stateRef.current.selectedTaskId
          let ok = 0
          const removed: string[] = []
          const failed: string[] = []
          for (const taskId of taskIds) {
            try {
              await apiClient.deleteTask(taskId)
              ok += 1
              removed.push(taskId)
            } catch {
              // 执行中的会话服务端 409,收集后如实汇报,本地列表不动它
              failed.push(taskId)
            }
          }
          for (const taskId of removed) {
            dispatch({ type: 'taskRemoved', taskId })
          }
          if (selectedId !== null && taskIds.includes(selectedId) && removed.includes(selectedId)) {
            patch({ view: 'home', selectedTaskId: null })
          }
          return failed.length === 0
            ? `已删除 ${ok} 个会话`
            : `已删除 ${ok} 个,${failed.length} 个失败(执行中的不能删)`
        })
      },
      async createSessionInProject(projectId: string): Promise<void> {
        await runOrToast(async () => {
          const s = stateRef.current
          const task = await apiClient.createTask({
            projectId,
            title: '新会话',
            description: '',
            access: s.composerAccess,
            model: s.composerModel,
            effort: s.composerEffort,
            // 首页 Composer 的联网选择随建任务一起提交（此处还没有 taskId，
            // 无法走 setTaskWeb 热切）。漏这一行 ⇒ 首页开关怎么点都没用。
            web: s.composerWeb,
          })
          dispatch({ type: 'taskAdded', task })
          patch({ view: 'task', selectedTaskId: task.id })
          return '会话已创建'
        })
      },
      async sendFirstMessage(taskId: string, text: string): Promise<void> {
        // 与续聊同一条执行路径;标题/描述由服务端按首条消息事实重建。
        await runOrToast((): Promise<string> => send(taskId, text))
      },
      async sendMessage(taskId: string, text: string): Promise<void> {
        await runOrToast((): Promise<string> => send(taskId, text))
      },
      async queueMessage(taskId: string, text: string): Promise<void> {
        // 可选方法先收窄到局部常量再进异步闭包(TS 跨闭包 narrow 不成立);
        // 提取调用安全:原型方法已在 HttpSigmaClient 构造器绑定。
        const queueTask = apiClient.queueTask
        if (queueTask === undefined) {
          throw new Error('当前后端不支持排队(桥接服务过旧或处于 mock 模式)')
        }
        await runOrToast(async (): Promise<string> => {
          await queueTask(taskId, text.trim())
          return '已排队:当前任务完成后自动执行(点「立即」可打断注入)'
        })
      },
      async steerTask(taskId: string, text: string): Promise<void> {
        const steerTask = apiClient.steerTask
        if (steerTask === undefined) {
          throw new Error('当前后端不支持打断注入(桥接服务过旧或处于 mock 模式)')
        }
        await runOrToast(async (): Promise<string> => {
          await steerTask(taskId, text.trim())
          return '已注入:下一轮模型调用前生效'
        })
      },
      async removeQueued(taskId: string, kind: 'steering' | 'followup', index: number): Promise<void> {
        const removeQueued = apiClient.removeQueued
        if (removeQueued === undefined) return
        await runOrToast(async (): Promise<string> => {
          await removeQueued(taskId, kind, index)
          return '已移除排队项'
        })
      },
      async editQueued(taskId: string, kind: 'steering' | 'followup', index: number, text: string): Promise<void> {
        const editQueued = apiClient.editQueued
        if (editQueued === undefined) return
        await runOrToast(async (): Promise<string> => {
          await editQueued(taskId, kind, index, text.trim())
          return '排队项已更新'
        })
      },
      async decideApproval(taskId: string, requestId: string, decision: 'approve' | 'deny'): Promise<void> {
        const decideApproval = apiClient.decideApproval
        if (decideApproval === undefined) return
        await runOrToast(async (): Promise<string> => {
          await decideApproval(taskId, requestId, decision)
          return decision === 'approve' ? '已批准' : '已拒绝'
        })
      },
      async answerQuestion(
        taskId: string,
        questionId: string,
        option: string,
        dismiss: boolean = false,
      ): Promise<void> {
        // 可选方法先收窄到局部再进异步闭包(TS 跨闭包 narrow 不成立);
        // 提取调用安全:原型方法已在 HttpSigmaClient 构造器绑定。
        const answerQuestion = apiClient.answerQuestion
        if (answerQuestion === undefined) {
          throw new Error('当前后端不支持交互问答(桥接服务过旧或处于 mock 模式)')
        }
        await runOrToast(async (): Promise<string> => {
          await answerQuestion(taskId, questionId, option, dismiss)
          if (dismiss) return '已忽略:模型将自行决定后续路径'
          return '已选择:按你的选择继续执行'
        })
      },
      async setTaskAccess(taskId: string, access: string): Promise<void> {
        const setter = apiClient.setTaskAccess
        if (setter === undefined) return
        await runOrToast(async (): Promise<string> => {
          await setter(taskId, access)
          // 回读真实载荷替换(记录里的 access 合并规则在服务端)
          const landed = await apiClient.getTask(taskId)
          if (landed !== null) {
            dispatch({ type: 'taskReplaced', task: landed })
          }
          const label = ACCESS_OPTIONS.find((option) => option.id === access)?.label ?? access
          return `权限已切换为「${label}」,下一声工具调用生效`
        })
      },
      async setTaskWeb(taskId: string, enabled: boolean): Promise<void> {
        const setter = apiClient.setTaskWeb
        if (setter === undefined) return
        await runOrToast(async (): Promise<string> => {
          const result = await setter(taskId, enabled)
          const landed = await apiClient.getTask(taskId)
          if (landed !== null) {
            dispatch({ type: 'taskReplaced', task: landed })
          }
          // ⚠ 拼文案只用**服务端给的字段**，不在前端另算一套"能不能开"。
          // 前端算的那套必然与服务端的判据漂移（key 解析只有那边做），
          // 症状就是"按钮显示已开启、agent 说没有工具"——本批次修的就是这个。
          const parts: string[] = [result.note]
          if (result.pendingDropped > 0) {
            // 排队项被丢了必须说:否则界面上"排队的消息"静默消失(元纪律:丢弃必须可见)
            parts.push(`已丢弃 ${result.pendingDropped} 条排队项`)
          } else if (!result.applied) {
            parts.push('本轮跑完后生效')
          }
          return parts.join(' · ')
        })
      },
      async stopTask(taskId: string): Promise<void> {
        const stopper = apiClient.stopTask
        if (stopper === undefined) return
        await runOrToast(async (): Promise<string> => {
          const result = await stopper(taskId)
          await actionsRef.current?.refreshAll()
          return result.interrupted
            ? '已请求中断:当前工具完成后在块边界停止(状态已保存,可续跑)'
            : '后端已无在跑的轮,滞留的执行状态已纠正'
        })
      },
      async importWorkspace(repoPath: string, name?: string): Promise<void> {
        const createProject = apiClient.createProject
        if (createProject === undefined) return
        await runOrToast(async (): Promise<string> => {
          const created = await createProject({ repoPath: repoPath.trim(), name: name ?? '' })
          const activateProject = apiClient.activateProject
          if (activateProject !== undefined) {
            await activateProject(created.id)
          }
          await actionsRef.current?.refreshAll()
          return `工作区「${created.name}」已导入并切换`
        })
      },
      /** 切换激活的工作区(侧栏过滤、新任务目标随之切换)。 */
      async switchProject(projectId: string): Promise<void> {
        const activateProject = apiClient.activateProject
        if (activateProject === undefined) {
          patch({ activeProjectId: projectId })
          return
        }
        await runOrToast(async (): Promise<string> => {
          await activateProject(projectId)
          await actionsRef.current?.refreshAll()
          const name = stateRef.current.projects.find((p) => p.id === projectId)?.name ?? ''
          return `已切换到「${name}」`
        })
      },
      async removeWorkspace(projectId: string): Promise<void> {
        const removeProject = apiClient.removeProject
        if (removeProject === undefined) return
        await runOrToast(async (): Promise<string> => {
          await removeProject(projectId)
          await actionsRef.current?.refreshAll()
          return '已移除工作区(其会话回放归入主工作区)'
        })
      },
      async refreshAll(): Promise<void> {
        try {
          const [projects, tasks, automations, plugins, models, workspace] = await Promise.all([
            apiClient.listProjects(),
            apiClient.listTasks(),
            apiClient.listAutomations(),
            apiClient.listPlugins(),
            apiClient.listModels(),
            apiClient.getWorkspace !== undefined
              ? apiClient.getWorkspace()
              : Promise.resolve({ activeId: 'proj-sigma' }),
          ])
          dispatch({
            type: 'loaded',
            projects,
            tasks,
            automations,
            plugins,
            models,
          })
          dispatch({ type: 'patch', patch: { activeProjectId: workspace.activeId } })
          // 模型/档位下拉数据来自服务端:当前选择不在列表里(首载/列表变化)
          // 时落回——模型取第一项,档位取该条目声明里的末档。
          const modelNames = models.map((m) => m.name)
          if (!modelNames.includes(stateRef.current.composerModel)) {
            patch({ composerModel: modelNames[0] ?? '' })
          }
          const entry = models.find((m) => m.name === (stateRef.current.composerModel || modelNames[0]))
          const efforts = entry?.efforts ?? []
          if (!efforts.includes(stateRef.current.composerEffort)) {
            patch({ composerEffort: efforts[efforts.length - 1] ?? '' })
          }
        } catch (error) {
          // 桥接服务没起(或 σ-server 未部署):空数据进场 + 常驻提示,
          // 界面保持可用(空态各视图都有说明),不能停在"加载中"。
          dispatch({
            type: 'loaded',
            projects: [],
            tasks: [],
            automations: [],
            plugins: [],
            models: [],
          })
          dispatch({
            type: 'patch',
            patch: {
              toast: `无法连接 sigma 桥接服务(默认 127.0.0.1:8301):${
                error instanceof Error ? error.message : String(error)
              }`,
            },
          })
        }
      },
      setStatusFilter(filter: StatusFilter): void {
        patch({ statusFilter: filter })
      },
      setActiveProjectId(projectId: string): void {
        patch({ activeProjectId: projectId })
      },
      setComposerOpt(p): void {
        const s = stateRef.current
        const next: Partial<AppState> = {}
        if (p.access !== undefined) next.composerAccess = p.access
        if (p.model !== undefined) next.composerModel = p.model
        if (p.effort !== undefined) next.composerEffort = p.effort
        if (p.web !== undefined) next.composerWeb = p.web
        // 换模型时档位跟随:新条目声明里没有当前档位 → 落到末档(最高档语义)。
        if (p.model !== undefined) {
          const entry = s.models.find((m) => m.name === p.model)
          const currentEffort = next.composerEffort ?? s.composerEffort
          if (entry !== undefined && !entry.efforts.includes(currentEffort)) {
            next.composerEffort = entry.efforts[entry.efforts.length - 1] ?? ''
          }
        }
        patch(next)
      },
      async saveModelConfig(input: ModelSaveInput): Promise<void> {
        const saver = apiClient.saveModel
        if (saver === undefined) return
        await runOrToast(async (): Promise<string> => {
          await saver(input)
          await actionsRef.current?.refreshAll()
          return `模型「${input.name}」已保存`
        })
      },
      async removeModelConfig(id: string): Promise<void> {
        const remover = apiClient.removeModel
        if (remover === undefined) return
        await runOrToast(async (): Promise<string> => {
          await remover(id)
          await actionsRef.current?.refreshAll()
          return '模型条目已移除'
        })
      },
      togglePalette(): void {
        patch({ paletteOpen: !stateRef.current.paletteOpen })
      },
      closePalette(): void {
        patch({ paletteOpen: false })
      },
      setOverlay(overlay: OverlayKind): void {
        patch({ overlay })
      },
      focusComposer(): void {
        patch({ focusComposerSignal: stateRef.current.focusComposerSignal + 1 })
      },
      async toggleAutomation(automationId: string): Promise<void> {
        const current = stateRef.current.automations.find((a) => a.id === automationId)
        if (current === undefined) return
        await runOrToast(async () => {
          const automation = await apiClient.setAutomationEnabled(automationId, !current.enabled)
          dispatch({ type: 'automationReplaced', automation })
          return `「${automation.name}」已${automation.enabled ? '启用' : '停用'}`
        })
      },
      async createAutomation(input: { name: string; schedule: string }): Promise<void> {
        await runOrToast(async () => {
          const automation = await apiClient.createAutomation(input)
          dispatch({ type: 'automationAdded', automation })
          return `自动化「${input.name}」已创建(未启用)`
        })
      },
      async setPluginInstalledById(pluginId: string, installed: boolean): Promise<void> {
        await runOrToast(async () => {
          const plugin = await apiClient.setPluginInstalled(pluginId, installed)
          dispatch({ type: 'pluginReplaced', plugin })
          return `「${plugin.name}」已${installed ? '安装' : '卸载'}`
        })
      },
    }
  }, [])
  actionsRef.current = actions

  return (
    <StateContext.Provider value={state}>
      <ActionsContext.Provider value={actions}>{children}</ActionsContext.Provider>
    </StateContext.Provider>
  )
}

export function useAppState(): AppState {
  const state = useContext(StateContext)
  if (state === null) {
    throw new Error('useAppState 必须在 AppProvider 内使用')
  }
  return state
}

export function useAppActions(): AppActions {
  const actions = useContext(ActionsContext)
  if (actions === null) {
    throw new Error('useAppActions 必须在 AppProvider 内使用')
  }
  return actions
}
