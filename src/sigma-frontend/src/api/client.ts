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
  /** 状态说明(为何失败/截断,如"达到 max_rounds=30";派生自 trace/悬空调用) */
  statusDetail?: string
  /** 审批模式,取值为 ACCESS_OPTIONS 的 id */
  access: string
  /** 联网搜索开关。**默认 false**(星辰 2026-10-02:默认不启动,要显式开)。
   *  语义是"下一次执行"的参数,不是立即生效——服务端要重建工具表与 loop。 */
  web: boolean
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
  /** 执行档位:sigma 暂无 reasoning effort 管线,前端已不传(服务端按空串落记录) */
  effort?: string
  /**
   * 联网搜索开关（可选，默认 false）。
   *
   * ⚠ 这是**建任务时**的一次性参数，与 ``Task.web``（任务创建后可热切的
   * 状态）同名字但不同环节：首页 Composer 发任务时还没有 taskId，
   * 只能随createTask 一起提交；任务建好后走 ``setTaskWeb(taskId)``。
   * 缺它 ⇒ 新任务一律「联网关闭」，用户在首页没有开关可用。
   */
  web?: boolean
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

/** 斜杠命令元数据(GET /commands;输入面板数据源,2026-10-03)。 */
export interface CommandInfo {
  name: string
  /** 参数提示(如 "<序号>");空 = 无参数 */
  args: string
  description: string
}

/** 技能条目(面板"技能"分区;选中即填入调用提示语)。 */
export interface SkillInfo {
  name: string
  description: string
}

/** GET /commands 的载荷:命令注册表 + 技能清单,面板的单一事实源。 */
export interface CommandsPayload {
  commands: CommandInfo[]
  skills: SkillInfo[]
}

/** 工作台状态(底部菜单状态区,GET /system/status)。 */
export interface SystemStatus {
  version: string
  workspace: string
  sessions: number
  uptimeSeconds: number
  port: number
}

/** 回收站条目(删除的会话/trace 移入 ~/.sigma/trash,可恢复)。 */
export interface TrashEntry {
  name: string
  taskId: string
  kind: 'session' | 'trace' | 'other'
  sizeBytes: number
  /** 删除时间(文件 mtime 的 Unix 秒,近似) */
  deletedAt: number
}

export interface ModelInfo {
  id: string
  name: string
  /** 档位取值由条目自己声明(空 = 该模型无档位,前端隐藏下拉) */
  efforts: string[]
  /** 自定义条目(模型设置里配的 base_url/model_id/密钥变量名) */
  custom?: boolean
  /** 实际发给 API 的 model id */
  modelId?: string
  baseUrl?: string
  protocol?: string
  /** 档位随附方式:reasoning_effort(OpenAI 风格)| thinking(智谱风格) */
  effortStyle?: string
  /**
   * 密钥变量名(指向 ~/.sigma/.env 里的一行,如 DEEPSEEK_API_KEY)。
   * 变量名不是机密,原样回传;密钥本体永不经过前端。
   */
  apiKeyEnv?: string
  /** 是否已配密钥(变量名非空) */
  hasKey?: boolean
}

/** 模型设置表单(新增/更新);密钥本体只写 ~/.sigma/.env,这里只填变量名 */
export interface ModelSaveInput {
  id?: string
  name: string
  protocol: string
  baseUrl: string
  apiKeyEnv: string
  modelId: string
  efforts: string[]
  effortStyle: string
}

/** SMTP 配置回显(自动化面板·邮件,2026-10-05):授权码本体永不回传,
 * 只有变量名与在位布尔——与 /models 的 apiKeyEnv/hasKey 同一口径 */
export interface EmailConfig {
  configured: boolean
  host: string
  port: number
  user: string
  sender: string
  useTls: boolean
  passwordEnv: string
  hasPassword: boolean
}

/** SMTP 配置保存:password 留空 = 不修改;非空 = 服务端写进 ~/.sigma/.env */
export interface EmailConfigInput {
  host: string
  port: number
  user: string
  sender: string
  useTls: boolean
  password: string
}

/** 寄信入参(纯文本邮件) */
export interface EmailSendInput {
  to: string
  subject: string
  body: string
}

/** 正文 AI 润色入参:model/effort 省略 = 走与任务相同的默认解析链 */
export interface EmailPolishInput {
  text: string
  model?: string
  effort?: string
}

export interface AccessOption {
  id: string
  label: string
}

export const ACCESS_OPTIONS: readonly AccessOption[] = [
  { id: 'full', label: '完全访问' },
  { id: 'auto', label: '自动审批' },
  { id: 'confirm', label: '手动确认' },
]

/** 联网开关的两档文案。与 ACCESS_OPTIONS 同款形状(下拉而非勾选框)。
 *
 *  **默认「联网关闭」**——星辰 2026-10-02 拍板"加一个是否开启联网搜索的
 *  判断按钮,默认不启动"。与 CLI 相反:CLI 是"配了 key 就开",这里是
 *  "配了 key 也要显式开"(工具 schema 是常驻成本,每轮都付)。 */
export const WEB_OPTIONS: readonly AccessOption[] = [
  { id: 'on', label: '联网搜索' },
  { id: 'off', label: '联网关闭' },
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

/** 流式增量**结构化块**:直播区分块渲染的原料(文本/思考/工具块,星辰 2026-10-02)。 */
export interface TaskDeltaPiece {
  k: 'text' | 'thinking' | 'tool_start' | 'tool_end' | 'user' | 'note'
  /** text/thinking 的文本增量 */
  t?: string
  /** 工具名(tool_start/tool_end) */
  name?: string
  /** 工具参数摘要(tool_start,服务端已截断) */
  args?: string
  /** 工具结果摘要(tool_end,服务端已截断) */
  preview?: string
  /** 工具结果状态(tool_end) */
  ok?: boolean
}

/** 流式增量(最小执行环):pieces = since 之后的**新块**;running=false 表示轮已结束。 */
export interface TaskDeltas {
  seq: number
  pieces: TaskDeltaPiece[]
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
  /** 待确认的审批(变更前确认环);args = 完整调用参数(安全审核卡可展开) */
  approvals: { id: string; tool: string; summary: string; args?: Record<string, unknown> }[]
  /** ask_user 待答问题(模型主动问的方向决策,真正等人选) */
  questions: TaskQuestion[]
}

/** ask_user 问题卡:question + 候选 + 推荐下标(0 起,null = 无推荐) */
export interface TaskQuestion {
  id: string
  question: string
  options: string[]
  recommended: number | null
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
  /** 模型设置:新增/更新自定义条目(可选方法,mock 不实现) */
  saveModel?(input: ModelSaveInput): Promise<void>
  /** 模型设置:移除自定义条目(可选方法,mock 不实现) */
  removeModel?(id: string): Promise<void>
  /** 自动化·邮件:SMTP 配置回显(授权码只回变量名+在位布尔;可选方法,mock 不实现) */
  getEmailConfig?(): Promise<EmailConfig>
  /** 自动化·邮件:保存 SMTP 配置;password 留空=不修改(可选方法,mock 不实现) */
  saveEmailConfig?(input: EmailConfigInput): Promise<void>
  /** 自动化·邮件:寄一封纯文本邮件(可选方法,mock 不实现) */
  sendEmail?(input: EmailSendInput): Promise<void>
  /** 自动化·邮件:正文一键 AI 润色,返回可预览的润色稿(可选方法,mock 不实现) */
  polishEmailBody?(input: EmailPolishInput): Promise<{ polished: string }>
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
  addTaskMessage?(taskId: string, text: string, opts?: { model?: string; effort?: string }): Promise<Task>
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
  /** 改写排队文本(排队项可编辑,星辰 2026-10-02)。 */
  editQueued?(taskId: string, kind: 'steering' | 'followup', index: number, text: string): Promise<void>
  /** 审批决策(变更前确认环)。 */
  decideApproval?(taskId: string, requestId: string, decision: 'approve' | 'deny'): Promise<void>
  /** ask_user 选择回填(方向决策交还用户)。option 命中候选=常规选择;
   *  非空不在候选=自由输入;dismiss=true=忽略本次(模型自行决定)。 */
  answerQuestion?(taskId: string, questionId: string, option: string, dismiss?: boolean): Promise<void>
  /** 权限模式中途切换(human-in-the-loop):热替换审批闸,下一声工具调用生效。 */
  setTaskAccess?(taskId: string, access: string): Promise<void>
  /** 联网开关切换(默认关,要显式开)。
   *
   *  **不是立即生效**——服务端要重建工具表与 AgentLoop,只在轮边界安全。
   *  返回值如实告知几件事,UI 要显示而不是假装"已生效"：
   *  ``note`` 区分"已开启" / "没配 TAVILY_API_KEY" / "未开启"(处置方式不同);
   *  ``applied=false`` 表示本轮正在跑、跑完后才生效;
   *  ``pendingDropped>0`` 表示重建时丢了这么多排队项(必须让用户看见)。 */
  setTaskWeb?(taskId: string, enabled: boolean): Promise<{
    web: boolean
    webSearch: boolean
    webFetch: boolean
    note: string
    applied: boolean
    reason: string
    pendingDropped: number
  }>
  /** 外部强制中断(协作式:在跑工具完成后于块边界停,状态已持久化可续跑)。
   *  interrupted=false 表示后端本就没在跑(滞留状态已被纠正)。 */
  stopTask?(taskId: string): Promise<{ interrupted: boolean }>
  /** 斜杠命令注册表 + 技能清单(输入面板数据源;可选方法,mock 不实现)。 */
  listCommands?(): Promise<CommandsPayload>
  /** 工作台状态(版本/工作区/会话数/运行时长;可选方法,mock 不实现)。 */
  getSystemStatus?(): Promise<SystemStatus>
  /** 局域网访问地址(手机访问面板;可选方法,mock 不实现)。 */
  listAddresses?(): Promise<{ urls: string[] }>
  /** 会话回收站清单(可选方法,mock 不实现)。 */
  listTrash?(): Promise<{ entries: TrashEntry[] }>
  /** 从回收站恢复一个会话(*.jsonl;可选方法,mock 不实现)。 */
  restoreTrash?(name: string): Promise<{ ok: boolean; taskId: string }>
  /** 清空回收站(不可恢复,调用方必须先确认;可选方法,mock 不实现)。 */
  clearTrash?(): Promise<{ ok: boolean; cleared: number }>
  /** 当前激活的工作区(可选方法,mock 不实现)。 */
  getWorkspace?(): Promise<WorkspaceState>
  /** 切换激活的工作区(可选方法,mock 不实现)。 */
  activateProject?(projectId: string): Promise<void>
  /** 移除导入的工作区(主工作区不可移;可选方法,mock 不实现)。 */
  removeProject?(projectId: string): Promise<void>
  /** 目录浏览(选择工作区目录 / 引用文件;opts.files=1 追加文件条目;mock 不实现)。 */
  browseFs?(path?: string, opts?: { files?: boolean }): Promise<FsListing>
}
