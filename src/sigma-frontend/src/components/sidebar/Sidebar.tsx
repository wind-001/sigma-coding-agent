import { useEffect, useRef, useState } from 'react'
import {
  ArrowLeft,
  ArrowRight,
  CalendarClock,
  Check,
  CirclePlus,
  Folder,
  FolderOpen,
  FolderPlus,
  Hash,
  LayoutGrid,
  ListFilter,
  Pin,
  Plus,
  Search,
  Settings,
  Smartphone,
  Trash2,
  User,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import {
  INBOX_PROJECT_ID,
  STATUS_META,
  type Project,
  type Task,
  type TaskStatus,
} from '../../api'
import { matchStatusFilter, useAppActions, useAppState, type StatusFilter } from '../../store/appStore'
import { formatRelative } from '../../lib/time'
import './sidebar.css'

/* ============================================================
   静态配置
   ============================================================ */

type NavId = 'new-task' | 'search' | 'import-ws' | 'automations' | 'plugins'

interface NavItemMeta {
  id: NavId
  label: string
  icon: LucideIcon
  shortcut?: string
  title: string
}

const NAV_ITEMS: readonly NavItemMeta[] = [
  { id: 'new-task', label: '新建任务', icon: CirclePlus, shortcut: 'Ctrl+N', title: '新建任务(Ctrl+N)' },
  { id: 'search', label: '搜索', icon: Search, shortcut: 'Ctrl+K', title: '搜索(Ctrl+K)' },
  { id: 'import-ws', label: '导入工作区', icon: FolderPlus, title: '导入工作区(输入绝对路径)' },
  { id: 'automations', label: '自动化', icon: CalendarClock, title: '自动化' },
  { id: 'plugins', label: '插件市场', icon: LayoutGrid, title: '插件市场' },
]

interface StatusFilterOption {
  value: StatusFilter
  label: string
}

const STATUS_FILTER_OPTIONS: readonly StatusFilterOption[] = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '进行中' },
  { value: 'completed', label: '已完成' },
]

interface GroupSectionMeta {
  key: string
  title: string
  statuses: readonly TaskStatus[]
}

/** 分组视图的三个跨项目 section */
const GROUP_SECTIONS: readonly GroupSectionMeta[] = [
  { key: 'active', title: '进行中', statuses: ['draft', 'running', 'waiting_approval'] },
  { key: 'completed', title: '已完成', statuses: ['completed'] },
  { key: 'failed', title: '已失败', statuses: ['failed'] },
]

/* ============================================================
   子组件
   ============================================================ */

interface SidebarTaskItemProps {
  task: Task
  /** group = 项目组/状态组内条目(缩进 26);task = 「任务」区条目(缩进 4) */
  variant: 'group' | 'task'
  selected: boolean
  onSelect: (taskId: string) => void
  /** 提供时条目悬停显示删除按钮 */
  onDelete?: (taskId: string) => void
}

function SidebarTaskItem({ task, variant, selected, onSelect, onDelete }: SidebarTaskItemProps): JSX.Element {
  const statusMeta = STATUS_META[task.status]
  const itemClassName = [
    'sidebar__item',
    variant === 'group' ? 'sidebar__item--group' : 'sidebar__item--task',
    selected ? 'sidebar__item--selected' : '',
  ]
    .filter((part) => part.length > 0)
    .join(' ')

  return (
    <div className="sidebar__item-row">
      <button type="button" className={itemClassName} title={task.title} onClick={() => onSelect(task.id)}>
        <span className="sidebar__item-main">
          <span
            className="sidebar__item-status"
            style={{ backgroundColor: statusMeta.color }}
            title={statusMeta.label}
            aria-label={`状态:${statusMeta.label}`}
          />
          <span className="sidebar__item-title">{task.title}</span>
        </span>
        <span className="sidebar__item-time">{formatRelative(task.updatedAt)}</span>
      </button>
      {onDelete !== undefined ? (
        <button
          type="button"
          className="sidebar__item-del"
          aria-label={`删除会话「${task.title}」`}
          title="删除会话"
          onClick={(event: React.MouseEvent<HTMLButtonElement>): void => {
            event.stopPropagation()
            onDelete(task.id)
          }}
        >
          <Trash2 size={13} aria-hidden="true" />
        </button>
      ) : null}
    </div>
  )
}

interface ProjectSectionProps {
  project: Project
  tasks: Task[]
  collapsed: boolean
  selectedTaskId: string | null
  onToggle: (projectId: string) => void
  onSelectTask: (taskId: string) => void
  onCreateSession: (projectId: string) => void
  onDeleteTask: (taskId: string) => void
}

/** 普通项目 section:Folder 图标 + 项目名(+ 任务数),组头可折叠,悬停出现「新建会话」 */
function ProjectSection({
  project,
  tasks,
  collapsed,
  selectedTaskId,
  onToggle,
  onSelectTask,
  onCreateSession,
  onDeleteTask,
}: ProjectSectionProps): JSX.Element {
  return (
    <div className="sidebar__group">
      <div className="sidebar__group-header-row">
        <button
          type="button"
          className="sidebar__group-header"
          onClick={() => onToggle(project.id)}
          aria-expanded={!collapsed}
          title={project.name}
        >
          {collapsed ? (
            <FolderOpen size={17} className="sidebar__group-icon" aria-hidden="true" />
          ) : (
            <Folder size={17} className="sidebar__group-icon" aria-hidden="true" />
          )}
          <span className="sidebar__group-name">{project.name}</span>
          {tasks.length > 0 ? <span className="sidebar__count">{tasks.length}</span> : null}
        </button>
        <button
          type="button"
          className="sidebar__group-add"
          aria-label={`在「${project.name}」中新建会话`}
          title={`在「${project.name}」中新建会话`}
          onClick={(event: React.MouseEvent<HTMLButtonElement>): void => {
            event.stopPropagation()
            onCreateSession(project.id)
          }}
        >
          <Plus size={14} aria-hidden="true" />
        </button>
      </div>
      {collapsed ? null : (
        <div className="sidebar__group-items">
          {tasks.map((task) => (
            <SidebarTaskItem
              key={task.id}
              task={task}
              variant="group"
              selected={selectedTaskId === task.id}
              onSelect={onSelectTask}
              onDelete={onDeleteTask}
            />
          ))}
        </div>
      )}
    </div>
  )
}

interface StatusSectionProps {
  title: string
  tasks: Task[]
  selectedTaskId: string | null
  onSelectTask: (taskId: string) => void
  onDeleteTask: (taskId: string) => void
}

/** 分组视图 section:纯文字组头(样式同小节标题,略大),不可折叠 */
function StatusSection({ title, tasks, selectedTaskId, onSelectTask, onDeleteTask }: StatusSectionProps): JSX.Element {
  return (
    <div className="sidebar__group">
      <div className="sidebar__section-title sidebar__section-title--lg">
        {title}
        {tasks.length > 0 ? <span className="sidebar__count">{tasks.length}</span> : null}
      </div>
      <div className="sidebar__group-items">
        {tasks.map((task) => (
          <SidebarTaskItem
            key={task.id}
            task={task}
            variant="group"
            selected={selectedTaskId === task.id}
            onSelect={onSelectTask}
            onDelete={onDeleteTask}
          />
        ))}
      </div>
    </div>
  )
}

/* ============================================================
   Sidebar 主体
   ============================================================ */

export default function Sidebar(): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [collapsedIds, setCollapsedIds] = useState<string[]>([])
  const [filterOpen, setFilterOpen] = useState<boolean>(false)
  const [toast, setToast] = useState<string | null>(null)
  const filterRef = useRef<HTMLDivElement | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect((): (() => void) => {
    return () => {
      if (toastTimer.current !== null) clearTimeout(toastTimer.current)
    }
  }, [])

  // 点击外部关闭筛选下拉
  useEffect((): (() => void) | undefined => {
    if (!filterOpen) {
      return undefined
    }
    const handleDocumentMouseDown = (event: MouseEvent): void => {
      const node = filterRef.current
      const target = event.target
      if (node !== null && target instanceof Node && !node.contains(target)) {
        setFilterOpen(false)
      }
    }
    document.addEventListener('mousedown', handleDocumentMouseDown)
    return (): void => {
      document.removeEventListener('mousedown', handleDocumentMouseDown)
    }
  }, [filterOpen])

  /** 本地 toast:复用全局 .toast 样式(store 未暴露 showToast,只能就地提示) */
  const showToast = (message: string): void => {
    if (toastTimer.current !== null) clearTimeout(toastTimer.current)
    setToast(message)
    toastTimer.current = setTimeout(() => setToast(null), 2600)
  }

  // 状态过滤后的任务(store 顺序),再按项目 / 状态分桶
  const visibleTasks: Task[] = state.tasks.filter((task) => matchStatusFilter(task, state.statusFilter))
  const tasksByProject = new Map<string, Task[]>()
  for (const task of visibleTasks) {
    const bucket = tasksByProject.get(task.projectId)
    if (bucket === undefined) {
      tasksByProject.set(task.projectId, [task])
    } else {
      bucket.push(task)
    }
  }

  const currentFilterLabel: string =
    STATUS_FILTER_OPTIONS.find((option) => option.value === state.statusFilter)?.label ?? '全部'

  const toggleGroup = (projectId: string): void => {
    setCollapsedIds((prev) =>
      prev.includes(projectId) ? prev.filter((id) => id !== projectId) : [...prev, projectId],
    )
  }

  const handleNav = (id: NavId): void => {
    if (id === 'new-task') {
      actions.newTaskDraft()
    } else if (id === 'search') {
      actions.togglePalette()
    } else if (id === 'import-ws') {
      // 浏览器没有目录选择器:用绝对路径导入(桥接端校验目录存在)。
      const repoPath = window.prompt('输入工作区的绝对路径(如 D:\\projects\\demo):')
      if (repoPath !== null && repoPath.trim() !== '') {
        void actions.importWorkspace(repoPath.trim())
      }
    } else if (id === 'automations') {
      actions.setOverlay('automations')
    } else {
      actions.setOverlay('plugins')
    }
  }

  const handleSelectTask = (taskId: string): void => {
    actions.selectTask(taskId)
  }

  const handleCreateSession = (projectId: string): void => {
    void actions.createSessionInProject(projectId)
  }

  const handleDeleteTask = (taskId: string): void => {
    const task = state.tasks.find((item) => item.id === taskId)
    if (task !== undefined && window.confirm(`确认删除会话「${task.title}」?该操作不可撤销。`)) {
      void actions.deleteTaskById(taskId)
    }
  }

  const handleStatusFilter = (filter: StatusFilter): void => {
    actions.setStatusFilter(filter)
    setFilterOpen(false)
  }

  const handleDeleteClick = (): void => {
    const selectedId = state.selectedTaskId
    const selected = selectedId !== null ? state.tasks.find((task) => task.id === selectedId) : undefined
    if (selected === undefined) {
      showToast('先在列表中选择一个任务')
      return
    }
    if (window.confirm(`确认删除任务「${selected.title}」?该操作不可撤销。`)) {
      void actions.deleteTaskById(selected.id)
    }
  }

  const renderList = (): JSX.Element => {
    if (!state.ready) {
      return <div className="sidebar__empty">加载中…</div>
    }
    if (visibleTasks.length === 0) {
      return <div className="sidebar__empty">暂无任务,按 Ctrl+N 新建</div>
    }
    if (state.sidebarView === 'groups') {
      return (
        <>
          {GROUP_SECTIONS.map((section) => {
            const sectionTasks: Task[] = visibleTasks.filter((task) => section.statuses.includes(task.status))
            return (
              <StatusSection
                key={section.key}
                title={section.title}
                tasks={sectionTasks}
                selectedTaskId={state.selectedTaskId}
                onSelectTask={handleSelectTask}
                onDeleteTask={handleDeleteTask}
              />
            )
          })}
        </>
      )
    }
    return (
      <>
        <div className="sidebar__section-title">项目</div>
        {state.projects.map((project) => {
          const projectTasks: Task[] = tasksByProject.get(project.id) ?? []
          // 收件箱项目:纯文字 section「任务」,无文件夹图标,条目缩进同截图
          if (project.id === INBOX_PROJECT_ID) {
            return (
              <div key={project.id} className="sidebar__group">
                <div className="sidebar__section-title">任务</div>
                <div className="sidebar__group-items">
                  {projectTasks.map((task) => (
                    <SidebarTaskItem
                      key={task.id}
                      task={task}
                      variant="task"
                      selected={state.selectedTaskId === task.id}
                      onSelect={handleSelectTask}
                      onDelete={handleDeleteTask}
                    />
                  ))}
                </div>
              </div>
            )
          }
          return (
            <ProjectSection
              key={project.id}
              project={project}
              tasks={projectTasks}
              collapsed={collapsedIds.includes(project.id)}
              selectedTaskId={state.selectedTaskId}
              onToggle={toggleGroup}
              onSelectTask={handleSelectTask}
              onCreateSession={handleCreateSession}
              onDeleteTask={handleDeleteTask}
            />
          )
        })}
      </>
    )
  }

  return (
    <aside className="sidebar">
      <header className="sidebar__brand">
        <div className="sidebar__logo">Z</div>
        <div className="sidebar__history">
          <button type="button" className="sidebar__history-btn" aria-label="后退" title="后退">
            <ArrowLeft size={18} aria-hidden="true" />
          </button>
          <button type="button" className="sidebar__history-btn" aria-label="前进" title="前进">
            <ArrowRight size={18} aria-hidden="true" />
          </button>
        </div>
      </header>

      <nav className="sidebar__nav">
        {NAV_ITEMS.map((nav) => {
          const NavIcon = nav.icon
          return (
            <button
              key={nav.id}
              type="button"
              className="sidebar__nav-item"
              title={nav.title}
              onClick={() => handleNav(nav.id)}
            >
              <NavIcon size={18} className="sidebar__nav-icon" aria-hidden="true" />
              <span className="sidebar__nav-label">{nav.label}</span>
              {nav.shortcut ? (
                <span className="sidebar__nav-shortcut">{nav.shortcut}</span>
              ) : null}
            </button>
          )
        })}
      </nav>

      <div className="sidebar__switcher">
        <button
          type="button"
          className={`sidebar__chip${state.sidebarView === 'groups' ? ' sidebar__chip--active' : ''}`}
          aria-pressed={state.sidebarView === 'groups'}
          onClick={() => actions.setSidebarView('groups')}
        >
          <Hash size={15} aria-hidden="true" />
          <span>分组</span>
        </button>
        <button
          type="button"
          className={`sidebar__chip${state.sidebarView === 'projects' ? ' sidebar__chip--active' : ''}`}
          aria-pressed={state.sidebarView === 'projects'}
          onClick={() => actions.setSidebarView('projects')}
        >
          <Folder size={15} aria-hidden="true" />
          <span>项目</span>
        </button>
        <button
          type="button"
          className="sidebar__icon-btn sidebar__switcher-pin"
          aria-label="固定侧边栏"
          title="固定侧边栏"
        >
          <Pin size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="sidebar__tools">
        <div className="sidebar__filter" ref={filterRef}>
          <button
            type="button"
            className={`sidebar__icon-btn${state.statusFilter === 'all' ? '' : ' sidebar__icon-btn--active'}`}
            aria-label={`状态筛选:当前${currentFilterLabel}`}
            aria-haspopup="listbox"
            aria-expanded={filterOpen}
            title={`状态筛选:${currentFilterLabel}`}
            onClick={() => setFilterOpen((prev) => !prev)}
          >
            <ListFilter size={16} aria-hidden="true" />
          </button>
          {filterOpen ? (
            <div className="sidebar__filter-menu" role="listbox">
              {STATUS_FILTER_OPTIONS.map((option) => {
                const selected: boolean = option.value === state.statusFilter
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    className={`sidebar__filter-item${selected ? ' sidebar__filter-item--selected' : ''}`}
                    onClick={() => handleStatusFilter(option.value)}
                  >
                    <span className="sidebar__filter-check">
                      {selected ? <Check size={14} aria-hidden="true" /> : null}
                    </span>
                    <span>{option.label}</span>
                  </button>
                )
              })}
            </div>
          ) : null}
        </div>
        <button
          type="button"
          className="sidebar__icon-btn"
          aria-label="删除当前任务"
          title="删除当前任务"
          onClick={handleDeleteClick}
        >
          <Trash2 size={16} aria-hidden="true" />
        </button>
      </div>

      <div className="sidebar__scroll">{renderList()}</div>

      <footer className="sidebar__user">
        <div className="sidebar__avatar">
          <User size={18} aria-hidden="true" />
        </div>
        <span className="sidebar__username">sigma 本机工作台</span>
        <div className="sidebar__user-actions">
          <button type="button" className="sidebar__icon-btn" aria-label="移动设备">
            <Smartphone size={17} aria-hidden="true" />
          </button>
          <button type="button" className="sidebar__icon-btn" aria-label="设置">
            <Settings size={17} aria-hidden="true" />
          </button>
        </div>
      </footer>

      {toast !== null ? <div className="toast">{toast}</div> : null}
    </aside>
  )
}
