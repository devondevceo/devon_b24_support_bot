/**
 * Трудозатраты за месяц — экранный близнец «⏱ Трудозатрат» бота.
 *
 * Считает тот же `domain/timesheet` и по тем же правилам: месяц берётся из самой
 * отметки времени, стадии сводятся по названию, а не по id. Два разреза — по
 * статусам и по стадиям — обязаны сойтись с итогом; расхождение здесь означало бы
 * ошибку в подсчёте, и молчать о нём нельзя.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, Failure } from '../components/States'
import { duration } from '../format'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { AppBar, Pill, Refreshing, TaskSkeleton } from '../ui/parts'
import type { Bucket, Context, Timesheet } from '../types'

type Props = { context: Context; onBack: () => void }

export function TimesheetScreen({ context, onBack }: Props) {
  const [months, setMonths] = useState<{ value: string; title: string }[]>([])
  const [month, setMonth] = useState('')
  const [data, setData] = useState<Timesheet | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)

  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  useEffect(() => {
    let cancelled = false
    api.timesheet
      .months()
      .then((res) => {
        if (cancelled) return
        setMonths(res.items)
        setMonth((cur) => cur || res.current)
      })
      .catch(() => undefined)
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!month) return
    let cancelled = false
    setLoading(true)
    setError(null)
    api.timesheet
      .report(context.chat_ref, month)
      .then((res) => !cancelled && setData(res))
      .catch((err) => !cancelled && setError(err))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [context.chat_ref, month, reload])

  const retry = useCallback(() => setReload((n) => n + 1), [])

  return (
    <>
      <AppBar title="Трудозатраты" subtitle={context.projects.map((p) => p.name).join(' · ')} />

      {/* Месяцы листают подряд, поэтому это полоса, а не выпадающий список:
          соседний месяц — одно касание, а не три. */}
      <div className="filters">
        <div className="filters-track" role="group" aria-label="Месяц">
          {months.map((m) => (
            <button
              key={m.value}
              type="button"
              className="chip"
              aria-pressed={month === m.value}
              onClick={() => {
                tg.tap()
                setMonth(m.value)
              }}
            >
              {m.title}
            </button>
          ))}
        </div>
      </div>

      {error ? (
        <Failure error={error} onRetry={retry} />
      ) : !data ? (
        <TaskSkeleton rows={3} />
      ) : (
        <>
          {loading ? <Refreshing label="Пересчитываем месяц" /> : null}

          <div className="task-hero">
            <div className="id">{data.title}</div>
            <h2 className="num">{duration(data.total_seconds)}</h2>
            <div className="pill-row">
              <Pill icon="folder" tone="plain">
                задач: {data.task_count}
              </Pill>
              <Pill icon="timer" tone="plain">
                списаний: {data.entry_count}
              </Pill>
            </div>
          </div>

          {/*
           * Честность про полноту выборки. Постраничность
           * `task.elapseditem.getlist` на портале сломана, и когда объединение
           * выборок с двух концов не сошлось с `total` из конверта — сумма
           * снизу, а не точная. Промолчать значило бы показать неверное число
           * с уверенным видом.
           */}
          {data.complete ? null : (
            <div className="notice warn">
              <Icon name="alert" size={18} />
              <span>
                Битрикс24 отдал {data.seen} записей из {data.total_on_portal}. Показанная
                сумма — это минимум, а не точное значение.
              </span>
            </div>
          )}

          {data.total_seconds === 0 ? (
            <Empty
              icon="timer"
              title="За этот месяц времени не списывали"
              hint="Выберите другой месяц или отметьте время в Битрикс24."
            />
          ) : (
            <>
              <Breakdown title="По статусам" rows={data.by_status} total={data.total_seconds} />
              <Breakdown title="По стадиям" rows={data.by_stage} total={data.total_seconds} />
            </>
          )}
        </>
      )}
    </>
  )
}

function Breakdown({ title, rows, total }: { title: string; rows: Bucket[]; total: number }) {
  if (rows.length === 0) return null
  const max = Math.max(1, ...rows.map((r) => r.seconds))
  return (
    <section>
      <h2 className="section-label">
        {title}
        {/* Сумма разреза печатается рядом с заголовком: она обязана совпасть
            с итогом сверху, и это единственный способ увидеть, что сошлось. */}
        <span className="count">{duration(rows.reduce((n, r) => n + r.seconds, 0))}</span>
      </h2>
      <div className="card">
        {rows.map((row) => (
          <div className="bar-row" key={row.title}>
            <span className="bar-title">{row.title}</span>
            <span className="bar-track" aria-hidden="true">
              <span
                className="bar-fill"
                style={{ width: `${Math.round((row.seconds / max) * 100)}%` }}
              />
            </span>
            <span className="bar-count num">{duration(row.seconds)}</span>
          </div>
        ))}
        <div className="bar-foot">
          <span>доля от месяца</span>
          <span className="num">
            {total ? `${Math.round((rows.reduce((n, r) => n + r.seconds, 0) / total) * 100)}%` : '—'}
          </span>
        </div>
      </div>
    </section>
  )
}
