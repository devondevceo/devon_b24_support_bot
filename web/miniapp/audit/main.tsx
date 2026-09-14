/**
 * Стенд аудита: все экраны рядом, на одной странице, в одной теме.
 *
 * Так проверка «26 экранов × 2 ширины × 2 темы» превращается в четыре загрузки
 * страницы вместо сотни кликов, и ни один экран нельзя забыть — забытый просто
 * не появится в колонке.
 */
import { createRoot } from 'react-dom/client'
import { Empty, Failure, Loading } from '../src/components/States'
import { ApiError } from '../src/api'
import { AboutScreen } from '../src/screens/AboutScreen'
import { ApprovalsScreen } from '../src/screens/ApprovalsScreen'
import { ContextPicker } from '../src/screens/ContextPicker'
import { CreateTask } from '../src/screens/CreateTask'
import { GuideScreen } from '../src/screens/GuideScreen'
import { NotLinkedScreen } from '../src/screens/NotLinkedScreen'
import { SummaryScreen } from '../src/screens/SummaryScreen'
import { TaskCardScreen } from '../src/screens/TaskCardScreen'
import { TaskList } from '../src/screens/TaskList'
import { TimesheetScreen } from '../src/screens/TimesheetScreen'
import type { Context } from '../src/types'
import { PROJECT, stubFetch } from './fixtures'
import '../src/styles.css'
import './audit'

stubFetch()

const DARK = new URLSearchParams(location.search).get('theme') === 'dark'
document.documentElement.dataset.scheme = DARK ? 'dark' : 'light'
document.body.style.background = 'var(--page)'

const width = new URLSearchParams(location.search).get('w')
if (width) document.documentElement.style.setProperty('--frame-width', `${width}px`)

const CONTEXT: Context = {
  chat_ref: 11,
  title: 'Линия Жизни — поддержка',
  pinned: false,
  task_id: null,
  projects: [PROJECT],
}

const noop = () => undefined

/*
 * `click` — нажать кнопку с этой подписью, `type` — напечатать это в поле
 * поиска. И то и другое делается так, как сделал бы человек: по видимому.
 */
const SCREENS: { name: string; node: React.ReactNode; click?: string; type?: string }[] = [
  {
    name: 'Список задач',
    node: (
      <TaskList
        context={CONTEXT}
        onOpen={noop}
        onCreate={noop}
        onApprovals={noop}
        onSummary={noop}
        onTimesheet={noop}
        onAbout={noop}
        onGuide={noop}
        onSwitchChat={noop}
      />
    ),
  },
  { name: 'Карточка задачи', node: <TaskCardScreen context={CONTEXT} taskId={233} onBack={noop} /> },
  { name: 'Новая задача', node: <CreateTask context={CONTEXT} onCreated={noop} onCancel={noop} /> },
  { name: 'На подтверждении', node: <ApprovalsScreen onBack={noop} /> },
  { name: 'Выбор чата', node: <ContextPicker onPick={noop} onBack={noop} onGuide={noop} /> },
  { name: 'Сводка', node: <SummaryScreen context={CONTEXT} onBack={noop} onOpenFilter={noop} /> },
  { name: 'Трудозатраты', node: <TimesheetScreen context={CONTEXT} onBack={noop} /> },
  { name: 'О приложении', node: <AboutScreen context={CONTEXT} onBack={noop} onGuide={noop} /> },
  { name: 'Не привязан', node: <NotLinkedScreen onRetry={noop} onGuide={noop} /> },
  { name: 'Инструкция', node: <GuideScreen onBack={noop} onGo={noop} /> },
  // Статья открывается из оглавления нажатием на её заголовок — тем же путём,
  // что у человека. «Списать время» выбрана за то, что в ней есть все виды
  // блоков: шаги, термины, предупреждение, кнопки бота и приложения.
  {
    name: 'Статья инструкции',
    node: <GuideScreen onBack={noop} onGo={noop} />,
    click: 'Списать время',
  },
  {
    name: 'Поиск по инструкции',
    node: <GuideScreen onBack={noop} onGo={noop} />,
    type: 'срок',
  },
  {
    name: 'Поиск без результата',
    node: <GuideScreen onBack={noop} onGo={noop} />,
    type: 'йцукен',
  },
  // Экраны, которые открываются нажатием: их доводит до нужного состояния
  // `drive()` ниже. Мерить только то, что видно сразу, значит не мерить
  // половину интерфейса — а ломается она ровно так же.
  {
    name: 'Опросник',
    node: <CreateTask context={CONTEXT} onCreated={noop} onCancel={noop} />,
    click: 'По вопросам',
  },
  {
    name: 'Правка задачи',
    node: <TaskCardScreen context={CONTEXT} taskId={233} onBack={noop} />,
    click: 'Изменить',
  },
  {
    name: 'Списание времени',
    node: <TaskCardScreen context={CONTEXT} taskId={233} onBack={noop} />,
    click: 'Списать время',
  },
  {
    name: 'Меню разделов',
    node: (
      <TaskList
        context={CONTEXT}
        onOpen={noop}
        onCreate={noop}
        onApprovals={noop}
        onSummary={noop}
        onTimesheet={noop}
        onAbout={noop}
        onGuide={noop}
        onSwitchChat={noop}
      />
    ),
    click: 'Разделы',
  },
  { name: 'Загрузка', node: <Loading title="Открываем задачу…" /> },
  {
    name: 'Пусто',
    node: (
      <Empty
        title="Задач нет"
        hint="В проектах этого чата нет открытых задач."
        action={<button type="button" className="btn">Создать задачу</button>}
      />
    ),
  },
  {
    name: 'Ошибка',
    node: (
      <Failure
        error={new ApiError(403, 'needs_reauth', 'Доступ к Битрикс24 истёк. Отправьте боту /link — он даст ссылку на вход в портал, и доступ обновится.')}
        onRetry={noop}
      />
    ),
  },
]

const gallery = document.getElementById('gallery')!
for (const screen of SCREENS) {
  const frame = document.createElement('div')
  frame.className = 'frame'
  frame.dataset.screen = screen.name

  const cap = document.createElement('div')
  cap.className = 'cap'
  cap.textContent = screen.name
  frame.appendChild(cap)

  const host = document.createElement('div')
  host.className = 'app'
  frame.appendChild(host)
  gallery.appendChild(frame)

  const step = screen.click ?? (screen.type ? `ввод «${screen.type}»` : undefined)
  if (step) frame.dataset.undriven = step
  createRoot(host).render(screen.node)
}

/*
 * Экран доводится ОДИН раз. Проходов два (фикстуры отвечают не мгновенно), и
 * второй раньше нажимал кнопку снова — безвредно, пока она оставалась на месте
 * (лист правки открывается поверх карточки). Статья инструкции заменяет собой
 * оглавление, и повторный поиск той же подписи промахнулся бы — по уже
 * доведённому экрану.
 */
const driven = new Set<string>()

/** Печать как у человека: React слушает событие input, а не свойство value. */
function typeInto(input: HTMLInputElement, text: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
  setter?.call(input, text)
  input.dispatchEvent(new Event('input', { bubbles: true }))
}

/**
 * Доводит экраны до состояния, которое открывается нажатием или вводом: лист
 * правки, меню разделов, режим опросника, статья и поиск инструкции. Ищем по
 * видимой подписи, а не по классу: подпись — это то, что нажимает человек, и
 * промах здесь означает, что кнопку не найти и ему тоже.
 */
function drive(): void {
  for (const screen of SCREENS) {
    if ((!screen.click && !screen.type) || driven.has(screen.name)) continue
    const frame = document.querySelector<HTMLElement>(`[data-screen="${screen.name}"]`)

    let done = false
    if (screen.click) {
      const button = [...(frame?.querySelectorAll('button') ?? [])].find((b) =>
        (b.textContent ?? '').includes(screen.click!) ||
        (b.getAttribute('aria-label') ?? '').includes(screen.click!),
      )
      if (button) {
        button.click()
        done = true
      }
    } else if (screen.type) {
      const input = frame?.querySelector<HTMLInputElement>('input[type="search"]')
      if (input) {
        typeInto(input, screen.type)
        done = true
      }
    }

    // Отметка снимается только удавшимся действием. Без неё промах был бы
    // виден лишь предупреждением в консоли страницы, которую раннер не
    // читает: стенд отчитался бы «чисто», померив экран, так и не открытый.
    if (done) {
      driven.add(screen.name)
      frame?.removeAttribute('data-undriven')
    }
  }
}

// Экраны ждут ответов фикстур: кнопка «Изменить» появляется только после того,
// как приехала карточка задачи.
window.setTimeout(drive, 400)
window.setTimeout(drive, 1200)
