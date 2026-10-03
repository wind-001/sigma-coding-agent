import { memo, useEffect, useMemo, useRef, useState } from 'react'
import { Terminal, Zap } from 'lucide-react'
import { apiClient } from '../../api'

/** 面板条目:命令与技能统一成一张过滤列表(参考 Claude Code/ZCode 的混合搜索)。 */
export interface SlashItem {
  kind: 'command' | 'skill'
  name: string
  args: string
  description: string
}

/** 按 "/" 后的**首词**过滤——参数阶段的空格不参与过滤,否则
 *  "/rollback 1" 刚打出空格面板就消失,没法对着面板补参数。 */
export function filterSlashItems(items: SlashItem[], query: string): SlashItem[] {
  const token = (query.split(/\s/, 1)[0] ?? '').trim().toLowerCase()
  if (token === '') return items
  return items.filter(
    (item) =>
      item.name.toLowerCase().startsWith(token) || item.name.toLowerCase().includes(token),
  )
}

/** 面板数据源:挂载时拉一次 GET /commands(命令注册表 + 技能扫描)。
 *  拉不到就降级为空列表——输入与发送完全不受影响(命令在服务端照常可用)。 */
export function useSlashItems(): SlashItem[] {
  const [items, setItems] = useState<SlashItem[]>([])
  useEffect(() => {
    let cancelled = false
    void (async (): Promise<void> => {
      try {
        const res = await apiClient.listCommands?.()
        if (cancelled || res === undefined) return
        setItems([
          ...res.commands.map(
            (c): SlashItem => ({ kind: 'command', name: c.name, args: c.args, description: c.description }),
          ),
          ...res.skills.map(
            (s): SlashItem => ({ kind: 'skill', name: s.name, args: '', description: s.description }),
          ),
        ])
      } catch {
        // 面板降级:见上
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])
  return items
}

export interface SlashNav {
  open: boolean
  filtered: SlashItem[]
  index: number
  setIndex: (index: number) => void
  pick: (item: SlashItem) => void
  /**
   * textarea 的 onKeyDown 前置处理。返回 true = 事件已消费(调用方直接
   * return);false = 放行——调用方继续走自己的 Enter 发送分支(命令文本
   * 交给服务端执行)。
   */
  handleKeyDown: (
    event: React.KeyboardEvent<HTMLTextAreaElement>,
    onSend: () => void,
  ) => boolean
}

/** 键盘导航与补全逻辑(宿主只管把 textarea 的 onKeyDown 委托进来)。 */
export function useSlashNav(
  text: string,
  setText: (value: string) => void,
  items: SlashItem[],
): SlashNav {
  const [index, setIndex] = useState(0)
  const [dismissed, setDismissed] = useState(false)
  const query = text.startsWith('/') ? text.slice(1) : null
  const filtered = useMemo(
    () => (query === null ? [] : filterSlashItems(items, query)),
    [query, items],
  )
  const open = query !== null && !dismissed && filtered.length > 0
  useEffect(() => {
    setIndex(0)
  }, [query])
  useEffect(() => {
    if (!text.startsWith('/')) setDismissed(false)
  }, [text])

  /** 选中一项:命令与技能统一补全成 "/name "(ZCode 同款 token;技能可继续补任务)。 */
  const pick = (item: SlashItem): void => {
    setText(`/${item.name} `)
  }

  const handleKeyDown = (
    event: React.KeyboardEvent<HTMLTextAreaElement>,
    onSend: () => void,
  ): boolean => {
    if (!open) return false
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setIndex((prev) => (prev + 1) % filtered.length)
      return true
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault()
      setIndex((prev) => (prev - 1 + filtered.length) % filtered.length)
      return true
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      setDismissed(true)
      return true
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      const trimmed = text.trim()
      const exact = /^\/([a-z0-9_-]+)$/.exec(trimmed)
      // 完整条目(命令或技能)→ 直接放行发送:技能由服务端兜底起一轮
      const isExactEntry = exact !== null && items.some((item) => item.name === exact[1])
      // 只在"半截名字"(无空格且不是完整条目)时拦截做补全;
      // 完整条目与带参数的输入都放行给发送分支(服务端执行/校验)。
      if (!trimmed.includes(' ') && !isExactEntry) {
        event.preventDefault()
        const item = filtered[index]
        if (item !== undefined) pick(item)
        return true
      }
      setDismissed(true)
      onSend()
      return true
    }
    return false
  }

  return { open, filtered, index, setIndex, pick, handleKeyDown }
}

/** 输入框上方的斜杠面板。定位仿 Composer 的 plus-menu(卡片浮层,输入框正上方),
 *  键盘导航由宿主 textarea 的 onKeyDown 委托给 useSlashNav——焦点始终留在输入框。 */
function SlashPaletteImpl({
  items,
  activeIndex,
  onPick,
  onHover,
}: {
  items: SlashItem[]
  activeIndex: number
  onPick: (item: SlashItem) => void
  onHover: (index: number) => void
}): JSX.Element {
  // 高亮项跟随键盘:列表超出一屏时,↑↓ 移动要自动滚入可视区(block:nearest
  // 只滚必要的距离,不跳顶部)。否则高亮停在屏幕外,用户"按了没反应"(实测)。
  const activeRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  return (
    <div className="wb-slash" onMouseDown={(event) => event.preventDefault()}>
      {items.length === 0 ? (
        <div className="wb-slash__empty">没有匹配的命令或技能</div>
      ) : (
        items.map((item, index) => (
          <button
            key={`${item.kind}:${item.name}`}
            ref={index === activeIndex ? activeRef : undefined}
            type="button"
            className={`wb-slash__item${index === activeIndex ? ' wb-slash__item--active' : ''}`}
            onMouseEnter={(): void => onHover(index)}
            onClick={(): void => onPick(item)}
          >
            <span className="wb-slash__icon" aria-hidden="true">
              {item.kind === 'command' ? <Terminal size={13} /> : <Zap size={13} />}
            </span>
            <span className="wb-slash__name">{`/${item.name}`}</span>
            {item.kind === 'command' && item.args !== '' ? (
              <span className="wb-slash__args">{item.args}</span>
            ) : null}
            <span className="wb-slash__desc">{item.description}</span>
          </button>
        ))
      )}
      <div className="wb-slash__foot">↑↓ 选择 · Enter 确认 · Esc 关闭</div>
    </div>
  )
}

const SlashPalette = memo(SlashPaletteImpl)
export default SlashPalette
