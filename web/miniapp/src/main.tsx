import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { tg } from './telegram'
import './styles.css'

tg.ready()

/**
 * Схема проставляется атрибутом на <html>, а не выводится из медиазапроса.
 *
 * Тему в Telegram человек выбирает В КЛИЕНТЕ, и она может не совпадать с темой
 * системы: тёмный Telegram на светлом телефоне — обычное дело. `prefers-color-
 * scheme` в таком случае врёт, а по нему выбираются тона danger/warn/ok, каждый
 * из которых померен против своей поверхности (styles.css §токены).
 */
function applyScheme(): void {
  document.documentElement.dataset.scheme = tg.colorScheme()
}

applyScheme()

// Тему клиент может сменить на ходу; CSS-переменные Telegram при этом обновляет
// сам, а нам остаётся перерисоваться, чтобы пересчитались color-mix и прочее.
const root = createRoot(document.getElementById('root')!)
const render = () =>
  root.render(
    <StrictMode>
      <App />
    </StrictMode>,
  )

render()
tg.onThemeChange(() => {
  applyScheme()
  render()
})
