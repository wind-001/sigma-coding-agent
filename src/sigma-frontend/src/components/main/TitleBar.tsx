import { CircleHelp, Keyboard, Minus, Square, SquareArrowOutUpRight, X } from 'lucide-react'
import { useAppActions } from '../../store/appStore'

/** 右上角标题栏:工具按钮组 + 模拟窗口控制按钮组(最小化/关闭为演示环境装饰) */
export default function TitleBar(): JSX.Element {
  const actions = useAppActions()

  const handlePopOut = (): void => {
    window.open(window.location.href, '_blank')
  }

  const handleToggleFullscreen = (): void => {
    try {
      if (document.fullscreenElement) {
        document.exitFullscreen().catch((error: unknown): void => {
          console.warn('退出全屏失败', error)
        })
      } else {
        document.documentElement.requestFullscreen().catch((error: unknown): void => {
          console.warn('进入全屏失败', error)
        })
      }
    } catch (error) {
      console.warn('全屏切换失败', error)
    }
  }

  return (
    <header className="titlebar">
      <div className="titlebar__group">
        <button
          type="button"
          className="titlebar__btn"
          aria-label="帮助"
          title="帮助"
          onClick={(): void => actions.setOverlay('help')}
        >
          <CircleHelp size={17} />
        </button>
        <button
          type="button"
          className="titlebar__btn"
          aria-label="在新窗口打开"
          title="在新窗口打开"
          onClick={handlePopOut}
        >
          <SquareArrowOutUpRight size={17} />
        </button>
        <button
          type="button"
          className="titlebar__btn"
          aria-label="快捷键"
          title="快捷键"
          onClick={(): void => actions.setOverlay('help')}
        >
          <Keyboard size={17} />
        </button>
      </div>
      <div className="titlebar__group titlebar__group--window">
        <button type="button" className="titlebar__btn" aria-label="最小化" title="演示环境">
          <Minus size={16} />
        </button>
        <button
          type="button"
          className="titlebar__btn"
          aria-label="最大化"
          title="全屏切换"
          onClick={handleToggleFullscreen}
        >
          <Square size={15} />
        </button>
        <button
          type="button"
          className="titlebar__btn titlebar__btn--close"
          aria-label="关闭"
          title="演示环境不可关闭"
        >
          <X size={18} />
        </button>
      </div>
    </header>
  )
}
