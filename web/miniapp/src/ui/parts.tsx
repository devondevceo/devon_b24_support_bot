/**
 * Мелкие детали интерфейса, общие для всех экранов.
 *
 * Вынесены сюда по той же причине, что и `States.tsx`: пока шапка и плашка
 * собираются в каждом экране заново, они расходятся — и одинаковые по смыслу
 * места начинают выглядеть по-разному.
 */
import { useEffect, useId, useRef, type ReactNode } from 'react'
import type { Tone } from '../status'
import { tg } from '../telegram'
import { Icon, type IconName } from './Icon'

/* ------------------------------------------------------------------ шапка */

export function AppBar({
  title,
  subtitle,
  actions,
}: {
  title: string
  subtitle?: ReactNode
  actions?: ReactNode
}) {
  return (
    <header className="appbar">
      <div className="appbar-titles">
        <h1>{title}</h1>
        {subtitle ? <div className="sub">{subtitle}</div> : null}
      </div>
      {actions ? <div className="appbar-actions">{actions}</div> : null}
    </header>
  )
}

/**
 * Кнопка-иконка. `label` обязателен: без текста внутри это единственное, что
 * получит скринридер, и единственное, что видит человек в подсказке.
 */
export function IconButton({
  icon,
  label,
  onClick,
  count,
  disabled,
}: {
  icon: IconName
  label: string
  onClick: () => void
  /** Счётчик поверх кнопки. Ноль не рисуется — пустой кружок ничего не значит. */
  count?: number
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      className="icon-btn"
      aria-label={count ? `${label}: ${count}` : label}
      title={label}
      disabled={disabled}
      onClick={() => {
        tg.press()
        onClick()
      }}
    >
      <Icon name={icon} size={22} />
      {count ? (
        <span className="count" aria-hidden="true">
          {count > 99 ? '99+' : count}
        </span>
      ) : null}
    </button>
  )
}

/* ------------------------------------------------------------------ плашки */

/**
 * Плашка состояния: иконка + подпись + тон.
 *
 * Иконка и текст идут ВСЕГДА вместе. Цвет здесь — третий, усиливающий признак,
 * а не носитель смысла: правило `color-not-only` и единственный способ остаться
 * читаемым при дальтонизме и в чёрно-белом скриншоте.
 */
export function Pill({
  icon,
  children,
  tone = 'muted',
  size,
}: {
  icon?: IconName
  children: ReactNode
  tone?: Tone | 'plain'
  size?: 'lg'
}) {
  return (
    <span className={size === 'lg' ? 'pill lg' : 'pill'} data-tone={tone}>
      {icon ? <Icon name={icon} size={size === 'lg' ? 18 : 15} /> : null}
      {/* Текст отдельным элементом, чтобы он умел обрезаться многоточием:
          у flex-контейнера ellipsis не работает, и длинное «Клиент · Проект»
          уезжало за правый край экрана. */}
      <span className="pill-text">{children}</span>
    </span>
  )
}

/* -------------------------------------------------------------- скелетоны */

/**
 * Каркас списка на время загрузки.
 *
 * Спиннер по центру пустого экрана заменял собой весь список, и переход
 * «загрузка → данные» двигал страницу целиком. Каркас той же высоты не двигает
 * ничего (правило `content-jumping`, оно же CLS).
 */
export function TaskSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="card flush" aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="task" style={{ cursor: 'default' }}>
          <div className="skeleton" style={{ width: 10, height: 10, marginTop: 6 }} />
          <div>
            <div
              className="skeleton"
              style={{ height: 15, width: `${88 - ((i * 13) % 34)}%` }}
            />
            <div
              className="skeleton"
              style={{ height: 11, width: `${52 - ((i * 7) % 18)}%`, marginTop: 9 }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

/** Полоса «идёт обновление» — данные на экране прежние и остаются читаемыми. */
export function Refreshing({ label }: { label: string }) {
  return (
    <div className="progress" role="status" aria-label={label}>
      <span className="sr-only">{label}</span>
    </div>
  )
}

/* ------------------------------------------------------------------- лист */

/**
 * Лист снизу — для правки и всего, что раньше разворачивалось прямо в потоке
 * страницы. Разворачивание сдвигало вниз всё, что было под ним (комментарии),
 * а сворачивание после сохранения возвращало страницу рывком.
 *
 * Закрывается тремя путями, как и требует `modal-escape`: подложкой, Esc и
 * системной кнопкой «назад» клиента.
 */
export function Sheet({
  title,
  onClose,
  children,
}: {
  title: string
  onClose: () => void
  children: ReactNode
}) {
  const panel = useRef<HTMLDivElement>(null)
  const titleId = useId()

  // Системная кнопка «назад» закрывает лист, а не экран под ним: она в стеке
  // выше обработчика карточки (см. tg.back).
  useEffect(() => tg.back(onClose), [onClose])

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    const body = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    panel.current?.focus()

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)

    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = body
      // Фокус возвращается тому, кто лист открыл, иначе после закрытия он
      // улетает в начало документа и навигация с клавиатуры начинается заново.
      opener?.focus?.()
    }
  }, [onClose])

  return (
    <>
      <button
        type="button"
        className="scrim"
        aria-label="Закрыть"
        onClick={onClose}
      />
      <div
        className="sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={panel}
      >
        <div className="sheet-grip" aria-hidden="true" />
        <h2 id={titleId}>{title}</h2>
        {children}
      </div>
    </>
  )
}
