/**
 * Аудит вёрстки приложения Б24: обе темы x две ширины, в настоящем браузере.
 *
 * Меряет то, что существует только после раскладки: горизонтальное
 * переполнение страницы, размеры целей касания при эмуляции тача (375px)
 * и контраст текста по WCAG. Плюс полностраничные скриншоты в каталог вывода.
 *
 * Превью собирает scripts/ui_preview_b24app.py из боевых компонентов ui_kit —
 * аудит меряет ровно ту разметку, что уедет в портал. Обвязка CDP — та же,
 * что в web/miniapp/audit/run.mjs: Node 22+, ни одной зависимости, браузер
 * уже стоит в системе.
 *
 * Запуск:  node scripts/audit_b24app.mjs [--browser <путь>]
 * Выход 0 — все прогоны чистые; 1 — есть находки (печатаются построчно).
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const args = process.argv.slice(2)
const argOf = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback
}

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
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

const outDir = mkdtempSync(join(tmpdir(), 'b24app-audit-'))
const pageFile = join(outDir, 'preview.html')
// shell: true — на Windows python из Microsoft Store это алиас, без оболочки
// spawnSync его иногда не находит.
const built = spawnSync('python', ['scripts/ui_preview_b24app.py', '--out', pageFile], {
  cwd: ROOT, stdio: 'inherit', shell: true,
  env: { ...process.env, PYTHONPATH: 'src' },
})
if (built.status !== 0 || !existsSync(pageFile)) throw new Error('Превью не собралось')
const PAGE = pathToFileURL(pageFile).href

const port = 9377
const profile = mkdtempSync(join(tmpdir(), 'b24app-audit-profile-'))
// Под root песочница Chrome не поднимается вовсе («Running as root without
// --no-sandbox is not supported»), а стенд гоняют и в контейнере. Флаг ставится
// только в этом случае: на машине разработчика песочница остаётся включённой.
const ROOT_FLAGS = process.getuid?.() === 0 ? ['--no-sandbox'] : []

const child = spawn(BROWSER, [
  '--headless=new', ...ROOT_FLAGS, `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--force-device-scale-factor=1', 'about:blank',
], { stdio: 'ignore' })

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

/*
 * Проба выполняется в странице. Три группы находок:
 *   OVERFLOW — элемент шире вьюпорта (лента вкладок скроллится сама и не в счёт);
 *   TAP      — цель меньше 44px при (pointer:coarse);
 *   CONTRAST — текст ниже нормы AA против настоящего фона под ним.
 */
const PROBE = String.raw`(() => {
  const out = []
  const vw = document.documentElement.clientWidth
  if (document.documentElement.scrollWidth > vw + 1)
    out.push('PAGE-HSCROLL scrollW=' + document.documentElement.scrollWidth + ' vw=' + vw)
  const inScroller = (el) => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      const ox = getComputedStyle(n).overflowX
      if (ox === 'auto' || ox === 'scroll') return true
    }
    return false
  }
  const seen = new Set()
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect()
    if (!r.width && !r.height) continue
    if (inScroller(el)) continue
    if (r.right > vw + 1 || r.left < -1) {
      const key = el.tagName + '.' + (typeof el.className === 'string' ? el.className : '')
      if (!seen.has(key)) {
        seen.add(key)
        out.push('OVERFLOW ' + key + ' right=' + Math.round(r.right))
      }
    }
  }
  if (matchMedia('(pointer:coarse)').matches) {
    for (const el of document.querySelectorAll('a.btn,button,summary,input,select')) {
      const r = el.getBoundingClientRect()
      if (!r.width || !r.height) continue
      if (r.height < 43.5 || r.width < 43.5) {
        const label = (el.textContent || el.name || el.type || '').trim().slice(0, 30)
        out.push('TAP ' + el.tagName + ' ' + Math.round(r.width) + 'x' +
                 Math.round(r.height) + ' «' + label + '»')
      }
    }
  }
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 }
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])
  }
  const parse = (s) => {
    const m = s.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/)
    return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null
  }
  const bgOf = (el) => {
    for (let n = el; n; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor)
      if (c && c[3] > 0.99) return c
    }
    return [255, 255, 255, 1]
  }
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
  const checked = new Set()
  while (walker.nextNode()) {
    const el = walker.currentNode.parentElement
    if (!el || checked.has(el)) continue
    checked.add(el)
    if (!walker.currentNode.textContent.trim()) continue
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) continue
    const st = getComputedStyle(el)
    const fg = parse(st.color)
    if (!fg || fg[3] < 1) continue
    const bg = bgOf(el)
    const [l1, l2] = [lum(fg), lum(bg)].sort((a, b) => b - a)
    const ratio = (l1 + 0.05) / (l2 + 0.05)
    const size = parseFloat(st.fontSize)
    const bold = parseInt(st.fontWeight, 10) >= 700
    const need = size >= 18.66 || (size >= 14 && bold) ? 3 : 4.5
    if (ratio < need - 0.02) {
      out.push('CONTRAST ' + el.tagName + '.' + el.className + ' ' +
               ratio.toFixed(2) + '<' + need +
               ' «' + walker.currentNode.textContent.trim().slice(0, 25) + '»')
    }
  }
  return out.join('\n') || 'CLEAN'
})()`

let dirty = 0
try {
  const cdp = connect(await endpoint())
  await cdp.ready
  const { targetId } = await cdp.send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true })
  await cdp.send('Page.enable', {}, sessionId)
  await cdp.send('Runtime.enable', {}, sessionId)

  for (const theme of ['light', 'dark']) {
    for (const width of [375, 1100]) {
      const mobile = width < 700
      await cdp.send('Emulation.setDeviceMetricsOverride',
        { width, height: mobile ? 2600 : 1500, deviceScaleFactor: 1, mobile }, sessionId)
      await cdp.send('Emulation.setTouchEmulationEnabled',
        { enabled: mobile, maxTouchPoints: mobile ? 5 : 1 }, sessionId)
      await cdp.send('Emulation.setEmulatedMedia',
        { features: [{ name: 'prefers-color-scheme', value: theme }] }, sessionId)
      await cdp.send('Page.navigate', { url: PAGE }, sessionId)
      await sleep(900)

      const res = await cdp.send('Runtime.evaluate',
        { expression: PROBE, returnByValue: true }, sessionId)
      const report = res.result.value ?? String(res.result.description)
      console.log(`--- ${theme} ${width} ---`)
      console.log(report)
      if (report !== 'CLEAN') dirty++

      const shot = await cdp.send('Page.captureScreenshot',
        { format: 'png', captureBeyondViewport: true }, sessionId)
      writeFileSync(join(outDir, `audit_${theme}_${width}.png`),
        Buffer.from(shot.data, 'base64'))
    }
  }
  console.log(dirty ? `ГРЯЗНО: прогонов с находками: ${dirty}` : 'ЧИСТО: все 4 прогона')
  console.log(`Скриншоты: ${outDir}`)
} finally {
  child.kill()
}
process.exit(dirty ? 1 : 0)
