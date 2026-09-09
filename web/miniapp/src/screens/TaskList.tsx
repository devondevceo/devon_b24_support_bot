/** Список задач чата: фильтры, поиск, группировка по стадиям канбана. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type TaskListResponse } from '../api'
import { Empty, Failure } from '../components/States'
import { deadlineView } from '../format'
import { OVERDUE, statusView } from '../status'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { MenuSheet } from '../ui/MenuSheet'
import { AppBar, IconButton, Pill, Refreshing, TaskSkeleton } from '../ui/parts'
import { TimelogSheet } from './TimelogSheet'
import {
  FILTER_SHORT,
  FILTER_TITLES,
  type Context,
  type Task,
  type TaskFilter,
} from '../types'

type Props = {
  context: Context
  /** Фильтр, с которого открыт список: из сводки сюда приходят «просроченные». */
  initialFilter?: TaskFilter
  onOpen: (taskId: number) => void
  onCreate: () => void
  onApprovals: () => void
  onSummary: () => void
  onTimesheet: () => void
  onAbout: () => void
  onSwitchChat: (() => void) | null
}

export function TaskList({
  context,
  initialFilter,
  onOpen,
  onCreate,
  onApprovals,
  onSummary,
  onTimesheet,
  onAbout,
  onSwitchChat,
}: Props) {
  const [menu, setMenu] = useState(false)
  const [filter, setFilter] = useState<TaskFilter>(initialFilter ?? 'all')
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [data, setData] = useState<TaskListResponse | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const [pending, setPending] = useState(0)
  /*
   * Списание времени прямо из списка: у задачи, по которой работали, время
   * списывают чаще, чем открывают её карточку. Лист сам читает и права, и
   * уже сделанные списания, поэтому знать о задаче что-то ещё списку не нужно.
   */
  const [timelogTask, setTimelogTask] = useState<number | null>(null)

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

  /*
   * Счётчик ожидающих решения — запросом при открытии списка.
   *
   * Это чистый запрос к своей базе, без похода в портал (approvals.pending_for),
   * и он отвечает на вопрос, который иначе виден только внутри экрана:
   * «ждёт ли чего-то от меня». Молчаливый отказ намеренный — счётчик
   * вспомогательный, и ронять из-за него список задач не за что.
   */
  useEffect(() => {
    let cancelled = false
    api.approvals
      .list()
      .then((res) => !cancelled && setPending(res.total))
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [reload])

  useEffect(() => tg.main({ text: 'Новая задача', onClick: onCreate }), [onCreate])
  useEffect(() => tg.back(null), [])

  const groups = useMemo(() => groupByStage(data), [data])
  const retry = useCallback(() => setReload((n) => n + 1), [])

  const subtitle = context.projects.map((p) => p.name).join(' · ')

  return (
    <>
      <AppBar
        title={context.title}
        subtitle={subtitle}
        actions={
          <>
            {/* Наружу — только то, что несёт счётчик: спрятанный в меню
                счётчик не сообщает ничего. Остальное в «⋯». */}
            <IconButton
              icon="bell"
              label="Ожидают подтверждения"
              count={pending}
              onClick={onApprovals}
            />
            <IconButton icon="menu" label="Разделы" onClick={() => setMenu(true)} />
          </>
        }
      />

      {menu ? (
        <MenuSheet
          onClose={() => setMenu(false)}
          items={[
            { key: 'summary', icon: 'chart', title: 'Сводка',
              hint: 'сколько задач на каждой стадии', onPick: onSummary },
            { key: 'time', icon: 'timer', title: 'Трудозатраты',
              hint: 'списанное время за месяц', onPick: onTimesheet },
            { key: 'pending', icon: 'bell', title: 'Ожидают подтверждения',
              hint: 'задачи, ждущие вашего решения', badge: pending,
              onPick: onApprovals },
            { key: 'new', icon: 'plus', title: 'Новая задача',
              hint: 'полной формой или по вопросам', onPick: onCreate },
            ...(onSwitchChat
              ? [{ key: 'chat' as const, icon: 'message' as const, title: 'Сменить чат',
                   hint: 'другой чат и его проекты', onPick: onSwitchChat }]
              : []),
            { key: 'about', icon: 'info', title: 'О приложении',
              hint: 'кто вы в системе и что тут есть', onPick: onAbout },
          ]}
        />
      ) : null}

      <Filters value={filter} total={data?.total ?? null} onChange={setFilter} />

      <SearchBar value={query} onChange={setQuery} />

      {error ? (
        <Failure error={error} onRetry={retry} />
      ) : loading && !data ? (
        <TaskSkeleton />
      ) : !data || data.items.length === 0 ? (
        <Empty
          icon={search ? 'search' : 'inbox'}
          title={search ? 'Ничего не нашлось' : 'Задач нет'}
          hint={
            search
              ? `По запросу «${search}» в проектах этого чата ничего нет. Проверьте фильтр — сейчас показаны «${FILTER_TITLES[filter]}».`
              : filter === 'all'
                ? 'В проектах этого чата нет открытых задач.'
                : `Под фильтр «${FILTER_TITLES[filter]}» ничего не подходит.`
          }
          action={
            search ? (
              <button type="button" className="btn sec" onClick={() => setQuery('')}>
                <Icon name="close" size={18} />
                Сбросить поиск
              </button>
            ) : (
              <button type="button" className="btn" onClick={onCreate}>
                <Icon name="plus" size={18} />
                Создать задачу
              </button>
            )
          }
        />
      ) : (
        <>
          {/* Обновление на месте: список остаётся на экране и читаемым,
              вместо того чтобы схлопнуться в спиннер по центру. */}
          {loading ? <Refreshing label="Обновляем список" /> : null}

          {data.truncated ? (
            <div className="notice warn">
              <Icon name="alert" size={18} />
              <span>
                Показаны первые {data.items.length} из {data.total}. Уточните фильтр
                или поиск, чтобы увидеть остальные.
              </span>
            </div>
          ) : null}

          {groups.map((group) => (
            <section key={group.key}>
              <h2 className="section-label">
                {group.title}
                <span className="count">{group.tasks.length}</span>
              </h2>
              <div className="card flush">
                {group.tasks.map((task) => (
                  <TaskRow
                    key={task.id}
                    task={task}
                    onOpen={onOpen}
                    onTimelog={setTimelogTask}
                  />
                ))}
              </div>
            </section>
          ))}
        </>
      )}

      {timelogTask === null ? null : (
        <TimelogSheet
          context={context}
          taskId={timelogTask}
          onClose={() => setTimelogTask(null)}
        />
      )}
    </>
  )
}

/* ---------------------------------------------------------------- фильтры */

function Filters({
  value,
  total,
  onChange,
}: {
  value: TaskFilter
  /** Счётчик показывается только у выбранного: сколько задач в остальных
      фильтрах, портал в этом ответе не сообщает, и выдумывать нечего. */
  total: number | null
  onChange: (next: TaskFilter) => void
}) {
  const track = useRef<HTMLDivElement>(null)

  // Полоса шире экрана, и выбранный фильтр может оказаться за краем —
  // тогда человек не видит, по чему отфильтровано.
  useEffect(() => {
    const active = track.current?.querySelector('[aria-pressed="true"]')
    active?.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' })
  }, [value])

  return (
    <div className="filters">
      <div className="filters-track" ref={track} role="group" aria-label="Фильтр задач">
        {(Object.keys(FILTER_SHORT) as TaskFilter[]).map((key) => (
          <button
            key={key}
            type="button"
            className="chip"
            aria-pressed={value === key}
            aria-label={FILTER_TITLES[key]}
            onClick={() => {
              tg.tap()
              onChange(key)
            }}
          >
            {FILTER_SHORT[key]}
            {value === key && total !== null ? (
              <span className="count">{total}</span>
            ) : null}
          </button>
        ))}
      </div>
    </div>
  )
}

function SearchBar({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="searchbar">
      <Icon name="search" size={20} />
      <input
        type="search"
        value={value}
        placeholder="Поиск по заголовку"
        aria-label="Поиск задач по заголовку"
        autoComplete="off"
        onChange={(e) => onChange(e.target.value)}
      />
      {value ? (
        <button type="button" className="clear" aria-label="Очистить поиск"
                onClick={() => onChange('')}>
          <Icon name="close" size={18} />
        </button>
      ) : null}
    </div>
  )
}

/* ------------------------------------------------------------------ строка */

/*
 * Строка списка — две кнопки в одной полосе, а не одна на всё.
 *
 * Кнопку в кнопку вложить нельзя, поэтому строка стала контейнером: слева
 * открытие задачи во всю ширину, справа — списание времени. Вторая кнопка
 * узкая и подписана только для скринридера: подпись «Списать время» у каждой
 * из двадцати задач превратила бы список в столбец одинаковых слов.
 */
function TaskRow({
  task,
  onOpen,
  onTimelog,
}: {
  task: Task
  onOpen: (id: number) => void
  onTimelog: (id: number) => void
}) {
  const status = statusView(task.status, task.status_title)
  const view = task.overdue ? OVERDUE : status
  const due = deadlineView(task.deadline, task.overdue)
  const who = task.responsible.name || 'не назначен'

  return (
    <div className="task-row">
      <button
        type="button"
        className="task"
        data-depth={Math.min(task.depth, 2)}
        data-priority={task.priority}
        /* Скринридер получает связную фразу вместо россыпи значков. */
        aria-label={`Задача ${task.id}. ${task.title}. ${view.title}. ${who}. ${due.text}`}
        onClick={() => {
          tg.press()
          onOpen(task.id)
        }}
      >
        <span className="status-dot" data-tone={view.tone} aria-hidden="true" />
        <span className="task-title">{task.title}</span>
        <span className="task-meta" aria-hidden="true">
          <span className="num">#{task.id}</span>
          <span className="who">{who}</span>
          <Pill icon={due.icon} tone={due.tone}>
            {due.text}
          </Pill>
        </span>
      </button>
      <button
        type="button"
        className="task-time"
        aria-label={`Списать время в задачу ${task.id}`}
        onClick={() => {
          tg.press()
          onTimelog(task.id)
        }}
      >
        <Icon name="timer" size={20} />
      </button>
    </div>
  )
}

/* -------------------------------------------------------------- стадии */

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
        // Два РАЗНЫХ смысла, и путать их нельзя: «Вне канбана» — нормальное
        // состояние задачи, «Стадия не опознана» — наш разлад с порталом.
        title: titles.get(stageId) ?? (stageId ? 'Стадия не опознана' : 'Вне канбана'),
        tasks: [],
      })
      order.push(key)
    }
    buckets.get(key)!.tasks.push(task)
  }
  return order.map((key) => buckets.get(key)!)
}
