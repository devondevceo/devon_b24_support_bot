/**
 * Выбор чата — только в личке, когда мини-апп открыт без ссылки из группы.
 *
 * Из группы контекст приходит подписанным параметром `startapp`, менять его
 * нельзя: иначе кнопка в чате одного клиента открывала бы задачи другого.
 */
import { useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, Failure } from '../components/States'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { AppBar, TaskSkeleton } from '../ui/parts'
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

  return (
    <>
      <AppBar title="Выберите чат" subtitle="Задачи показываются в разрезе чата и его проектов" />

      {items === null ? (
        <TaskSkeleton rows={3} />
      ) : items.length === 0 ? (
        <Empty
          icon="message"
          title="Нет доступных чатов"
          hint="Ни один чат с вашими проектами не привязан к Битрикс24. Привязку делает администратор портала — командой /bind в чате или в приложении внутри Битрикс24."
        />
      ) : (
        <div className="card flush">
          {items.map((item) => (
            <button
              type="button"
              key={item.chat_ref}
              className="task"
              aria-label={`${item.title}. Проектов: ${item.projects.length}`}
              onClick={() => {
                tg.press()
                onPick(item.chat_ref)
              }}
            >
              <Icon name="message" size={20} style={{ marginTop: 2, color: 'var(--hint)' }} />
              <span className="task-title">{item.title}</span>
              <span className="task-meta" aria-hidden="true">
                {item.projects.map((p) => (
                  <span key={p.id} className="who">
                    {p.client} · {p.name}
                  </span>
                ))}
              </span>
            </button>
          ))}
        </div>
      )}
    </>
  )
}
