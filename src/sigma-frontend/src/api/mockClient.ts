import {
  ACCESS_OPTIONS,
  DEFAULT_MODELS,
  STATUS_META,
  type Automation,
  type CreateTaskInput,
  type ModelInfo,
  type PingInfo,
  type Plugin,
  type Project,
  type SigmaApiClient,
  type Task,
  type TaskEvent,
  type TaskFilter,
  type TaskPatch,
  type TaskStatus,
} from './client'

interface MockDb {
  projects: Project[]
  tasks: Task[]
  automations: Automation[]
  plugins: Plugin[]
  seq: number
}

const STORAGE_KEY = 'sigma-frontend-db-v1'

const MIN = 60_000
const HOUR = 3_600_000
const DAY = 86_400_000

function isoAgo(ms: number): string {
  return new Date(Date.now() - ms).toISOString()
}

function nowIso(): string {
  return new Date().toISOString()
}

function createdEvent(taskId: string, at: string): TaskEvent {
  return { id: `${taskId}-ev-created`, kind: 'created', text: '任务已创建', at }
}

function seedDb(): MockDb {
  const projects: Project[] = [
    { id: 'proj-purify', name: 'purify', repoPath: 'C:/work/purify', branch: 'main', createdAt: isoAgo(30 * DAY) },
    { id: 'proj-chiron', name: 'Chiron-agent', repoPath: 'C:/work/Chiron-agent', branch: 'main', createdAt: isoAgo(20 * DAY) },
    { id: 'proj-sigma', name: 'sigma', repoPath: 'C:/Users/刘康鑫/Desktop/sigma', branch: 'main', createdAt: isoAgo(60 * DAY) },
    { id: 'proj-inbox', name: '任务', repoPath: '', branch: '', createdAt: isoAgo(60 * DAY) },
  ]

  const base = {
    access: 'full',
    // 默认**关**(星辰 2026-10-02)。mock 也要照这个默认,否则
    // "mock 里是开的、真后端是关的"会让按钮的初始态撒谎。
    web: false,
    model: 'GLM-5.3-Flash',
    effort: '最高',
  }

  const raw: Array<Pick<Task, 'id' | 'projectId' | 'title' | 'description' | 'status'> & { createdMs: number; updatedMs: number }> = [
    { id: 'task-101', projectId: 'proj-purify', title: '构建进程管理工具并打包', description: '用 PyInstaller 把构建进程管理工具打包成单个 exe,并附使用说明与版本号规则。', status: 'completed', createdMs: 2 * DAY, updatedMs: 1 * DAY },
    { id: 'task-201', projectId: 'proj-chiron', title: 'Chiron-agent Python Agent 开发', description: '开发 Chiron-agent 的 Python Agent 主体:工具注册、循环调度与回调接口。', status: 'running', createdMs: 3 * DAY, updatedMs: 3 * MIN },
    { id: 'task-202', projectId: 'proj-chiron', title: '我现在已经安装好了docker', description: '验证 Docker 环境并记录常用命令与镜像加速配置。', status: 'completed', createdMs: 2 * DAY, updatedMs: 10 * HOUR },
    { id: 'task-203', projectId: 'proj-chiron', title: 'SSH克隆私有仓库Chiron-Agent', description: '配置 SSH key 并克隆私有仓库,梳理分支保护规则。', status: 'completed', createdMs: 3 * DAY, updatedMs: 1 * DAY },
    { id: 'task-301', projectId: 'proj-sigma', title: '优化git影子库', description: '影子 checkpoint 迁移到 .sigma/session/ 并加水位治理(512MB/LRU/砍尾)。', status: 'completed', createdMs: 4 * DAY, updatedMs: 3 * MIN },
    { id: 'task-302', projectId: 'proj-sigma', title: 'ask-matt项目链路追踪与面试', description: '梳理 ask-matt 技能的调用链路,准备面试讲解口径。', status: 'running', createdMs: 1 * DAY, updatedMs: 19 * MIN },
    { id: 'task-303', projectId: 'proj-sigma', title: '[$review](C:\\Users\\刘康鑫\\Desktop)', description: '对桌面临时改动做一轮 review 并清理。', status: 'draft', createdMs: 1 * DAY, updatedMs: 47 * MIN },
    { id: 'task-304', projectId: 'proj-sigma', title: 'Archify Skill 生成项目架构图', description: '用 archify 产出 sigma 分层架构图(HTML + 源规格),showcase 验收 9/9。', status: 'completed', createdMs: 3 * DAY, updatedMs: 1 * DAY },
    { id: 'task-401', projectId: 'proj-inbox', title: '我当前电脑c盘怎么突然没有了', description: '排查 C 盘空间异常与盘符消失问题。', status: 'draft', createdMs: 2 * DAY, updatedMs: 1 * DAY },
  ]

  const tasks: Task[] = raw.map((t) => ({
    ...t,
    ...base,
    createdAt: isoAgo(t.createdMs),
    updatedAt: isoAgo(t.updatedMs),
    events: [createdEvent(t.id, isoAgo(t.createdMs))],
  }))

  const automations: Automation[] = [
    { id: 'auto-1', name: '周报总结', schedule: '每周五 17:00', enabled: true, lastRunAt: isoAgo(2 * DAY) },
    { id: 'auto-2', name: '影子库水位巡检', schedule: '每 6 小时', enabled: true, lastRunAt: isoAgo(3 * HOUR) },
    { id: 'auto-3', name: '依赖更新检查', schedule: '每日 09:00', enabled: false, lastRunAt: null },
  ]

  const plugins: Plugin[] = [
    { id: 'plugin-1', name: 'Web 搜索(Tavily)', description: '联网搜索工具,接入 Tavily API。', version: '0.3.1', installed: true, builtin: false },
    { id: 'plugin-2', name: '网页抓取(Firecrawl)', description: '整站抓取与正文抽取,配合搜索使用。', version: '0.2.0', installed: true, builtin: false },
    { id: 'plugin-3', name: '影子 checkpoint', description: '工作区级影子 git 快照,支持回滚与断点续跑。', version: '内核内置', installed: true, builtin: true },
    { id: 'plugin-4', name: 'Repo Map', description: '仓库结构图生成,常驻区按需注入。', version: '0.1.0', installed: false, builtin: false },
    { id: 'plugin-5', name: 'MCP 协议桥', description: '把 MCP 服务器暴露的工具挂进 sigma 工具表。', version: '0.4.0', installed: false, builtin: false },
    { id: 'plugin-6', name: '技能市场同步', description: '同步本地 agents/skills 目录与云端技能索引。', version: '0.1.2', installed: false, builtin: false },
  ]

  return { projects, tasks, automations, plugins, seq: 1000 }
}

function loadDb(): MockDb {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw !== null) {
      return JSON.parse(raw) as MockDb
    }
  } catch {
    // 损坏的存量数据直接重新播种
  }
  const db = seedDb()
  saveDb(db)
  return db
}

function saveDb(db: MockDb): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(db))
}

function clone<T>(value: T): T {
  return structuredClone(value)
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function latency(): Promise<void> {
  return sleep(30 + Math.random() * 70)
}

/** localStorage 版 API 实现:接口与 HTTP 版完全一致,便于无缝切换 */
export class MockSigmaClient implements SigmaApiClient {
  readonly mode = 'mock' as const

  async ping(): Promise<PingInfo> {
    await latency()
    return { ok: true, mode: 'mock', version: '0.1.0-mock' }
  }

  async listProjects(): Promise<Project[]> {
    await latency()
    return clone(loadDb().projects)
  }

  async createProject(input: { name: string; repoPath?: string; branch?: string }): Promise<Project> {
    await latency()
    const db = loadDb()
    const project: Project = {
      id: `proj-${db.seq++}`,
      name: input.name,
      repoPath: input.repoPath ?? '',
      branch: input.branch ?? 'main',
      createdAt: nowIso(),
    }
    db.projects.push(project)
    saveDb(db)
    return clone(project)
  }

  async listTasks(filter?: TaskFilter): Promise<Task[]> {
    await latency()
    const tasks = loadDb().tasks
    const query = filter?.query?.trim().toLowerCase()
    const statuses = filter?.statuses ? new Set<TaskStatus>(filter.statuses) : null
    const filtered = tasks.filter((t) => {
      if (filter?.projectId !== undefined && t.projectId !== filter.projectId) return false
      if (statuses !== null && !statuses.has(t.status)) return false
      if (query !== undefined && query !== '' && !t.title.toLowerCase().includes(query) && !t.description.toLowerCase().includes(query)) return false
      return true
    })
    return clone(filtered)
  }

  async createTask(input: CreateTaskInput): Promise<Task> {
    await latency()
    const db = loadDb()
    const at = nowIso()
    const task: Task = {
      id: `task-${db.seq++}`,
      projectId: input.projectId,
      title: input.title,
      description: input.description,
      status: 'draft',
      access: input.access,
      // 新会话默认**关**(与真后端同口径,星辰 2026-10-02)。
      // mock 里默认开会让人以为"默认就是开的",去真后台找不到开关。
      // 但用户在首页 Composer 显式打开时要**尊重**——写死false 会让 mock
      // 与真后端分叉（一个听开关一个不听），那是更难查的一类不一致。
      web: input.web ?? false,
      model: input.model,
      effort: input.effort ?? '',
      createdAt: at,
      updatedAt: at,
      events: [],
    }
    task.events.push(createdEvent(task.id, at))
    db.tasks.unshift(task)
    saveDb(db)
    return clone(task)
  }

  async getTask(taskId: string): Promise<Task | null> {
    await latency()
    return clone(loadDb().tasks.find((t) => t.id === taskId) ?? null)
  }

  async updateTask(taskId: string, patch: TaskPatch): Promise<Task> {
    await latency()
    const db = loadDb()
    const task = db.tasks.find((t) => t.id === taskId)
    if (task === undefined) {
      throw new Error(`任务不存在: ${taskId}`)
    }
    if (patch.title !== undefined) task.title = patch.title
    if (patch.description !== undefined) task.description = patch.description
    if (patch.status !== undefined && patch.status !== task.status) {
      const from: string = STATUS_META[task.status].label
      task.status = patch.status
      task.events.push({ id: `${task.id}-ev-${db.seq++}`, kind: 'status', text: `状态变更为 ${STATUS_META[patch.status].label}(原 ${from})`, at: nowIso() })
    }
    if (patch.appendEvent !== undefined) {
      task.events.push({ id: `${task.id}-ev-${db.seq++}`, kind: patch.appendEvent.kind, text: patch.appendEvent.text, at: nowIso() })
    }
    task.updatedAt = nowIso()
    saveDb(db)
    return clone(task)
  }

  async deleteTask(taskId: string): Promise<void> {
    await latency()
    const db = loadDb()
    db.tasks = db.tasks.filter((t) => t.id !== taskId)
    saveDb(db)
  }

  async listAutomations(): Promise<Automation[]> {
    await latency()
    return clone(loadDb().automations)
  }

  async setAutomationEnabled(automationId: string, enabled: boolean): Promise<Automation> {
    await latency()
    const db = loadDb()
    const item = db.automations.find((a) => a.id === automationId)
    if (item === undefined) {
      throw new Error(`自动化不存在: ${automationId}`)
    }
    item.enabled = enabled
    saveDb(db)
    return clone(item)
  }

  async createAutomation(input: { name: string; schedule: string }): Promise<Automation> {
    await latency()
    const db = loadDb()
    const item: Automation = { id: `auto-${db.seq++}`, name: input.name, schedule: input.schedule, enabled: false, lastRunAt: null }
    db.automations.push(item)
    saveDb(db)
    return clone(item)
  }

  async listPlugins(): Promise<Plugin[]> {
    await latency()
    return clone(loadDb().plugins)
  }

  async setPluginInstalled(pluginId: string, installed: boolean): Promise<Plugin> {
    await latency()
    const db = loadDb()
    const item = db.plugins.find((p) => p.id === pluginId)
    if (item === undefined) {
      throw new Error(`插件不存在: ${pluginId}`)
    }
    if (item.builtin) {
      throw new Error('内置插件不可卸载')
    }
    item.installed = installed
    saveDb(db)
    return clone(item)
  }

  async listModels(): Promise<ModelInfo[]> {
    await latency()
    return clone([...DEFAULT_MODELS])
  }
}

/** 供 UI 校验 access 合法性使用 */
export function accessLabel(accessId: string): string {
  return ACCESS_OPTIONS.find((o) => o.id === accessId)?.label ?? accessId
}
