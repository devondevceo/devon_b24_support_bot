/** Карточка задачи: поля, действия, редактирование, комментарии. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { Failure, Loading } from '../components/States'
import {
  deadlineView,
  duration,
  fromLocalInput,
  fullDate,
  initials,
  shortDate,
  toLocalInput,
} from '../format'
import { OVERDUE, priorityView, statusView } from '../status'
import { tg } from '../telegram'
import { Icon, type IconName } from '../ui/Icon'
import { AppBar, IconButton, Pill, Sheet } from '../ui/parts'
import { TimelogSheet } from './TimelogSheet'
import { PRIORITY_TITLES, type Comment, type Context, type Member, type TaskCard } from '../types'

type Props = { context: Context; taskId: number; onBack: () => void }

/**
 * Действия и порядок их важности.
 *
 * Порядок задаёт, какое действие станет ЕДИНСТВЕННЫМ первичным на экране:
 * берётся первое разрешённое портлом. Раньше «Завершить», «Пауза» и «В Битрикс24»
 * выглядели одинаково важными, и глазу не за что было зацепиться.
 */
const ACTIONS: { act: string; title: string; icon: IconName }[] = [
  { act: 'complete', title: 'Завершить', icon: 'check' },
  { act: 'start', title: 'В работу', icon: 'play' },
  { act: 'renew', title: 'Возобновить', icon: 'renew' },
  { act: 'pause', title: 'Пауза', icon: 'pause' },
  { act: 'defer', title: 'Отложить', icon: 'hourglass' },
]

export function TaskCardScreen({ context, taskId, onBack }: Props) {
  const [task, setTask] = useState<TaskCard | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'warn' | 'err'; text: string } | null>(null)
  const [editing, setEditing] = useState(false)
  const [timelog, setTimelog] = useState(false)

  const load = useCallback(() => {
    setError(null)
    api.task(context.chat_ref, taskId).then(setTask).catch(setError)
  }, [context.chat_ref, taskId])

  useEffect(load, [load])
  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  const apply = useCallback(async (run: () => Promise<TaskCard>, success: string) => {
    setBusy(true)
    setNotice(null)
    try {
      const fresh = await run()
      setTask(fresh)
      if (fresh.not_applied && fresh.not_applied.length > 0) {
        tg.warn()
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
  }, [])

  if (error) return <Failure error={error} onRetry={load} />
  if (!task) return <Loading title="Открываем задачу…" />

  const status = statusView(task.status, task.status_title)
  const priority = priorityView(task.priority)
  const due = deadlineView(task.deadline, task.overdue)
  const available = ACTIONS.filter((a) => task.allowed.includes(a.act))
  const [primary, ...secondary] = available

  return (
    <>
      <AppBar
        title={task.project ? task.project.name : context.title}
        subtitle={task.project ? task.project.client : undefined}
        actions={
          <IconButton
            icon="external"
            label="Открыть в Битрикс24"
            onClick={() => tg.openLink(task.portal_url)}
          />
        }
      />

      {notice ? (
        <div className={`notice ${notice.kind}`} role="status">
          <Icon name={notice.kind === 'ok' ? 'checkCircle' : 'alert'} size={18} />
          <span>{notice.text}</span>
        </div>
      ) : null}

      {/* Что это за задача и в каком она состоянии — до всякой прокрутки. */}
      <div className="task-hero">
        <div className="id">#{task.id}</div>
        <h2>{task.title}</h2>
        <div className="pill-row">
          <Pill icon={status.icon} tone={status.tone} size="lg">
            {status.title}
          </Pill>
          {task.overdue ? (
            <Pill icon={OVERDUE.icon} tone={OVERDUE.tone} size="lg">
              {OVERDUE.title}
            </Pill>
          ) : null}
          {task.stage_title ? (
            <Pill icon="board" size="lg">
              {task.stage_title}
            </Pill>
          ) : null}
          {priority ? (
            <Pill icon={priority.icon} tone={priority.tone} size="lg">
              {priority.title}
            </Pill>
          ) : null}
        </div>
      </div>

      {/* Три факта, за которыми сюда и приходят. Остальное — ниже, под «Ещё». */}
      <dl className="card">
        <div className="fact">
          <dt>Ответственный</dt>
          <dd>{task.responsible.name || 'не назначен'}</dd>
        </div>
        <div className="fact">
          <dt>Срок</dt>
          <dd>
            {task.deadline ? (
              <Pill icon={due.icon} tone={due.tone}>
                {fullDate(task.deadline)}
              </Pill>
            ) : (
              <span className="muted">не задан</span>
            )}
          </dd>
        </div>
        {/* Трудозатраты — не просто факт, а вход в списание: строка кликабельна
            целиком, потому что цель в 44px тут дороже лишней кнопки рядом. */}
        <div className="fact">
          <dt>Трудозатраты</dt>
          <dd>
            <button type="button" className="fact-link" onClick={() => setTimelog(true)}>
              <span className="num">{duration(task.time_spent)}</span>
              <Icon name="chevronRight" size={16} />
            </button>
          </dd>
        </div>
      </dl>

      <details className="disclosure card">
        <summary>
          <Icon name="chevronRight" size={18} />
          Ещё о задаче
        </summary>
        <dl style={{ margin: 0 }}>
          <div className="fact">
            <dt>Постановщик</dt>
            <dd>{task.creator.name || '—'}</dd>
          </div>
          <div className="fact">
            <dt>Приоритет</dt>
            <dd>{PRIORITY_TITLES[task.priority] ?? '—'}</dd>
          </div>
          <div className="fact">
            <dt>Создана</dt>
            <dd className="num">{shortDate(task.created_date)}</dd>
          </div>
          {task.tags.length > 0 ? (
            <div className="fact">
              <dt>Метки</dt>
              <dd className="pill-row">
                {task.tags.map((tag) => (
                  <Pill key={tag}>{tag}</Pill>
                ))}
              </dd>
            </div>
          ) : null}
        </dl>
      </details>

      {task.description ? (
        <div className="card">
          <h2 className="section-label" style={{ margin: '0 0 8px' }}>
            Описание
          </h2>
          <div className="description">{task.description}</div>
        </div>
      ) : null}

      <div className="actions">
        {primary ? (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => apply(() => api.action(context.chat_ref, task.id, primary.act), 'Готово.')}
          >
            <Icon name={primary.icon} size={18} />
            {primary.title}
          </button>
        ) : null}
        {secondary.map((a) => (
          <button
            key={a.act}
            type="button"
            className="btn sec"
            disabled={busy}
            onClick={() => apply(() => api.action(context.chat_ref, task.id, a.act), 'Готово.')}
          >
            <Icon name={a.icon} size={18} />
            {a.title}
          </button>
        ))}
        {task.allowed.includes('edit') ? (
          <button type="button" className="btn sec" disabled={busy} onClick={() => setEditing(true)}>
            <Icon name="edit" size={18} />
            Изменить
          </button>
        ) : null}
        {/*
          Списание времени — кнопка среди действий, а не только строка факта
          выше. Строку читают как показание прибора, а не как дверь, и «где
          списать время» оставалось вопросом. Показывается всегда: список
          списаний полезен и тому, кому запись не разрешена, а само право
          проверяет портал — лист скажет, если нельзя.
        */}
        <button type="button" className="btn sec" disabled={busy} onClick={() => setTimelog(true)}>
          <Icon name="timer" size={18} />
          Списать время
        </button>
      </div>

      <Attach
        context={context}
        taskId={task.id}
        onDone={(fresh, attached, rejected) => {
          setTask(fresh)
          if (rejected.length > 0) {
            tg.warn()
            setNotice({ kind: 'warn', text: `Не уехало: ${rejected.join('; ')}.` })
          } else {
            tg.done()
            setNotice({
              kind: 'ok',
              text: attached === 1 ? 'Файл прикреплён.' : `Прикреплено файлов: ${attached}.`,
            })
          }
        }}
      />

      {timelog ? (
        <TimelogSheet
          context={context}
          taskId={task.id}
          onClose={() => setTimelog(false)}
          onLogged={(seconds) => setTask({ ...task, time_spent: seconds })}
        />
      ) : null}

      {editing ? (
        <EditSheet
          context={context}
          task={task}
          busy={busy}
          onClose={() => setEditing(false)}
          onSubmit={(fields) =>
            apply(() => api.patch(context.chat_ref, task.id, fields), 'Изменения сохранены.')
          }
        />
      ) : null}

      <Comments context={context} taskId={task.id} />
    </>
  )
}

/* -------------------------------------------------------------- правка */

/**
 * Правка срока, стадии, ответственного и приоритета — листом снизу.
 *
 * Раньше форма разворачивалась прямо в потоке страницы: она сдвигала вниз
 * обсуждение, а после сохранения схлопывалась, и страница прыгала обратно.
 */
function EditSheet({
  context,
  task,
  busy,
  onClose,
  onSubmit,
}: {
  context: Context
  task: TaskCard
  busy: boolean
  onClose: () => void
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

  /*
   * Уходить с несохранённым — только осознанно. Лист закрывается тремя путями
   * (подложка, Esc, кнопка «назад» клиента), и любой из них без спроса стёр бы
   * набранное (правило sheet-dismiss-confirm).
   */
  const dismiss = useCallback(() => {
    if (nothing) {
      onClose()
      return
    }
    tg.confirm('Изменения не сохранены. Закрыть?', (ok) => ok && onClose())
  }, [nothing, onClose])

  return (
    <Sheet title="Изменить задачу" onClose={dismiss}>
      <label className="field">
        <span className="label">Срок</span>
        <input
          type="datetime-local"
          value={deadline}
          onChange={(e) => setDeadline(e.target.value)}
        />
        {deadline ? (
          <button
            type="button"
            className="btn ghost"
            style={{ marginTop: 4 }}
            onClick={() => setDeadline('')}
          >
            <Icon name="close" size={16} />
            Снять срок
          </button>
        ) : null}
      </label>

      <label className="field">
        <span className="label">Стадия канбана</span>
        {stages === null ? (
          <div className="muted">загружаем колонки…</div>
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

      <div className="field">
        <span className="label" id="prio-label">
          Приоритет
        </span>
        <div className="segmented" role="group" aria-labelledby="prio-label">
          {[0, 1, 2].map((value) => (
            <button
              key={value}
              type="button"
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
      </div>

      <label className="field">
        <span className="label">Ответственный</span>
        {membersError ? (
          <div className="notice err">
            <Icon name="alert" size={18} />
            <span>Список участников проекта не загрузился.</span>
          </div>
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

      <div className="sheet-footer">
        <button type="button" className="btn sec" disabled={busy} onClick={dismiss}>
          Отмена
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy || nothing}
          onClick={() => onSubmit(changed)}
        >
          {busy ? 'Сохраняем…' : nothing ? 'Нет изменений' : 'Сохранить'}
        </button>
      </div>
    </Sheet>
  )
}

/* ----------------------------------------------------------- вложения */

/**
 * Прикрепить файлы к задаче — то же, что послать файл боту вместе с `/comment`.
 *
 * Пределы взяты у бота один в один (10 файлов, 20 МБ на файл), и проверяются
 * ЗДЕСЬ тоже, а не только на сервере: сказать «больше 20 МБ» до того, как файл
 * поехал по мобильной сети, — это разница между секундой и минутой.
 */
const MAX_FILES = 10
const MAX_BYTES = 20 * 1024 * 1024

function Attach({
  context,
  taskId,
  onDone,
}: {
  context: Context
  taskId: number
  onDone: (task: TaskCard, attached: number, rejected: string[]) => void
}) {
  const [queue, setQueue] = useState<File[]>([])
  const [busy, setBusy] = useState(false)
  const picker = useRef<HTMLInputElement>(null)

  const add = (list: FileList | null) => {
    if (!list) return
    const picked = [...list]
    const room = MAX_FILES - queue.length
    const fits = picked.filter((f) => f.size <= MAX_BYTES).slice(0, room)
    const heavy = picked.filter((f) => f.size > MAX_BYTES)
    if (heavy.length > 0) {
      tg.fail()
      tg.alert(
        heavy.length === 1
          ? `«${heavy[0]!.name}» больше 20 МБ — Битрикс24 такой не примет.`
          : `${heavy.length} файлов больше 20 МБ — Битрикс24 такие не примет.`,
      )
    } else if (picked.length > fits.length) {
      tg.alert(`За раз можно прикрепить не больше ${MAX_FILES} файлов.`)
    }
    if (fits.length > 0) setQueue((cur) => [...cur, ...fits])
    // Значение сбрасывается, иначе тот же файл нельзя выбрать второй раз:
    // change не срабатывает, когда значение не изменилось.
    if (picker.current) picker.current.value = ''
  }

  const send = async () => {
    if (queue.length === 0) return
    setBusy(true)
    try {
      const res = await api.upload(context.chat_ref, taskId, queue)
      setQueue([])
      onDone(res, res.attached, res.rejected)
    } catch (err) {
      tg.fail()
      tg.alert(err instanceof ApiError ? err.message : 'Файлы не загрузились.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h2 className="section-label" style={{ margin: '0 0 var(--sp-4)' }}>
        Файлы
        {queue.length ? <span className="count">{queue.length}</span> : null}
      </h2>

      {queue.map((file, i) => (
        <div className="file-row" key={`${file.name}-${i}`}>
          <Icon name="paperclip" size={18} />
          <span className="file-name">{file.name}</span>
          <span className="file-size num">{sizeOf(file.size)}</span>
          <button
            type="button"
            className="icon-btn"
            aria-label={`Убрать ${file.name}`}
            disabled={busy}
            onClick={() => setQueue((cur) => cur.filter((_, k) => k !== i))}
          >
            <Icon name="trash" size={18} />
          </button>
        </div>
      ))}

      {/*
        Поле выбора файлов спрятано и выключено из обхода: нажимают на кнопку
        рядом, она и есть настоящий орган управления. Оставить его доступным
        значило бы поставить в интерфейс цель размером 1×1.
      */}
      <input
        ref={picker}
        type="file"
        multiple
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => add(e.target.files)}
      />

      <div className="actions" style={{ marginTop: queue.length ? 'var(--sp-5)' : 0 }}>
        <button
          type="button"
          className="btn sec"
          disabled={busy || queue.length >= MAX_FILES}
          onClick={() => picker.current?.click()}
        >
          <Icon name="paperclip" size={18} />
          Выбрать файлы
        </button>
        {queue.length > 0 ? (
          <button type="button" className="btn" disabled={busy} onClick={send}>
            {busy ? 'Загружаем…' : `Прикрепить (${queue.length})`}
          </button>
        ) : null}
      </div>

      {queue.length === 0 ? (
        <div className="help" style={{ marginTop: 'var(--sp-4)' }}>
          До {MAX_FILES} файлов за раз, каждый не больше 20 МБ. Уедут на Диск проекта
          в Битрикс24 и прикрепятся к этой задаче.
        </div>
      ) : null}
    </div>
  )
}

/** «2,4 МБ». Размер называется до отправки, а не после отказа. */
function sizeOf(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} КБ`
  return `${(bytes / 1024 / 1024).toFixed(1).replace('.', ',')} МБ`
}

/* --------------------------------------------------------- комментарии */

function Comments({ context, taskId }: { context: Context; taskId: number }) {
  const [items, setItems] = useState<Comment[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const box = useRef<HTMLTextAreaElement>(null)

  const load = useCallback(() => {
    setError(null)
    api
      .comments(context.chat_ref, taskId)
      .then((res) => setItems(res.items))
      .catch(setError)
  }, [context.chat_ref, taskId])

  useEffect(load, [load])

  // Поле растёт под текст: обсуждение — это абзацы, а не одна строка,
  // и вручную тянуть уголок на телефоне нечем.
  const grow = useCallback(() => {
    const el = box.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`
  }, [])

  const send = useMemo(
    () => async () => {
      const value = text.trim()
      if (!value) return
      setBusy(true)
      try {
        const res = await api.addComment(context.chat_ref, taskId, value)
        setItems(res.items)
        setText('')
        if (box.current) box.current.style.height = 'auto'
        tg.done()
      } catch (err) {
        tg.fail()
        tg.alert(err instanceof ApiError ? err.message : 'Комментарий не отправился.')
      } finally {
        setBusy(false)
      }
    },
    [context.chat_ref, taskId, text],
  )

  return (
    <>
      <h2 className="section-label">
        Обсуждение
        {items ? <span className="count">{items.length}</span> : null}
      </h2>
      <div className="card">
        {error ? (
          <Failure error={error} onRetry={load} />
        ) : items === null ? (
          <div className="muted">загружаем…</div>
        ) : items.length === 0 ? (
          <div className="muted">Пока никто ничего не написал.</div>
        ) : (
          items.map((c) => (
            <article className="comment" key={c.id}>
              <div className="avatar" aria-hidden="true">
                {initials(c.author)}
              </div>
              <div>
                <div className="who">
                  <span className="author">{c.author}</span>
                  <span className="when num">{shortDate(c.date)}</span>
                </div>
                <div className="text">{c.text}</div>
              </div>
            </article>
          ))
        )}

        <div className="composer">
          <label className="sr-only" htmlFor="new-comment">
            Новый комментарий
          </label>
          <textarea
            id="new-comment"
            ref={box}
            rows={1}
            value={text}
            placeholder="Написать в задачу…"
            onChange={(e) => {
              setText(e.target.value)
              grow()
            }}
          />
          <button
            type="button"
            className="send"
            aria-label="Отправить комментарий"
            disabled={busy || text.trim().length === 0}
            onClick={send}
          >
            <Icon name="send" size={20} />
          </button>
        </div>
      </div>
    </>
  )
}
