/**
 * Прогон аудита в настоящем браузере.
 *
 * jsdom тут не годится: и контраст, и размеры целей, и переполнение существуют
 * только после раскладки, а `color-mix` кто-то должен посчитать. Поэтому —
 * headless-браузер по CDP. Ни одной новой зависимости: WebSocket есть в Node 22+,
 * а браузер уже стоит в системе.
 *
 * Запуск:  node audit/run.mjs [--browser <путь>] [--base http://localhost:5173/miniapp]
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const args = process.argv.slice(2)
const argOf = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback
}

const HERE = dirname(fileURLToPath(import.meta.url))
const CANDIDATES = [
  argOf('--browser', ''),
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
].filter(Boolean)

const BROWSER = CANDIDATES.find((p) => existsSync(p))
if (!BROWSER) {
  console.error('Не нашёл браузер. Передайте путь: --browser <путь к chrome/msedge>')
  process.exit(2)
}

/** Матрица: каждый экран проверяется в обеих темах и на двух ширинах. */
const THEMES = ['light', 'dark']
const WIDTHS = [375, 414]

/*
 * Стенд собирается в ОДИН файл: скрипт и стили инлайнятся в HTML.
 * Внешние подгрузки с file:// Chrome блокирует как межисточниковые, а сервера
 * тут нет — петлевые соединения в этой среде закрыты.
 */
function buildStand() {
  // shell: true — на Windows npx это .cmd, и без оболочки spawnSync его не находит.
  const build = spawnSync(
    'npx',
    ['vite', 'build', '--config', join(HERE, 'vite.config.ts'), '--logLevel', 'warn'],
    { cwd: join(HERE, '..'), stdio: 'inherit', shell: true },
  )
  if (build.status !== 0) throw new Error('Стенд не собрался')

  const dist = join(HERE, 'dist')
  let html = readFileSync(join(dist, 'index.html'), 'utf8')
  html = html.replace(
    /<script type="module"[^>]*src="\.\/([^"]+)"[^>]*><\/script>/,
    (_m, src) => `<script type="module">
${readFileSync(join(dist, src), 'utf8')}
</script>`,
  )
  html = html.replace(
    /<link rel="stylesheet"[^>]*href="\.\/([^"]+)"[^>]*>/,
    (_m, href) => `<style>
${readFileSync(join(dist, href), 'utf8')}
</style>`,
  )
  const page = join(dist, 'stand.html')
  writeFileSync(page, html, 'utf8')
  return pathToFileURL(page).href
}

const STAND = buildStand()

// Под root песочница Chrome не поднимается вовсе («Running as root without
// --no-sandbox is not supported»), а стенд гоняют и в контейнере. Флаг ставится
// только в этом случае: на машине разработчика песочница остаётся включённой.
const ROOT_FLAGS = process.getuid?.() === 0 ? ['--no-sandbox'] : []

const port = 9333
const profile = mkdtempSync(join(tmpdir(), 'b24-audit-'))
const child = spawn(
  BROWSER,
  [
    '--headless=new',
    ...ROOT_FLAGS,
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--force-device-scale-factor=1',
    'about:blank',
  ],
  { stdio: 'ignore' },
)

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function endpoint() {
  for (let i = 0; i < 60; i++) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (res.ok) return (await res.json()).webSocketDebuggerUrl
    } catch {
      /* браузер ещё поднимается */
    }
    await sleep(250)
  }
  throw new Error('Браузер не отдал отладочный порт')
}

/** Минимальный клиент CDP: сообщения по номеру, ответы по нему же. */
function connect(url) {
  const ws = new WebSocket(url)
  const waiting = new Map()
  let seq = 0
  const ready = new Promise((resolve, reject) => {
    ws.addEventListener('open', () => resolve())
    ws.addEventListener('error', reject)
  })
  ws.addEventListener('message', (event) => {
    const msg = JSON.parse(event.data)
    const slot = waiting.get(msg.id)
    if (!slot) return
    waiting.delete(msg.id)
    if (msg.error) slot.reject(new Error(msg.error.message))
    else slot.resolve(msg.result)
  })
  return {
    ready,
    send(method, params, sessionId) {
      const id = ++seq
      return new Promise((resolve, reject) => {
        waiting.set(id, { resolve, reject })
        ws.send(JSON.stringify({ id, method, params: params ?? {}, sessionId }))
      })
    },
    close: () => ws.close(),
  }
}

const findings = []
const totals = { text: 0, tap: 0, frames: 0, runs: 0 }

try {
  const cdp = connect(await endpoint())
  await cdp.ready

  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true })
  await cdp.send('Page.enable', {}, sessionId)
  await cdp.send('Runtime.enable', {}, sessionId)

  for (const theme of THEMES) {
    for (const width of WIDTHS) {
      // Ширина кадра задаётся стенду параметром: колонок несколько, а окно одно.
      await cdp.send(
        'Emulation.setDeviceMetricsOverride',
        { width: 1600, height: 900, deviceScaleFactor: 1, mobile: false },
        sessionId,
      )
      const url = `${STAND}?theme=${theme}&w=${width}`
      await cdp.send('Page.navigate', { url }, sessionId)

      // Ждём, пока стенд домонтирует экраны и придут ответы фикстур.
      let ok = false
      for (let i = 0; i < 60 && !ok; i++) {
        await sleep(250)
        const probe = await cdp.send(
          'Runtime.evaluate',
          {
            // Ждём не только каркас, но и то, что открывается нажатием:
            // без листа и меню половина интерфейса осталась бы непроверенной.
            expression:
              'Boolean(window.__audit) && document.querySelectorAll(".frame .task, .frame .state, .frame .card").length > 6 && document.querySelectorAll(".frame .sheet").length >= 2 && document.querySelectorAll(".frame .choices").length >= 1',
            returnByValue: true,
          },
          sessionId,
        )
        ok = probe.result.value === true
      }
      if (!ok) {
        // Молча падать нельзя: без причины проверку чинят наугад.
        const dump = await cdp.send(
          'Runtime.evaluate',
          {
            expression:
              'JSON.stringify({url: location.href, audit: typeof window.__audit, html: document.body.innerHTML.slice(0, 900)})',
            returnByValue: true,
          },
          sessionId,
        )
        console.error('Стенд не отрисовался:', url)
        console.error(dump.result.value)
        throw new Error('Аудит не запустился')
      }
      await sleep(400)

      const out = await cdp.send(
        'Runtime.evaluate',
        { expression: 'JSON.stringify(window.__audit())', returnByValue: true, awaitPromise: false },
        sessionId,
      )
      // --shot <экран>: снимок одного кадра после проверки. Мерить полезнее,
      // чем смотреть, но увидеть глазами иногда нужно — и лучше тем же стендом,
      // чем пересобирая всё вручную.
      const shot = argOf('--shot', '')
      if (shot && theme === THEMES[0] && width === WIDTHS[0]) {
        const box = await cdp.send(
          'Runtime.evaluate',
          {
            // Сначала в начало страницы: полоса фильтров зовёт scrollIntoView
            // и прокручивает документ вбок, а clip у CDP — в координатах
            // страницы. Без сброса снимок уезжает на соседние кадры.
            expression: `(() => {
              window.scrollTo(0, 0)
              const f = document.querySelector('[data-screen=${JSON.stringify(shot)}]')
              if (!f) return null
              const r = f.getBoundingClientRect()
              return JSON.stringify({
                x: r.x + window.scrollX,
                y: r.y + window.scrollY,
                width: r.width,
                height: Math.min(r.height, 700),
              })
            })()`,
            returnByValue: true,
          },
          sessionId,
        )
        if (box.result.value) {
          const clip = { ...JSON.parse(box.result.value), scale: 2 }
          const png = await cdp.send('Page.captureScreenshot',
                                     { format: 'png', clip }, sessionId)
          const file = join(HERE, 'shot.png')
          writeFileSync(file, Buffer.from(png.data, 'base64'))
          console.log(`снимок: ${file}`)
        } else {
          console.warn(`экрана «${shot}» на стенде нет`)
        }
      }

      const result = JSON.parse(out.result.value)
      totals.runs++
      totals.text += result.checked.text
      totals.tap += result.checked.tap
      totals.frames += result.checked.frames
      for (const f of result.findings) findings.push({ theme, width, ...f })
    }
  }

  cdp.close()
} finally {
  child.kill()
}

const label = `${totals.frames} экранов · ${totals.text} текстов · ${totals.tap} целей · ${totals.runs} прогонов (${THEMES.join('/')} × ${WIDTHS.join('/')}px)`

if (findings.length === 0) {
  console.log(`ЧИСТО. Проверено: ${label}`)
  process.exit(0)
}

console.log(`НАЙДЕНО ${findings.length}. Проверено: ${label}\n`)
const byKind = new Map()
for (const f of findings) {
  const key = `${f.kind}|${f.screen}|${f.detail}|${f.value}`
  const seen = byKind.get(key)
  if (seen) seen.where.push(`${f.theme}/${f.width}`)
  else byKind.set(key, { ...f, where: [`${f.theme}/${f.width}`] })
}
for (const f of byKind.values()) {
  console.log(`[${f.kind}] ${f.screen} — ${f.detail}`)
  console.log(`    ${f.value}   (${f.where.join(', ')})`)
}
process.exit(1)
