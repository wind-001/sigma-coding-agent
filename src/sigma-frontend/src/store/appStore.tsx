import { createContext, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from 'react'
import { apiClient, STATUS_META, type Automation, type ModelInfo, type Plugin, type Project, type Task, type TaskStatus } from '../api'

export type SidebarView = 'projects' | 'groups'
export type StatusFilter = 'all' | 'active' | 'completed'
export type OverlayKind = 'automations' | 'plugins' | 'help' | null

export interface AppState {
  ready: boolean
  projects: Project[]
  tasks: Task[]
  automations: Automation[]
  plugins: Plugin[]
  models: ModelInfo[]
  /** 输入卡当前目标项目 */
  activeProjectId: string
  selectedTaskId: string | null
  view: 'home' | 'task'
  sidebarView: SidebarView
  statusFilter: StatusFilter
  draft: string
  composerAccess: string
  composerModel: string
  composerEffort: string
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
  sidebarView: 'projects',
  statusFilter: 'all',
  draft: '',
  composerAccess: 'full',
  composerModel: 'GLM-5.3-Flash',
  composerEffort: '最高',
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
  newTaskDraft(): void
  goHome(): void
  selectTask(taskId: string): void
  submitDraft(): Promise<void>
  setTaskStatus(taskId: string, status: TaskStatus): Promise<void>
  deleteTaskById(taskId: string): Promise<void>
  /** 在指定项目下立即创建一个新会话(draft)并打开 */
  createSessionInProject(projectId: string): Promise<void>
  /** 发送会话首条消息:写入描述、标题取首行(原为「新会话」时)并开始执行 */
  sendFirstMessage(taskId: string, text: string): Promise<void>
  /** 向已有会话追加一条消息并同步执行一轮(草稿首条 / 已完成续聊同一条路) */
  sendMessage(taskId: string, text: string): Promise<void>
  setSidebarView(view: SidebarView): void
  setStatusFilter(filter: StatusFilter): void
  setActiveProjectId(projectId: string): void
  setComposerOpt(patch: { access?: string; model?: string; effort?: string }): void
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

  useEffect(() => {
    void (async () => {
      try {
        const [projects, tasks, automations, plugins, models] = await Promise.all([
          apiClient.listProjects(),
          apiClient.listTasks(),
          apiClient.listAutomations(),
          apiClient.listPlugins(),
          apiClient.listModels(),
        ])
        dispatch({ type: 'loaded', projects, tasks, automations, plugins, models })
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
    })()
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
    /** 最小执行环:追加消息 → 同步跑一轮 → 载荷替换(乐观置 running)。 */
    const send = async (taskId: string, text: string): Promise<string> => {
      const loader = apiClient.addTaskMessage
      if (loader === undefined) {
        throw new Error('当前后端不支持执行(桥接服务过旧或处于 mock 模式)')
      }
      const trimmed: string = text.trim()
      if (trimmed === '') return '消息为空'
      const existing = stateRef.current.tasks.find((t) => t.id === taskId)
      if (existing !== undefined) {
        dispatch({ type: 'taskReplaced', task: { ...existing, status: 'running' } })
      }
      showToast('正在执行(执行完自动更新)…')
      const final = await loader(taskId, trimmed)
      dispatch({ type: 'taskReplaced', task: final })
      return final.status === 'completed' ? '本轮执行完成' : `本轮结束:${final.status}`
    }
    return {
      setDraft(value: string): void {
        patch({ draft: value })
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
          })
          dispatch({ type: 'taskAdded', task: created })
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
      setSidebarView(view: SidebarView): void {
        patch({ sidebarView: view })
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
        if (p.model !== undefined) {
          const model = s.models.find((m) => m.name === p.model)
          const effort = next.composerEffort ?? s.composerEffort
          if (model !== undefined && !model.efforts.includes(effort)) {
            next.composerEffort = model.efforts[model.efforts.length - 1]
          }
        }
        patch(next)
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
