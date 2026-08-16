/**
 * Выбор чата — только в личке, когда мини-апп открыт без ссылки из группы.
 *
 * Из группы контекст приходит подписанным параметром `startapp`, менять его
 * нельзя: иначе кнопка в чате одного клиента открывала бы задачи другого.
 */
import { useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, Failure, Loading } from '../components/States'
import { tg } from '../telegram'
import type { ContextListItem } from '../types'

type Props = { onPick: (chatRef: number) => void; onBack: (() => void) | null }

export function ContextPicker({ onPick, onBack }: Props) {
  const [items, setItems] = useState<ContextListItem[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    setError(null)
    api
      .contexts()
      .then((res) => setItems(res.items))
      .catch(setError)
  }, [reload])

  useEffect(() => tg.back(onBack), [onBack])
  useEffect(() => tg.main(null), [])

  if (error) return <Failure error={error} onRetry={() => setReload((n) => n + 1)} />
  if (items === null) return <Loading title="Ищем ваши чаты…" />
  if (items.length === 0) {
    return (
      <Empty
        title="Нет доступных чатов"
        hint="Ни один чат с вашими проектами не привязан к Битрикс24. Привязку делает администратор портала — командой /bind в чате или в приложении внутри Битрикс24."
      />
    )
  }

  return (
    <>
      <div className="head">
        <h1>Выберите чат</h1>
        <div className="sub">Задачи показываются в разрезе чата и его проектов</div>
      </div>
      <div className="card tight" style={{ padding: 0, overflow: 'hidden' }}>
        {items.map((item) => (
          <button
            key={item.chat_ref}
            className="task"
            onClick={() => {
              tg.tap()
              onPick(item.chat_ref)
            }}
          >
            <div className="task-title">{item.title}</div>
            <div className="task-meta">
              {item.projects.map((p) => (
                <span key={p.id}>
                  {p.client} · {p.name}
                </span>
              ))}
            </div>
          </button>
        ))}
      </div>
    </>
  )
}
