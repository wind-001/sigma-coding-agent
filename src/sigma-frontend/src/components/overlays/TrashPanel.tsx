import { useEffect, useState } from 'react'
import { FileJson, RotateCcw, Trash2, X } from 'lucide-react'
import { apiClient, type TrashEntry } from '../../api'
import { useAppActions } from '../../store/appStore'
import { formatDateTime } from '../../lib/time'
import './overlays.css'

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** 会话回收站(2026-10-03):删除的会话都在 ~/.sigma/trash,这里恢复或清空。
 *  恢复后 refreshAll 让侧栏列表即刻回来。 */
export default function TrashPanel(): JSX.Element {
  const actions = useAppActions()
  const [entries, setEntries] = useState<TrashEntry[] | null>(null)
  const [hint, setHint] = useState<string | null>(null)

  const close = (): void => {
    actions.setOverlay(null)
  }

  const load = (): void => {
    void (async (): Promise<void> => {
      try {
        const res = await apiClient.listTrash?.()
        setEntries(res?.entries ?? [])
      } catch {
        setEntries([])
      }
    })()
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 挂载拉一次,操作后手动刷新
  }, [])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') {
        e.preventDefault()
        close()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [actions])

  const handleRestore = (entry: TrashEntry): void => {
    void (async (): Promise<void> => {
      const restore = apiClient.restoreTrash
      if (restore === undefined) return
      try {
        await restore(entry.name)
        setHint(`已恢复「${entry.taskId}」,列表已刷新`)
        await actions.refreshAll()
        load()
      } catch (error) {
        setHint(`恢复失败:${error instanceof Error ? error.message : String(error)}`)
      }
    })()
  }

  const handleClear = (): void => {
    const clear = apiClient.clearTrash
    if (clear === undefined) return
    if (!window.confirm('确认清空回收站?清空后无法恢复。')) return
    void (async (): Promise<void> => {
      try {
        const res = await clear()
        setHint(`已清空 ${res.cleared} 个文件`)
        load()
      } catch (error) {
        setHint(`清空失败:${error instanceof Error ? error.message : String(error)}`)
      }
    })()
  }

  const sessions = (entries ?? []).filter((entry) => entry.kind === 'session')

  return (
    <>
      <div className="auto-panel-mask" onClick={close} />
      <aside className="auto-panel trash-panel" role="dialog" aria-modal="true" aria-label="会话回收站">
        <header className="auto-panel__header">
          <h2 className="auto-panel__title">会话回收站</h2>
          <button type="button" className="auto-panel__close" aria-label="关闭" onClick={close}>
            <X size={18} />
          </button>
        </header>
        <div className="auto-panel__body">
          {hint !== null ? <div className="trash-panel__hint">{hint}</div> : null}
          {entries === null ? (
            <div className="auto-panel__empty">加载中…</div>
          ) : sessions.length === 0 ? (
            <div className="auto-panel__empty">回收站是空的。删除的会话会先躺在这里,可随时恢复。</div>
          ) : (
            sessions.map((entry) => (
              <div key={entry.name} className="trash-panel__item">
                <FileJson size={15} aria-hidden="true" className="trash-panel__icon" />
                <div className="trash-panel__main">
                  <div className="trash-panel__task" title={entry.taskId}>
                    {entry.taskId}
                  </div>
                  <div className="trash-panel__meta">
                    {formatDateTime(
                      new Date(entry.deletedAt * 1000).toISOString().replace('Z', ''),
                    )}{' '}
                    · {formatSize(entry.sizeBytes)}
                  </div>
                </div>
                <button
                  type="button"
                  className="trash-panel__restore"
                  title="恢复到会话列表"
                  onClick={(): void => handleRestore(entry)}
                >
                  <RotateCcw size={13} aria-hidden="true" />
                  恢复
                </button>
              </div>
            ))
          )}
        </div>
        {sessions.length > 0 ? (
          <footer className="auto-panel__footer">
            <button type="button" className="trash-panel__clear" onClick={handleClear}>
              <Trash2 size={13} aria-hidden="true" />
              清空回收站
            </button>
          </footer>
        ) : null}
      </aside>
    </>
  )
}
