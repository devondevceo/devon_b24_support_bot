/**
 * Измерения, а не взгляд на глаз.
 *
 * Три проверки, которые нельзя сделать зрением и которые ломались в этом
 * проекте раньше: контраст в обеих темах, размер целей касания, переполнение
 * по горизонтали. Результат — `window.__audit()`.
 */

type Finding = { kind: string; screen: string; detail: string; value: string }

/** Порог AA. Крупный текст — 3:1, обычный — 4.5:1 (WCAG 1.4.3). */
const AA_NORMAL = 4.5
const AA_LARGE = 3

/**
 * Единственное послабление — пара «цвет кнопки клиента + цвет текста на ней».
 *
 * Оба значения приходят из темы Telegram, и переопределить их — значит сделать
 * наши главные кнопки другого цвета, чем родная кнопка мессенджера под ними.
 * Собственные пары Telegram дают 3.7–4.1:1 (белое на #2481cc — 4.13, на
 * #5288c1 — 3.71), то есть чуть ниже 4.5 и заметно выше 3.0. Узнаваемость
 * платформы здесь весит больше, чем эта разница; порог 3:1 держим и проверяем.
 *
 * На вторичный текст послабление НЕ распространяется: он у нас выведен
 * из --text (токен --text-2), а не взят из палитры клиента.
 */
const PLATFORM_PAIR = 3

const TAP_MIN = 44

/**
 * Разбор цвета во всех формах, которые отдаёт Chrome.
 *
 * `color-mix()` он сериализует как `color(srgb 0.94 0.94 0.94)` — доли, а не
 * 0..255. Разбор «просто вытащить числа» принимал 0.94 за 0.94 из 255, то есть
 * почти чёрный, и вся проверка врала: подложки плашек выходили «чёрными»,
 * а контраст на них — 1.01. Правило общее: формат ответа браузера читается
 * целиком, а не по первому совпадению регулярки.
 */
function parseColor(value: string): [number, number, number, number] {
  const v = value.trim()
  if (v === 'transparent') return [0, 0, 0, 0]

  if (v.startsWith('#')) {
    const hex = v.slice(1)
    const wide = hex.length >= 6
    const at = (i: number) =>
      wide ? parseInt(hex.slice(i * 2, i * 2 + 2), 16) : parseInt(hex[i]! + hex[i]!, 16)
    const alpha = hex.length === 8 ? at(3) / 255 : hex.length === 4 ? at(3) / 255 : 1
    return [at(0), at(1), at(2), alpha]
  }

  const nums = (v.match(/[-\d.]+(?:e[-+]?\d+)?/gi) ?? []).map(Number)
  if (nums.length < 3) return [0, 0, 0, 0]

  // color(srgb …) и color(display-p3 …) — доли единицы, остальные формы — 0..255.
  const unit = v.startsWith('color(')
  const scale = unit ? 255 : 1
  return [
    nums[0]! * scale,
    nums[1]! * scale,
    nums[2]! * scale,
    nums[3] === undefined ? 1 : nums[3]!,
  ]
}

function luminance([r, g, b]: [number, number, number, number]): number {
  const f = (c: number) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function ratio(fg: [number, number, number, number], bg: [number, number, number, number]): number {
  const a = luminance(fg)
  const b = luminance(bg)
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)
}

/** Складывает полупрозрачный слой с тем, что под ним. */
function over(
  top: [number, number, number, number],
  bottom: [number, number, number, number],
): [number, number, number, number] {
  const a = top[3]
  return [
    top[0] * a + bottom[0] * (1 - a),
    top[1] * a + bottom[1] * (1 - a),
    top[2] * a + bottom[2] * (1 - a),
    1,
  ]
}

/** Фон под элементом: вверх по предкам, со сложением полупрозрачных слоёв. */
function backdrop(el: Element): [number, number, number, number] {
  const layers: [number, number, number, number][] = []
  let node: Element | null = el
  while (node) {
    const bg = parseColor(getComputedStyle(node).backgroundColor)
    if (bg[3] > 0) {
      layers.push(bg)
      if (bg[3] === 1) break
    }
    node = node.parentElement
  }
  let result: [number, number, number, number] = [255, 255, 255, 1]
  for (let i = layers.length - 1; i >= 0; i--) result = over(layers[i]!, result)
  return result
}

/** Лежит ли элемент внутри собственного горизонтального скроллера. */
function insideScroller(el: Element, stop: Element): boolean {
  let node = el.parentElement
  while (node && node !== stop) {
    const overflow = getComputedStyle(node).overflowX
    if (overflow === 'auto' || overflow === 'scroll') return true
    node = node.parentElement
  }
  return false
}

function screenOf(el: Element): string {
  return (el.closest('[data-screen]') as HTMLElement | null)?.dataset.screen ?? '—'
}

function hasOwnText(el: Element): boolean {
  for (const node of el.childNodes) {
    if (node.nodeType === Node.TEXT_NODE && (node.textContent ?? '').trim().length > 0) return true
  }
  return false
}

function audit(): { findings: Finding[]; checked: Record<string, number> } {
  const findings: Finding[] = []
  const counts = { text: 0, tap: 0, frames: 0 }

  /*
   * Токен читается через пробный элемент, а не getPropertyValue: последний
   * отдаёт значение как ЗАПИСАНО (`var(--tg-theme-...)` или hex), а нам нужен
   * тот же формат, в котором браузер вернёт `color` у настоящего текста.
   */
  const probe = document.createElement('span')
  probe.style.position = 'fixed'
  probe.style.opacity = '0'
  document.body.appendChild(probe)
  const resolve = (token: string): string => {
    probe.style.color = `var(${token})`
    return parseColor(getComputedStyle(probe).color).slice(0, 3).join(',')
  }
  const accent = resolve('--accent')
  const onAccent = resolve('--on-accent')
  probe.remove()

  // ------------------------------------------------------------- контраст
  for (const el of Array.from(document.querySelectorAll<HTMLElement>('.app *'))) {
    if (!hasOwnText(el)) continue
    const style = getComputedStyle(el)
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue
    if (el.closest('.skeleton') || el.getAttribute('aria-hidden') === 'true') continue

    const rect = el.getBoundingClientRect()
    if (rect.width === 0 || rect.height === 0) continue

    counts.text++
    const fg = parseColor(style.color)
    const bg = backdrop(el)
    const composed = fg[3] < 1 ? over(fg, bg) : fg
    const got = ratio(composed, bg)

    const size = parseFloat(style.fontSize)
    const weight = Number(style.fontWeight) || 400
    const large = size >= 24 || (size >= 18.66 && weight >= 700)
    const platform =
      fg.slice(0, 3).join(',') === onAccent && bg.slice(0, 3).join(',') === accent
    const need = platform ? PLATFORM_PAIR : large ? AA_LARGE : AA_NORMAL

    if (got < need) {
      findings.push({
        kind: platform ? 'contrast-platform' : 'contrast',
        screen: screenOf(el),
        detail: `${el.tagName.toLowerCase()}.${el.className || '-'} «${(el.textContent ?? '').trim().slice(0, 40)}» ${size}px/${weight}`,
        value: `${got.toFixed(2)} < ${need}`,
      })
    }
  }

  // -------------------------------------------------------- цели касания
  const TAPPABLE = 'button, a[href], select, textarea, summary, input:not([type=hidden])'
  for (const el of Array.from(document.querySelectorAll<HTMLElement>(`.app ${TAPPABLE}`))) {
    const style = getComputedStyle(el)
    if (style.display === 'none' || style.visibility === 'hidden') continue
    // aria-hidden + tabindex=-1 значит «этого элемента для человека нет»:
    // так спрятано поле выбора файлов, за которое нажимает кнопка рядом.
    if (el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0) continue
    const rect = el.getBoundingClientRect()
    if (rect.width === 0 || rect.height === 0) continue
    counts.tap++
    if (rect.width + 0.5 < TAP_MIN || rect.height + 0.5 < TAP_MIN) {
      findings.push({
        kind: 'tap-target',
        screen: screenOf(el),
        detail: `${el.tagName.toLowerCase()}.${el.className || '-'} «${(el.textContent ?? el.getAttribute('aria-label') ?? '').trim().slice(0, 30)}»`,
        value: `${Math.round(rect.width)}×${Math.round(rect.height)} < ${TAP_MIN}`,
      })
    }
  }

  // ----------------------------------------------------- переполнение
  for (const frame of Array.from(document.querySelectorAll<HTMLElement>('.frame'))) {
    counts.frames++
    const host = frame.querySelector<HTMLElement>('.app')
    if (!host) continue
    const limit = host.getBoundingClientRect().right + 1
    for (const el of Array.from(host.querySelectorAll<HTMLElement>('*'))) {
      const style = getComputedStyle(el)
      if (style.position === 'fixed' || style.display === 'none') continue
      const rect = el.getBoundingClientRect()
      if (rect.width === 0) continue
      // Содержимое НАРОЧНОГО горизонтального скроллера (полоса фильтров) за
      // край выходит по замыслу: у него своя прокрутка и маска у краёв.
      // Переполнением считается только то, что уносит за край всю страницу.
      if (insideScroller(el, host)) continue
      if (rect.right > limit) {
        findings.push({
          kind: 'overflow',
          screen: screenOf(el),
          detail: `${el.tagName.toLowerCase()}.${el.className || '-'}`,
          value: `${Math.round(rect.right - limit)}px за правый край`,
        })
      }
    }
  }

  return { findings, checked: counts }
}

declare global {
  interface Window {
    __audit: typeof audit
  }
}

window.__audit = audit
