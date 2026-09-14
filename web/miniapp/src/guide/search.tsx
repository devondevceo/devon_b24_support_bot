/**
 * Поиск по инструкции.
 *
 * Сервер не нужен: статей два десятка, и всё уже в бандле. Ищем по тем же
 * строкам, что видит человек, плюс по словам-синонимам статьи (`keywords`):
 * «как списать часы» обязано находить статью, в тексте которой написано «время».
 */
import type { ReactNode } from 'react'
import { GUIDE, type GuideArticle, type GuideBlock, type GuideSection } from './content'
import { plain } from './markup'

export type Hit = {
  article: GuideArticle
  section: GuideSection
  /** Кусок текста вокруг первого совпадения — чтобы было видно, почему нашлось. */
  snippet: string
}

/** Строчные и «е» вместо «ё»: «ещё» и «еще» одно слово, найтись должны одинаково. */
function norm(text: string): string {
  return text.toLowerCase().replace(/ё/g, 'е')
}

/**
 * Слова запроса. Однобуквенные отброшены: «в» и «и» находятся в каждой
 * статье и превратили бы поиск в оглавление с другим порядком строк.
 */
export function queryWords(query: string): string[] {
  return norm(query).split(/\s+/).filter((w) => w.length > 1)
}

function blockLines(block: GuideBlock): string[] {
  switch (block.kind) {
    case 'p':
    case 'h':
    case 'note':
      return [block.text]
    case 'steps':
    case 'list':
      return block.items
    case 'defs':
    case 'legend':
      return block.items.map(([term, text]) => `${term} — ${text}`)
  }
}

type Indexed = {
  article: GuideArticle
  section: GuideSection
  title: string
  head: string
  lines: string[]
  normLines: string[]
}

const INDEX: Indexed[] = GUIDE.flatMap((section) =>
  section.articles.map((article) => {
    const lines = article.blocks.flatMap(blockLines).map(plain)
    return {
      article,
      section,
      title: norm(article.title),
      head: norm([article.title, article.summary, ...(article.keywords ?? [])].join(' ')),
      lines,
      normLines: lines.map(norm),
    }
  }),
)

/**
 * Статьи, где есть ВСЕ слова запроса. Сначала те, где слово в заголовке,
 * потом — в описании и синонимах, потом — только в тексте. Внутри группы
 * порядок оглавления: сортировка устойчивая.
 */
export function search(query: string): Hit[] {
  const words = queryWords(query)
  if (words.length === 0) return []
  const ranked: { hit: Hit; rank: number }[] = []
  for (const item of INDEX) {
    const all = `${item.head} ${item.normLines.join(' ')}`
    if (!words.every((w) => all.includes(w))) continue
    const rank = words.some((w) => item.title.includes(w))
      ? 0
      : words.every((w) => item.head.includes(w))
        ? 1
        : 2
    ranked.push({
      hit: { article: item.article, section: item.section, snippet: snippetOf(item, words) },
      rank,
    })
  }
  return ranked.sort((a, b) => a.rank - b.rank).map((r) => r.hit)
}

const BEFORE = 32
const AFTER = 72

function snippetOf(item: Indexed, words: string[]): string {
  const index = item.normLines.findIndex((line) => words.some((w) => line.includes(w)))
  if (index < 0) return item.article.summary
  const line = item.lines[index]!
  const lower = item.normLines[index]!
  const at = Math.min(...words.map((w) => lower.indexOf(w)).filter((i) => i >= 0))

  const start = Math.max(0, at - BEFORE)
  const end = Math.min(line.length, at + AFTER)
  let out = line.slice(start, end)
  // Обрезаем по словам: половина слова в начале выдержки читается как опечатка.
  // Но не дальше самого совпадения — ради него выдержка и показывается.
  if (start > 0) {
    const cut = out.indexOf(' ')
    if (cut >= 0 && cut < at - start) out = out.slice(cut + 1)
    out = `…${out}`
  }
  if (end < line.length) {
    const cut = out.lastIndexOf(' ')
    if (cut > 0) out = out.slice(0, cut)
    out = `${out.replace(/[\s.,;:—]+$/, '')}…`
  }
  return withoutEmoji(out)
}

/**
 * В выдержке эмодзи из цитат кнопок — шум: строка результата сама стоит со
 * значком, а «⏰ Срок — Сегодня… 🚫 Снять срок» читается хуже, чем без них.
 * В статье они остаются: там по ним узнают кнопку в чате.
 */
function withoutEmoji(text: string): string {
  return text.replace(/\p{Extended_Pictographic}️?\s?/gu, '').replace(/\s{2,}/g, ' ').trim()
}

/** Найденное подсвечено: глазу не нужно заново искать в выдержке то же слово. */
export function Highlight({ text, words }: { text: string; words: string[] }) {
  const lower = norm(text)
  const out: ReactNode[] = []
  let pos = 0
  while (pos < text.length) {
    let best = -1
    let size = 0
    for (const w of words) {
      const i = lower.indexOf(w, pos)
      if (i >= 0 && (best < 0 || i < best || (i === best && w.length > size))) {
        best = i
        size = w.length
      }
    }
    if (best < 0) break
    if (best > pos) out.push(text.slice(pos, best))
    out.push(
      <mark key={best} className="guide-hit">
        {text.slice(best, best + size)}
      </mark>,
    )
    pos = best + size
  }
  if (pos < text.length) out.push(text.slice(pos))
  return <>{out}</>
}
