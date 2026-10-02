import { useState, type ReactNode } from 'react'

const INLINE_SPLIT = /(\*\*[^*]+\*\*|`[^`]+`)/g

/** 行内解析:**加粗** 与 `行内代码`,其余原样。不碰 HTML——纯 React 节点。 */
function renderInline(text: string): ReactNode[] {
  return text.split(INLINE_SPLIT).map((part, index): ReactNode => {
    if (part.length > 4 && part.startsWith('**') && part.endsWith('**')) {
      return <strong key={index}>{part.slice(2, -2)}</strong>
    }
    if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      return (
        <code key={index} className="rt-code">
          {part.slice(1, -1)}
        </code>
      )
    }
    return part
  })
}

/** 代码围栏:默认折叠(模型爱贴大段 JSON/日志,是直播区最大噪音源,
 * 星辰 2026-10-02"不重要的块支持折叠"),点按钮展开全文。 */
function CodeFence({ lines }: { lines: string[] }): JSX.Element {
  const [open, setOpen] = useState<boolean>(false)
  return (
    <div className="rt-fence">
      <pre className={`rt-pre${open ? ' rt-pre--open' : ''}`}>
        <code>{lines.join('\n')}</code>
      </pre>
      <button
        type="button"
        className="rt-fence__toggle"
        onClick={(): void => setOpen((prev) => !prev)}
      >
        {open ? '收起' : `展开代码(${lines.length} 行)`}
      </button>
    </div>
  )
}

/**
 * 轻量富文本渲染(不引库、不 innerHTML):气泡正文来自模型输出。
 * 支持:**加粗**、`行内代码`、小标题、分隔线、列表、``` 代码围栏(默认折叠)、
 * 【…】进度标记(模型自发的关键信息约定,突出显示与普通文本区分,
 * 星辰 2026-10-02"重要的文本块应与普通文本块有所区别")。
 */
export default function RichText({ text }: { text: string }): JSX.Element {
  const blocks: ReactNode[] = []
  let list: string[] = []
  let fence: string[] | null = null
  let n = 0
  const flushList = (): void => {
    if (list.length === 0) return
    const items = [...list]
    list = []
    blocks.push(
      <ul key={`rt-l-${blocks.length}`} className="rt-list">
        {items.map((item, index): ReactNode => (
          <li key={index}>{renderInline(item)}</li>
        ))}
      </ul>,
    )
  }
  const flushFence = (): void => {
    if (fence === null) return
    const lines = [...fence]
    fence = null
    blocks.push(<CodeFence key={`rt-f-${blocks.length}-${n++}`} lines={lines} />)
  }
  text.split('\n').forEach((raw, index): void => {
    const line = raw.trim()
    if (fence !== null) {
      // 围栏内:结束标记 ``` 出列,其余原样保留(不 trim 内容)
      if (/^```/.test(line)) {
        flushFence()
      } else {
        fence.push(raw)
      }
      return
    }
    if (/^```/.test(line)) {
      flushList()
      fence = []
      return
    }
    const heading = /^#{1,6}\s+(.*)$/.exec(line)
    if (heading !== null) {
      flushList()
      blocks.push(
        <div key={index} className="rt-heading">
          {renderInline(heading[1])}
        </div>,
      )
      return
    }
    if (/^-{3,}$/.test(line)) {
      flushList()
      blocks.push(<hr key={index} className="rt-hr" />)
      return
    }
    const bullet = /^[-*]\s+(.*)$/.exec(line)
    if (bullet !== null) {
      list.push(bullet[1])
      return
    }
    flushList()
    if (line === '') return
    // 【…】开头 = 模型自发的进度/关键标记,突出显示
    if (/^【.+/.test(line)) {
      blocks.push(
        <div key={index} className="rt-milestone">
          {renderInline(line)}
        </div>,
      )
      return
    }
    blocks.push(
      <p key={index} className="rt-p">
        {renderInline(line)}
      </p>,
    )
  })
  flushFence()
  flushList()
  return <div className="rt">{blocks}</div>
}
