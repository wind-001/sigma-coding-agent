import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import {
  CalendarClock,
  CircleHelp,
  CirclePlus,
  Folder,
  Hash,
  LayoutGrid,
  Search,
  type LucideIcon,
} from 'lucide-react'
import { INBOX_PROJECT_ID, STATUS_META, type Project, type Task } from '../../api'
import { useAppActions, useAppState } from '../../store/appStore'
import { formatRelative } from '../../lib/time'
import './overlays.css'

type ItemKind = 'command' | 'task' | 'project'

interface CommandItem {
  kind: 'command'
  key: string
  title: string
  icon: LucideIcon
  run: () => void
}

interface TaskItem {
  kind: 'task'
  key: string
  task: Task
  run: () => void
}

interface ProjectItem {
  kind: 'project'
  key: string
  project: Project
  run: () => void
}

type PaletteItem = CommandItem | TaskItem | ProjectItem

const SECTION_TITLES: Record<ItemKind, string> = {
  command: '命令',
  task: '任务',
  project: '项目',
}

/** 高亮行滚动到可视区 */
function scrollActiveIntoView(list: HTMLDivElement | null): void {
  list?.querySelector('.palette__item--active')?.scrollIntoView({ block: 'nearest' })
}

function ItemContent({ item }: { item: PaletteItem }): JSX.Element {
  if (item.kind === 'command') {
    const Icon = item.icon
    return (
      <>
        <Icon size={15} className="palette__item-icon" aria-hidden="true" />
        <span className="palette__item-title">{item.title}</span>
      </>
    )
  }
  if (item.kind === 'task') {
    const meta = STATUS_META[item.task.status]
    return (
      <>
        <span className="palette__item-dot" style={{ backgroundColor: meta.color }} title={meta.label} />
        <span className="palette__item-title">{item.task.title}</span>
        <span className="palette__item-time">{formatRelative(item.task.updatedAt)}</span>
      </>
    )
  }
  return (
    <>
      <Folder size={15} className="palette__item-icon" aria-hidden="true" />
      <span className="palette__item-title">{item.project.name}</span>
    </>
  )
}

/** Ctrl+K 全局命令面板:命令 / 任务 / 项目 三段搜索 */
export default function CommandPalette(): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [query, setQuery] = useState('')
  const [rawIndex, setRawIndex] = useState(0)
  const listRef = useRef<HTMLDivElement | null>(null)
  const indexRef = useRef(0)

  const close = (): void => {
    actions.closePalette()
  }

  const flatItems = useMemo<PaletteItem[]>(() => {
    const q = query.trim().toLowerCase()
    const runAndClose = (run: () => void): (() => void) => {
      return (): void => {
        run()
        actions.closePalette()
      }
    }
    const commands: CommandItem[] = [
      {
        kind: 'command',
        key: 'cmd-new-task',
        title: '新建任务',
        icon: CirclePlus,
        run: runAndClose(actions.newTaskDraft),
      },
      {
        kind: 'command',
        key: 'cmd-automations',
        title: '打开自动化',
        icon: CalendarClock,
        run: runAndClose(() => actions.setOverlay('automations')),
      },
      {
        kind: 'command',
        key: 'cmd-plugins',
        title: '打开插件市场',
        icon: LayoutGrid,
        run: runAndClose(() => actions.setOverlay('plugins')),
      },
      {
        kind: 'command',
        key: 'cmd-help',
        title: '帮助与快捷键',
        icon: CircleHelp,
        run: runAndClose(() => actions.setOverlay('help')),
      },
      {
        kind: 'command',
        key: 'cmd-view-groups',
        title: '切换到分组视图',
        icon: Hash,
        run: runAndClose(() => actions.setSidebarView('groups')),
      },
      {
        kind: 'command',
        key: 'cmd-view-projects',
        title: '切换到项目视图',
        icon: Folder,
        run: runAndClose(() => actions.setSidebarView('projects')),
      },
    ]
    const matchedCommands = commands.filter((c) => c.title.toLowerCase().includes(q))

    const tasks: PaletteItem[] =
      q === ''
        ? [...state.tasks]
            .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime())
            .slice(0, 5)
            .map((task) => ({
              kind: 'task' as const,
              key: `task-${task.id}`,
              task,
              run: runAndClose(() => actions.selectTask(task.id)),
            }))
        : state.tasks
            .filter(
              (t) =>
                t.title.toLowerCase().includes(q) || t.description.toLowerCase().includes(q),
            )
            .map((task) => ({
              kind: 'task' as const,
              key: `task-${task.id}`,
              task,
              run: runAndClose(() => actions.selectTask(task.id)),
            }))

    const projects: PaletteItem[] =
      q === ''
        ? []
        : state.projects
            .filter((p) => p.id !== INBOX_PROJECT_ID && p.name.toLowerCase().includes(q))
            .map((project) => ({
              kind: 'project' as const,
              key: `project-${project.id}`,
              project,
              run: runAndClose(() => actions.setActiveProjectId(project.id)),
            }))

    return [...matchedCommands, ...tasks, ...projects]
  }, [query, state.tasks, state.projects, actions])

  const activeIndex = flatItems.length === 0 ? -1 : Math.min(rawIndex, flatItems.length - 1)
  indexRef.current = activeIndex

  useEffect(() => {
    setRawIndex(0)
  }, [query])

  useEffect(() => {
    scrollActiveIntoView(listRef.current)
  }, [activeIndex, flatItems])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault()
        actions.closePalette()
        return
      }
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        const count = flatItems.length
        if (count === 0) return
        setRawIndex((prev) => {
          const base = Math.min(prev, count - 1)
          return e.key === 'ArrowDown' ? (base + 1) % count : (base - 1 + count) % count
        })
        return
      }
      if (e.key === 'Enter') {
        e.preventDefault()
        const item = flatItems[indexRef.current]
        if (item !== undefined) item.run()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [actions, flatItems])

  return (
    <div className="palette-mask" onClick={close}>
      <div
        className="palette"
        role="dialog"
        aria-modal="true"
        aria-label="搜索命令面板"
        onClick={(e: React.MouseEvent<HTMLDivElement>) => e.stopPropagation()}
      >
        <div className="palette__search">
          <Search size={16} className="palette__search-icon" aria-hidden="true" />
          <input
            className="palette__input"
            value={query}
            onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuery(e.target.value)}
            placeholder="搜索任务、项目或命令…"
            autoFocus
            aria-label="搜索任务、项目或命令"
          />
        </div>
        <div className="palette__list" ref={listRef}>
          {flatItems.length === 0 ? (
            <div className="palette__empty">未找到匹配的任务、项目或命令</div>
          ) : (
            flatItems.map((item, index) => {
              const prev = index > 0 ? flatItems[index - 1] : null
              const showSectionTitle = prev === null || prev.kind !== item.kind
              return (
                <Fragment key={item.key}>
                  {showSectionTitle ? (
                    <div className="palette__section-title">{SECTION_TITLES[item.kind]}</div>
                  ) : null}
                  <button
                    type="button"
                    className={`palette__item${index === activeIndex ? ' palette__item--active' : ''}`}
                    onMouseEnter={() => setRawIndex(index)}
                    onClick={() => item.run()}
                  >
                    <ItemContent item={item} />
                  </button>
                </Fragment>
              )
            })
          )}
        </div>
        <footer className="palette__footer">
          <span>↑↓ 选择</span>
          <span>Enter 确认</span>
          <span>Esc 关闭</span>
        </footer>
      </div>
    </div>
  )
}
