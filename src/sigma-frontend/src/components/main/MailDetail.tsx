import { useEffect, useState } from 'react'
import { ChevronDown, Mail, Send, Settings2 } from 'lucide-react'
import type { EmailConfig } from '../../api'
import { apiClient } from '../../api'
import { useAppActions } from '../../store/appStore'

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

/** 裸 fetch 错误翻译:服务不在跑时浏览器只有一句 "Failed to fetch",
 *  用户不知道该干什么——指路到"重启服务+刷新"。 */
function mailErrorMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  if (/failed to fetch|networkerror|load failed/i.test(raw)) {
    return '连不上工作台服务(127.0.0.1:8301)——服务可能没在运行,重启服务后刷新页面再试。'
  }
  return raw
}

/** 自动化·邮件功能详情(主区,2026-10-06 星辰"邮件是自动化的一个功能,
 *  面板单列一条,点击详情在主区"):上半配置(第三方 SMTP),下半写信直寄。
 *  授权码只落 ~/.sigma/.env,表单回显只有"已保存"布尔。 */
export default function MailDetail(): JSX.Element {
  const actions = useAppActions()
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
      setNote({ kind: 'err', text: mailErrorMessage(err) })
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
      setNote({ kind: 'err', text: mailErrorMessage(err) })
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="mail-detail">
      <div className="mail-detail__head">
        <span className="auto-mail__icon" aria-hidden="true">
          <Mail size={16} />
        </span>
        <div className="mail-detail__titles">
          <h2 className="mail-detail__title">发送邮件</h2>
          <p className="mail-detail__sub">
            {config?.configured
              ? `SMTP 已配置 · ${config.host}${config.hasPassword ? '' : '(尚未填授权码)'}`
              : '尚未配置——先点「配置」填写第三方 SMTP 的服务器 / 账号 / 授权码'}
          </p>
        </div>
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
            <label className="auto-mail__label" htmlFor="mail-host">SMTP 服务器</label>
            <input
              id="mail-host"
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
              <label className="auto-mail__label" htmlFor="mail-port">端口</label>
              <input
                id="mail-port"
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
            <label className="auto-mail__label" htmlFor="mail-user">账号</label>
            <input
              id="mail-user"
              className="auto-mail__input"
              value={form.user}
              placeholder="如 lkk@qq.com"
              onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                setForm({ ...form, user: e.target.value })
              }
            />
          </div>
          <div className="auto-mail__row">
            <label className="auto-mail__label" htmlFor="mail-sender">发件人显示(可选)</label>
            <input
              id="mail-sender"
              className="auto-mail__input"
              value={form.sender}
              placeholder="如 sigma 工作台,留空 = 用账号地址"
              onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                setForm({ ...form, sender: e.target.value })
              }
            />
          </div>
          <div className="auto-mail__row">
            <label className="auto-mail__label" htmlFor="mail-password">授权码</label>
            <input
              id="mail-password"
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
          <label className="auto-mail__label" htmlFor="mail-to">收件人</label>
          <input
            id="mail-to"
            className="auto-mail__input"
            value={compose.to}
            placeholder="收件邮箱地址"
            onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
              setCompose({ ...compose, to: e.target.value })
            }
          />
        </div>
        <div className="auto-mail__row">
          <label className="auto-mail__label" htmlFor="mail-subject">主题</label>
          <input
            id="mail-subject"
            className="auto-mail__input"
            value={compose.subject}
            placeholder="邮件主题"
            onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
              setCompose({ ...compose, subject: e.target.value })
            }
          />
        </div>
        <div className="auto-mail__row">
          <label className="auto-mail__label" htmlFor="mail-body">正文</label>
          <textarea
            id="mail-body"
            className="auto-mail__input auto-mail__textarea"
            rows={7}
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

      <button type="button" className="mail-detail__back" onClick={actions.goHome}>
        返回首页
      </button>
    </div>
  )
}
