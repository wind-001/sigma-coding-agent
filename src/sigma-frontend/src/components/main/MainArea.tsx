import { useAppState } from '../../store/appStore'
import type { Task } from '../../api'
import TitleBar from './TitleBar'
import HomeView from './HomeView'
import TaskDetail from './TaskDetail'
import './main.css'

/** 主区域:标题栏常驻,内容区按视图分发(任务详情;任务缺失时回退首页) */
export default function MainArea(): JSX.Element {
  const state = useAppState()

  const selectedTask: Task | undefined =
    state.view === 'task' && state.selectedTaskId !== null
      ? state.tasks.find((task: Task): boolean => task.id === state.selectedTaskId)
      : undefined

  return (
    <main className={selectedTask !== undefined ? 'main main--task' : 'main'}>
      <TitleBar />
      {selectedTask !== undefined ? <TaskDetail task={selectedTask} /> : <HomeView />}
    </main>
  )
}
