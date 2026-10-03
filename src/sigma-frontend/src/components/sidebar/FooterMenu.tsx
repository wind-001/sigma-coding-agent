import { useEffect, useRef, useState } from 'react'
import { CircleHelp, Info, Settings, Smartphone, Trash2, User } from 'lucide-react'
import { apiClient, type SystemStatus } from '../../api'
import { useAppActions } from '../../store/appStore'

/** 工作区路径缩略:只留最后两段(状态区一行放得下,悬停看全路径)。 */
function shortWorkspace(workspace: string): string {
  const parts = workspace.split(/[\\/]/).filter((part) => part !== '')
  return parts.length <= 2 ? workspace : `…\\${parts.slice(-2).join('\\')}`
}

function formatUptime(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`
  const hours = Math.floor(seconds / 3600)
  return `${hours} 小时 ${Math.floor((seconds % 3600) / 60)} 分`
}

/** 底部用户区菜单(2026-10-03,对标 Cursor/ChatGPT 桌面端:头像 → 工作台菜单)。
 *  状态区数据来自 GET /system/status,菜单项打开各自浮层。 */
export default function FooterMenu(): JSX.Element {
  const actions = useAppActions()
  const [open, setOpen] = useState<boolean>(false)
  const [status, setStatus] = useState<SystemStatus | null>(null)
  const rootRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!open) return undefined
    const handleDocumentMouseDown = (event: MouseEvent): void => {
      if (rootRef.current !== null && event.target instanceof Node && !rootRef.current.contains(event.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handleDocumentMouseDown)
    return (): void => {
      document.removeEventListener('mousedown', handleDocumentMouseDown)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    void (async (): Promise<void> => {
      try {
        const res = await apiClient.getSystemStatus?.()
        if (!cancelled && res !== undefined) setStatus(res)
      } catch {
        // 状态区拉不到就保持占位:菜单其余项照常可用
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open])

  const openOverlay = (id: 'phone-access' | 'trash' | 'help' | 'model-settings'): void => {
    setOpen(false)
    actions.setOverlay(id)
  }

  return (
    <div className="footer-menu" ref={rootRef}>
      <button
        type="button"
        className="footer-menu__trigger"
        aria-expanded={open}
        title="工作台菜单"
        onClick={(): void => setOpen((prev) => !prev)}
      >
        <span className="sidebar__avatar">
          <User size={18} aria-hidden="true" />
        </span>
        <span className="sidebar__username">sigma 本机工作台</span>
      </button>
      {open ? (
        <div className="footer-menu__pop" role="menu">
          <div className="footer-menu__status">
            <div className="footer-menu__status-title">
              <Info size={13} aria-hidden="true" />
              <span>工作台状态</span>
            </div>
            {status === null ? (
              <div className="footer-menu__muted">状态获取中…</div>
            ) : (
              <>
                <div className="footer-menu__status-row">
                  <span>版本</span>
                  <code>{status.version}</code>
                </div>
                <div className="footer-menu__status-row">
                  <span>工作区</span>
                  <code title={status.workspace}>{shortWorkspace(status.workspace)}</code>
                </div>
                <div className="footer-menu__status-row">
                  <span>会话</span>
                  <code>{status.sessions}</code>
                </div>
                <div className="footer-menu__status-row">
                  <span>已运行</span>
                  <code>{formatUptime(status.uptimeSeconds)}</code>
                </div>
              </>
            )}
          </div>
          <button
            type="button"
            role="menuitem"
            className="footer-menu__item"
            onClick={(): void => openOverlay('phone-access')}
          >
            <Smartphone size={14} aria-hidden="true" />
            <span>手机访问…</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className="footer-menu__item"
            onClick={(): void => openOverlay('trash')}
          >
            <Trash2 size={14} aria-hidden="true" />
            <span>会话回收站</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className="footer-menu__item"
            onClick={(): void => openOverlay('help')}
          >
            <CircleHelp size={14} aria-hidden="true" />
            <span>帮助与快捷键</span>
          </button>
          <button
            type="button"
            role="menuitem"
            className="footer-menu__item"
            onClick={(): void => openOverlay('model-settings')}
          >
            <Settings size={14} aria-hidden="true" />
            <span>模型设置</span>
          </button>
        </div>
      ) : null}
    </div>
  )
}
