/**
 * Четыре обязательных состояния экрана: загрузка, пусто, ошибка, нет прав
 * (docs/50-web-and-b24-app.md §1.2). Вынесены сюда, чтобы ни один экран не мог
 * «забыть» какое-то из них и показать человеку пустоту без объяснения.
 */
import type { ReactNode } from 'react'
import { ApiError } from '../api'

export function Loading({ title = 'Загружаем…' }: { title?: string }) {
  return (
    <div className="state">
      <div className="spinner" />
      <p>{title}</p>
    </div>
  )
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="state">
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

  return (
    <div className="state">
      <h2>{titleFor(code)}</h2>
      <p>{message}</p>
      {code === 'unauthenticated' ? null : (
        <p className="muted">
          код: {code}
          {api?.status ? ` · ${api.status}` : ''}
        </p>
      )}
      {onRetry && code !== 'unauthenticated' ? (
        <button className="btn sec" onClick={onRetry}>
          Повторить
        </button>
      ) : null}
    </div>
  )
}

function titleFor(code: string): string {
  switch (code) {
    case 'unauthenticated':
      return 'Приложение открыто не из Telegram'
    case 'not_linked':
      return 'Telegram не привязан'
    case 'needs_reauth':
      return 'Доступ к Битрикс24 истёк'
    case 'forbidden':
    case 'b24_forbidden':
      return 'Битрикс24 не разрешает'
    case 'not_found':
      return 'Не найдено'
    case 'no_project':
      return 'Чат не привязан к проекту'
    case 'rate_limited':
      return 'Слишком часто'
    case 'network':
      return 'Нет связи'
    default:
      return 'Не получилось'
  }
}
