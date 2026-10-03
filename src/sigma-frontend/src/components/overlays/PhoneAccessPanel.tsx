import { useEffect, useState } from 'react'
import { Copy, Check, Smartphone, X } from 'lucide-react'
import { apiClient } from '../../api'
import { useAppActions } from '../../store/appStore'
import './overlays.css'

/** 手机访问面板(2026-10-03):列出本机内网地址,手机同网段浏览器打开即可用。
 *
 *  ⚠ 如实告知两个前提:服务默认只监听 127.0.0.1(手机连不上),
 *  要用 ``--host 0.0.0.0`` 重启;且工作台**没有鉴权**,开放局域网 =
 *  同网段任何人都能驱动你的 agent——这是安全边界,不是 bug。 */
export default function PhoneAccessPanel(): JSX.Element {
  const actions = useAppActions()
  const [urls, setUrls] = useState<string[] | null>(null)
  const [copied, setCopied] = useState<string | null>(null)

  const close = (): void => {
    actions.setOverlay(null)
  }

  useEffect(() => {
    let cancelled = false
    void (async (): Promise<void> => {
      try {
        const res = await apiClient.listAddresses?.()
        if (!cancelled && res !== undefined) setUrls(res.urls)
      } catch {
        if (!cancelled) setUrls([])
      }
    })()
    return () => {
      cancelled = true
    }
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

  const handleCopy = (url: string): void => {
    void navigator.clipboard
      .writeText(url)
      .then(() => {
        setCopied(url)
        window.setTimeout(() => setCopied(null), 1500)
      })
      .catch(() => setCopied(null))
  }

  return (
    <>
      <div className="auto-panel-mask" onClick={close} />
      <aside className="auto-panel phone-panel" role="dialog" aria-modal="true" aria-label="手机访问">
        <header className="auto-panel__header">
          <h2 className="auto-panel__title">手机访问</h2>
          <button type="button" className="auto-panel__close" aria-label="关闭" onClick={close}>
            <X size={18} />
          </button>
        </header>
        <div className="auto-panel__body">
          <p className="phone-panel__hint">
            <Smartphone size={14} aria-hidden="true" />
            手机与电脑连**同一个 WiFi**,用浏览器打开下面的地址,即可在手机上使用这套工作台。
          </p>
          {urls === null ? (
            <div className="auto-panel__empty">获取中…</div>
          ) : urls.length === 0 ? (
            <div className="auto-panel__empty">没有找到内网地址(无可用网卡?)</div>
          ) : (
            urls.map((url) => (
              <div key={url} className="phone-panel__row">
                <code className="phone-panel__url">{url}</code>
                <button
                  type="button"
                  className="phone-panel__copy"
                  onClick={(): void => handleCopy(url)}
                  title="复制地址"
                >
                  {copied === url ? <Check size={13} /> : <Copy size={13} />}
                  {copied === url ? '已复制' : '复制'}
                </button>
              </div>
            ))
          )}
          <p className="phone-panel__warn">
            打不开时按序查:① 服务以默认方式启动(0.0.0.0 监听,重启后无需带参);
            ② 防火墙规则 <code>sigma-workbench-8301</code> 在(已配置,仅限本地子网);
            ③ 路由器没开 AP 隔离。
            ⚠ 工作台没有登录鉴权:**同一 WiFi 里任何人都能操作**,只在可信网络开放。
          </p>
        </div>
      </aside>
    </>
  )
}
