import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * Сборка стенда аудита в статику.
 *
 * Отдельным конфигом и отдельным корнем: боевая сборка (../vite.config.ts)
 * знать про стенд не должна, иначе он однажды уедет в образ. Проверяется тем,
 * что `npm run build` кладёт в dist/ только index.html и его ассеты.
 *
 * Собирается ради `file://`: в этой среде петлевые HTTP-соединения закрыты,
 * и до dev-сервера браузер не достучится. Готовая страница ничего не грузит.
 */
export default defineConfig({
  root: __dirname,
  base: './',
  plugins: [react()],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Один файл на всё: инлайн в HTML снимает вопрос CORS для file://.
    cssCodeSplit: false,
    modulePreload: { polyfill: false },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
})
