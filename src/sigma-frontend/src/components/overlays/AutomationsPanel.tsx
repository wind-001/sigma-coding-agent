import { useEffect, useState } from 'react'
import { ChevronRight, Clock, Mail, X } from 'lucide-react'
import type { Automation } from '../../api'
import { useAppActions, useAppState } from '../../store/appStore'
import { formatRelative } from '../../lib/time'
import './overlays.css'

interface AutomationRowProps {
  automation: Automation
  onToggle: (automationId: string) => void
}

function AutomationRow({ automation, onToggle }: AutomationRowProps): JSX.Element {
  return (
    <div className="auto-panel__item">
      <div className="auto-panel__item-main">
        <div className="auto-panel__item-name">{automation.name}</div>
        <div className="auto-panel__item-schedule">
          <Clock size={13} aria-hidden="true" />
          <span>{automation.schedule}</span>
        </div>
        <div className="auto-panel__item-run">
          {automation.lastRunAt !== null
            ? `上次运行 ${formatRelative(automation.lastRunAt)}`
            : '尚未运行'}
        </div>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={automation.enabled}
        aria-label={`启用或停用「${automation.name}」`}
        className={`auto-panel__switch${automation.enabled ? ' auto-panel__switch--on' : ''}`}
        onClick={() => onToggle(automation.id)}
      >
        <span className="auto-panel__switch-knob" />
      </button>
    </div>
  )
}

/** 右侧滑出的自动化管理面板。功能(如发送邮件)在这里**单列一条**
 *  (星辰 2026-10-06:邮件是自动化的一个功能,点击后详情在主区展示,
 *  不塞在面板里)。 */
export default function AutomationsPanel(): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [name, setName] = useState('')
  const [schedule, setSchedule] = useState('')

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

  const canCreate = name.trim() !== '' && schedule.trim() !== ''

  const handleCreate = (): void => {
    const trimmedName = name.trim()
    const trimmedSchedule = schedule.trim()
    if (trimmedName === '' || trimmedSchedule === '') return
    void actions.createAutomation({ name: trimmedName, schedule: trimmedSchedule })
    setName('')
    setSchedule('')
  }

  return (
    <>
      <div className="auto-panel-mask" onClick={close} />
      <aside className="auto-panel" role="dialog" aria-modal="true" aria-label="自动化">
        <header className="auto-panel__header">
          <h2 className="auto-panel__title">自动化</h2>
          <button type="button" className="auto-panel__close" aria-label="关闭" onClick={close}>
            <X size={18} />
          </button>
        </header>
        <div className="auto-panel__body">
          <div className="auto-panel__section">功能</div>
          <button
            type="button"
            className="auto-panel__feature"
            onClick={() => {
              actions.openMailFeature()
              close()
            }}
          >
            <span className="auto-panel__feature-icon" aria-hidden="true">
              <Mail size={14} />
            </span>
            <span className="auto-panel__feature-main">
              <span className="auto-panel__feature-name">发送邮件</span>
              <span className="auto-panel__feature-sub">配置第三方 SMTP,写信直寄</span>
            </span>
            <ChevronRight size={15} className="auto-panel__feature-chevron" aria-hidden="true" />
          </button>
          <div className="auto-panel__section">定时任务</div>
          {state.automations.length === 0 ? (
            <div className="auto-panel__empty">暂无自动化,在下方创建一个吧</div>
          ) : (
            state.automations.map((automation) => (
              <AutomationRow
                key={automation.id}
                automation={automation}
                onToggle={(id) => void actions.toggleAutomation(id)}
              />
            ))
          )}
        </div>
        <footer className="auto-panel__footer">
          <div className="auto-panel__create">
            <input
              className="auto-panel__input"
              value={name}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setName(e.target.value)}
              onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>): void => {
                if (e.key === 'Enter') handleCreate()
              }}
              placeholder="自动化名称"
              aria-label="自动化名称"
            />
            <div className="auto-panel__create-row">
              <input
                className="auto-panel__input"
                value={schedule}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSchedule(e.target.value)}
                onKeyDown={(e: React.KeyboardEvent<HTMLInputElement>): void => {
                  if (e.key === 'Enter') handleCreate()
                }}
                placeholder="周期,如「每日 09:00」"
                aria-label="执行周期"
              />
              <button
                type="button"
                className="auto-panel__add"
                disabled={!canCreate}
                onClick={handleCreate}
              >
                添加
              </button>
            </div>
          </div>
          <p className="auto-panel__note">
            待接入(σ-server M3):sigma 目前没有定时执行能力,调度器是 σ-server 的里程碑,
            见 sigma-frontend/docs/sigma-backend-api-design.md
          </p>
        </footer>
      </aside>
    </>
  )
}
