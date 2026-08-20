/**
 * «Кто я» и «что тут есть» — экранный близнец `/whoami` и `/help` бота.
 *
 * Нужен ровно тогда, когда что-то не работает: без него человек не может
 * проверить ни к какому порталу он подключён, ни какая у него роль, ни какие
 * проекты видит этот чат. В боте это две команды, здесь — один экран, потому
 * что и спрашивают их всегда вместе.
 */
import { useEffect, useState } from 'react'
import { api } from '../api'
import { Failure } from '../components/States'
import { tg } from '../telegram'
import { Icon, type IconName } from '../ui/Icon'
import { AppBar, Pill, TaskSkeleton } from '../ui/parts'
import { ROLE_TITLES, type Context, type Me } from '../types'

type Props = { context: Context | null; onBack: () => void }

/** Что умеет приложение. Список ровно тот же, что разделы `/help` бота. */
const ABILITIES: { icon: IconName; title: string; text: string }[] = [
  { icon: 'inbox', title: 'Задачи чата', text: 'Списки с фильтрами и поиском, дерево подзадач, группировка по колонкам канбана.' },
  { icon: 'chart', title: 'Сводка', text: 'Сколько задач на каждой стадии, сколько на вас и сколько просрочено.' },
  { icon: 'timer', title: 'Трудозатраты', text: 'Списанное время за месяц в двух разрезах — по статусам и по стадиям.' },
  { icon: 'plus', title: 'Создание задачи', text: 'Полной формой или по вопросам — теми же наборами, что и /ask в чате.' },
  { icon: 'edit', title: 'Правка', text: 'Срок, ответственный, приоритет, стадия. Действия — теми же спец-методами портала.' },
  { icon: 'paperclip', title: 'Файлы', text: 'До 10 файлов за раз, каждый не больше 20 МБ. Уезжают на Диск проекта.' },
  { icon: 'message', title: 'Обсуждение', text: 'Комментарии в обе стороны: написанное здесь видно в задаче, и наоборот.' },
  { icon: 'bell', title: 'Подтверждение', text: 'Задачи, ждущие вашего решения, — по всему теннанту, а не только в этом чате.' },
]

export function AboutScreen({ context, onBack }: Props) {
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  useEffect(() => {
    let cancelled = false
    setError(null)
    api
      .me()
      .then((res) => !cancelled && setMe(res))
      .catch((err) => !cancelled && setError(err))
    return () => {
      cancelled = true
    }
  }, [reload])

  return (
    <>
      <AppBar title="О приложении" subtitle="Кто вы в системе и что тут есть" />

      {error ? (
        <Failure error={error} onRetry={() => setReload((n) => n + 1)} />
      ) : !me ? (
        <TaskSkeleton rows={3} />
      ) : (
        <>
          <h2 className="section-label" style={{ marginTop: 0 }}>
            Кто я
          </h2>
          <dl className="card">
            <div className="fact">
              <dt>Вы</dt>
              <dd>
                {me.name}
                {me.username ? <span className="muted"> · @{me.username}</span> : null}
              </dd>
            </div>
            <div className="fact">
              <dt>Роль</dt>
              <dd>{ROLE_TITLES[me.role] ?? me.role}</dd>
            </div>
            <div className="fact">
              <dt>Компания</dt>
              <dd>{me.tenant.name || '—'}</dd>
            </div>
            <div className="fact">
              <dt>Портал</dt>
              <dd>
                {/* Ссылка на портал, а не просто имя: когда что-то не сходится,
                    первое, что делают, — идут смотреть в сам Битрикс24.
                    Кнопкой, а не текстовой ссылкой: 22px в высоту — это не
                    цель касания, аудит ловил её как нарушение. */}
                {me.portal ? (
                  <button
                    type="button"
                    className="btn ghost"
                    style={{ marginLeft: 'calc(var(--sp-4) * -1)' }}
                    onClick={() => tg.openLink(`https://${me.portal}`)}
                  >
                    {me.portal}
                    <Icon name="external" size={16} />
                  </button>
                ) : (
                  <span className="muted">—</span>
                )}
              </dd>
            </div>
            <div className="fact">
              <dt>ID в Битрикс24</dt>
              <dd className="num">{me.b24_user_id}</dd>
            </div>
          </dl>

          {context ? (
            <>
              <h2 className="section-label">Этот чат</h2>
              <div className="card">
                <div className="fact">
                  <dt>Чат</dt>
                  <dd>{context.title}</dd>
                </div>
                <div className="fact">
                  <dt>Проекты</dt>
                  <dd className="pill-row">
                    {context.projects.length === 0 ? (
                      <span className="muted">не привязаны</span>
                    ) : (
                      context.projects.map((p) => (
                        <Pill key={p.id} icon="folder">
                          {p.client} · {p.name}
                        </Pill>
                      ))
                    )}
                  </dd>
                </div>
              </div>
              {/* Привязку меняет администратор теннанта и только в двух местах —
                  командой в чате или в приложении внутри портала. Третьей точки
                  для той же операции не заводим: расходиться будут все три. */}
              <p className="muted" style={{ margin: '0 var(--sp-2) var(--sp-5)', fontSize: 'var(--fs-md)' }}>
                Привязать чат к другому проекту может администратор теннанта —
                командой <code>/bind</code> в чате или в приложении внутри Битрикс24.
              </p>
            </>
          ) : null}

          <h2 className="section-label">Что умеет</h2>
          <div className="card">
            {ABILITIES.map((item) => (
              <div className="ability" key={item.title}>
                <span className="ability-icon" aria-hidden="true">
                  <Icon name={item.icon} size={18} />
                </span>
                <div>
                  <div className="ability-title">{item.title}</div>
                  <div className="muted">{item.text}</div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}
