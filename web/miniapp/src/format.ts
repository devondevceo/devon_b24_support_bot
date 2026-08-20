/** Форматирование дат. Всё показываем в часовом поясе устройства человека. */

const TIME = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' })
const DAY = new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit' })
const FULL = new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit',
  month: '2-digit',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

function parse(value: string | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function shortDate(value: string | null | undefined): string {
  const date = parse(value)
  if (!date) return '—'
  const now = new Date()
  if (date.toDateString() === now.toDateString()) return `сегодня ${TIME.format(date)}`
  if (date.getFullYear() === now.getFullYear()) return `${DAY.format(date)} ${TIME.format(date)}`
  return FULL.format(date)
}

export function fullDate(value: string | null | undefined): string {
  const date = parse(value)
  return date ? FULL.format(date) : '—'
}

/**
 * Значение для <input type="datetime-local">: он принимает ЛОКАЛЬНОЕ время без
 * зоны, поэтому UTC-строку из портала надо сдвинуть, иначе поле показывает
 * не тот час, который видит человек в Битриксе.
 */
export function toLocalInput(value: string | null | undefined): string {
  const date = parse(value)
  if (!date) return ''
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return shifted.toISOString().slice(0, 16)
}

/**
 * Обратное преобразование — с ЯВНЫМ смещением.
 *
 * Без него портал трактует дату в часовом поясе пользователя Битрикса, а он может
 * не совпадать с поясом телефона. В мультитенанте это расхождение однажды сдвинет
 * все сроки на несколько часов, и найти причину будет трудно.
 */
export function fromLocalInput(value: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const offset = -date.getTimezoneOffset()
  const sign = offset >= 0 ? '+' : '-'
  const pad = (n: number) => String(Math.floor(Math.abs(n))).padStart(2, '0')
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return `${local.toISOString().slice(0, 19)}${sign}${pad(offset / 60)}:${pad(offset % 60)}`
}

export function deadlineLabel(value: string | null | undefined, overdue: boolean): string {
  if (!value) return 'без срока'
  return overdue ? `просрочено ${shortDate(value)}` : `до ${shortDate(value)}`
}

/** Идентификатор формы для идемпотентности создания задачи (инвариант И-10). */
export function formId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** «5 ч 30 мин». Ноль — прочерк, а не «0 ч»: работали ноль времени. */
export function duration(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.round((seconds ?? 0) / 60))
  if (total === 0) return '—'
  const hours = Math.floor(total / 60)
  const minutes = total % 60
  if (hours && minutes) return `${hours} ч ${minutes} мин`
  return hours ? `${hours} ч` : `${minutes} мин`
}
