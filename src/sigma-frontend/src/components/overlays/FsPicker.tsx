import { useEffect, useState } from 'react'
import { ArrowUp, Folder, FolderPlus, File, FilePlus2, Loader2, X } from 'lucide-react'
import { apiClient, type FsListing } from '../../api'
import { useAppActions } from '../../store/appStore'
import './overlays.css'

interface FsPickerProps {
  /** dir = 导入工作区(只列子目录,选目录);file = 引用文件(列文件,点文件插路径) */
  mode?: 'dir' | 'file'
}

/**
 * 目录选择器:服务端目录浏览,逐级点选,不手输路径。
 * 浏览器拿不到本机绝对路径(File System Access API 只给句柄名),
 * 所以由桥接列目录/文件——这是本地 agent UI 的标准做法。
 * - dir 模式(选工作区):只列子目录,底部「选择此目录」确认;
 * - file 模式(引用路径):追加文件条目,点文件直接把路径插进输入框。
 */
export default function FsPicker({ mode = 'dir' }: FsPickerProps): JSX.Element | null {
  const actions = useAppActions()
  const [listing, setListing] = useState<FsListing | null>(null)
  const [busy, setBusy] = useState<boolean>(false)
  const isFile = mode === 'file'

  const close = (): void => actions.setOverlay(null)

  useEffect((): (() => void) => {
    let cancelled = false
    const load = async (path: string): Promise<void> => {
      if (apiClient.browseFs === undefined) return
      setBusy(true)
      try {
        const info = await apiClient.browseFs(path, { files: isFile })
        if (!cancelled) setListing(info)
      } catch {
        if (!cancelled) setListing({ path: '', parent: null, entries: [] })
      } finally {
        if (!cancelled) setBusy(false)
      }
    }
    void load('')
    return (): void => {
      cancelled = true
    }
  }, [isFile])

  const enter = (path: string): void => {
    if (apiClient.browseFs === undefined) return
    setBusy(true)
    apiClient
      .browseFs(path, { files: isFile })
      .then((info: FsListing): void => setListing(info))
      .catch((): void => undefined)
      .finally((): void => setBusy(false))
  }

  /** file 模式:把文件/目录路径插入输入框(换行拼接),随即收起 */
  const pick = (path: string): void => {
    actions.appendToDraft(path)
    close()
  }

  const confirm = (): void => {
    if (listing === null || listing.path === '') return
    if (isFile) {
      pick(listing.path)
      return
    }
    void actions.importWorkspace(listing.path)
    close()
  }

  return (
    <>
      <div className="fs-picker-mask" onClick={close} />
      <aside className="fs-picker" role="dialog" aria-modal="true" aria-label={isFile ? '引用文件或目录' : '选择工作区目录'}>
        <header className="fs-picker__header">
          <h2 className="fs-picker__title">{isFile ? '引用文件或目录' : '选择工作区目录'}</h2>
          <button type="button" className="fs-picker__close" aria-label="关闭" onClick={close}>
            <X size={18} />
          </button>
        </header>
        <div className="fs-picker__path" title={listing?.path}>
          {listing?.path !== undefined && listing.path !== '' ? listing.path : '此电脑'}
        </div>
        <div className="fs-picker__body">
          {busy ? (
            <div className="fs-picker__empty">
              <Loader2 size={18} className="fs-picker__spin" /> 读取中…
            </div>
          ) : listing?.error !== undefined && listing.error !== '' ? (
            <div className="fs-picker__empty">{listing.error}</div>
          ) : (listing?.entries.length ?? 0) === 0 ? (
            <div className="fs-picker__empty">{isFile ? '这里没有可引用的条目' : '没有子目录了'}</div>
          ) : (
            listing?.entries.map((entry) =>
              entry.isDir ? (
                <button
                  key={entry.path}
                  type="button"
                  className="fs-picker__entry"
                  onClick={(): void => enter(entry.path)}
                >
                  <Folder size={15} />
                  <span>{entry.name}</span>
                </button>
              ) : (
                <button
                  key={entry.path}
                  type="button"
                  className="fs-picker__entry fs-picker__entry--file"
                  title={`插入路径:${entry.path}`}
                  onClick={(): void => pick(entry.path)}
                >
                  <File size={15} />
                  <span>{entry.name}</span>
                </button>
              ),
            )
          )}
        </div>
        <footer className="fs-picker__footer">
          <button
            type="button"
            className="fs-picker__up"
            disabled={listing?.parent === null || listing === null || busy}
            onClick={(): void => enter(listing?.parent ?? '')}
          >
            <ArrowUp size={15} />
            上一级
          </button>
          <button
            type="button"
            className="fs-picker__confirm"
            disabled={busy || listing === null || listing.path === ''}
            onClick={confirm}
          >
            {isFile ? <FilePlus2 size={15} /> : <FolderPlus size={15} />}
            {isFile ? '引用此目录' : '选择此目录'}
          </button>
        </footer>
      </aside>
    </>
  )
}
