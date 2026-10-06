import { useEffect, useState } from 'react'
import { ChevronDown, Clock, Mail, Send, Settings2, X } from 'lucide-react'
import type { Automation, EmailConfig } from '../../api'
import { apiClient } from '../../api'
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

interface MailFormState {
  host: string
  port: number
  user: string
  sender: string
  password: string
  useTls: boolean
}

const EMPTY_MAIL_FORM: MailFormState = {
  host: '',
  port: 465,
  user: '',
  sender: '',
  password: '',
  useTls: false,
}

/** 邮件卡(2026-10-05,星辰"自动化里加发送邮件+SMTP 配置按钮"):
 *  配置=第三方 SMTP 的服务器/账号/授权码(授权码只落 ~/.sigma/.env,表单
 *  回显只有"已保存"布尔);写信=收件人/主题/正文直寄,服务端 smtplib 发送。
 *  样式走古风玻璃一族:圆角 16、宣纸暖底、墨色细描边,不做尖锐直角。 */
function MailCard(): JSX.Element {
  const [config, setConfig] = useState<EmailConfig | null>(null)
  const [configuring, setConfiguring] = useState(false)
  const [form, setForm] = useState<MailFormState>(EMPTY_MAIL_FORM)
  const [saving, setSaving] = useState(false)
  const [compose, setCompose] = useState({ to: '', subject: '', body: '' })
  const [sending, setSending] = useState(false)
  const [note, setNote] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  useEffect(() => {
    let cancelled = false
    void apiClient
      .getEmailConfig?.()
      .then((cfg) => {
        if (!cancelled) setConfig(cfg)
      })
      .catch(() => {
        if (!cancelled) setConfig(null)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const openConfig = (): void => {
    setNote(null)
    setForm({
      host: config?.host ?? '',
      port: config?.port ?? 465,
      user: config?.user ?? '',
      sender: config?.sender ?? '',
      password: '',
      useTls: config?.useTls ?? false,
    })
    setConfiguring(true)
  }

  const handleSave = async (): Promise<void> => {
    if (form.host.trim() === '' || form.user.trim() === '') return
    setSaving(true)
    setNote(null)
    try {
      await apiClient.saveEmailConfig?.({
        host: form.host.trim(),
        port: form.port,
        user: form.user.trim(),
        sender: form.sender.trim(),
        useTls: form.useTls,
        password: form.password,
      })
      const fresh = await apiClient.getEmailConfig?.()
      setConfig(fresh ?? null)
      setConfiguring(false)
      setNote({ kind: 'ok', text: '配置已保存,授权码收入 ~/.sigma/.env' })
    } catch (err) {
      setNote({ kind: 'err', text: err instanceof Error ? err.message : String(err) })
    } finally {
      setSaving(false)
    }
  }

  const canSend = compose.to.trim() !== '' && compose.subject.trim() !== '' && compose.body.trim() !== ''

  const handleSend = async (): Promise<void> => {
    if (!canSend || sending) return
    setSending(true)
    setNote(null)
    try {
      await apiClient.sendEmail?.({
        to: compose.to.trim(),
        subject: compose.subject.trim(),
        body: compose.body,
      })
      setCompose((prev) => ({ ...prev, body: '' }))
      setNote({ kind: 'ok', text: '已寄出' })
    } catch (err) {
      setNote({ kind: 'err', text: err instanceof Error ? err.message : String(err) })
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="auto-mail">
      <div className="auto-mail__head">
        <span className="auto-mail__icon" aria-hidden="true">
          <Mail size={14} />
        </span>
        <span className="auto-mail__title">邮件</span>
        <span
          className={`auto-mail__status${config?.configured ? ' auto-mail__status--on' : ''}`}
          title={config?.configured ? `SMTP ${config.host}` : '尚未配置 SMTP'}
        >
          {config === null ? '读取中…' : config.configured ? `已配置 · ${config.host}` : '未配置'}
        </span>
        <button
          type="button"
          className={`auto-mail__config-btn${configuring ? ' auto-mail__config-btn--open' : ''}`}
          aria-expanded={configuring}
          onClick={() => (configuring ? setConfiguring(false) : openConfig())}
        >
          <Settings2 size={13} aria-hidden="true" />
          配置
          <ChevronDown size={12} className="auto-mail__chevron" aria-hidden="true" />
        </button>
      </div>

      {configuring ? (
        <div className="auto-mail__form">
          <div className="auto-mail__row">
            <label className="auto-mail__label" htmlFor="auto-mail-host">SMTP 服务器</label>
            <input
              id="auto-mail-host"
              className="auto-mail__input"
              value={form.host}
              placeholder="如 smtp.qq.com"
              onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                setForm({ ...form, host: e.target.value })
              }
            />
          </div>
          <div className="auto-mail__row auto-mail__row--pair">
            <div>
              <label className="auto-mail__label" htmlFor="auto-mail-port">端口</label>
              <input
                id="auto-mail-port"
                className="auto-mail__input"
                type="number"
                min={1}
                max={65535}
                value={form.port}
                onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                  setForm({ ...form, port: Number(e.target.value) || 465 })
                }
              />
            </div>
            <div>
              <span className="auto-mail__label">加密</span>
              <div className="auto-mail__seg" role="group" aria-label="加密方式">
                <button
                  type="button"
                  className={`auto-mail__seg-btn${!form.useTls ? ' auto-mail__seg-btn--on' : ''}`}
                  onClick={(): void => setForm({ ...form, useTls: false, port: 465 })}
                >
                  SSL
                </button>
                <button
                  type="button"
                  className={`auto-mail__seg-btn${form.useTls ? ' auto-mail__seg-btn--on' : ''}`}
                  onClick={(): void => setForm({ ...form, useTls: true, port: 587 })}
                >
                  STARTTLS
                </button>
              </div>
            </div>
          </div>
          <div className="auto-mail__row">
            <label className="auto-mail__label" htmlFor="auto-mail-user">账号</label>
            <input
              id="auto-mail-user"
              className="auto-mail__input"
              value={form.user}
              placeholder="如 lkk@qq.com"
              onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                setForm({ ...form, user: e.target.value })
              }
            />
          </div>
          <div className="auto-mail__row">
            <label className="auto-mail__label" htmlFor="auto-mail-sender">发件人显示(可选)</label>
            <input
              id="auto-mail-sender"
              className="auto-mail__input"
              value={form.sender}
              placeholder="如 sigma 工作台<lkk@qq.com>"
              onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                setForm({ ...form, sender: e.target.value })
              }
            />
          </div>
          <div className="auto-mail__row">
            <label className="auto-mail__label" htmlFor="auto-mail-password">授权码</label>
            <input
              id="auto-mail-password"
              className="auto-mail__input"
              type="password"
              value={form.password}
              placeholder={config?.hasPassword ? '已保存 · 留空 = 不修改' : '邮箱设置里生成的 SMTP 授权码'}
              onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                setForm({ ...form, password: e.target.value })
              }
            />
          </div>
          <p className="auto-mail__hint">
            授权码只落 ~/.sigma/.env,工作台配置里只存变量名。QQ / 163
            等邮箱需先在设置里开通 SMTP 并生成授权码——填登录密码是登不上的。
          </p>
          <div className="auto-mail__actions">
            <button
              type="button"
              className="auto-mail__btn"
              onClick={(): void => setConfiguring(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="auto-mail__btn auto-mail__btn--primary"
              disabled={form.host.trim() === '' || form.user.trim() === '' || saving}
              onClick={() => void handleSave()}
            >
              {saving ? '保存中…' : '保存配置'}
            </button>
          </div>
        </div>
      ) : null}

      <div className="auto-mail__compose">
        <div className="auto-mail__row">
          <label className="auto-mail__label" htmlFor="auto-mail-to">收件人</label>
          <input
            id="auto-mail-to"
            className="auto-mail__input"
            value={compose.to}
            placeholder="收件邮箱地址"
            onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
              setCompose({ ...compose, to: e.target.value })
            }
          />
        </div>
        <div className="auto-mail__row">
          <label className="auto-mail__label" htmlFor="auto-mail-subject">主题</label>
          <input
            id="auto-mail-subject"
            className="auto-mail__input"
            value={compose.subject}
            placeholder="邮件主题"
            onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
              setCompose({ ...compose, subject: e.target.value })
            }
          />
        </div>
        <div className="auto-mail__row">
          <label className="auto-mail__label" htmlFor="auto-mail-body">正文</label>
          <textarea
            id="auto-mail-body"
            className="auto-mail__input auto-mail__textarea"
            rows={5}
            value={compose.body}
            placeholder="要寄出的内容…"
            onChange={(e: React.ChangeEvent<HTMLTextAreaElement>): void =>
              setCompose({ ...compose, body: e.target.value })
            }
          />
        </div>
        {note !== null ? (
          <p className={`auto-mail__note auto-mail__note--${note.kind}`} role="status">
            {note.text}
          </p>
        ) : null}
        <button
          type="button"
          className="auto-mail__btn auto-mail__btn--primary auto-mail__send"
          disabled={!canSend || sending}
          onClick={() => void handleSend()}
        >
          <Send size={13} aria-hidden="true" />
          {sending ? '寄出中…' : '寄 出'}
        </button>
      </div>
    </div>
  )
}

/** 右侧滑出的自动化管理面板 */
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

  const handleNameKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Enter') handleCreate()
  }

  const handleScheduleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>): void => {
    if (e.key === 'Enter') handleCreate()
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
          <MailCard />
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
              onKeyDown={handleNameKeyDown}
              placeholder="自动化名称"
              aria-label="自动化名称"
            />
            <div className="auto-panel__create-row">
              <input
                className="auto-panel__input"
                value={schedule}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSchedule(e.target.value)}
                onKeyDown={handleScheduleKeyDown}
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
