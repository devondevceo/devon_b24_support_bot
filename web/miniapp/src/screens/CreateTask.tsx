/**
 * Создание задачи полной формой — то, ради чего мини-апп и нужен: в чате задача
 * создаётся одним движением и всеми умолчаниями, а здесь можно сразу назначить
 * ответственного, срок, стадию и приоритет.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from '../api'
import { formId, fromLocalInput } from '../format'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { AppBar } from '../ui/parts'
import {
  PRIORITY_TITLES,
  type Context,
  type Member,
  type SurveyQuestion,
  type SurveyTemplate,
} from '../types'

type Props = { context: Context; onCreated: (taskId: number) => void; onCancel: () => void }

const TITLE_MIN = 3
const TITLE_MAX = 250

/**
 * Два способа завести задачу, ровно как у бота: `/task` — своими словами,
 * `/ask` — по вопросам. Наборы вопросов те же самые, из тех же шаблонов;
 * отличается только подача. В чате вопросы идут по одному, потому что диалог
 * линеен, а здесь видны списком: можно вернуться и поправить ответ, не начиная
 * опрос заново, и видно, сколько осталось, ещё до первого ответа.
 */
type Mode = 'form' | 'survey'

export function CreateTask({ context, onCreated, onCancel }: Props) {
  const [mode, setMode] = useState<Mode>('form')
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
  const [titleError, setTitleError] = useState<string | null>(null)
  const titleBox = useRef<HTMLInputElement>(null)

  // ------------------------------------------------------------ опросник
  const [templates, setTemplates] = useState<SurveyTemplate[] | null>(null)
  const [templateId, setTemplateId] = useState(0)
  const [questions, setQuestions] = useState<SurveyQuestion[] | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [missing, setMissing] = useState<string[]>([])

  // Ключ идемпотентности живёт столько же, сколько форма: повторная отправка
  // после таймаута не создаст вторую задачу (инвариант И-10).
  const idem = useMemo(() => formId(), [])

  // Наборы вопросов спрашиваем один раз: если их нет вовсе, переключателя
  // режимов быть не должно — кнопка, ведущая в пустоту, хуже её отсутствия.
  useEffect(() => {
    let cancelled = false
    api.surveys
      .list(context.chat_ref)
      .then((res) => !cancelled && setTemplates(res.items))
      .catch(() => !cancelled && setTemplates([]))
    return () => {
      cancelled = true
    }
  }, [context.chat_ref])

  useEffect(() => {
    if (!templateId) return
    let cancelled = false
    setQuestions(null)
    api.surveys
      .form(context.chat_ref, templateId)
      .then((res) => !cancelled && setQuestions(res.items))
      .catch(() => !cancelled && setQuestions([]))
    return () => {
      cancelled = true
    }
  }, [context.chat_ref, templateId])

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

  /** Проверка одна и та же для потери фокуса и для отправки. */
  const checkTitle = useCallback((value: string): string | null => {
    const trimmed = value.trim()
    if (trimmed.length === 0) return 'Без заголовка задачу не создать.'
    if (trimmed.length < TITLE_MIN) return `Хотя бы ${TITLE_MIN} символа — иначе непонятно, о чём она.`
    return null
  }, [])

  const submit = useMemo(
    () => async () => {
      if (mode === 'survey') {
        // Обязательные проверяем ДО отправки: сервер откажет так же, но
        // человек к тому моменту уже нажал и ждёт.
        const gaps = (questions ?? [])
          .filter((q) => q.required && !(answers[q.code] ?? '').trim())
          .map((q) => q.code)
        setMissing(gaps)
        if (gaps.length > 0 || !templateId) {
          tg.fail()
          if (gaps[0]) {
            document.getElementById(`q-${gaps[0]}`)?.scrollIntoView({
              block: 'center',
              behavior: 'smooth',
            })
            document.getElementById(`q-${gaps[0]}`)?.focus()
          }
          return
        }
      } else {
        const problem = checkTitle(title)
        if (problem) {
          // Ошибка у поля, фокус в поле: искать её глазами по форме не нужно
          // (правила error-placement и focus-management).
          setTitleError(problem)
          titleBox.current?.focus()
          tg.fail()
          return
        }
      }

      setBusy(true)
      setError(null)
      try {
        const task = await api.create(context.chat_ref, {
          form_id: idem,
          project_id: projectId,
          // Заголовок и описание в режиме опросника собирает сервер тем же
          // `survey.assemble`, что и бот, — здесь их не выдумываем.
          ...(mode === 'survey'
            ? { survey_template_id: templateId, answers }
            : { title, description }),
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
    [answers, checkTitle, context.chat_ref, deadline, description, idem, mode,
     onCreated, priority, projectId, questions, responsible, stageId, templateId, title],
  )

  /*
   * Отправка — только нативной кнопкой клиента.
   *
   * Раньше рядом с ней жила своя такая же в форме: две кнопки «Создать задачу»
   * на одном экране, из которых одна уезжает при прокрутке, а другая нет.
   * Нативная всегда на виду и над клавиатурой — это и есть place для главного
   * действия в Telegram.
   */
  useEffect(() => tg.main({ text: 'Создать задачу', onClick: submit, busy }), [submit, busy])

  const left = TITLE_MAX - title.length

  return (
    <>
      <AppBar title="Новая задача" subtitle={context.title} />

      {error ? (
        <div className="notice err" role="alert">
          <Icon name="alert" size={18} />
          <span>{error}</span>
        </div>
      ) : null}

      {/* Переключателя нет, когда выбирать не из чего: наборов вопросов у
          теннанта может не быть вовсе. */}
      {templates && templates.length > 0 ? (
        <div className="segmented" style={{ marginBottom: 'var(--sp-5)' }}
             role="group" aria-label="Как заводим задачу">
          <button type="button" aria-pressed={mode === 'form'}
                  onClick={() => { tg.tap(); setMode('form') }}>
            Своими словами
          </button>
          <button type="button" aria-pressed={mode === 'survey'}
                  onClick={() => {
                    tg.tap()
                    setMode('survey')
                    if (!templateId && templates[0]) setTemplateId(templates[0].id)
                  }}>
            По вопросам
          </button>
        </div>
      ) : null}

      {mode === 'survey' ? (
        <SurveyForm
          templates={templates ?? []}
          templateId={templateId}
          onPickTemplate={setTemplateId}
          questions={questions}
          answers={answers}
          missing={missing}
          onAnswer={(code, value) => {
            setAnswers((cur) => ({ ...cur, [code]: value }))
            if (missing.includes(code) && value.trim()) {
              setMissing((cur) => cur.filter((c) => c !== code))
            }
          }}
        />
      ) : null}

      <h2 className="section-label" style={{ marginTop: 0 }}>
        {mode === 'survey' ? 'Куда и кому' : 'Суть'}
      </h2>
      <div className="card">
        {context.projects.length > 1 ? (
          <label className="field">
            <span className="label">Проект</span>
            <select value={projectId} onChange={(e) => setProjectId(Number(e.target.value))}>
              {context.projects.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.client} · {p.name}
                </option>
              ))}
            </select>
          </label>
        ) : null}

        {mode === 'survey' ? null : (
        <label className="field">
          <span className="label">
            <span>
              Что нужно сделать <span className="req" aria-hidden="true">*</span>
            </span>
            {/* Счётчик появляется на подходе к пределу, а не висит всегда:
                иначе он читается как требование набрать 250 символов. */}
            {left <= 40 ? <span className="num">{left}</span> : null}
          </span>
          <input
            ref={titleBox}
            type="text"
            value={title}
            required
            maxLength={TITLE_MAX}
            placeholder="Коротко, одной строкой"
            aria-invalid={titleError ? true : undefined}
            aria-describedby={titleError ? 'title-error' : undefined}
            onChange={(e) => {
              setTitle(e.target.value)
              if (titleError) setTitleError(checkTitle(e.target.value))
            }}
            // Проверяем по потере фокуса, а не на каждую букву: ругаться на
            // «За» посреди слова «Заявка» — значит ругаться на всех подряд.
            onBlur={(e) => setTitleError(checkTitle(e.target.value))}
          />
          {titleError ? (
            <div className="err" id="title-error" role="alert">
              <Icon name="alert" size={16} />
              {titleError}
            </div>
          ) : null}
        </label>
        )}

        {/* В режиме опросника поле остаётся, но значит другое: ответы уже
            собраны, а это — то, что человек хочет добавить сверх вопросов.
            Сервер дописывает его ПОСЛЕ блока ответов, ничего не затирая. */}
        <label className="field">
          <span className="label">{mode === 'survey' ? 'Что-то ещё' : 'Подробности'}</span>
          <textarea
            value={description}
            placeholder={
              mode === 'survey'
                ? 'Необязательно: всё, чего не спросили'
                : 'Что случилось, где, что уже пробовали'
            }
            onChange={(e) => setDescription(e.target.value)}
          />
          <div className="help">Попадёт в описание задачи в Битрикс24.</div>
        </label>
      </div>

      <h2 className="section-label">Кому и когда</h2>
      <div className="card">
        <label className="field">
          <span className="label">Ответственный</span>
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
          <span className="label">Срок</span>
          <input
            type="datetime-local"
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
          />
          <div className="help">Без срока задача создастся бессрочной.</div>
        </label>

        <div className="field">
          <span className="label" id="new-prio">
            Приоритет
          </span>
          <div className="segmented" role="group" aria-labelledby="new-prio">
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

        {stages.length > 0 ? (
          <label className="field">
            <span className="label">Стадия канбана</span>
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
    </>
  )
}

/* ------------------------------------------------------------- опросник */

/**
 * Все вопросы набора на одном экране.
 *
 * Выпадающий список в боте — это кнопки под вопросом; здесь — те же варианты
 * сегментами или списком, смотря сколько их. Подпись «уедет в поле “Срок”»
 * стоит у привязанных вопросов не для красоты: человек вправе знать, что его
 * ответ станет полем задачи, а не строкой в описании.
 */
function SurveyForm({
  templates,
  templateId,
  onPickTemplate,
  questions,
  answers,
  missing,
  onAnswer,
}: {
  templates: SurveyTemplate[]
  templateId: number
  onPickTemplate: (id: number) => void
  questions: SurveyQuestion[] | null
  answers: Record<string, string>
  missing: string[]
  onAnswer: (code: string, value: string) => void
}) {
  const answered = (questions ?? []).filter((q) => (answers[q.code] ?? '').trim()).length
  const total = questions?.length ?? 0

  return (
    <>
      <h2 className="section-label" style={{ marginTop: 0 }}>
        Набор вопросов
        {total ? (
          <span className="count">
            {answered} из {total}
          </span>
        ) : null}
      </h2>

      <div className="card">
        <label className="field">
          <span className="label">О чём обращение</span>
          <select value={templateId} onChange={(e) => onPickTemplate(Number(e.target.value))}>
            {templateId ? null : <option value={0}>— выберите —</option>}
            {templates.map((t) => (
              <option key={t.id} value={t.id}>
                {t.title}
              </option>
            ))}
          </select>
        </label>
      </div>

      {questions === null && templateId ? (
        <div className="card">
          <div className="muted">загружаем вопросы…</div>
        </div>
      ) : null}

      {questions?.map((q, index) => {
        const value = answers[q.code] ?? ''
        const bad = missing.includes(q.code)
        return (
          <div className="card" key={q.code}>
            <div className="field">
              <span className="label">
                <span>
                  <span className="num muted">{index + 1}. </span>
                  {q.text}
                  {q.required ? <span className="req" aria-hidden="true"> *</span> : null}
                </span>
              </span>

              {q.kind === 'choice' && q.options.length > 0 ? (
                <div className="choices" role="group" aria-label={q.text}>
                  {q.options.map((o) => (
                    <button
                      key={o.value}
                      type="button"
                      className="choice"
                      aria-pressed={value === o.value}
                      onClick={() => {
                        tg.tap()
                        // Второе нажатие снимает выбор: у необязательного
                        // вопроса иначе нет пути назад к «не отвечал».
                        onAnswer(q.code, value === o.value ? '' : o.value)
                      }}
                    >
                      {o.label}
                    </button>
                  ))}
                </div>
              ) : (
                <textarea
                  id={`q-${q.code}`}
                  value={value}
                  rows={2}
                  aria-invalid={bad ? true : undefined}
                  aria-describedby={bad ? `e-${q.code}` : undefined}
                  onChange={(e) => onAnswer(q.code, e.target.value)}
                />
              )}

              {bad ? (
                <div className="err" id={`e-${q.code}`} role="alert">
                  <Icon name="alert" size={16} />
                  Без этого ответа задачу не создать.
                </div>
              ) : null}

              {q.field ? (
                <div className="help">
                  Ответ уедет в поле задачи <code>{q.field}</code>.
                </div>
              ) : null}
            </div>
          </div>
        )
      })}
    </>
  )
}
