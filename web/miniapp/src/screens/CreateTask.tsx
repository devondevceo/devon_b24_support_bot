/**
 * Создание задачи полной формой — то, ради чего мини-апп и нужен: в чате задача
 * создаётся одним движением и всеми умолчаниями, а здесь можно сразу назначить
 * ответственного, срок, стадию и приоритет.
 */
import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api'
import { formId, fromLocalInput } from '../format'
import { tg } from '../telegram'
import { PRIORITY_TITLES, type Context, type Member } from '../types'

type Props = { context: Context; onCreated: (taskId: number) => void; onCancel: () => void }

export function CreateTask({ context, onCreated, onCancel }: Props) {
  const firstProject = context.projects[0]
  const [projectId, setProjectId] = useState<number>(firstProject ? firstProject.id : 0)
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [deadline, setDeadline] = useState('')
  const [priority, setPriority] = useState(1)
  const [responsible, setResponsible] = useState(0)
  const [stageId, setStageId] = useState(0)
  const [members, setMembers] = useState<Member[]>([])
  const [stages, setStages] = useState<{ id: number; title: string }[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Ключ идемпотентности живёт столько же, сколько форма: повторная отправка
  // после таймаута не создаст вторую задачу (инвариант И-10).
  const idem = useMemo(() => formId(), [])

  useEffect(() => {
    if (!projectId) return
    setMembers([])
    setStages([])
    api
      .members(context.chat_ref, projectId)
      .then((res) => setMembers(res.items))
      .catch(() => undefined)
    api
      .stages(context.chat_ref, projectId)
      .then((res) => setStages(res.items))
      .catch(() => undefined)
  }, [context.chat_ref, projectId])

  useEffect(() => tg.back(onCancel), [onCancel])

  const submit = useMemo(
    () => async () => {
      if (title.trim().length < 3) {
        setError('Заголовок слишком короткий.')
        return
      }
      setBusy(true)
      setError(null)
      try {
        const task = await api.create(context.chat_ref, {
          form_id: idem,
          project_id: projectId,
          title,
          description,
          deadline: deadline ? fromLocalInput(deadline) : '',
          priority,
          responsible_id: responsible || undefined,
          stage_id: stageId || undefined,
        })
        tg.done()
        onCreated(task.id)
      } catch (err) {
        tg.fail()
        setError(err instanceof ApiError ? err.message : 'Задача не создалась.')
      } finally {
        setBusy(false)
      }
    },
    [context.chat_ref, deadline, description, idem, onCreated, priority, projectId, responsible, stageId, title],
  )

  useEffect(
    () => tg.main({ text: 'Создать задачу', onClick: submit, busy }),
    [submit, busy],
  )

  return (
    <>
      <div className="head">
        <h1>Новая задача</h1>
        <div className="sub">{context.title}</div>
      </div>

      {error ? <div className="notice err">{error}</div> : null}

      <div className="card">
        {context.projects.length > 1 ? (
          <label className="field">
            <span>Проект</span>
            <select value={projectId} onChange={(e) => setProjectId(Number(e.target.value))}>
              {context.projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.client} · {p.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        <label className="field">
          <span>Что нужно сделать</span>
          <input
            type="text"
            value={title}
            maxLength={250}
            placeholder="Коротко, одной строкой"
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>

        <label className="field">
          <span>Подробности</span>
          <textarea
            value={description}
            placeholder="Что случилось, где, что уже пробовали"
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>

        <label className="field">
          <span>Ответственный</span>
          <select value={responsible} onChange={(e) => setResponsible(Number(e.target.value))}>
            <option value={0}>я</option>
            {members.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
                {m.position ? ` · ${m.position}` : ''}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span>Срок</span>
          <input
            type="datetime-local"
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
          />
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

        {stages.length > 0 ? (
          <label className="field">
            <span>Стадия канбана</span>
            <select value={stageId} onChange={(e) => setStageId(Number(e.target.value))}>
              <option value={0}>по умолчанию</option>
              {stages.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title}
                </option>
              ))}
            </select>
          </label>
        ) : null}
      </div>

      <div className="actions">
        <button className="btn" disabled={busy} onClick={submit}>
          {busy ? 'Создаём…' : 'Создать задачу'}
        </button>
        <button className="btn sec" disabled={busy} onClick={onCancel}>
          Отмена
        </button>
      </div>
    </>
  )
}
