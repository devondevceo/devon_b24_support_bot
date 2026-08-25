/**
 * Обёртка над window.Telegram.WebApp.
 *
 * Ни один экран не обращается к SDK напрямую: в браузере (когда мини-апп открыт
 * не из Telegram) объекта нет вовсе, и без единой точки проверка «а есть ли он»
 * расползлась бы по всем компонентам.
 */

type BackButton = {
  show: () => void
  hide: () => void
  onClick: (cb: () => void) => void
  offClick: (cb: () => void) => void
}

type MainButton = {
  setText: (text: string) => void
  show: () => void
  hide: () => void
  enable: () => void
  disable: () => void
  showProgress: (leaveActive?: boolean) => void
  hideProgress: () => void
  onClick: (cb: () => void) => void
  offClick: (cb: () => void) => void
}

type HapticFeedback = {
  impactOccurred: (style: 'light' | 'medium' | 'heavy') => void
  notificationOccurred: (type: 'error' | 'success' | 'warning') => void
  selectionChanged: () => void
}

type WebApp = {
  initData: string
  initDataUnsafe: { start_param?: string; user?: { id: number } }
  version: string
  colorScheme: 'light' | 'dark'
  themeParams: Record<string, string>
  isExpanded: boolean
  ready: () => void
  expand: () => void
  close: () => void
  openLink: (url: string, options?: { try_instant_view?: boolean }) => void
  showAlert: (message: string, cb?: () => void) => void
  showConfirm: (message: string, cb: (ok: boolean) => void) => void
  onEvent: (event: string, cb: () => void) => void
  offEvent: (event: string, cb: () => void) => void
  disableVerticalSwipes?: () => void
  BackButton: BackButton
  MainButton: MainButton
  HapticFeedback: HapticFeedback
}

declare global {
  interface Window {
    Telegram?: { WebApp?: WebApp }
  }
}

const webApp = window.Telegram?.WebApp

/*
 * Нативные кнопки клиента — стек, а не одна ячейка.
 *
 * `BackButton.onClick` в SDK ДОБАВЛЯЕТ обработчик, а не заменяет. Пока на
 * экране жил ровно один компонент, это было незаметно; с листом правки поверх
 * карточки одно нажатие «назад» закрыло бы лист И ушло к списку разом.
 * Поэтому к SDK всегда подключён только верх стека, а размонтирование
 * возвращает кнопку предыдущему владельцу.
 */
type BackEntry = { fn: (() => void) | null }
type MainOptions = { text: string; onClick: () => void; busy?: boolean; enabled?: boolean }
type MainEntry = { opts: MainOptions | null }

const backStack: BackEntry[] = []
const mainStack: MainEntry[] = []

let backAttached: (() => void) | null = null
let mainAttached: (() => void) | null = null

function syncBack(): void {
  const button = webApp?.BackButton
  if (!button) return
  const top = backStack.length ? backStack[backStack.length - 1]!.fn : null
  if (backAttached && backAttached !== top) {
    button.offClick(backAttached)
    backAttached = null
  }
  if (top) {
    if (backAttached !== top) {
      button.onClick(top)
      backAttached = top
    }
    button.show()
  } else {
    button.hide()
  }
}

function syncMain(): void {
  const button = webApp?.MainButton
  if (!button) return
  const top = mainStack.length ? mainStack[mainStack.length - 1]!.opts : null
  if (mainAttached && mainAttached !== top?.onClick) {
    button.offClick(mainAttached)
    mainAttached = null
  }
  if (!top) {
    button.hide()
    return
  }
  button.setText(top.text)
  if (mainAttached !== top.onClick) {
    button.onClick(top.onClick)
    mainAttached = top.onClick
  }
  button.show()
  if (top.busy) {
    button.showProgress(false)
    button.disable()
  } else {
    button.hideProgress()
    if (top.enabled === false) button.disable()
    else button.enable()
  }
}

export const tg = {
  available: Boolean(webApp?.initData),

  initData(): string {
    return webApp?.initData ?? ''
  },

  ready(): void {
    webApp?.ready()
    webApp?.expand()
    // Свайп вниз внутри списка задач закрывал приложение вместо прокрутки.
    // Метод появился в 7.7, поэтому вызывается через проверку.
    webApp?.disableVerticalSwipes?.()
  },

  colorScheme(): 'light' | 'dark' {
    return webApp?.colorScheme ?? 'light'
  },

  onThemeChange(cb: () => void): () => void {
    webApp?.onEvent('themeChanged', cb)
    return () => webApp?.offEvent('themeChanged', cb)
  },

  /**
   * Кнопка «назад» в шапке клиента. Возвращает функцию отписки.
   * `null` — «на этом экране кнопки нет»; это тоже позиция в стеке, иначе
   * экран без кнопки унаследовал бы чужую.
   */
  back(handler: (() => void) | null): () => void {
    const entry: BackEntry = { fn: handler }
    backStack.push(entry)
    syncBack()
    return () => {
      const i = backStack.indexOf(entry)
      if (i >= 0) backStack.splice(i, 1)
      syncBack()
    }
  },

  /** Нижняя кнопка клиента — главное действие экрана. Тот же стек. */
  main(options: MainOptions | null): () => void {
    const entry: MainEntry = { opts: options }
    mainStack.push(entry)
    syncMain()
    return () => {
      const i = mainStack.indexOf(entry)
      if (i >= 0) mainStack.splice(i, 1)
      syncMain()
    }
  },

  /** Выбор изменился: фильтр, сегмент, пункт списка. */
  tap(): void {
    webApp?.HapticFeedback?.selectionChanged()
  },

  /** Нажали на что-то заметное: открыли задачу, открыли лист. */
  press(): void {
    webApp?.HapticFeedback?.impactOccurred('light')
  },

  done(): void {
    webApp?.HapticFeedback?.notificationOccurred('success')
  },

  fail(): void {
    webApp?.HapticFeedback?.notificationOccurred('error')
  },

  /** Получилось, но не полностью — например, портал принял не все поля. */
  warn(): void {
    webApp?.HapticFeedback?.notificationOccurred('warning')
  },

  alert(message: string): void {
    if (webApp) webApp.showAlert(message)
    else window.alert(message)
  },

  confirm(message: string, cb: (ok: boolean) => void): void {
    if (webApp) webApp.showConfirm(message, cb)
    else cb(window.confirm(message))
  },

  openLink(url: string): void {
    if (webApp) webApp.openLink(url)
    else window.open(url, '_blank', 'noopener')
  },

  startParam(): string {
    return webApp?.initDataUnsafe?.start_param ?? ''
  },
}
