/**
 * Четыре обязательных состояния экрана: загрузка, пусто, ошибка, нет прав
 * (docs/50-web-and-b24-app.md §1.2). Вынесены сюда, чтобы ни один экран не мог
 * «забыть» какое-то из них и показать человеку пустоту без объяснения.
 */
import type { ReactNode } from 'react'
import { ApiError } from '../api'
import { Icon, type IconName } from '../ui/Icon'

export function Loading({ title = 'Загружаем…' }: { title?: string }) {
  return (
    <div className="state" role="status">
      <div className="spinner center" />
      <p>{title}</p>
    </div>
  )
}

export function Empty({
  title,
  hint,
  action,
  icon = 'inbox',
}: {
  title: string
  hint?: string
  action?: ReactNode
  icon?: IconName
}) {
  return (
    <div className="state">
      <div className="glyph">
        <Icon name={icon} size={26} />
      </div>
      <h2>{title}</h2>
      {hint ? <p>{hint}</p> : null}
      {action}
    </div>
  )
}

export function Failure({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const api = error instanceof ApiError ? error : null
  const code = api?.code ?? 'unknown'
  const message = api?.message ?? 'Не получилось. Попробуйте ещё раз.'
  const view = VIEW[code] ?? { title: 'Не получилось', icon: 'alert' as IconName }

  return (
    <div className="state" role="alert">
      <div className="glyph">
        <Icon name={view.icon} size={26} />
      </div>
      <h2>{view.title}</h2>
      <p>{message}</p>
      {/* Код нужен для разговора с поддержкой, но он служебный: мельче и тише
          самого объяснения, а не вровень с ним. */}
      {code === 'unauthenticated' ? null : (
        <p className="muted code">
          код: {code}
          {api?.status ? ` · ${api.status}` : ''}
        </p>
      )}
      {onRetry && code !== 'unauthenticated' ? (
        <button type="button" className="btn sec" onClick={onRetry}>
          <Icon name="refresh" size={18} />
          Повторить
        </button>
      ) : null}
    </div>
  )
}

/**
 * Заголовок и значок по коду ошибки.
 *
 * «Просрочена подпись», «нет прав» и «нет связи» — три разные беды с тремя
 * разными действиями человека, и одно «что-то пошло не так» на всех троих
 * не подсказывает ни одного из них.
 */
const VIEW: Record<string, { title: string; icon: IconName }> = {
  unauthenticated: { title: 'Приложение открыто не из Telegram', icon: 'info' },
  not_linked: { title: 'Telegram не привязан', icon: 'link' },
  needs_reauth: { title: 'Доступ к Битрикс24 истёк', icon: 'link' },
  forbidden: { title: 'Битрикс24 не разрешает', icon: 'alert' },
  b24_forbidden: { title: 'Битрикс24 не разрешает', icon: 'alert' },
  not_found: { title: 'Не найдено', icon: 'search' },
  no_project: { title: 'Чат не привязан к проекту', icon: 'folder' },
  rate_limited: { title: 'Слишком часто', icon: 'timer' },
  network: { title: 'Нет связи', icon: 'refresh' },
  already_done: { title: 'Решение уже принято', icon: 'checkCircle' },
}
