import { useEffect, useState } from 'react'
import { ArrowLeft, Folder } from 'lucide-react'
import {
  apiClient,
  STATUS_META,
  type Task,
  type TaskEvent,
  type TaskEventKind,
  type TaskTimeline,
} from '../../api'
import { formatDateTime, formatRelative } from '../../lib/time'
import { useAppActions, useAppState } from '../../store/appStore'

/** 十六进制颜色 → rgba 字符串(用于状态徽章底色的透明度) */
function hexToRgba(hex: string, alpha: number): string {
  const body: string = hex.replace('#', '')
  const r: number = parseInt(body.slice(0, 2), 16)
  const g: number = parseInt(body.slice(2, 4), 16)
  const b: number = parseInt(body.slice(4, 6), 16)
  return `rgba(${r}, ${g}, ${b}, ${alpha})`
}

/** 时间线圆点颜色:status 事件跟随当前任务状态色,其余按事件类型取固定色 */
function eventDotColor(kind: TaskEventKind, statusColor: string): string {
  switch (kind) {
    case 'created':
    case 'note':
      return '#9aa0aa'
    case 'message':
      return '#3b82f6'
    case 'tool_call':
      return '#8b5cf6'
    case 'approval':
      return '#ff8f1f'
    case 'status':
      return statusColor
  }
}

/** 运行时间线里一枚指标 chip(label + value)。 */
function MetricChip({ label, value, title }: { label: string; value: string; title?: string }): JSX.Element {
  return (
    <span className="wb-chip" title={title}>
      <span className="wb-chip__label">{label}</span>
      <span className="wb-chip__value">{value}</span>
    </span>
  )
}

interface TaskDetailProps {
  /** 当前展示的任务(由 MainArea 从 state.tasks 解析后传入) */
  task: Task
}

/**
 * 会话详情页(只读工作台):标题 / 状态徽章 / 元信息 / 描述 /
 * 运行时间线(观测层指标) / 动态回放。
 *
 * 数据源 = 真实 sigma 会话 JSONL(经只读桥接服务);执行控制
 * (开始/推进/打断/审批)是 σ-server M2 的范围——如实标"待接入",
 * 不渲染点了必然失败的假按钮。
 */
export default function TaskDetail({ task }: TaskDetailProps): JSX.Element {
  const state = useAppState()
  const actions = useAppActions()
  const [timeline, setTimeline] = useState<TaskTimeline | null>(null)
  const [composeText, setComposeText] = useState<string>('')
  const [sending, setSending] = useState<boolean>(false)
  const composeReady: boolean = composeText.trim().length > 0 && !sending

  // 运行时间线:可选方法(mock 未实现时跳过);失败静默(时间线是增强,不是事实源)。
  useEffect((): (() => void) => {
    let cancelled = false
    const loader = apiClient.getTaskTimeline
    if (loader === undefined) {
      return (): void => undefined
    }
    loader(task.id)
      .then((report: TaskTimeline | null): void => {
        if (!cancelled) setTimeline(report)
      })
      .catch((): void => {
        if (!cancelled) setTimeline(null)
      })
    return (): void => {
      cancelled = true
    }
  }, [task.id])

  const project = state.projects.find((p): boolean => p.id === task.projectId)
  const projectName: string = project?.name ?? '未知工作区'
  const statusMeta = STATUS_META[task.status]
  const engineLabel: string = [task.model, task.effort].filter((part) => part !== '').join(' · ')

  const handleSendCompose = (): void => {
    if (!composeReady || sending) return
    const text = composeText
    setSending(true)
    void (async (): Promise<void> => {
      try {
        await actions.sendMessage(task.id, text)
        setComposeText('')
      } finally {
        setSending(false)
      }
    })()
  }

  return (
    <div className="task-detail">
      <div className="task-detail__inner">
        <div className="task-detail__topbar">
          <button
            type="button"
            className="task-detail__back"
            aria-label="返回首页"
            onClick={(): void => actions.goHome()}
          >
            <ArrowLeft size={18} />
          </button>
          <span className="task-detail__crumb">{projectName} / 会话</span>
        </div>

        <div className="task-detail__title-row">
          <h1 className="task-detail__title">{task.title}</h1>
          <span
            className="status-badge"
            style={{ backgroundColor: hexToRgba(statusMeta.color, 0.12), color: statusMeta.color }}
          >
            <span className="status-badge__dot" style={{ backgroundColor: statusMeta.color }} />
            <span className="status-badge__text">{statusMeta.label}</span>
          </span>
        </div>

        <div className="task-detail__meta">
          <span className="task-detail__meta-item">
            <Folder size={14} />
            {projectName}
          </span>
          <span className="task-detail__meta-item">会话 id {task.id}</span>
          <span className="task-detail__meta-item">创建于 {formatDateTime(task.createdAt)}</span>
          <span className="task-detail__meta-item">更新于 {formatRelative(task.updatedAt)}</span>
          {engineLabel !== '' ? (
            <span className="task-detail__meta-item">{engineLabel}</span>
          ) : null}
        </div>

        {task.description !== '' ? (
          <div className="task-detail__desc-card">
            <p className="task-detail__desc">{task.description}</p>
          </div>
        ) : null}

        {task.status !== 'running' && task.status !== 'archived' ? (
          <div className="task-detail__compose">
            <textarea
              className="task-detail__compose-input"
              value={composeText}
              rows={2}
              placeholder={
                task.status === 'draft'
                  ? '发送首条消息,会话开始执行(Enter 发送,Shift+Enter 换行)'
                  : '继续这条会话,Enter 发送(同步执行一轮,完成后回放自动更新)'
              }
              onChange={(event: React.ChangeEvent<HTMLTextAreaElement>): void =>
                setComposeText(event.target.value)
              }
              onKeyDown={(event: React.KeyboardEvent<HTMLTextAreaElement>): void => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  handleSendCompose()
                }
              }}
            />
            <div className="task-detail__compose-foot">
              <span className="task-detail__compose-hint">
                {sending
                  ? '正在执行(同步一轮),完成后自动更新…'
                  : '工具调用自动放行(L1 路径沙箱 + L2 影子快照在岗);审批确认 / 打断待接入 σ-server M2'}
              </span>
              <button
                type="button"
                className="task-detail__btn task-detail__btn--primary task-detail__compose-send"
                disabled={!composeReady}
                onClick={handleSendCompose}
              >
                发送
              </button>
            </div>
          </div>
        ) : null}

        {timeline !== null ? (
          <div className="wb-metrics">
            <h2 className="task-detail__section-title">运行时间线</h2>
            <div className="wb-chips">
              <MetricChip
                label="轮数"
                value={String(timeline.rounds.length)}
                title="LLM 调用轮次(与 AgentLoop 的 round 同口径)"
              />
              <MetricChip
                label="token"
                value={`${timeline.totalPrompt} / ${timeline.totalCompletion}`}
                title="prompt / completion 合计(provider 返回的真实用量)"
              />
              <MetricChip
                label="缓存命中"
                value={timeline.cacheRate !== null ? `${Math.round(timeline.cacheRate * 100)}%` : '—'}
                title="cached / prompt(7.5 口径);无用量数据时不可用"
              />
              <MetricChip
                label="工具错误"
                value={String(timeline.toolErrors)}
                title="执行失败的工具调用数(错误回给模型纠错)"
              />
              {timeline.truncated > 0 ? (
                <MetricChip label="截断" value={String(timeline.truncated)} title="触发输出截断的调用数" />
              ) : null}
              {timeline.injected > 0 ? (
                <MetricChip label="注入" value={String(timeline.injected)} title="steering / 信箱注入次数" />
              ) : null}
              {timeline.approvals.length > 0 ? (
                <MetricChip
                  label="审批"
                  value={`${timeline.approvals.filter((a) => a.allowed).length}/${timeline.approvals.length}`}
                  title="审批放行/总数"
                />
              ) : null}
              <MetricChip
                label="耗时"
                value={timeline.wallSeconds !== null ? `${timeline.wallSeconds.toFixed(1)}s` : '—'}
                title={timeline.wallApprox ? '由消息时间戳差近似(≈)' : '端到端墙钟'}
              />
              <MetricChip
                label="trace"
                value={timeline.hasTrace ? '在场' : '无(近似)'}
                title="有无逐事件 trace 文件(P5-批次1 观测层)"
              />
            </div>
            {timeline.rounds.length > 0 ? (
              <ul className="wb-rounds">
                {timeline.rounds.map((round) => (
                  <li key={round.index} className="wb-round">
                    <span className="wb-round__no">#{round.index}</span>
                    <span className="wb-round__model">{round.model || '—'}</span>
                    <span className="wb-round__tokens">
                      {round.promptTokens}+{round.completionTokens}
                      {round.cachedTokens > 0 ? `(${round.cachedTokens} 缓存)` : ''}
                    </span>
                    <span className="wb-round__latency">
                      {round.latencyMs !== null ? `${round.latencyMs}ms${round.latencyApprox ? '≈' : ''}` : '—'}
                    </span>
                    <span className="wb-round__tools">
                      {round.tools.map((tool, i) => (
                        <span
                          key={`${round.index}-${i}`}
                          className={tool.ok ? 'wb-tool wb-tool--ok' : 'wb-tool wb-tool--bad'}
                          title={`${tool.name}${tool.durationMs !== null ? ` · ${tool.durationMs}ms` : ''}`}
                        >
                          {tool.name}
                        </span>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        <h2 className="task-detail__section-title">动态回放</h2>
        <div className="timeline">
          {task.events.length === 0 ? (
            <p className="timeline__empty">暂无动态</p>
          ) : (
            <ul className="timeline__list">
              {task.events.map((event: TaskEvent): JSX.Element => (
                <li key={`${event.at}-${event.id}`} className="timeline__item">
                  <span
                    className="timeline__dot"
                    style={{ backgroundColor: eventDotColor(event.kind, statusMeta.color) }}
                  />
                  <span className="timeline__text">{event.text}</span>
                  <span className="timeline__time">{formatDateTime(event.at)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="task-detail__actions task-detail__actions--readonly">
          <p className="task-detail__pending-note">
            执行环(MVP)已接:消息经 sigma 真实跑一轮——断点续跑 / 影子快照 / trace 与 CLI 同源。
            待接入:审批交互确认、打断、流式增量、自动化(σ-server M2/M3,见
            sigma-frontend/docs/sigma-backend-api-design.md)。数据源 = ~/.sigma/sessions,与 CLI 同一份。
          </p>
        </div>
      </div>
    </div>
  )
}
