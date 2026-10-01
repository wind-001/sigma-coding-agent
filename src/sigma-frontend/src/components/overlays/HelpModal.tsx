import { Fragment, useEffect } from 'react'
import { useAppActions } from '../../store/appStore'
import './overlays.css'

interface ShortcutEntry {
  key: string
  keys: readonly string[]
  label: string
}

const SHORTCUTS: readonly ShortcutEntry[] = [
  { key: 'ctrl-n', keys: ['Ctrl', 'N'], label: '新建任务' },
  { key: 'ctrl-k', keys: ['Ctrl', 'K'], label: '搜索命令面板' },
  { key: 'esc', keys: ['Esc'], label: '关闭弹层' },
]

/** 居中的帮助与快捷键弹窗 */
export default function HelpModal(): JSX.Element {
  const actions = useAppActions()

  const close = (): void => {
    actions.setOverlay(null)
  }

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault()
        actions.setOverlay(null)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [actions])

  return (
    <div className="help-modal-mask" onClick={close}>
      <div
        className="help-modal"
        role="dialog"
        aria-modal="true"
        aria-label="帮助与快捷键"
        onClick={(e: React.MouseEvent<HTMLDivElement>) => e.stopPropagation()}
      >
        <header className="help-modal__header">
          <h2 className="help-modal__title">帮助与快捷键</h2>
        </header>
        <div className="help-modal__body">
          <div className="help-modal__section-title">快捷键</div>
          <ul className="help-modal__shortcuts">
            {SHORTCUTS.map((shortcut) => (
              <li key={shortcut.key} className="help-modal__shortcut">
                <span className="help-modal__keys">
                  {shortcut.keys.map((k, i) => (
                    <Fragment key={k}>
                      {i > 0 ? <span className="help-modal__key-plus">+</span> : null}
                      <kbd className="help-modal__kbd">{k}</kbd>
                    </Fragment>
                  ))}
                </span>
                <span>{shortcut.label}</span>
              </li>
            ))}
          </ul>
          <p className="help-modal__note">
            当前为前端演示模式,数据保存在浏览器 localStorage;配置{' '}
            <code className="help-modal__code">VITE_SIGMA_API_BASE</code>{' '}
            并启动 sigma 后端后自动切换真实接口(设计方案见{' '}
            <code className="help-modal__code">docs/sigma-backend-api-design.md</code>)。
          </p>
        </div>
        <footer className="help-modal__footer">
          <button type="button" className="help-modal__ok" onClick={close}>
            知道了
          </button>
        </footer>
      </div>
    </div>
  )
}
