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

  /** Кнопка «назад» в шапке клиента. Возвращает функцию отписки. */
  back(handler: (() => void) | null): () => void {
    const button = webApp?.BackButton
    if (!button) return () => undefined
    if (!handler) {
      button.hide()
      return () => undefined
    }
    button.onClick(handler)
    button.show()
    return () => {
      button.offClick(handler)
      button.hide()
    }
  },

  main(options: { text: string; onClick: () => void; busy?: boolean } | null): () => void {
    const button = webApp?.MainButton
    if (!button) return () => undefined
    if (!options) {
      button.hide()
      return () => undefined
    }
    button.setText(options.text)
    button.onClick(options.onClick)
    button.show()
    if (options.busy) {
      button.showProgress(false)
      button.disable()
    } else {
      button.hideProgress()
      button.enable()
    }
    return () => {
      button.offClick(options.onClick)
      button.hide()
    }
  },

  tap(): void {
    webApp?.HapticFeedback?.selectionChanged()
  },

  done(): void {
    webApp?.HapticFeedback?.notificationOccurred('success')
  },

  fail(): void {
    webApp?.HapticFeedback?.notificationOccurred('error')
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
