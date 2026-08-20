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
import { Icon } from '../ui/Icon'
import { AppBar, Pill } from '../ui/parts'
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
    api.approvals
      .list()
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

  const run = useCallback(async (id: number, decision: 'confirm' | 'reject') => {
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

  /*
   * Решение спрашивается подтверждением, и оба — и «да», и «нет».
   *
   * Отменить его нельзя: задача уже уехала на свою стадию канбана в портале,
   * а тамошнее уведомление уже ушло автору. Раз отката нет, единственное место,
   * где ошибку ещё можно поймать, — до нажатия (правило confirmation-dialogs).
   */
  const act = useCallback(
    (item: Approval, decision: 'confirm' | 'reject') => {
      const what = decision === 'confirm' ? 'Подтвердить' : 'Отклонить'
      tg.confirm(
        `${what} задачу #${item.task_id} «${item.title}»?\n\nОтменить решение будет нельзя.`,
        (ok) => {
          if (ok) void run(item.id, decision)
        },
      )
    },
    [run],
  )

  return (
    <>
      <AppBar
        title="Ожидают подтверждения"
        subtitle={items && items.length > 0 ? 'Решение уйдёт в Битрикс24 сразу' : undefined}
      />

      {error ? (
        <Failure error={error} onRetry={() => setReload((n) => n + 1)} />
      ) : !items ? (
        <Loading title="Смотрим, что ждёт решения…" />
      ) : items.length === 0 ? (
        <Empty
          icon="checkCircle"
          title="Нечего подтверждать"
          hint="Здесь появятся задачи, для которых вас назначили ответственным за решение."
        />
      ) : (
        <>
          {total > items.length ? (
            <div className="notice warn">
              <Icon name="alert" size={18} />
              <span>
                Показаны первые {items.length} из {total}.
              </span>
            </div>
          ) : null}

          {/* Та же вёрстка, что у шапки карточки задачи: одна и та же вещь —
              номер, заголовок, признаки — обязана выглядеть одинаково.
              Инлайн-стили здесь были и увели цвет мимо токенов: аудит поймал
              номер задачи на 2.85:1. */}
          {items.map((item) => (
            <article className="task-hero" key={item.id}>
              <div className="id">#{item.task_id}</div>
              <h2>{item.title}</h2>
              <div className="pill-row" style={{ marginBottom: 'var(--sp-6)' }}>
                <Pill icon="folder">
                  {item.project.client} · {item.project.name}
                </Pill>
                <Pill icon="clock" tone="plain">
                  {shortDate(item.requested_at)}
                </Pill>
              </div>
              <div className="actions">
                <button
                  type="button"
                  className="btn"
                  disabled={busyId === item.id}
                  onClick={() => act(item, 'confirm')}
                >
                  <Icon name="check" size={18} />
                  Подтвердить
                </button>
                {/* Отказ — вторичный по виду и в опасном тоне: это не
                    равнозначная альтернатива, а другой по последствиям шаг. */}
                <button
                  type="button"
                  className="btn danger"
                  disabled={busyId === item.id}
                  onClick={() => act(item, 'reject')}
                >
                  <Icon name="close" size={18} />
                  Отклонить
                </button>
              </div>
            </article>
          ))}
        </>
      )}
    </>
  )
}
