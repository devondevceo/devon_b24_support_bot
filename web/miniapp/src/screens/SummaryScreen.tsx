/**
 * Сводка по стадиям канбана — экранный близнец «📊 Сводки» бота.
 *
 * Бот рисует её строками текста, потому что в чате другого не дано. Здесь у
 * колонки есть ширина, и доля стадии видна раньше, чем прочитано число: это
 * тот же ответ на тот же вопрос, просто прочитанный глазом, а не подсчитанный.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, Failure } from '../components/States'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { AppBar, Pill, TaskSkeleton } from '../ui/parts'
import type { Context, Summary, SummaryProject } from '../types'

type Props = { context: Context; onBack: () => void; onOpenFilter: (f: 'overdue' | 'mine') => void }

export function SummaryScreen({ context, onBack, onOpenFilter }: Props) {
  const [data, setData] = useState<Summary | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  useEffect(() => {
    let cancelled = false
    setError(null)
    api
      .summary(context.chat_ref)
      .then((res) => !cancelled && setData(res))
      .catch((err) => !cancelled && setError(err))
    return () => {
      cancelled = true
    }
  }, [context.chat_ref, reload])

  const retry = useCallback(() => setReload((n) => n + 1), [])

  return (
    <>
      <AppBar title="Сводка" subtitle={context.title} />

      {error ? (
        <Failure error={error} onRetry={retry} />
      ) : !data ? (
        <TaskSkeleton rows={4} />
      ) : data.open === 0 ? (
        <Empty title="Открытых задач нет" hint="В проектах этого чата всё закрыто." />
      ) : (
        <>
          {/* Три числа, ради которых сводку и открывают. Каждое — кнопка:
              увидел «просрочено 3» и сразу попал в эти три задачи. */}
          <div className="totals">
            <div className="total">
              <span className="n num">{data.open}</span>
              <span className="k">открыто</span>
            </div>
            <button type="button" className="total" onClick={() => onOpenFilter('mine')}>
              <span className="n num">{data.mine}</span>
              <span className="k">на мне</span>
            </button>
            <button
              type="button"
              className="total"
              data-tone={data.overdue ? 'danger' : undefined}
              onClick={() => onOpenFilter('overdue')}
            >
              <span className="n num">{data.overdue}</span>
              <span className="k">просрочено</span>
            </button>
          </div>

          {data.projects.map((project) => (
            <ProjectBlock key={project.id} project={project} />
          ))}
        </>
      )}
    </>
  )
}

function ProjectBlock({ project }: { project: SummaryProject }) {
  /*
   * «Вне канбана» и «Стадия не опознана» — две разные строки и всегда в конце.
   * Первое нормальное состояние задачи, второе — наш разлад с порталом; слить
   * их в одну строку значит спрятать второй смысл навсегда.
   */
  const rows = [
    ...project.stages.map((s) => ({ key: `s${s.id}`, title: s.title, count: s.count, odd: false })),
    ...(project.outside
      ? [{ key: 'outside', title: project.outside_title, count: project.outside, odd: false }]
      : []),
    ...(project.unresolved
      ? [{ key: 'unknown', title: project.unresolved_title, count: project.unresolved, odd: true }]
      : []),
  ]
  const max = Math.max(1, ...rows.map((r) => r.count))

  return (
    <section>
      <h2 className="section-label">
        {project.name}
        <span className="count">{project.open}</span>
      </h2>
      <div className="card">
        <div className="pill-row" style={{ marginBottom: 'var(--sp-5)' }}>
          <Pill icon="folder" tone="plain">
            {project.client}
          </Pill>
          {project.overdue ? (
            <Pill icon="alert" tone="danger">
              просрочено {project.overdue}
            </Pill>
          ) : null}
        </div>

        {rows.length === 0 ? (
          <div className="muted">Задачи есть, но ни одна не разложена по колонкам.</div>
        ) : (
          rows.map((row) => (
            <div className="bar-row" key={row.key}>
              <span className="bar-title">
                {row.odd ? <Icon name="alert" size={15} /> : null}
                {row.title}
              </span>
              <span className="bar-track" aria-hidden="true">
                <span
                  className="bar-fill"
                  data-tone={row.odd ? 'warn' : undefined}
                  style={{ width: `${Math.round((row.count / max) * 100)}%` }}
                />
              </span>
              <span className="bar-count num">{row.count}</span>
            </div>
          ))
        )}
      </div>
    </section>
  )
}
