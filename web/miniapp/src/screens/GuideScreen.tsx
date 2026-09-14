/**
 * Инструкция: оглавление, поиск и статьи.
 *
 * Не ходит в API и не требует ни привязки, ни чата — поэтому открывается там,
 * где остальное не работает: на экране «не привязан», до выбора чата, даже без
 * подписи Telegram. Именно в этих местах она нужнее всего.
 *
 * Текст — данными (`guide/content.ts`), экран один на все статьи. Кнопка
 * «назад» клиента ходит по истории статей, как браузер: «Дальше» кладёт статью
 * сверху, «назад» снимает; из оглавления — туда, откуда инструкцию открыли.
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { Empty } from '../components/States'
import {
  GUIDE,
  findArticle,
  nextArticle,
  type GuideArticle,
  type GuideBlock,
  type GuideTarget,
} from '../guide/content'
import { Inline } from '../guide/markup'
import { Highlight, queryWords, search } from '../guide/search'
import { tg } from '../telegram'
import { Icon } from '../ui/Icon'
import { AppBar, SearchBar } from '../ui/parts'

type Props = {
  /** Статья, на которой открыть. Без неё — оглавление. */
  article?: string | null
  onBack: () => void
  /** Переход в раздел приложения. null — идти некуда: нет привязки или чата. */
  onGo: ((to: GuideTarget) => void) | null
}

export function GuideScreen({ article = null, onBack, onGo }: Props) {
  const known = article !== null && findArticle(article) !== null
  const [history, setHistory] = useState<string[]>(() => (known && article ? [article] : []))
  /*
   * Открыли сразу на статье — «назад» из неё уводит туда, откуда пришли, а не
   * в оглавление, которого человек не видел: он пришёл за ответом на один
   * вопрос. Путь в оглавление при этом есть всегда — «Все статьи» внизу.
   */
  const [rootIsToc, setRootIsToc] = useState(!known)
  const [query, setQuery] = useState('')

  // Прокрутка каждого уровня истории: вернувшись из статьи, человек обязан
  // оказаться у той же строки оглавления, а не в самом его начале.
  const scrolls = useRef<number[]>([])
  const pendingScroll = useRef<number | null>(0)

  const depth = history.length
  const current = depth ? findArticle(history[depth - 1]!) : null

  const open = useCallback(
    (id: string) => {
      scrolls.current[depth] = window.scrollY
      pendingScroll.current = 0
      tg.press()
      setHistory((h) => [...h, id])
    },
    [depth],
  )

  const back = useCallback(() => {
    if (depth === 0) {
      // Сначала гаснет поиск: «назад» из результатов — к оглавлению, а не прочь.
      if (query) setQuery('')
      else onBack()
      return
    }
    if (depth === 1 && !rootIsToc) {
      onBack()
      return
    }
    pendingScroll.current = scrolls.current[depth - 1] ?? 0
    setHistory((h) => h.slice(0, -1))
  }, [depth, onBack, query, rootIsToc])

  const toToc = useCallback(() => {
    tg.press()
    pendingScroll.current = 0
    setRootIsToc(true)
    setQuery('')
    setHistory([])
  }, [])

  useEffect(() => tg.back(back), [back])
  useEffect(() => tg.main(null), [])

  // После отрисовки нового вида, а не до: иначе страница прыгает к позиции,
  // которой у ещё не отрисованного содержимого нет.
  useLayoutEffect(() => {
    if (pendingScroll.current === null) return
    window.scrollTo(0, pendingScroll.current)
    pendingScroll.current = null
  })

  if (current) {
    return (
      <ArticleView
        article={current.article}
        section={current.section.title}
        onGo={onGo}
        onOpen={open}
        onToc={toToc}
      />
    )
  }
  return <Contents query={query} onQuery={setQuery} onOpen={open} />
}

/* ------------------------------------------------------------ оглавление */

function Contents({
  query,
  onQuery,
  onOpen,
}: {
  query: string
  onQuery: (q: string) => void
  onOpen: (id: string) => void
}) {
  const words = useMemo(() => queryWords(query), [query])
  const hits = useMemo(() => (words.length ? search(query) : null), [query, words])

  return (
    <>
      <AppBar title="Инструкция" subtitle="Как пользоваться ботом и приложением" />

      <SearchBar
        value={query}
        onChange={onQuery}
        placeholder="Поиск по инструкции"
        label="Поиск по инструкции"
      />

      {/* Число найденного — для скринридера: глазами его видно в подписи ниже,
          а на слух без этой строки поиск выглядел бы молчащим. */}
      <p className="sr-only" role="status">
        {hits === null ? '' : hits.length ? `Найдено статей: ${hits.length}` : 'Ничего не нашлось'}
      </p>

      {hits === null ? (
        GUIDE.map((section) => (
          <section key={section.id}>
            <h2 className="section-label">{section.title}</h2>
            <div className="card guide-menu">
              <div className="menu-list">
                {section.articles.map((a) => (
                  <Row key={a.id} article={a} hint={a.summary} onOpen={onOpen} />
                ))}
              </div>
            </div>
          </section>
        ))
      ) : hits.length === 0 ? (
        <Empty
          icon="search"
          title="Ничего не нашлось"
          hint={`По запросу «${query.trim()}» в инструкции ничего нет. Попробуйте слово попроще — например, «срок» или «время».`}
          action={
            <button type="button" className="btn sec" onClick={() => onQuery('')}>
              <Icon name="close" size={18} />
              Сбросить поиск
            </button>
          }
        />
      ) : (
        <section>
          <h2 className="section-label">
            Найдено
            <span className="count">{hits.length}</span>
          </h2>
          <div className="card guide-menu">
            <div className="menu-list">
              {hits.map((hit) => (
                <Row
                  key={hit.article.id}
                  article={hit.article}
                  hint={
                    <>
                      {hit.section.title} · <Highlight text={hit.snippet} words={words} />
                    </>
                  }
                  onOpen={onOpen}
                />
              ))}
            </div>
          </div>
        </section>
      )}
    </>
  )
}

function Row({
  article,
  hint,
  onOpen,
}: {
  article: GuideArticle
  hint: ReactNode
  onOpen: (id: string) => void
}) {
  return (
    <button type="button" className="menu-item" onClick={() => onOpen(article.id)}>
      <span className="menu-icon" aria-hidden="true">
        <Icon name={article.icon} size={20} />
      </span>
      <span className="menu-text">
        <span className="menu-title">{article.title}</span>
        <span className="muted">{hint}</span>
      </span>
      <Icon name="chevronRight" size={18} />
    </button>
  )
}

/* ------------------------------------------------------------------ статья */

type Part = { heading: string | null; blocks: GuideBlock[] }

/** Подзаголовок начинает новую карточку — как разделы на остальных экранах. */
function toParts(blocks: GuideBlock[]): Part[] {
  const parts: Part[] = []
  for (const block of blocks) {
    if (block.kind === 'h') parts.push({ heading: block.text, blocks: [] })
    else if (parts.length === 0) parts.push({ heading: null, blocks: [block] })
    else parts[parts.length - 1]!.blocks.push(block)
  }
  return parts.filter((p) => p.blocks.length > 0)
}

function ArticleView({
  article,
  section,
  onGo,
  onOpen,
  onToc,
}: {
  article: GuideArticle
  section: string
  onGo: ((to: GuideTarget) => void) | null
  onOpen: (id: string) => void
  onToc: () => void
}) {
  const parts = useMemo(() => toParts(article.blocks), [article])
  const next = nextArticle(article.id)
  const go = article.go

  return (
    <>
      <AppBar title={article.title} subtitle={section} />

      <article className="guide-article">
        {parts.map((part, i) => (
          <section key={i}>
            {part.heading ? <h2 className="section-label">{part.heading}</h2> : null}
            <div className="card guide-body">
              {part.blocks.map((block, k) => (
                <Block key={k} block={block} />
              ))}
            </div>
          </section>
        ))}
      </article>

      {/* Статья, после которой надо идти делать, ведёт туда кнопкой: прочитал
          «как списать время» — и сразу к задачам, а не искать их по меню. */}
      {go && onGo ? (
        <button
          type="button"
          className="btn block guide-go"
          onClick={() => {
            tg.press()
            onGo(go.to)
          }}
        >
          {go.label}
          <Icon name="chevronRight" size={18} />
        </button>
      ) : null}

      <nav className="card guide-menu" aria-label="Другие статьи">
        <div className="menu-list">
          {next ? (
            <button type="button" className="menu-item" onClick={() => onOpen(next.id)}>
              <span className="menu-icon" aria-hidden="true">
                <Icon name={next.icon} size={20} />
              </span>
              <span className="menu-text">
                <span className="muted">Дальше</span>
                <span className="menu-title">{next.title}</span>
              </span>
              <Icon name="chevronRight" size={18} />
            </button>
          ) : null}
          <button type="button" className="menu-item" onClick={onToc}>
            <span className="menu-icon" aria-hidden="true">
              <Icon name="book" size={20} />
            </span>
            <span className="menu-text">
              <span className="menu-title">Все статьи</span>
              <span className="muted">оглавление и поиск</span>
            </span>
            <Icon name="chevronRight" size={18} />
          </button>
        </div>
      </nav>
    </>
  )
}

function Block({ block }: { block: GuideBlock }) {
  switch (block.kind) {
    case 'p':
      return (
        <p className="guide-p">
          <Inline text={block.text} />
        </p>
      )
    case 'h':
      // Подзаголовки разбирает toParts: до сюда они не доходят.
      return null
    case 'steps':
      return (
        <ol className="guide-steps">
          {block.items.map((item, i) => (
            <li key={i}>
              <Inline text={item} />
            </li>
          ))}
        </ol>
      )
    case 'list':
      return (
        <ul className="guide-list">
          {block.items.map((item, i) => (
            <li key={i}>
              <Inline text={item} />
            </li>
          ))}
        </ul>
      )
    case 'defs':
      return (
        <dl className="guide-defs">
          {block.items.map(([term, text], i) => (
            <div className="guide-def" key={i}>
              <dt>
                <Inline text={term} />
              </dt>
              <dd>
                <Inline text={text} />
              </dd>
            </div>
          ))}
        </dl>
      )
    case 'legend':
      return (
        <ul className="guide-legend">
          {block.items.map(([symbol, text]) => (
            <li key={text}>
              {/* Значок — цитата из сообщений бота; скринридеру хватит слова. */}
              <span className="guide-sym" aria-hidden="true">
                {symbol}
              </span>
              {text}
            </li>
          ))}
        </ul>
      )
    case 'note':
      return (
        <div className={`notice ${block.tone}`}>
          <Icon name={block.tone === 'warn' ? 'alert' : 'info'} size={18} />
          <span>
            <Inline text={block.text} />
          </span>
        </div>
      )
  }
}
