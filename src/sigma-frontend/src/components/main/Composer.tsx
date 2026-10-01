import { useEffect, useRef, useState } from 'react'
import { ArrowUp, ChevronDown, FilePlus2, FolderPlus, Folder, GitBranch, Plus, ShieldCheck } from 'lucide-react'
import Dropdown from './Dropdown'
import { useAppActions, useAppState } from '../../store/appStore'
import { ACCESS_OPTIONS, type ModelInfo, type Project } from '../../api'

/** 分支下拉为装饰性选项(分支不入全局状态) */
const BRANCH_OPTIONS: readonly string[] = ['main', 'dev']
/** activeProjectId 无对应项目时的仓库占位文案 */
const REPO_PLACEHOLDER: string = '选择项目'

/** 任务输入卡片:头部仓库/分支选择条 + 正文输入 + 底部工具行(状态与动作均来自全局 store) */
export default function Composer(): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)
  const plusRef = useRef<HTMLDivElement | null>(null)
  /** 「完全访问」的严厉警告:必须勾选知情后才允许启用(星辰 2026-10-02)。 */
  const [showFullWarning, setShowFullWarning] = useState<boolean>(false)
  const [fullAck, setFullAck] = useState<boolean>(false)
  /** + 号的「引用/导入」菜单 */
  const [plusOpen, setPlusOpen] = useState<boolean>(false)

  const activeProject: Project | undefined = state.projects.find(
    (project: Project): boolean => project.id === state.activeProjectId,
  )
  const accessOption = ACCESS_OPTIONS.find((option): boolean => option.id === state.composerAccess)
  const repoLabel: string = activeProject?.name ?? REPO_PLACEHOLDER
  const accessLabel: string = accessOption?.label ?? state.composerAccess
  // 模型显示值:服务端注册表是数据源,当前选择不在列表里时显示第一项。
  const modelValue: string = state.models.some((m) => m.name === state.composerModel)
    ? state.composerModel
    : state.models[0]?.name ?? state.composerModel

  useEffect((): void => {
    if (state.focusComposerSignal > 0) {
      textareaRef.current?.focus()
    }
  }, [state.focusComposerSignal])

  // 点击外部关闭 + 号菜单
  useEffect((): (() => void) | undefined => {
    if (!plusOpen) return undefined
    const handleDocumentMouseDown = (event: MouseEvent): void => {
      if (plusRef.current !== null && event.target instanceof Node && !plusRef.current.contains(event.target)) {
        setPlusOpen(false)
      }
    }
    document.addEventListener('mousedown', handleDocumentMouseDown)
    return (): void => {
      document.removeEventListener('mousedown', handleDocumentMouseDown)
    }
  }, [plusOpen])

  const handleSend = (): void => {
    void actions.submitDraft()
  }

  const handleTextareaChange = (event: React.ChangeEvent<HTMLTextAreaElement>): void => {
    actions.setDraft(event.target.value)
  }

  const handleTextareaKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      handleSend()
    }
  }

  const handleRepoSelect = (name: string): void => {
    // 与侧栏切换器同一条路:服务端持久化 + 列表刷新,不再只是本地补丁。
    const project: Project | undefined = state.projects.find((p): boolean => p.name === name)
    if (project !== undefined && project.id !== state.activeProjectId) {
      void actions.switchProject(project.id)
    }
  }

  const handleAccessSelect = (label: string): void => {
    const option = ACCESS_OPTIONS.find((o): boolean => o.label === label)
    if (option === undefined) return
    if (option.id === 'full' && state.composerAccess !== 'full') {
      // 启用完全访问前必须过风险知情确认
      setFullAck(false)
      setShowFullWarning(true)
      return
    }
    actions.setComposerOpt({ access: option.id })
  }

  return (
    <section className="composer">
      <div className="composer__header">
        <Dropdown
          trigger={
            <>
              <Folder size={16} color="#565b63" />
              <span className="composer__select-value">{repoLabel}</span>
              <ChevronDown size={14} color="#9aa0aa" />
            </>
          }
          items={state.projects.map((project: Project): string => project.name)}
          value={repoLabel}
          onSelect={handleRepoSelect}
        />
        <Dropdown
          trigger={
            <>
              <GitBranch size={15} color="#565b63" />
              <span className="composer__select-value">{activeProject?.branch || 'main'}</span>
              <ChevronDown size={14} color="#9aa0aa" />
            </>
          }
          items={BRANCH_OPTIONS}
          value={activeProject?.branch || 'main'}
          onSelect={(): void => {}}
        />
      </div>

      <div className="composer__body">
        <textarea
          ref={textareaRef}
          className="composer__textarea"
          value={state.draft}
          onChange={handleTextareaChange}
          onKeyDown={handleTextareaKeyDown}
          placeholder="描述你的任务,Enter 发送——会在当前项目下新建会话并开始执行"
        />
      </div>

      <div className="composer__footer">
        {/* + 号 = 引用/导入菜单(引用文件路径插入输入框;导入工作区) */}
        <div className="composer__plus" ref={plusRef}>
          <button
            type="button"
            className={`composer__icon-btn${plusOpen ? ' composer__icon-btn--active' : ''}`}
            aria-label="添加"
            aria-expanded={plusOpen}
            title="引用文件或导入工作区"
            onClick={(): void => setPlusOpen((prev) => !prev)}
          >
            <Plus size={18} />
          </button>
          {plusOpen ? (
            <div className="composer__plus-menu" role="menu">
              <button
                type="button"
                className="composer__plus-item"
                role="menuitem"
                onClick={(): void => {
                  setPlusOpen(false)
                  actions.setOverlay('fs-file')
                }}
              >
                <FilePlus2 size={15} />
                <span>引用文件或目录…</span>
              </button>
              <button
                type="button"
                className="composer__plus-item"
                role="menuitem"
                onClick={(): void => {
                  setPlusOpen(false)
                  actions.setOverlay('fs-picker')
                }}
              >
                <FolderPlus size={15} />
                <span>导入工作区…</span>
              </button>
            </div>
          ) : null}
        </div>
        <Dropdown
          trigger={
            <span className="composer__trigger-accent">
              <ShieldCheck size={16} />
              <span className="composer__select-value--sm">{accessLabel}</span>
              <ChevronDown size={14} />
            </span>
          }
          items={ACCESS_OPTIONS.map((option): string => option.label)}
          value={accessLabel}
          onSelect={handleAccessSelect}
          menuWidth={112}
        />
        <div className="composer__spacer" aria-hidden="true" />
        <span className="composer__dot" aria-hidden="true" />
        <Dropdown
          trigger={
            <>
              <span className="composer__select-value--sm">{modelValue}</span>
              <ChevronDown size={14} color="#9aa0aa" />
            </>
          }
          items={state.models.map((model: ModelInfo): string => model.name)}
          value={modelValue}
          onSelect={(name: string): void => actions.setComposerOpt({ model: name })}
          align="right"
          menuWidth={220}
        />
        <button
          type="button"
          className="composer__send"
          aria-label="发送"
          title="新建会话并开始执行"
          onClick={handleSend}
        >
          <ArrowUp size={17} />
        </button>
      </div>

      {/* 完全访问的严厉警告(必须勾选知情才可启用) */}
      {showFullWarning ? <div className="wb-warn-mask" onClick={(): void => setShowFullWarning(false)} /> : null}
      {showFullWarning ? (
        <div className="wb-warn" role="alertdialog" aria-modal="true" aria-label="启用完全访问的风险告知">
          <h3 className="wb-warn__title">⚠ 启用「完全访问」前必读</h3>
          <ul className="wb-warn__list">
            <li>sigma <b>不是沙箱</b>:工具以你的用户权限执行<b>任意命令</b>;</li>
            <li>可以删除或覆盖<b>工作区之外</b>的任何文件,也可以把数据发送到网络;</li>
            <li>L1 路径沙箱只拦"写路径越出工作区",L2 快照只覆盖工作区内文件——
                <b>都挡不住上面两条</b>;</li>
            <li>字符串审批拦不住 python -c、base64、先写脚本再执行。</li>
          </ul>
          <p className="wb-warn__note">
            模型理解错你的意图时,以上行为可能<b>无意发生</b>。请只在受控目录中使用,
            且不要让它接触不信任的脚本。
          </p>
          <label className="wb-warn__ack">
            <input
              type="checkbox"
              checked={fullAck}
              onChange={(event: React.ChangeEvent<HTMLInputElement>): void =>
                setFullAck(event.target.checked)
              }
            />
            <span>我已理解并接受上述风险</span>
          </label>
          <div className="wb-warn__actions">
            <button
              type="button"
              className="wb-warn__btn wb-warn__btn--cancel"
              onClick={(): void => setShowFullWarning(false)}
            >
              取消
            </button>
            <button
              type="button"
              className="wb-warn__btn wb-warn__btn--enable"
              disabled={!fullAck}
              onClick={(): void => {
                actions.setComposerOpt({ access: 'full' })
                setShowFullWarning(false)
              }}
            >
              启用完全访问
            </button>
          </div>
        </div>
      ) : null}
    </section>
  )
}
