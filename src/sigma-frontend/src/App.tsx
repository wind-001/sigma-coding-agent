import { useEffect } from 'react'
import Sidebar from './components/sidebar/Sidebar'
import MainArea from './components/main/MainArea'
import CommandPalette from './components/overlays/CommandPalette'
import AutomationsPanel from './components/overlays/AutomationsPanel'
import PluginsPanel from './components/overlays/PluginsPanel'
import HelpModal from './components/overlays/HelpModal'
import { AppProvider, useAppState, useAppActions } from './store/appStore'

function Toast(): JSX.Element | null {
  const { toast } = useAppState()
  if (toast === null) return null
  return <div className="toast">{toast}</div>
}

function Shell(): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (e.ctrlKey && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault()
        actions.newTaskDraft()
      }
      if (e.ctrlKey && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault()
        actions.togglePalette()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [actions])

  return (
    <div className="app-shell">
      <Sidebar />
      <MainArea />
      {state.paletteOpen ? <CommandPalette /> : null}
      {state.overlay === 'automations' ? <AutomationsPanel /> : null}
      {state.overlay === 'plugins' ? <PluginsPanel /> : null}
      {state.overlay === 'help' ? <HelpModal /> : null}
      <Toast />
    </div>
  )
}

export default function App(): JSX.Element {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  )
}
