import {
  DEFAULT_MODELS,
  type Automation,
  type CreateTaskInput,
  type ModelInfo,
  type PingInfo,
  type Plugin,
  type Project,
  type SigmaApiClient,
  type Task,
  type TaskFilter,
  type TaskPatch,
  type TaskTimeline,
} from './client'

/**
 * HTTP 版 API 实现。
 * 端点与 docs/sigma-backend-api-design.md 一一对应。当前配对的后端是
 * 只读桥接服务(workbench_server.py,契约子集 + timeline);执行类端点
 * 由其返回 501,detail 里带指路文案——错误信息原样抛给 toast 展示。
 */
export class HttpSigmaClient implements SigmaApiClient {
  readonly mode = 'http' as const

  constructor(private readonly baseUrl: string) {}

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

  setPluginInstalled(pluginId: string, installed: boolean): Promise<Plugin> {
    return this.request<Plugin>(`/plugins/${pluginId}`, {
      method: 'PATCH',
      body: JSON.stringify({ installed }),
    })
  }

  async listModels(): Promise<ModelInfo[]> {
    return [...DEFAULT_MODELS]
  }

  getTaskTimeline(taskId: string): Promise<TaskTimeline | null> {
    return this.request<TaskTimeline | null>(`/tasks/${taskId}/timeline`)
  }
}
