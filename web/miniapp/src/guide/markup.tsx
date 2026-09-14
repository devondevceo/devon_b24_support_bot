/**
 * Разметка строк инструкции → узлы React.
 *
 * Своя, а не markdown-библиотека: знаков четыре (см. content.ts), а HTML из
 * строк здесь не собирается вообще — только текстовые узлы и элементы, которые
 * React экранирует сам. `dangerouslySetInnerHTML` в приложении нет (И-6).
 */
import type { ReactNode } from 'react'
import { Icon, isIconName } from '../ui/Icon'

/** Код, кнопка бота, элемент приложения, выделение — ровно в этом порядке. */
const TOKEN = /(`[^`\n]+`|\[\[[^\]\n]+\]\]|\{\{[^}\n]+\}\}|\*\*[^*\n]+\*\*)/g

export function Inline({ text }: { text: string }) {
  // split с группой в выражении оставляет найденное в массиве: чётные — текст,
  // нечётные — разметка.
  const parts = text.split(TOKEN)
  return <>{parts.map((part, i) => (i % 2 ? token(part, i) : part))}</>
}

function token(part: string, key: number): ReactNode {
  if (part.startsWith('`')) return <code key={key}>{part.slice(1, -1)}</code>
  if (part.startsWith('[[')) {
    // Кнопка бота выглядит кнопкой, а не словом в кавычках: её ищут глазами
    // под сообщением в чате, и узнавать надо форму, а не только подпись.
    return (
      <span key={key} className="bot-btn">
        {part.slice(2, -2)}
      </span>
    )
  }
  if (part.startsWith('{{')) {
    const [label = '', icon = ''] = part.slice(2, -2).split('|')
    return (
      <span key={key} className="ui-ref">
        {isIconName(icon) ? <Icon name={icon} size={16} /> : null}
        {label}
      </span>
    )
  }
  return <strong key={key}>{part.slice(2, -2)}</strong>
}

/** Та же строка без разметки — для поиска и для выдержки в результатах. */
export function plain(text: string): string {
  return text.replace(TOKEN, (part) => {
    if (part.startsWith('`')) return part.slice(1, -1)
    if (part.startsWith('{{')) return part.slice(2, -2).split('|')[0] ?? ''
    return part.slice(2, -2)
  })
}
