/** Карточка задачи: поля, действия, редактирование, комментарии. */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { Failure, Loading } from '../components/States'
import { fromLocalInput, fullDate, shortDate, toLocalInput } from '../format'
import { tg } from '../telegram'
import { PRIORITY_TITLES, type Comment, type Context, type Member, type TaskCard } from '../types'

type Props = { context: Context; taskId: number; onBack: () => void }

const ACTION_TITLES: Record<string, string> = {
  complete: '✅ Завершить',
  start: '▶️ В работу',
  pause: '⏸ Пауза',
  defer: '⏳ Отложить',
  renew: '🔄 Возобновить',
}

export function TaskCardScreen({ context, taskId, onBack }: Props) {
  const [task, setTask] = useState<TaskCard | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)
  const [editing, setEditing] = useState(false)

  const load = useCallback(() => {
    setError(null)
    api
      .task(context.chat_ref, taskId)
      .then(setTask)
      .catch(setError)
  }, [context.chat_ref, taskId])

  useEffect(load, [load])
  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  const apply = useCallback(
    async (run: () => Promise<TaskCard>, success: string) => {
      setBusy(true)
      setNotice(null)
      try {
        const fresh = await run()
        setTask(fresh)
        if (fresh.not_applied && fresh.not_applied.length > 0) {
          tg.fail()
          setNotice({
            kind: 'warn',
            text: `Битрикс24 не принял: ${fresh.not_applied.join(', ')}. Скорее всего, у вас нет права менять это поле.`,
          })
        } else {
          tg.done()
          setNotice({ kind: 'ok', text: success })
        }
        setEditing(false)
      } catch (err) {
        tg.fail()
        setNotice({
          kind: 'err',
          text: err instanceof ApiError ? err.message : 'Не получилось. Попробуйте ещё раз.',
        })
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  if (error) return <Failure error={error} onRetry={load} />
  if (!task) return <Loading title="Открываем задачу…" />

  return (
    <>
      <div className="head">
        <h1>
          #{task.id} · {task.title}
        </h1>
        <div className="sub">
          {task.project ? `${task.project.client} · ${task.project.name}` : context.title}
        </div>
      </div>

      {notice ? <div className={`notice ${notice.kind}`}>{notice.text}</div> : null}

      <div className="card">
        <div className="row">
          <span className="label">Статус</span>
          <span className="value">
            {task.status_emoji} {task.status_title}
            {task.overdue ? <span className="overdue"> · просрочена</span> : null}
          </span>
        </div>
        <div className="row">
          <span className="label">Стадия</span>
          <span className="value">{task.stage_title || '—'}</span>
        </div>
        <div className="row">
          <span className="label">Ответственный</span>
          <span className="value">{task.responsible.name || '—'}</span>
        </div>
        <div className="row">
          <span className="label">Постановщик</span>
          <span className="value">{task.creator.name || '—'}</span>
        </div>
        <div className="row">
          <span className="label">Срок</span>
          <span className={`value${task.overdue ? ' overdue' : ''}`}>
            {task.deadline ? fullDate(task.deadline) : 'не задан'}
          </span>
        </div>
        <div className="row">
          <span className="label">Приоритет</span>
          <span className="value">{PRIORITY_TITLES[task.priority] ?? '—'}</span>
        </div>
        <div className="row">
          <span className="label">Создана</span>
          <span className="value">{shortDate(task.created_date)}</span>
        </div>
      </div>

      {task.description ? (
        <div className="card">
          <div className="description">{task.description}</div>
        </div>
      ) : null}

      <div className="actions">
        {Object.keys(ACTION_TITLES)
          .filter((act) => task.allowed.includes(act))
          .map((act) => (
            <button
              key={act}
              className="btn"
              disabled={busy}
              onClick={() =>
                apply(() => api.action(context.chat_ref, task.id, act), 'Готово.')
              }
            >
              {ACTION_TITLES[act]}
            </button>
          ))}
        {task.allowed.includes('edit') ? (
          <button className="btn sec" disabled={busy} onClick={() => setEditing((v) => !v)}>
            ✏️ {editing ? 'Свернуть' : 'Изменить'}
          </button>
        ) : null}
        <button className="btn sec" onClick={() => tg.openLink(task.portal_url)}>
          🔗 В Битрикс24
        </button>
      </div>

      {editing ? (
        <EditForm
          context={context}
          task={task}
          busy={busy}
          onSubmit={(fields) =>
            apply(() => api.patch(context.chat_ref, task.id, fields), 'Изменения сохранены.')
          }
        />
      ) : null}

      <Comments context={context} taskId={task.id} />
    </>
  )
}

/** Редактирование срока, стадии, ответственного и приоритета. */
function EditForm({
  context,
  task,
  busy,
  onSubmit,
}: {
  context: Context
  task: TaskCard
  busy: boolean
  onSubmit: (fields: Record<string, unknown>) => void
}) {
  const [deadline, setDeadline] = useState(toLocalInput(task.deadline))
  const [priority, setPriority] = useState(task.priority)
  const [responsible, setResponsible] = useState(task.responsible.id ?? 0)
  const [stage, setStage] = useState(task.stage_id ?? 0)
  const [members, setMembers] = useState<Member[] | null>(null)
  const [membersError, setMembersError] = useState<unknown>(null)
  const [stages, setStages] = useState<{ id: number; title: string }[] | null>(null)

  const projectId = task.project?.id
  useEffect(() => {
    if (!projectId) return
    api
      .members(context.chat_ref, projectId)
      .then((res) => setMembers(res.items))
      .catch(setMembersError)
  }, [context.chat_ref, projectId])

  // Колонки берём с портала живьём: заведённую пять минут назад человек должен
  // увидеть здесь, а не после суточной синхронизации справочника.
  useEffect(() => {
    if (!projectId) return
    api
      .stages(context.chat_ref, projectId)
      .then((res) => setStages(res.items))
      .catch(() => setStages([]))
  }, [context.chat_ref, projectId])

  const changed: Record<string, unknown> = {}
  if (toLocalInput(task.deadline) !== deadline) {
    changed.deadline = deadline ? fromLocalInput(deadline) : ''
  }
  if (priority !== task.priority) changed.priority = priority
  if (responsible && responsible !== task.responsible.id) changed.responsible_id = responsible
  if (stage !== (task.stage_id ?? 0)) changed.stage_id = stage
  const nothing = Object.keys(changed).length === 0

  return (
    <div className="card">
      <label className="field">
        <span>Срок</span>
        <input
          type="datetime-local"
          value={deadline}
          onChange={(e) => setDeadline(e.target.value)}
        />
        {task.deadline ? (
          <button className="link" style={{ marginTop: 6 }} onClick={() => setDeadline('')}>
            снять срок
          </button>
        ) : null}
      </label>

      <label className="field">
        <span>Стадия</span>
        {stages === null ? (
          <div className="muted">загружаем колонки канбана…</div>
        ) : stages.length === 0 ? (
          <div className="muted">у проекта нет колонок канбана</div>
        ) : (
          <select value={stage} onChange={(e) => setStage(Number(e.target.value))}>
            {/* Стадия и статус независимы: «вне канбана» — нормальное состояние
                задачи, а не отсутствие выбора, поэтому пункт настоящий. */}
            <option value={0}>Вне канбана</option>
            {stages.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title}
              </option>
            ))}
          </select>
        )}
      </label>

      <label className="field">
        <span>Приоритет</span>
        <div className="segmented">
          {[0, 1, 2].map((value) => (
            <button
              key={value}
              aria-pressed={priority === value}
              onClick={() => {
                tg.tap()
                setPriority(value)
              }}
            >
              {PRIORITY_TITLES[value]}
            </button>
          ))}
        </div>
      </label>

      <label className="field">
        <span>Ответственный</span>
        {membersError ? (
          <div className="notice err">Список участников проекта не загрузился.</div>
        ) : members === null ? (
          <div className="muted">загружаем участников…</div>
        ) : (
          <select value={responsible} onChange={(e) => setResponsible(Number(e.target.value))}>
            {members.some((m) => m.id === task.responsible.id) ? null : (
              <option value={task.responsible.id ?? 0}>
                {task.responsible.name || 'текущий ответственный'}
              </option>
            )}
            {members.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
                {m.position ? ` · ${m.position}` : ''}
              </option>
            ))}
          </select>
        )}
      </label>

      <button className="btn" disabled={busy || nothing} onClick={() => onSubmit(changed)}>
        {nothing ? 'Ничего не изменено' : 'Сохранить'}
      </button>
    </div>
  )
}

function Comments({ context, taskId }: { context: Context; taskId: number }) {
  const [items, setItems] = useState<Comment[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    setError(null)
    api
      .comments(context.chat_ref, taskId)
      .then((res) => setItems(res.items))
      .catch(setError)
  }, [context.chat_ref, taskId])

  useEffect(load, [load])

  const send = async () => {
    const value = text.trim()
    if (!value) return
    setBusy(true)
    try {
      const res = await api.addComment(context.chat_ref, taskId, value)
      setItems(res.items)
      setText('')
      tg.done()
    } catch (err) {
      tg.fail()
      tg.alert(err instanceof ApiError ? err.message : 'Комментарий не отправился.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <div className="stage-title" style={{ margin: '0 0 6px' }}>
        Обсуждение
      </div>
      {error ? (
        <Failure error={error} onRetry={load} />
      ) : items === null ? (
        <div className="muted">загружаем…</div>
      ) : items.length === 0 ? (
        <div className="muted">Пока никто ничего не написал.</div>
      ) : (
        items.map((c) => (
          <div className="comment" key={c.id}>
            <div className="author">{c.author}</div>
            <div className="text">{c.text}</div>
            <div className="muted" style={{ fontSize: 12 }}>
              {shortDate(c.date)}
            </div>
          </div>
        ))
      )}

      <label className="field" style={{ marginTop: 12 }}>
        <span>Новый комментарий</span>
        <textarea value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <button className="btn" disabled={busy || text.trim().length === 0} onClick={send}>
        Отправить
      </button>
    </div>
  )
}
