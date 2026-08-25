/**
 * Навигация мини-аппа. Роутера нет намеренно: экранов четыре, а адресная строка
 * внутри Telegram не видна и не сохраняется — маршруты некуда и незачем писать.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from './api'
import { Empty, Failure, Loading } from './components/States'
import { AboutScreen } from './screens/AboutScreen'
import { ApprovalsScreen } from './screens/ApprovalsScreen'
import { ContextPicker } from './screens/ContextPicker'
import { CreateTask } from './screens/CreateTask'
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

export function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [context, setContext] = useState<Context | null>(null)
  const [screen, setScreen] = useState<Screen>({ name: 'list' })
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!tg.available) {
      setError(
        new ApiError(401, 'unauthenticated', 'Эта страница работает только внутри Telegram.'),
      )
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

  if (error) return <Shell><Failure error={error} onRetry={() => setReload((n) => n + 1)} /></Shell>
  if (!boot) return <Shell><Loading title="Проверяем доступ…" /></Shell>

  if (boot.state === 'not_linked') {
    return (
      <Shell>
        <Empty
          icon="link"
          title="Telegram не привязан к Битрикс24"
          hint="Откройте в Битрикс24 приложение «Поддержка в Telegram» и нажмите «Привязать Telegram». Это одна кнопка и полминуты."
          action={
            <button type="button" className="btn" onClick={() => setReload((n) => n + 1)}>
              <Icon name="refresh" size={18} />
              Я привязал, проверить
            </button>
          }
        />
      </Shell>
    )
  }

  if (screen.name === 'pick' || !context) {
    return (
      <Shell>
        <ContextPicker onPick={pick} onBack={context ? backToList : null} />
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
        <AboutScreen context={context} onBack={backToList} />
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
