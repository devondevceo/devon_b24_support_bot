/**
 * Задачи, ожидающие моего подтверждения — список дел, а не задачи одного чата.
 *
 * Единственный экран мини-аппа без завязки на `context`: решение ответственного
 * не зависит от того, из какого чата открыто приложение.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { Empty, Failure, Loading } from '../components/States'
import { shortDate } from '../format'
import { tg } from '../telegram'
import type { Approval } from '../types'

type Props = { onBack: () => void }

export function ApprovalsScreen({ onBack }: Props) {
  const [items, setItems] = useState<Approval[] | null>(null)
  const [total, setTotal] = useState(0)
  const [error, setError] = useState<unknown>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  useEffect(() => {
    let cancelled = false
    api
      .approvals.list()
      .then((res) => {
        if (cancelled) return
        setItems(res.items)
        setTotal(res.total)
        setError(null)
      })
      .catch((err) => !cancelled && setError(err))
    return () => {
      cancelled = true
    }
  }, [reload])

  const act = useCallback(async (id: number, decision: 'confirm' | 'reject') => {
    setBusyId(id)
    try {
      await api.approvals.act(id, decision)
      tg.done()
      setItems((cur) => (cur ? cur.filter((item) => item.id !== id) : cur))
      setTotal((n) => Math.max(0, n - 1))
    } catch (err) {
      tg.fail()
      tg.alert(err instanceof ApiError ? err.message : 'Не получилось. Попробуйте ещё раз.')
      setReload((n) => n + 1)
    } finally {
      setBusyId(null)
    }
  }, [])

  return (
    <>
      <div className="head">
        <h1>Ожидают подтверждения</h1>
      </div>

      {error ? (
        <Failure error={error} onRetry={() => setReload((n) => n + 1)} />
      ) : !items ? (
        <Loading />
      ) : items.length === 0 ? (
        <Empty
          title="Нечего подтверждать"
          hint="Здесь появятся задачи, для которых вас назначили ответственным за решение."
        />
      ) : (
        <>
          {total > items.length ? (
            <div className="notice warn">
              Показаны первые {items.length} из {total}.
            </div>
          ) : null}
          {items.map((item) => (
            <div key={item.id} className="card">
              <div className="task-head">
                <span className="task-title">
                  #{item.task_id} · {item.title}
                </span>
              </div>
              <div className="task-meta">
                <span>
                  {item.project.client} · {item.project.name}
                </span>
                <span>запрошено {shortDate(item.requested_at)}</span>
              </div>
              <div className="actions">
                <button
                  className="btn"
                  disabled={busyId === item.id}
                  onClick={() => act(item.id, 'confirm')}
                >
                  ✅ Подтвердить
                </button>
                <button
                  className="btn danger"
                  disabled={busyId === item.id}
                  onClick={() => act(item.id, 'reject')}
                >
                  ❌ Отклонить
                </button>
              </div>
            </div>
          ))}
        </>
      )}
    </>
  )
}
