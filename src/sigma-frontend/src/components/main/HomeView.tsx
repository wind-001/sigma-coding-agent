import Composer from './Composer'
import Hero from './Hero'
import QuickActions from './QuickActions'

/** 首页:水印问候语 + 任务输入卡 + 快捷 chips(居中列布局) */
export default function HomeView(): JSX.Element {
  return (
    <div className="main__center">
      <Hero />
      <Composer />
      <QuickActions />
    </div>
  )
}
