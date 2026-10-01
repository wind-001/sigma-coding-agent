import { Bug, Clock, Presentation, Timer } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useAppActions } from '../../store/appStore'

interface QuickActionItem {
  label: string
  icon: LucideIcon
  /** 点击 chip 后预填到输入框的任务草稿 */
  draft: string
}

const ACTIONS: readonly QuickActionItem[] = [
  { label: '周报总结', icon: Timer, draft: '帮我总结本周的工作进展,生成一份周报' },
  { label: '报错修复', icon: Bug, draft: '帮我定位并修复以下报错:' },
  { label: 'PPT 制作', icon: Presentation, draft: '帮我制作一份 PPT,主题:' },
  { label: '闲时任务', icon: Clock, draft: '创建一个闲时任务:' },
]

/** 输入卡下方的快捷操作 chips:预填草稿并聚焦输入框 */
export default function QuickActions(): JSX.Element {
  const actions = useAppActions()

  return (
    <div className="quick-actions">
      {ACTIONS.map((action: QuickActionItem): JSX.Element => (
        <button
          key={action.label}
          type="button"
          className="quick-action"
          onClick={(): void => {
            actions.setDraft(action.draft)
            actions.focusComposer()
          }}
        >
          <action.icon size={16} />
          <span className="quick-action__label">{action.label}</span>
        </button>
      ))}
    </div>
  )
}
