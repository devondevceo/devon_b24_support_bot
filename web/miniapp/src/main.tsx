import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { tg } from './telegram'
import './styles.css'

tg.ready()

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
tg.onThemeChange(render)
