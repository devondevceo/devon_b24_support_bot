/**
 * Списание времени в задачу — листом снизу поверх карточки.
 *
 * Здесь же видно, кто сколько уже списал: вопрос «сколько мы потратили на эту
 * задачу» коллективный, и делить список на «своё/чужое» значило бы мешать
 * сверять итог с тем, что показывает сам Битрикс24.
 *
 * Отличие от бота — не в наборе действий, а в том, что экран позволяет: дату
 * начала (списать за вчера) и комментарий одной формой. В чате того же добиваются
 * командой `/time`, потому что диалог линеен, а свободный ввод в группе перехватывал
 * бы чужие реплики.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import { duration, fromLocalInput, fullDate } from '../format'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { Sheet } from '../ui/parts'
import type { Context, Timelog } from '../types'

type Props = {
  context: Context
  taskId: number
  onClose: () => void
  /** Карточка обновляет свою строку «Трудозатраты»; список ничего не пересчитывает. */
  onLogged?: (totalSeconds: number) => void
}

/*
 * Право на списание приходит вместе со списаниями (`can_add`), а не задаётся
 * снаружи: лист открывается и из карточки, и прямо из списка задач, где блок
 * `action` никто не читал. Одно место принимает решение — второго и не нужно.
 */
export function TimelogSheet({ context, taskId, onClose, onLogged }: Props) {
  const [data, setData] = useState<Timelog | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const [value, setValue] = useState('')
  const [comment, setComment] = useState('')
  const [startedAt, setStartedAt] = useState('')

  useEffect(() => {
    api.timelog(context.chat_ref, taskId).then(setData).catch(setError)
  }, [context.chat_ref, taskId])

  const submit = useCallback(
    async (text: string) => {
      if (!text.trim()) return
      setBusy(true)
      setNotice(null)
      try {
        const fresh = await api.addTimelog(
          context.chat_ref,
          taskId,
          text,
          comment,
          startedAt ? fromLocalInput(startedAt) : undefined,
        )
        setData(fresh)
        onLogged?.(fresh.total_seconds)
        setValue('')
        setComment('')
        setStartedAt('')
        tg.done()
        setNotice({ kind: 'ok', text: 'Время списано.' })
      } catch (err) {
        tg.fail()
        setNotice({
          kind: 'err',
          text: err instanceof ApiError ? err.message : 'Не получилось. Попробуйте ещё раз.',
        })
      } finally {
        setBusy(false)
      }
    },
    [comment, context.chat_ref, onLogged, startedAt, taskId],
  )

  return (
    <Sheet title="Трудозатраты" onClose={onClose}>
      {error ? (
        <p className="notice err">Не удалось прочитать списания.</p>
      ) : data === null ? (
        <p className="muted">Загружаем…</p>
      ) : (
        <>
          <p className="timelog-total">
            Всего по задаче <strong className="num">{duration(data.total_seconds)}</strong>
          </p>

          {data.items.length === 0 ? (
            <p className="muted">Списаний пока нет.</p>
          ) : (
            <ul className="timelog-list">
              {data.items.map((e) => (
                <li key={e.id}>
                  <div className="timelog-row">
                    <span className="num">{duration(e.seconds)}</span>
                    <span className="who">{e.user_name}</span>
                    <span className="muted num">{e.at ? fullDate(e.at) : '—'}</span>
                  </div>
                  {e.comment ? <div className="timelog-comment">{e.comment}</div> : null}
                </li>
              ))}
            </ul>
          )}

          {/* Неполнота выборки — не деталь реализации, а разница между «списаний
              больше нет» и «остальные не видны». Молчать о ней нельзя. */}
          {data.complete ? null : (
            <p className="notice warn">
              Показаны не все списания: портал отдаёт список порциями и не умеет листать.
              Сумма выше — из самой задачи и верна.
            </p>
          )}

          {data.can_add ? (
            <>
              <h3 className="section-label">Списать время</h3>
              <div className="preset-row">
                {data.presets.map((p) => (
                  <button
                    key={p.seconds}
                    type="button"
                    className="btn sec"
                    disabled={busy}
                    // Пресет уходит той же строкой, что и ручной ввод: голое
                    // число — минуты. Один разбор на все двери, второго нет.
                    onClick={() => submit(String(Math.round(p.seconds / 60)))}
                  >
                    {p.label}
                  </button>
                ))}
              </div>

              <label className="field">
                <span className="label">Другая длительность</span>
                <input
                  type="text"
                  inputMode="text"
                  value={value}
                  placeholder="1ч30м, 1:30, 45 или 1,5"
                  onChange={(e) => setValue(e.target.value)}
                />
                {/* Правило то же, что в боте (`texts.MSG_TIMELOG_FORMAT`), и
                    названо теми же словами: разбор один, значит и объяснение
                    обязано быть одно. */}
                <span className="help">
                  Целое число — минуты, дробное — часы: 1,5 это полтора.
                </span>
              </label>

              <label className="field">
                <span className="label">Комментарий</span>
                <input
                  type="text"
                  value={comment}
                  placeholder="чем занимались"
                  onChange={(e) => setComment(e.target.value)}
                />
              </label>

              <label className="field">
                <span className="label">Когда работали</span>
                <input
                  type="datetime-local"
                  value={startedAt}
                  onChange={(e) => setStartedAt(e.target.value)}
                />
                <span className="help">Пусто — сейчас.</span>
              </label>

              <button type="button" className="btn" disabled={busy || !value.trim()} onClick={() => submit(value)}>
                <Icon name="timer" size={18} />
                Списать
              </button>
            </>
          ) : (
            <p className="muted">Списывать время в эту задачу вам не разрешено в Битрикс24.</p>
          )}

          {notice ? <p className={`notice ${notice.kind}`}>{notice.text}</p> : null}
        </>
      )}
    </Sheet>
  )
}
