import { useState } from 'react'
import { KeyRound, Pencil, Plus, Trash2, X } from 'lucide-react'
import { useAppActions, useAppState } from '../../store/appStore'
import type { ModelInfo, ModelSaveInput } from '../../api'
import './overlays.css'

/**
 * 档位候选值（快捷填充用）。
 *
 * ⚠ 这些是**常见写法**，不是「这个模型一定支持」的断言 —— 核心层不持
 * 厂商知识（见本组件 docstring）。用户点它只是省掉手打，不代替核实。
 *
 * OpenAI 风格的 low/medium/high 是 2026-10-02 对 DeepSeek **实测**确认的
 * 真三档（推理链 1051/1804/2243 字符，有梯度）；`none` 对应「关掉推理」，
 * 对 deepseek-reasoner 这类恒定推理的模型是**唯一**有意义的选择。
 * 智谱风格的 enabled/disabled 用于 thinking 对象。
 */
const EFFORT_PRESETS: Readonly<Record<string, readonly string[]>> = {
  reasoning_effort: ['low,medium,high', 'none', 'minimal'],
  thinking: ['enabled,disabled'],
}

/** 空表单(添加);编辑时由条目填充。密钥本体只存 ~/.sigma/.env,
 * 表单里只填**变量名**(变量名非机密,服务端原样回传,编辑可回显) */
function emptyForm(): ModelSaveInput {
  return {
    id: undefined,
    name: '',
    protocol: 'openai-compat',
    baseUrl: '',
    apiKeyEnv: '',
    modelId: '',
    efforts: [],
    effortStyle: 'reasoning_effort',
  }
}

/**
 * 模型设置:自定义条目(name/baseUrl/密钥变量名/modelId/档位)的管理面板。
 * 密钥本体只写在 ~/.sigma/.env,条目只记指向它的变量名——服务端从头到尾
 * 不接触 key 本身。档位取值与随附方式由**条目自己声明**——各家线格式不一
 * (OpenAI 风格 reasoning_effort / 智谱风格 thinking),核心层不持厂商知识,
 * 谁配模型谁知道自己的 API 接受什么。内置 preset 只读展示。
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
      apiKeyEnv: m.apiKeyEnv ?? '',
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
                  密钥变量名(~/.sigma/.env 里的变量;留空 = 用全局 SIGMA_API_KEY)
                </label>
                <input
                  className="models-form__input"
                  value={form.apiKeyEnv}
                  placeholder="例如:DEEPSEEK_API_KEY"
                  onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                    setForm({ ...form, apiKeyEnv: e.target.value })
                  }
                />
                <p className="models-form__hint">
                  密钥本体只写在 ~/.sigma/.env,不经过浏览器、不落工作台配置。
                </p>
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
                  <label className="models-form__label">
                    档位值(逗号分隔,留空 = 无档位)
                  </label>
                  <input
                    className="models-form__input"
                    value={effortsText}
                    placeholder="例如:low,medium,high"
                    onChange={(e: React.ChangeEvent<HTMLInputElement>): void =>
                      setEffortsText(e.target.value)
                    }
                  />
                  {/* 快捷填充：档位词表各家不同，手打容易错字。这里给的是
                      **常见写法**而非断言——点它只是省手打，不代替核实。 */}
                  <div className="models-form__quick">
                    <span className="models-form__quick-label">常用：</span>
                    {(EFFORT_PRESETS[form.effortStyle] ?? []).map(
                      (preset: string): JSX.Element => (
                        <button
                          key={preset}
                          type="button"
                          className="models-form__quick-btn"
                          onClick={(): void => setEffortsText(preset)}
                        >
                          {preset}
                        </button>
                      )
                    )}
                  </div>
                  {/* 实测得来的提示，不是文档抄的。见 EFFORT_PRESETS 上方。 */}
                  <p className="models-form__hint">
                    OpenAI 风格：low/medium/high 是真三档（DeepSeek 实测有梯度）；
                    <strong>reasoner 类模型（恒定推理）只认 none</strong>，填三档会
                    「能选但没效果」。智谱风格用 enabled/disabled。
                  </p>
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
              还没有自定义模型——点「添加模型」填入 BaseURL / 密钥变量名 / ModelID,
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
