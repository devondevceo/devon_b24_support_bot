/**
 * Состояние задачи → иконка, тон и подпись.
 *
 * Раскладка живёт в одном месте, потому что состояние показывают четыре разных
 * места: точка в строке списка, плашка в карточке, значок в форме, подпись для
 * скринридера. Разъедься они — «Выполняется» в списке и в карточке выглядели бы
 * по-разному, и человек решил бы, что это разные вещи.
 *
 * Коды статусов — 2, 3, 4, 5, 6. Ни «Новой», ни «Отклонена» на портале нет,
 * а «Новые» — это стадия канбана, а не статус (docs/00-portal-facts.md §3).
 */
import type { IconName } from './ui/Icon'

/** Тон = семантический цвет из палитры. Сырых hex в компонентах нет. */
export type Tone = 'muted' | 'accent' | 'warn' | 'ok' | 'danger'

export type StatusView = { icon: IconName; tone: Tone; title: string }

const BY_CODE: Record<number, StatusView> = {
  2: { icon: 'hourglass', tone: 'muted', title: 'Ждёт выполнения' },
  3: { icon: 'play', tone: 'accent', title: 'Выполняется' },
  4: { icon: 'eye', tone: 'warn', title: 'Ожидает контроля' },
  5: { icon: 'check', tone: 'ok', title: 'Завершена' },
  6: { icon: 'pause', tone: 'muted', title: 'Отложена' },
}

const UNKNOWN: StatusView = { icon: 'info', tone: 'muted', title: 'Статус неизвестен' }

/**
 * `status_title` приходит с сервера и остаётся источником подписи: одно и то же
 * состояние обязано называться одинаково в боте и здесь. Локальная таблица
 * добавляет к подписи иконку и тон и подстраховывает, если код незнаком.
 */
export function statusView(code: number | null, serverTitle?: string): StatusView {
  const known = code === null ? undefined : BY_CODE[code]
  const base = known ?? UNKNOWN
  return serverTitle ? { ...base, title: serverTitle } : base
}

/**
 * Просрочка — отдельный признак, а не шестой статус: просроченной может быть
 * и задача «в работе», и «ждёт выполнения». На портале это вообще псевдостатус
 * в другом поле (`subStatus`), и смешивать их нельзя.
 */
export const OVERDUE: StatusView = { icon: 'alert', tone: 'danger', title: 'Просрочена' }

/** Приоритет: показываем только высокий — средний это умолчание, а низкий шум. */
export function priorityView(priority: number): StatusView | null {
  return priority === 2 ? { icon: 'flag', tone: 'danger', title: 'Высокий приоритет' } : null
}
