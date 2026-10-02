import { useState } from 'react'
import { KeyRound, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useAppActions, useAppState } from '../../store/appStore'
import type { ModelInfo, ModelSaveInput } from '../../api'
import './overlays.css'

/** 空表单(添加);编辑时由条目填充,apiKey 恒空(服务端不回传,留空=保留) */
function emptyForm(): ModelSaveInput {
  return {
    id: undefined,
    name: '',
    protocol: 'openai-compat',
    baseUrl: '',
    apiKey: '',
    modelId: '',
    efforts: [],
    effortStyle: 'reasoning_effort',
  }
}

/**
 * 模型设置:自定义条目(name/baseUrl/apiKey/modelId/档位)的管理面板。
 * 档位取值与随附方式由**条目自己声明**——各家线格式不一(OpenAI 风格
 * reasoning_effort / 智谱风格 thinking),核心层不持厂商知识,谁配模型
 * 谁知道自己的 API 接受什么。内置 preset 只读展示。
 */
export default function ModelSettings(): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [form, setForm] = useState<ModelSaveInput | null>(null)
  const [effortsText, setEffortsText] = useState<string>('')
  const [error, setError] = useState<string | null>(null)

  const close = (): void => actions.setOverlay(null)

  const startAdd = (): void => {
    setForm(emptyForm())
    setEffortsText('')
    setError(null)
  }

  const startEdit = (m: ModelInfo): void => {
    setForm({
      id: m.id,
      name: m.name,
      protocol: m.protocol ?? 'openai-compat',
      baseUrl: m.baseUrl ?? '',
      apiKey: '',
      modelId: m.modelId ?? '',
      efforts: m.efforts,
      effortStyle: m.effortStyle ?? 'reasoning_effort',
    })
    setEffortsText(m.efforts.join(','))
    setError(null)
  }

  const save = (): void => {
    if (form === null) return
    if (form.name.trim() === '' || form.baseUrl.trim() === '' || form.modelId.trim() === '') {
      setError('显示名 / BaseURL / ModelID 均必填')
      return
    }
    void actions.saveModelConfig({
      ...form,
      name: form.name.trim(),
      baseUrl: form.baseUrl.trim(),
      modelId: form.modelId.trim(),
      efforts: effortsText
        .split(/[,,]/)
        .map((e) => e.trim())
        .filter((e) => e !== ''),
    })
    setForm(null)
  }

  const remove = (m: ModelInfo): void => {
    if (window.confirm(`移除模型「${m.name}」?已选它的会话不受影响,新任务需重选。`)) {
      void actions.removeModelConfig(m.id)
    }
  }

  const custom: ModelInfo[] = state.models.filter((m) => m.custom === true)
  const builtin: ModelInfo[] = state.models.filter((m) => m.custom !== true)

  return (
    <>
      <div className="fs-picker-mask" onClick={close} />
      <aside className="models-panel" role="dialog" aria-modal="true" aria-label="模型设置">
        <header className="fs-picker__header">
          <h2 className="fs-picker__title">模型设置</h2>
          <div className="models-panel__header-actions">
            <button type="button" className="models-panel__add" onClick={startAdd}>
              <Plus size={15} />
              添加模型
            </button>
            <button type="button" className="fs-picker__close" aria-label="关闭" onClick={close}>
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="models-panel__body">
          {/* 表单(添加/编辑) */}
          {form !== null ? (
            <div className="models-form">
              <div className="models-form__row">
                <label className="models-form__label">显示名(任务列表与下拉里看到的名字)</label>
                <input
                  className="models-form__input"
                  value={form.name}
                  placeholder="例如:GLM-5.3-Flash"
                  onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                    setForm({ ...form, name: e.target.value })
                  }
                />
              </div>
              <div className="models-form__row">
                <label className="models-form__label">协议</label>
                <select
                  className="models-form__input"
                  value={form.protocol}
                  onChange={(e: React.ChangeEvent<HTMLSelectElement>): void =>
                    setForm({ ...form, protocol: e.target.value })
                  }
                >
                  <option value="openai-compat">OpenAI 兼容</option>
                  <option value="anthropic">Anthropic</option>
                </select>
              </div>
              <div className="models-form__row">
                <label className="models-form__label">BaseURL</label>
                <input
                  className="models-form__input"
                  value={form.baseUrl}
                  placeholder="例如:https://open.bigmodel.cn/api/paas/v4"
                  onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                    setForm({ ...form, baseUrl: e.target.value })
                  }
                />
              </div>
              <div className="models-form__row">
                <label className="models-form__label">
                  API Key{form.id !== undefined ? '(留空 = 保留原值)' : ''}
                </label>
                <input
                  className="models-form__input"
                  type="password"
                  value={form.apiKey}
                  placeholder={form.id !== undefined && form.apiKey === '' ? '••••••' : 'sk-…'}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                    setForm({ ...form, apiKey: e.target.value })
                  }
                />
              </div>
              <div className="models-form__row">
                <label className="models-form__label">ModelID(实际发给 API 的模型 id)</label>
                <input
                  className="models-form__input"
                  value={form.modelId}
                  placeholder="例如:glm-5.3-flash"
                  onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                    setForm({ ...form, modelId: e.target.value })
                  }
                />
              </div>
              <div className="models-form__row models-form__row--pair">
                <div>
                  <label className="models-form__label">档位值(逗号分隔,留空 = 无档位)</label>
                  <input
                    className="models-form__input"
                    value={effortsText}
                    placeholder="例如:开启,关闭 或 low,medium,high"
                    onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                      setEffortsText(e.target.value)
                    }
                  />
                </div>
                <div>
                  <label className="models-form__label">随附方式</label>
                  <select
                    className="models-form__input"
                    value={form.effortStyle}
                    disabled={effortsText.trim() === ''}
                    onChange={(e: React.ChangeEvent<HTMLSelectElement>): void =>
                      setForm({ ...form, effortStyle: e.target.value })
                    }
                  >
                    <option value="reasoning_effort">reasoning_effort(OpenAI 风格)</option>
                    <option value="thinking">thinking(智谱风格)</option>
                  </select>
                </div>
              </div>
              {error !== null ? <p className="models-form__error">{error}</p> : null}
              <div className="models-form__actions">
                <button
                  type="button"
                  className="models-form__btn"
                  onClick={(): void => setForm(null)}
                >
                  取消
                </button>
                <button type="button" className="models-form__btn models-form__btn--primary" onClick={save}>
                  保存
                </button>
              </div>
            </div>
          ) : null}

          {/* 自定义条目 */}
          <div className="models-panel__section">自定义模型</div>
          {custom.length === 0 ? (
            <p className="models-panel__empty">
              还没有自定义模型——点「添加模型」填入 BaseURL / API Key / ModelID,
              下拉与执行立即用它。
            </p>
          ) : (
            custom.map((m) => (
              <div key={m.id} className="models-row">
                <div className="models-row__main">
                  <span className="models-row__name">{m.name}</span>
                  {m.hasKey === true ? (
                    <KeyRound size={12} className="models-row__key" aria-label="已配密钥" />
                  ) : null}
                  <span className="models-row__meta">
                    {m.modelId} · {m.baseUrl}
                  </span>
                  {m.efforts.length > 0 ? (
                    <span className="models-row__efforts">档位:{m.efforts.join(' / ')}</span>
                  ) : null}
                </div>
                <div className="models-row__actions">
                  <button type="button" className="models-row__btn" aria-label={`编辑 ${m.name}`} onClick={(): void => startEdit(m)}>
                    <Pencil size={14} />
                  </button>
                  <button type="button" className="models-row__btn" aria-label={`移除 ${m.name}`} onClick={(): void => remove(m)}>
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>
            ))
          )}

          {/* 内置 preset(只读) */}
          <div className="models-panel__section">内置预设(CLI --preset 同源,只读)</div>
          {builtin.map((m) => (
            <div key={m.id} className="models-row models-row--builtin">
              <div className="models-row__main">
                <span className="models-row__name">{m.name}</span>
                <span className="models-row__meta">
                  {m.modelId} · {m.baseUrl}
                </span>
              </div>
            </div>
          ))}
        </div>
      </aside>
    </>
  )
}
