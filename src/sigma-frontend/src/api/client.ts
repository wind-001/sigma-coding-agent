/**
 * sigma 前端 API 契约层。
 * 所有业务数据访问都经过 SigmaApiClient:默认 MockSigmaClient(浏览器 localStorage),
 * 配置 VITE_SIGMA_API_BASE 后切换 HttpSigmaClient 直连 sigma 后端。
 * 后端端点设计见 docs/sigma-backend-api-design.md,与本接口一一对应。
 */

export type TaskStatus =
  | 'draft'
  | 'running'
  | 'waiting_approval'
  | 'completed'
  | 'failed'
  | 'archived'

export interface Project {
  id: string
  name: string
  repoPath: string
  branch: string
  createdAt: string
}

export type TaskEventKind =
  | 'created'
  | 'message'
  | 'thinking'
  | 'tool_call'
  | 'approval'
  | 'status'
  | 'note'

export interface TaskEvent {
  id: string
  kind: TaskEventKind
  text: string
  at: string
  /** message 类事件的角色(user/assistant)——气泡分边;其它 kind 无此字段 */
  role?: string
  /** 工具名(tool_call 事件) */
  tool?: string
  /** 工具执行结果:ok=已完成 / error=失败 / 缺省=未完成(悬空调用,中断) */
  status?: string
  /** 工具耗时(调用到结果的时间戳差,≈ 口径) */
  durationMs?: number | null
}

export interface Task {
  id: string
  projectId: string
  title: string
  description: string
  status: TaskStatus
  /** 审批模式,取值为 ACCESS_OPTIONS 的 id */
  access: string
  model: string
  effort: string
  createdAt: string
  updatedAt: string
  events: TaskEvent[]
}

export interface TaskPatch {
  title?: string
  description?: string
  status?: TaskStatus
  /** 追加一条动态(如会话首条消息);id 与时间戳由服务端补齐 */
  appendEvent?: { kind: TaskEventKind; text: string }
}

export interface CreateTaskInput {
  projectId: string
  title: string
  description: string
  access: string
  model: string
  effort: string
}

export interface TaskFilter {
  projectId?: string
  statuses?: TaskStatus[]
  query?: string
}

export interface Automation {
  id: string
  name: string
  schedule: string
  enabled: boolean
  lastRunAt: string | null
}

export interface Plugin {
  id: string
  name: string
  description: string
  version: string
  installed: boolean
  builtin: boolean
}

export interface ModelInfo {
  id: string
  name: string
  efforts: string[]
}

export interface AccessOption {
  id: string
  label: string
}

export const ACCESS_OPTIONS: readonly AccessOption[] = [
  { id: 'full', label: '完全访问' },
  { id: 'auto', label: '自动审批' },
  { id: 'confirm', label: '手动确认' },
  { id: 'readonly', label: '只读' },
]

export const STATUS_META: Record<TaskStatus, { label: string; color: string }> = {
  draft: { label: '草稿', color: '#9aa0aa' },
  running: { label: '进行中', color: '#3b82f6' },
  waiting_approval: { label: '等待审批', color: '#ff8f1f' },
  completed: { label: '已完成', color: '#22a06b' },
  // 工作台的真实数据源是只读会话回放:failed 覆盖「执行出错」与「被打断
  // (存在悬空工具调用,可断点续跑)」两种事实,标签如实写全。
  failed: { label: '失败/中断', color: '#e5484d' },
  archived: { label: '已归档', color: '#8a8f99' },
}

export const DEFAULT_MODELS: readonly ModelInfo[] = [
  { id: 'glm-5.3-flash', name: 'GLM-5.3-Flash', efforts: ['最低', '中', '高', '最高'] },
  { id: 'glm-5.3', name: 'GLM-5.3', efforts: ['最低', '中', '高', '最高'] },
  { id: 'glm-4.7-plus', name: 'GLM-4.7-Plus', efforts: ['中', '高', '最高'] },
]

export const INBOX_PROJECT_ID = 'proj-inbox'

export interface PingInfo {
  ok: boolean
  mode: 'mock' | 'http'
  version: string
}

/** 一轮 = 一次 LLM 调用 + 它的工具批次(与 sigma 观测层 RoundView 同口径)。 */
export interface TimelineTool {
  name: string
  ok: boolean
  /** 只有 trace 在场才有;null = 由消息时间戳差近似或不可用 */
  durationMs: number | null
}

export interface TimelineRound {
  index: number
  model: string
  promptTokens: number
  completionTokens: number
  cachedTokens: number
  latencyMs: number | null
  latencyApprox: boolean
  ttftMs: number | null
  tools: TimelineTool[]
}

/**
 * 运行时间线(sigma `--timeline` 的数据面,观测层 P5-批次1)。
 * 契约外附加端点 GET /tasks/{id}/timeline 的载荷——工作台用它渲染指标区。
 */
export interface TaskTimeline {
  sessionId: string
  /** trace 里最后一轮的落点(completed / error / stopped);无 trace 为空串 */
  status: string
  hasTrace: boolean
  totalPrompt: number
  totalCompletion: number
  totalCached: number
  /** cache 命中率 = cached / prompt(7.5 口径);prompt 为 0 时 null */
  cacheRate: number | null
  toolErrors: number
  truncated: number
  injected: number
  wallSeconds: number | null
  wallApprox: boolean
  approvals: { name: string; allowed: boolean; reason: string }[]
  rounds: TimelineRound[]
}

/**
 * 常驻区预算表(D4 v3,"表即常量")——上下文构成看板的数据源。
 * caps = 各分区上限;measured = 同口径实测锚点(键为中文分区名)。
 */
export interface WorkbenchBudget {
  residentBudgetTokens: number
  capsSum: number
  caps: Record<string, number>
  measured: Record<string, number>
}

/** 流式增量(最小执行环):since 之后的新文本;running=false 表示轮已结束。 */
export interface TaskDeltas {
  seq: number
  text: string
  running: boolean
  error: string | null
}

/** 队列与待审批(执行中管理,图一/图三语义)。 */
export interface TaskQueues {
  running: boolean
  /** steer 队列(下一轮模型调用前生效的打断注入) */
  steering: string[]
  /** follow-up 队列(当前任务完成后逐条自动执行) */
  followups: string[]
  /** 待确认的审批(变更前确认环) */
  approvals: { id: string; tool: string; summary: string }[]
}

/** 目录浏览(选择工作区用):只列子目录,不读文件内容。 */
export interface FsListing {
  path: string
  parent: string | null
  entries: { name: string; path: string; isDir: boolean }[]
  error?: string
}

/** 当前激活的工作区(切换语义)。 */
export interface WorkspaceState {
  activeId: string
}

export interface SigmaApiClient {
  readonly mode: 'mock' | 'http'
  ping(): Promise<PingInfo>
  listProjects(): Promise<Project[]>
  createProject(input: { name: string; repoPath?: string; branch?: string }): Promise<Project>
  listTasks(filter?: TaskFilter): Promise<Task[]>
  createTask(input: CreateTaskInput): Promise<Task>
  getTask(taskId: string): Promise<Task | null>
  updateTask(taskId: string, patch: TaskPatch): Promise<Task>
  deleteTask(taskId: string): Promise<void>
  listAutomations(): Promise<Automation[]>
  setAutomationEnabled(automationId: string, enabled: boolean): Promise<Automation>
  createAutomation(input: { name: string; schedule: string }): Promise<Automation>
  listPlugins(): Promise<Plugin[]>
  setPluginInstalled(pluginId: string, installed: boolean): Promise<Plugin>
  listModels(): Promise<ModelInfo[]>
  /**
   * 运行时间线(契约外附加,只读桥接服务已实现)。
   * 可选方法:mock 不实现,消费方须以 `apiClient.getTaskTimeline?.()` 调用。
   */
  getTaskTimeline?(taskId: string): Promise<TaskTimeline | null>
  /**
   * 发一条消息并**启动一轮执行**(立即返回 running;σ-server M2 切片)。
   * 文本经 deltas 端点流式送达;轮结束后用 getTask 取最终载荷。
   * 可选方法:mock 不实现。
   */
  addTaskMessage?(taskId: string, text: string): Promise<Task>
  /** 流式增量轮询(TextChunk 钩子缓冲;可选方法,mock 不实现)。 */
  getTaskDeltas?(taskId: string, since: number): Promise<TaskDeltas>
  /** 队列与待审批快照(可选方法,mock 不实现)。 */
  getTaskQueues?(taskId: string): Promise<TaskQueues>
  /** 「立即」:把一条排队指导升级为 steering(下一轮模型调用前生效)。 */
  steerTask?(taskId: string, text: string): Promise<void>
  /** 排队:当前任务完成后自动执行(follow-up)。 */
  queueTask?(taskId: string, text: string): Promise<void>
  /** 删除排队项。 */
  removeQueued?(taskId: string, kind: 'steering' | 'followup', index: number): Promise<void>
  /** 审批决策(变更前确认环)。 */
  decideApproval?(taskId: string, requestId: string, decision: 'approve' | 'deny'): Promise<void>
  /** 当前激活的工作区(可选方法,mock 不实现)。 */
  getWorkspace?(): Promise<WorkspaceState>
  /** 切换激活的工作区(可选方法,mock 不实现)。 */
  activateProject?(projectId: string): Promise<void>
  /** 移除导入的工作区(主工作区不可移;可选方法,mock 不实现)。 */
  removeProject?(projectId: string): Promise<void>
  /** 目录浏览(选择工作区目录;可选方法,mock 不实现)。 */
  browseFs?(path?: string): Promise<FsListing>
}
