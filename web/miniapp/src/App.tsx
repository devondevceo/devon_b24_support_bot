/**
 * Навигация мини-аппа. Роутера нет намеренно: экранов десяток, а адресная строка
 * внутри Telegram не видна и не сохраняется — маршруты некуда и незачем писать.
 */
import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, ApiError } from './api'
import { Empty, Failure, Loading } from './components/States'
import type { GuideTarget } from './guide/content'
import { AboutScreen } from './screens/AboutScreen'
import { ApprovalsScreen } from './screens/ApprovalsScreen'
import { ContextPicker } from './screens/ContextPicker'
import { CreateTask } from './screens/CreateTask'
import { NotLinkedScreen } from './screens/NotLinkedScreen'
import { SummaryScreen } from './screens/SummaryScreen'
import { TaskCardScreen } from './screens/TaskCardScreen'
import { TaskList } from './screens/TaskList'
import { TimesheetScreen } from './screens/TimesheetScreen'
import { tg } from './telegram'
import { Icon } from './ui/Icon'
import type { Bootstrap, Context, TaskFilter } from './types'

type Screen =
  | { name: 'list'; filter?: TaskFilter }
  | { name: 'card'; taskId: number }
  | { name: 'create' }
  | { name: 'pick' }
  | { name: 'approvals' }
  | { name: 'summary' }
  | { name: 'timesheet' }
  | { name: 'about' }

/**
 * Инструкция — не экран в общем ряду, а слой поверх любого из них.
 *
 * Она не ходит в API, поэтому доступна там, где остальное не работает: без
 * привязки к Битрикс24, до выбора чата и даже без подписи Telegram. «Назад» из
 * неё возвращает туда, откуда открыли: экран под ней остаётся в `screen`.
 */
type Guide = { article: string | null } | null

/** Кнопка «📖 Инструкция» бота открывает приложение сразу на инструкции. */
function guideFromUrl(): Guide {
  return new URLSearchParams(window.location.search).get('open') === 'guide'
    ? { article: null }
    : null
}

/*
 * Инструкция — отдельный кусок сборки. Это 56 КБ из 270, а открывают её редко:
 * каждое открытие приложения за ней по мобильной сети не ходит, грузится она
 * при первом нажатии. Каждая попытка — новый `lazy`: отказ загрузки React
 * запоминает, и повтор с тем же компонентом упал бы сразу, не сходив в сеть.
 */
const loadGuide = () =>
  import('./screens/GuideScreen').then((module) => ({ default: module.GuideScreen }))

/** Куда ведут кнопки в конце статей. Таблицей, чтобы новый раздел не забыли. */
const TARGETS: Record<GuideTarget, Screen> = {
  list: { name: 'list' },
  create: { name: 'create' },
  summary: { name: 'summary' },
  timesheet: { name: 'timesheet' },
  approvals: { name: 'approvals' },
  about: { name: 'about' },
}

export function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [error, setError] = useState<unknown>(null)
  /*
   * Подписи Telegram нет — различаем, где мы: в обычном браузере или в клиенте
   * Telegram, открытые кнопкой нижней клавиатуры (у таких initData пуст по
   * устройству Telegram). Совет в двух случаях разный.
   */
  const [unsigned, setUnsigned] = useState<'browser' | 'keyboard' | null>(null)
  const [context, setContext] = useState<Context | null>(null)
  const [screen, setScreen] = useState<Screen>({ name: 'list' })
  const [guide, setGuide] = useState<Guide>(guideFromUrl)
  const [guideAttempt, setGuideAttempt] = useState(0)
  // Зависимость от номера попытки — не лишняя: каждая попытка обязана дать
  // НОВЫЙ lazy-компонент, иначе React вернёт запомненный отказ (см. loadGuide).
  const GuideScreen = useMemo(() => lazy(loadGuide), [guideAttempt])
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!tg.available) {
      setUnsigned(tg.insideTelegram() ? 'keyboard' : 'browser')
      return
    }
    setError(null)
    api
      .bootstrap()
      .then((data) => {
        setBoot(data)
        if (data.state === 'ok') {
          setContext(data.context)
          // Из карточки задачи в чате ссылка ведёт сразу в эту задачу.
          setScreen(
            data.context?.task_id
              ? { name: 'card', taskId: data.context.task_id }
              : data.context
                ? { name: 'list' }
                : { name: 'pick' },
          )
        }
      })
      .catch(setError)
  }, [reload])

  const pick = useCallback(async (chatRef: number) => {
    try {
      const res = await api.tasks(chatRef, 'all', '')
      setContext(res.context)
      setScreen({ name: 'list' })
    } catch (err) {
      tg.fail()
      tg.alert(err instanceof ApiError ? err.message : 'Чат не открылся.')
    }
  }, [])

  const backToList = useCallback(() => setScreen({ name: 'list' }), [])
  const openGuide = useCallback((article?: string) => setGuide({ article: article ?? null }), [])
  const closeGuide = useCallback(() => setGuide(null), [])
  const go = useCallback((to: GuideTarget) => {
    setGuide(null)
    setScreen(TARGETS[to])
  }, [])

  if (guide) {
    return (
      <Shell>
        <GuideBoundary
          key={guideAttempt}
          onRetry={() => setGuideAttempt((n) => n + 1)}
          onBack={closeGuide}
        >
          <Suspense fallback={<Loading title="Открываем инструкцию…" />}>
            <GuideScreen
              // Открыли на другой статье — это другая инструкция, с чистой историей.
              key={guide.article ?? 'contents'}
              article={guide.article}
              onBack={closeGuide}
              // Переходить из статьи в раздел можно, только когда разделам есть
              // что показать: привязка проверена и чат выбран.
              onGo={boot?.state === 'ok' && context ? go : null}
            />
          </Suspense>
        </GuideBoundary>
      </Shell>
    )
  }

  if (unsigned) {
    return (
      <Shell>
        <Empty
          icon="info"
          title={unsigned === 'keyboard' ? 'Не видно подписи Telegram' : 'Приложение открыто не из Telegram'}
          hint={
            unsigned === 'keyboard'
              ? 'Так бывает, если открыть приложение кнопкой нижней клавиатуры бота: с неё Telegram не сообщает приложению, кто вы. Закройте его и откройте кнопкой «Задачи» у поля ввода в чате с ботом.'
              : 'Эта страница работает только внутри Telegram: откройте её из чата с ботом.'
          }
          action={
            <button type="button" className="btn sec" onClick={() => openGuide('open-app')}>
              <Icon name="book" size={18} />
              Как открыть приложение
            </button>
          }
        />
      </Shell>
    )
  }

  if (error) return <Shell><Failure error={error} onRetry={() => setReload((n) => n + 1)} /></Shell>
  if (!boot) return <Shell><Loading title="Проверяем доступ…" /></Shell>

  if (boot.state === 'not_linked') {
    return (
      <Shell>
        <NotLinkedScreen onRetry={() => setReload((n) => n + 1)} onGuide={() => openGuide('link')} />
      </Shell>
    )
  }

  if (screen.name === 'pick' || !context) {
    return (
      <Shell>
        <ContextPicker onPick={pick} onBack={context ? backToList : null} onGuide={() => openGuide()} />
      </Shell>
    )
  }

  if (screen.name === 'card') {
    return (
      <Shell>
        <TaskCardScreen context={context} taskId={screen.taskId} onBack={backToList} />
      </Shell>
    )
  }

  if (screen.name === 'create') {
    return (
      <Shell>
        <CreateTask
          context={context}
          onCreated={(taskId) => setScreen({ name: 'card', taskId })}
          onCancel={backToList}
        />
      </Shell>
    )
  }

  if (screen.name === 'approvals') {
    return (
      <Shell>
        <ApprovalsScreen onBack={backToList} />
      </Shell>
    )
  }

  if (screen.name === 'summary') {
    return (
      <Shell>
        <SummaryScreen
          context={context}
          onBack={backToList}
          // Число в сводке — это путь к задачам, которые за ним стоят.
          onOpenFilter={(filter) => setScreen({ name: 'list', filter })}
        />
      </Shell>
    )
  }

  if (screen.name === 'timesheet') {
    return (
      <Shell>
        <TimesheetScreen context={context} onBack={backToList} />
      </Shell>
    )
  }

  if (screen.name === 'about') {
    return (
      <Shell>
        <AboutScreen context={context} onBack={backToList} onGuide={() => openGuide()} />
      </Shell>
    )
  }

  return (
    <Shell>
      <TaskList
        // Ключ по фильтру: приход из сводки с «просроченными» должен пересобрать
        // список, а не оставить прежний с новым начальным значением в состоянии.
        key={screen.filter ?? 'all'}
        context={context}
        initialFilter={screen.filter}
        onOpen={(taskId) => setScreen({ name: 'card', taskId })}
        onCreate={() => setScreen({ name: 'create' })}
        onApprovals={() => setScreen({ name: 'approvals' })}
        onSummary={() => setScreen({ name: 'summary' })}
        onTimesheet={() => setScreen({ name: 'timesheet' })}
        onAbout={() => setScreen({ name: 'about' })}
        onGuide={() => openGuide()}
        // Чат, пришедший ссылкой из группы, менять нельзя: кнопка в чате одного
        // клиента не должна открывать задачи другого.
        onSwitchChat={context.pinned ? null : () => setScreen({ name: 'pick' })}
      />
    </Shell>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return <div className="app">{children}</div>
}

/**
 * Кусок сборки с инструкцией не приехал — пропала связь или прошла выкатка,
 * и старого файла на сервере больше нет. Без этой границы отказ `lazy` ронял бы
 * всё приложение в белый экран; с ней — объясняет и даёт повторить.
 */
class GuideBoundary extends Component<
  { onRetry: () => void; onBack: () => void; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false }

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <Empty
        icon="refresh"
        title="Инструкция не загрузилась"
        hint="Похоже, пропала связь. Попробуйте ещё раз, а если не выйдет — закройте и откройте приложение."
        action={
          <div className="actions stack state-actions">
            <button type="button" className="btn" onClick={this.props.onRetry}>
              <Icon name="refresh" size={18} />
              Повторить
            </button>
            <button type="button" className="btn sec" onClick={this.props.onBack}>
              Назад
            </button>
          </div>
        }
      />
    )
  }
}
