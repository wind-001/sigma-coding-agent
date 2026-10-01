import { useEffect, useRef, useState } from 'react'

interface DropdownProps {
  /** 触发器内容(图标 + 文本 + 展开箭头等) */
  trigger: React.ReactNode
  /** 下拉选项列表 */
  items: readonly string[]
  /** 当前选中值 */
  value: string
  /** 选中某项后的回调 */
  onSelect: (value: string) => void
  /** 菜单对齐方式,默认靠左 */
  align?: 'left' | 'right'
  /** 菜单最小宽度,缺省 150 */
  menuWidth?: number
}

export default function Dropdown({
  trigger,
  items,
  value,
  onSelect,
  align = 'left',
  menuWidth,
}: DropdownProps): JSX.Element {
  const [open, setOpen] = useState<boolean>(false)
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect((): (() => void) | undefined => {
    if (!open) {
      return undefined
    }
    const handleDocumentMouseDown = (event: MouseEvent): void => {
      const node = rootRef.current
      const target = event.target
      if (node !== null && target instanceof Node && !node.contains(target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleDocumentMouseDown)
    return (): void => {
      document.removeEventListener('mousedown', handleDocumentMouseDown)
    }
  }, [open])

  const handleToggle = (): void => {
    setOpen((prev: boolean): boolean => !prev)
  }

  const handleSelect = (item: string): void => {
    onSelect(item)
    setOpen(false)
  }

  return (
    <div className="dropdown" ref={rootRef}>
      <button
        type="button"
        className="dropdown__trigger"
        onClick={handleToggle}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        {trigger}
      </button>
      {open && (
        <div
          className={`dropdown__menu dropdown__menu--${align}`}
          style={menuWidth === undefined ? undefined : { minWidth: menuWidth }}
          role="listbox"
        >
          {items.map((item: string): JSX.Element => (
            <button
              key={item}
              type="button"
              role="option"
              aria-selected={item === value}
              className="dropdown__item"
              onClick={(): void => handleSelect(item)}
            >
              {item}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
