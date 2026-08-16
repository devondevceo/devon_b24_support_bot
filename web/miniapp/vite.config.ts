import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base обязателен: приложение живёт по /miniapp/, а не в корне домена.
// Без него ассеты запрашиваются от корня и упираются в 404 внутри Telegram,
// где консоль браузера не открыть и причина не видна.
export default defineConfig({
  plugins: [react()],
  base: '/miniapp/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Без хеша в имени Telegram и прокси кэшируют старую сборку.
    assetsDir: 'assets',
    sourcemap: false,
    target: 'es2020',
  },
})
