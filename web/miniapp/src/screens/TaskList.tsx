/** Список задач чата: фильтры, поиск, группировка по стадиям канбана. */
import { useEffect, useMemo, useState } from 'react'
import { api, type TaskListResponse } from '../api'
import { Empty, Failure, Loading } from '../components/States'
import { deadlineLabel } from '../format'
import { tg } from '../telegram'
import { FILTER_TITLES, type Context, type Task, type TaskFilter } from '../types'

type Props = {
  context: Context
  onOpen: (taskId: number) => void
  onCreate: () => void
  onApprovals: () => void
  onSwitchChat: (() => void) | null
}

export function TaskList({ context, onOpen, onCreate, onApprovals, onSwitchChat }: Props) {
  const [filter, setFilter] = useState<TaskFilter>('all')
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [data, setData] = useState<TaskListResponse | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)

  // Поиск идёт в портал, поэтому не на каждую букву: полсекунды тишины —
  // и запрос. Иначе на длинном слове мы выбираем частотный лимит вхолостую.
  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(query.trim()), 500)
    return () => window.clearTimeout(timer)
  }, [query])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .tasks(context.chat_ref, filter, search)
      .then((res) => {
        if (!cancelled) {
          setData(res)
          setError(null)
        }
      })
      .catch((err) => !cancelled && setError(err))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [context.chat_ref, filter, search, reload])

  useEffect(() => tg.main({ text: '➕ Новая задача', onClick: onCreate }), [onCreate])

  const groups = useMemo(() => groupByStage(data), [data])

  return (
    <>
      <div className="head">
        <h1>{context.title}</h1>
        <div className="sub">
          {context.projects.map((p) => p.name).join(' · ')}
          {' · '}
          <button className="link" onClick={onApprovals}>
            🙋 на подтверждении
          </button>
          {onSwitchChat ? (
            <>
              {' · '}
              <button className="link" onClick={onSwitchChat}>
                сменить чат
              </button>
            </>
          ) : null}
        </div>
      </div>

      <div className="chips">
        {(Object.keys(FILTER_TITLES) as TaskFilter[]).map((key) => (
          <button
            key={key}
            className="chip"
            aria-pressed={filter === key}
            onClick={() => {
              tg.tap()
              setFilter(key)
            }}
          >
            {FILTER_TITLES[key]}
          </button>
        ))}
      </div>

      <input
        className="search"
        type="search"
        value={query}
        placeholder="Поиск по заголовку"
        onChange={(e) => setQuery(e.target.value)}
      />

      {error ? (
        <Failure error={error} onRetry={() => setReload((n) => n + 1)} />
      ) : loading && !data ? (
        <Loading />
      ) : !data || data.items.length === 0 ? (
        <Empty
          title="Задач нет"
          hint={
            search
              ? 'По этому запросу ничего не нашлось.'
              : filter === 'all'
                ? 'В проектах этого чата нет открытых задач.'
                : 'Под этот фильтр ничего не подходит.'
          }
        />
      ) : (
        <>
          {data.truncated ? (
            <div className="notice warn">
              Показаны первые {data.items.length} задач. Уточните фильтр или поиск.
            </div>
          ) : null}
          {groups.map((group) => (
            <section key={group.key}>
              {groups.length > 1 || group.title ? (
                <div className="stage-title">
                  {group.title} · {group.tasks.length}
                </div>
              ) : null}
              <div className="card tight" style={{ padding: 0, overflow: 'hidden' }}>
                {group.tasks.map((task) => (
                  <TaskRow key={task.id} task={task} onOpen={onOpen} />
                ))}
              </div>
            </section>
          ))}
        </>
      )}
    </>
  )
}

function TaskRow({ task, onOpen }: { task: Task; onOpen: (id: number) => void }) {
  return (
    <button
      className={`task${task.depth > 0 ? ' child' : ''}`}
      onClick={() => {
        tg.tap()
        onOpen(task.id)
      }}
    >
      <div className="task-head">
        <span>{task.overdue ? '🔥' : task.status_emoji}</span>
        <span className="task-title">{task.title}</span>
      </div>
      <div className="task-meta">
        <span>#{task.id}</span>
        <span>{task.responsible.name || 'не назначен'}</span>
        <span className={task.overdue ? 'overdue' : ''}>
          {deadlineLabel(task.deadline, task.overdue)}
        </span>
        {task.priority === 2 ? <span className="badge high">важно</span> : null}
      </div>
    </button>
  )
}

type Group = { key: string; title: string; tasks: Task[] }

/**
 * Группировка по стадиям канбана, а не по статусам: «Новые» на портале — это
 * стадия, у каждого проекта своя, и статус с ней не связан (docs/00-portal-facts.md §3.2).
 */
function groupByStage(data: TaskListResponse | null): Group[] {
  if (!data) return []
  const titles = new Map<number, string>()
  for (const stage of data.stages) titles.set(stage.id, stage.title)

  const order: string[] = []
  const buckets = new Map<string, Group>()
  for (const task of data.items) {
    const stageId = task.stage_id ?? 0
    const key = String(stageId)
    if (!buckets.has(key)) {
      buckets.set(key, {
        key,
        title: titles.get(stageId) ?? (stageId ? `Стадия ${stageId}` : 'Вне канбана'),
        tasks: [],
      })
      order.push(key)
    }
    buckets.get(key)!.tasks.push(task)
  }
  return order.map((key) => buckets.get(key)!)
}
