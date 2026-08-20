/**
 * Меню разделов — лист снизу от кнопки «⋯» в шапке.
 *
 * Разделов у приложения стало шесть, и все шесть в шапку иконками не влезают:
 * при 44px на цель это 264px из 375px ширины экрана, то есть заголовку чата
 * не остаётся ничего. Правило `overflow-menu` ровно про этот случай — когда
 * действий больше, чем места, они уходят в меню, а не ужимаются до 24px.
 *
 * Наружу вынесена одна кнопка — «ожидают подтверждения»: она несёт счётчик,
 * а счётчик, спрятанный в меню, не сообщает ничего.
 */
import type { ReactNode } from 'react'
import { tg } from '../telegram'
import { Icon, type IconName } from './Icon'
import { Sheet } from './parts'

export type MenuItem = {
  key: string
  icon: IconName
  title: string
  hint?: string
  badge?: number
  onPick: () => void
}

export function MenuSheet({
  items,
  onClose,
  footer,
}: {
  items: MenuItem[]
  onClose: () => void
  footer?: ReactNode
}) {
  return (
    <Sheet title="Разделы" onClose={onClose}>
      <div className="menu-list">
        {items.map((item) => (
          <button
            type="button"
            className="menu-item"
            key={item.key}
            onClick={() => {
              tg.press()
              // Лист закрывается до перехода: иначе он на мгновение остаётся
              // поверх нового экрана и выглядит как незакрывшееся окно.
              onClose()
              item.onPick()
            }}
          >
            <span className="menu-icon" aria-hidden="true">
              <Icon name={item.icon} size={20} />
            </span>
            <span className="menu-text">
              <span className="menu-title">{item.title}</span>
              {item.hint ? <span className="muted">{item.hint}</span> : null}
            </span>
            {item.badge ? (
              <span className="pill" data-tone="danger">
                {item.badge}
              </span>
            ) : null}
            <Icon name="chevronRight" size={18} />
          </button>
        ))}
      </div>
      {footer}
    </Sheet>
  )
}
