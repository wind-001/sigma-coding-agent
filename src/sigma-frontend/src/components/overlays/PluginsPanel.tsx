import { useEffect } from 'react'
import { X } from 'lucide-react'
import type { Plugin } from '../../api'
import { useAppActions, useAppState } from '../../store/appStore'
import './overlays.css'

interface PluginCardProps {
  plugin: Plugin
  onSetInstalled: (pluginId: string, installed: boolean) => void
}

function PluginCard({ plugin, onSetInstalled }: PluginCardProps): JSX.Element {
  return (
    <div className="plugin-panel__card">
      <div className="plugin-panel__card-main">
        <div className="plugin-panel__card-name">
          {plugin.installed ? <span className="plugin-panel__card-dot" aria-hidden="true" /> : null}
          <span className="plugin-panel__card-name-text">{plugin.name}</span>
          <span className="plugin-panel__card-version">{plugin.version}</span>
        </div>
        <p className="plugin-panel__card-desc">{plugin.description}</p>
      </div>
      <div className="plugin-panel__card-action">
        {plugin.builtin ? (
          <span className="plugin-panel__card-builtin">内置</span>
        ) : plugin.installed ? (
          <button
            type="button"
            className="plugin-panel__btn plugin-panel__btn--uninstall"
            onClick={() => onSetInstalled(plugin.id, false)}
          >
            卸载
          </button>
        ) : (
          <button
            type="button"
            className="plugin-panel__btn plugin-panel__btn--install"
            onClick={() => onSetInstalled(plugin.id, true)}
          >
            安装
          </button>
        )}
      </div>
    </div>
  )
}

/** 右侧滑出的插件市场面板 */
export default function PluginsPanel(): JSX.Element {
  const state = useAppState()
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

  const setInstalled = (pluginId: string, installed: boolean): void => {
    void actions.setPluginInstalledById(pluginId, installed)
  }

  // AppState.plugins 的类型标注可能落在 DOM 全局 Plugin(navigator.plugins)上,
  // 在数据边界统一收敛为 API 契约的 Plugin 类型(store 修正导入后此转换无副作用)。
  const plugins = state.plugins as unknown as Plugin[]

  return (
    <>
      <div className="plugin-panel-mask" onClick={close} />
      <aside className="plugin-panel" role="dialog" aria-modal="true" aria-label="插件市场">
        <header className="plugin-panel__header">
          <h2 className="plugin-panel__title">插件市场</h2>
          <button type="button" className="plugin-panel__close" aria-label="关闭" onClick={close}>
            <X size={18} />
          </button>
        </header>
        <p className="plugin-panel__note">
          真实数据:内置工具 + extensions/skills 技能。装卸待接入(σ-server M3);
          扩展工具(extensions/*.py)装载即执行代码,只读工作台不列出。
        </p>
        <div className="plugin-panel__body">
          {plugins.length === 0 ? (
            <div className="plugin-panel__empty">暂无可用的插件</div>
          ) : (
            plugins.map((plugin) => (
              <PluginCard key={plugin.id} plugin={plugin} onSetInstalled={setInstalled} />
            ))
          )}
        </div>
      </aside>
    </>
  )
}
