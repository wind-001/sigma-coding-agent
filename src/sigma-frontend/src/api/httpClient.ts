import {
  type Automation,
  type CommandsPayload,
  type CreateTaskInput,
  type EmailConfig,
  type EmailConfigInput,
  type EmailSendInput,
  type SystemStatus,
  type TrashEntry,
  type ModelInfo,
  type ModelSaveInput,
  type PingInfo,
  type Plugin,
  type Project,
  type SigmaApiClient,
  type Task,
  type TaskFilter,
  type TaskPatch,
  type TaskTimeline,
  type TaskDeltas,
  type TaskQueues,
  type FsListing,
  type WorkspaceState,
} from './client'

/**
 * HTTP 版 API 实现。
 * 端点与 docs/sigma-backend-api-design.md 一一对应。当前配对的后端是
 * 只读桥接服务(workbench_server.py,契约子集 + timeline);执行类端点
 * 由其返回 501,detail 里带指路文案——错误信息原样抛给 toast 展示。
 */
export class HttpSigmaClient implements SigmaApiClient {
  readonly mode = 'http' as const

  constructor(private readonly baseUrl: string) {
    // 构造期把全部原型方法绑到实例:调用点存在「取下来再调」的形态
    // (轮询定时器、条件守卫),常规方法那样取会丢 this——实测报
    // "Cannot read properties of undefined (reading 'request')" 且 effect
    // 同步抛错直接卸载整棵 React 树(白屏)。在源头消灭这一整类问题。
    for (const name of Object.getOwnPropertyNames(HttpSigmaClient.prototype)) {
      const value = (this as unknown as Record<string, unknown>)[name]
      if (typeof value === 'function' && name !== 'constructor') {
        ;(this as unknown as Record<string, unknown>)[name] = value.bind(this)
      }
    }
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}/api/v1${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
    if (!response.ok) {
      // 后端的 501/404 带 {detail} 指路文案(哪个里程碑待接入)——原样给用户。
      let detail = ''
      try {
        const body = (await response.json()) as { detail?: string }
        detail = body.detail ?? ''
      } catch {
        // 非 JSON 错误体(网关/断连),退回通用文案。
      }
      throw new Error(
        detail !== ''
          ? detail
          : `sigma 后端请求失败: ${init?.method ?? 'GET'} ${path} → HTTP ${response.status}`,
      )
    }
    if (response.status === 204) {
      return undefined as T
    }
    return (await response.json()) as T
  }

  ping(): Promise<PingInfo> {
    return this.request<PingInfo>('/system/ping')
  }

  listProjects(): Promise<Project[]> {
    return this.request<Project[]>('/projects')
  }

  createProject(input: { name: string; repoPath?: string; branch?: string }): Promise<Project> {
    return this.request<Project>('/projects', { method: 'POST', body: JSON.stringify(input) })
  }

  listTasks(filter?: TaskFilter): Promise<Task[]> {
    const params = new URLSearchParams()
    if (filter?.projectId !== undefined) params.set('project', filter.projectId)
    if (filter?.statuses !== undefined) params.set('status', filter.statuses.join(','))
    if (filter?.query !== undefined && filter.query !== '') params.set('q', filter.query)
    const qs = params.toString()
    return this.request<Task[]>(`/tasks${qs !== '' ? `?${qs}` : ''}`)
  }

  createTask(input: CreateTaskInput): Promise<Task> {
    return this.request<Task>('/tasks', { method: 'POST', body: JSON.stringify(input) })
  }

  getTask(taskId: string): Promise<Task | null> {
    return this.request<Task>(`/tasks/${taskId}`)
  }

  updateTask(taskId: string, patch: TaskPatch): Promise<Task> {
    return this.request<Task>(`/tasks/${taskId}`, { method: 'PATCH', body: JSON.stringify(patch) })
  }

  async deleteTask(taskId: string): Promise<void> {
    await this.request<void>(`/tasks/${taskId}`, { method: 'DELETE' })
  }

  listAutomations(): Promise<Automation[]> {
    return this.request<Automation[]>('/automations')
  }

  setAutomationEnabled(automationId: string, enabled: boolean): Promise<Automation> {
    return this.request<Automation>(`/automations/${automationId}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    })
  }

  createAutomation(input: { name: string; schedule: string }): Promise<Automation> {
    return this.request<Automation>('/automations', { method: 'POST', body: JSON.stringify(input) })
  }

  listPlugins(): Promise<Plugin[]> {
    return this.request<Plugin[]>('/plugins')
  }

  /** 斜杠命令注册表 + 技能清单(输入面板数据源,2026-10-03)。 */
  listCommands(): Promise<CommandsPayload> {
    return this.request<CommandsPayload>('/commands')
  }

  getSystemStatus(): Promise<SystemStatus> {
    return this.request<SystemStatus>('/system/status')
  }

  listAddresses(): Promise<{ urls: string[] }> {
    return this.request<{ urls: string[] }>('/system/addresses')
  }

  listTrash(): Promise<{ entries: TrashEntry[] }> {
    return this.request<{ entries: TrashEntry[] }>('/system/trash')
  }

  restoreTrash(name: string): Promise<{ ok: boolean; taskId: string }> {
    return this.request<{ ok: boolean; taskId: string }>('/system/trash/restore', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
  }

  clearTrash(): Promise<{ ok: boolean; cleared: number }> {
    return this.request<{ ok: boolean; cleared: number }>('/system/trash/clear', {
      method: 'POST',
    })
  }

  setPluginInstalled(pluginId: string, installed: boolean): Promise<Plugin> {
    return this.request<Plugin>(`/plugins/${pluginId}`, {
      method: 'PATCH',
      body: JSON.stringify({ installed }),
    })
  }

  listModels(): Promise<ModelInfo[]> {
    // 服务端合并:自定义条目(模型设置)+ 内置 preset——apiKey 不回传。
    return this.request<ModelInfo[]>('/models')
  }

  async saveModel(input: ModelSaveInput): Promise<void> {
    await this.request<unknown>('/models/save', {
      method: 'POST',
      body: JSON.stringify(input),
    })
  }

  async removeModel(id: string): Promise<void> {
    await this.request<unknown>('/models/remove', {
      method: 'POST',
      body: JSON.stringify({ id }),
    })
  }

  getEmailConfig(): Promise<EmailConfig> {
    // SMTP 配置回显:授权码本体不回传,只有变量名与在位布尔。
    return this.request<EmailConfig>('/email/config')
  }

  async saveEmailConfig(input: EmailConfigInput): Promise<void> {
    await this.request<unknown>('/email/config', {
      method: 'POST',
      body: JSON.stringify(input),
    })
  }

  async sendEmail(input: EmailSendInput): Promise<void> {
    await this.request<unknown>('/email/send', {
      method: 'POST',
      body: JSON.stringify(input),
    })
  }

  getTaskTimeline(taskId: string): Promise<TaskTimeline | null> {
    return this.request<TaskTimeline | null>(`/tasks/${taskId}/timeline`)
  }

  addTaskMessage(taskId: string, text: string, opts?: { model?: string; effort?: string }): Promise<Task> {
    // model/effort 随消息提交:TaskDetail 切换器的选择落到任务记录,
    // 服务端会话层发现元数据变化就重建(切模型下一轮立即生效)。
    return this.request<Task>(`/tasks/${taskId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ text, model: opts?.model, effort: opts?.effort }),
    })
  }

  getTaskDeltas(taskId: string, since: number): Promise<TaskDeltas> {
    return this.request<TaskDeltas>(`/tasks/${taskId}/deltas?since=${since}`)
  }

  getTaskQueues(taskId: string): Promise<TaskQueues> {
    return this.request<TaskQueues>(`/tasks/${taskId}/queues`)
  }

  async steerTask(taskId: string, text: string): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/steer`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    })
  }

  async queueTask(taskId: string, text: string): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/queue`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    })
  }

  async removeQueued(
    taskId: string,
    kind: 'steering' | 'followup',
    index: number,
  ): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/queue-remove`, {
      method: 'POST',
      body: JSON.stringify({ kind, index }),
    })
  }

  async editQueued(
    taskId: string,
    kind: 'steering' | 'followup',
    index: number,
    text: string,
  ): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/queue-edit`, {
      method: 'POST',
      body: JSON.stringify({ kind, index, text }),
    })
  }

  async decideApproval(
    taskId: string,
    requestId: string,
    decision: 'approve' | 'deny',
  ): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/approvals/${requestId}`, {
      method: 'POST',
      body: JSON.stringify({ decision }),
    })
  }

  async answerQuestion(
    taskId: string,
    questionId: string,
    option: string,
    dismiss: boolean = false,
  ): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/questions/${questionId}`, {
      method: 'POST',
      body: JSON.stringify({ option: dismiss ? '' : option, dismiss }),
    })
  }

  async setTaskAccess(taskId: string, access: string): Promise<void> {
    await this.request<unknown>(`/tasks/${taskId}/access`, {
      method: 'POST',
      body: JSON.stringify({ access }),
    })
  }

  async setTaskWeb(
    taskId: string,
    enabled: boolean,
  ): Promise<{
    web: boolean
    webSearch: boolean
    webFetch: boolean
    note: string
    applied: boolean
    reason: string
    pendingDropped: number
  }> {
    return this.request(`/tasks/${taskId}/web`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    })
  }

  async stopTask(taskId: string): Promise<{ interrupted: boolean }> {
    return this.request<{ interrupted: boolean }>(`/tasks/${taskId}/stop`, {
      method: 'POST',
    })
  }

  getWorkspace(): Promise<WorkspaceState> {
    return this.request<WorkspaceState>('/workspace')
  }

  async activateProject(projectId: string): Promise<void> {
    await this.request<unknown>('/workspace/activate', {
      method: 'POST',
      body: JSON.stringify({ id: projectId }),
    })
  }

  async removeProject(projectId: string): Promise<void> {
    await this.request<unknown>('/projects/remove', {
      method: 'POST',
      body: JSON.stringify({ id: projectId }),
    })
  }

  browseFs(path?: string, opts?: { files?: boolean }): Promise<FsListing> {
    const params = new URLSearchParams()
    if (path !== undefined && path !== '') params.set('path', path)
    if (opts?.files === true) params.set('files', '1')
    const query = params.toString()
    return this.request<FsListing>(`/fs${query !== '' ? `?${query}` : ''}`)
  }
}
