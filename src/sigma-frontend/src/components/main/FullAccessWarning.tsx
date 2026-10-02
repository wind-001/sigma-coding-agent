import { useState } from 'react'

interface FullAccessWarningProps {
  onCancel(): void
  /** 用户勾选知情并点「启用完全访问」后回调(调用方负责真正切档) */
  onEnable(): void
}

/**
 * 「完全访问」的严厉警告(两处入口共用:输入卡与工作台会话页,星辰
 * 2026-10-02 拍板"完全访问被切换时必须弹警告")。必须勾选知情才允许启用。
 * 文案是安全告知,不是免责声明:sigma 不是沙箱,这条事实写进 UI。
 */
export default function FullAccessWarning({
  onCancel,
  onEnable,
}: FullAccessWarningProps): JSX.Element {
  const [ack, setAck] = useState<boolean>(false)
  return (
    <>
      <div className="wb-warn-mask" onClick={onCancel} />
      <div className="wb-warn" role="alertdialog" aria-modal="true" aria-label="启用完全访问的风险告知">
        <h3 className="wb-warn__title">⚠ 启用「完全访问」前必读</h3>
        <ul className="wb-warn__list">
          <li>
            sigma <b>不是沙箱</b>:工具以你的用户权限执行<b>任意命令</b>;
          </li>
          <li>
            可以删除或覆盖<b>工作区之外</b>的任何文件,也可以把数据发送到网络;
          </li>
          <li>
            L1 路径沙箱只拦"写路径越出工作区",L2 快照只覆盖工作区内文件——
            <b>都挡不住上面两条</b>;
          </li>
          <li>字符串审批拦不住 python -c、base64、先写脚本再执行。</li>
        </ul>
        <p className="wb-warn__note">
          模型理解错你的意图时,以上行为可能<b>无意发生</b>。请只在受控目录中使用,
          且不要让它接触不信任的脚本。
        </p>
        <label className="wb-warn__ack">
          <input
            type="checkbox"
            checked={ack}
            onChange={(event: React.ChangeEvent<HTMLInputElement>): void =>
              setAck(event.target.checked)
            }
          />
          <span>我已理解并接受上述风险</span>
        </label>
        <div className="wb-warn__actions">
          <button
            type="button"
            className="wb-warn__btn wb-warn__btn--cancel"
            onClick={onCancel}
          >
            取消
          </button>
          <button
            type="button"
            className="wb-warn__btn wb-warn__btn--enable"
            disabled={!ack}
            onClick={onEnable}
          >
            启用完全访问
          </button>
        </div>
      </div>
    </>
  )
}
