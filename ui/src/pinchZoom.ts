// Trackpad pinch magnifies the whole app, like a browser's own pinch zoom on a touchscreen or
// a Mac: the layout stays put, the page is enlarged around the fingers, and the browser's zoom
// level stays at 100%.
//
// Chromium on Windows sends a trackpad pinch as wheel events with ctrlKey set, and by default
// turns them into a change of the browser zoom. Cancelling them (a window listener must say
// `passive: false`, since Chromium makes window wheel listeners passive by default) and
// scaling #root ourselves gives the magnifier instead. Ctrl+mouse-wheel arrives as the same
// event, but with the Ctrl key really held down - a pinch sets ctrlKey without pressing it -
// so that one is left to zoom the browser as normal, as are Ctrl+= / Ctrl+- and Edge's menu.
//
// Everything is done on the DOM directly, once per frame - no React state, so a pinch never
// re-renders the app.

const MAX_SCALE = 4
// Each event zooms by exp(-deltaY * rate). A pinch sends small deltas (a few px); a mouse
// wheel notch is ~100px, capped so one notch is one ~22% step.
const RATE = 0.008
const MAX_EVENT_DELTA = 25

let scale = 1
let tx = 0
let ty = 0
let frame = 0
// whether the Ctrl key is physically down, to tell Ctrl+mouse-wheel from a pinch
let ctrlHeld = false

/** A wheel event's delta in pixels, whatever unit the browser reported it in. */
function pixels(delta: number, mode: number) {
  return delta * (mode === WheelEvent.DOM_DELTA_LINE ? 16 : mode === WheelEvent.DOM_DELTA_PAGE ? 800 : 1)
}

/** Keep the magnified app covering the window: no gap at any edge. */
function clamp() {
  tx = Math.min(0, Math.max(window.innerWidth * (1 - scale), tx))
  ty = Math.min(0, Math.max(window.innerHeight * (1 - scale), ty))
}

function apply() {
  frame = 0
  const root = document.getElementById('root')
  if (!root) return
  if (scale === 1) {
    // back to plain layout: a transform left on #root would blur text and become the
    // containing block of every position: fixed element
    root.style.transform = ''
    root.style.transformOrigin = ''
    document.documentElement.style.overflow = ''
  } else {
    root.style.transformOrigin = '0 0'
    root.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`
    // the enlarged #root would otherwise give the page itself scrollbars
    document.documentElement.style.overflow = 'hidden'
  }
}

function schedule() {
  if (!frame) frame = requestAnimationFrame(apply)
}

/** Can `el` scroll any further in the direction of (dx, dy)? */
function canScroll(el: Element, dx: number, dy: number) {
  const style = getComputedStyle(el)
  const scrollsY = /(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight
  const scrollsX = /(auto|scroll)/.test(style.overflowX) && el.scrollWidth > el.clientWidth
  if (dy && scrollsY) {
    if (dy < 0 ? el.scrollTop > 0 : el.scrollTop + el.clientHeight < el.scrollHeight - 1) return true
  }
  if (dx && scrollsX) {
    if (dx < 0 ? el.scrollLeft > 0 : el.scrollLeft + el.clientWidth < el.scrollWidth - 1) return true
  }
  return false
}

function onWheel(e: WheelEvent) {
  if (e.ctrlKey && !ctrlHeld) {
    // An uncancelable event can't be kept from zooming the browser; leave it alone.
    if (!e.cancelable) return
    e.preventDefault()
    const delta = Math.max(-MAX_EVENT_DELTA, Math.min(MAX_EVENT_DELTA, pixels(e.deltaY, e.deltaMode)))
    let next = Math.min(MAX_SCALE, Math.max(1, scale * Math.exp(-delta * RATE)))
    if (next < 1.01) next = 1
    if (next === scale) return
    // keep the point under the fingers where it is
    tx = e.clientX - ((e.clientX - tx) * next) / scale
    ty = e.clientY - ((e.clientY - ty) * next) / scale
    scale = next
    clamp()
    schedule()
    return
  }
  if (e.ctrlKey) return // Ctrl+mouse-wheel: the browser's own zoom
  // Two-finger scrolling while magnified: what's under the pointer scrolls as usual, and once
  // it can't scroll any further the magnified view pans - as the browser's own pinch zoom does.
  if (scale === 1 || !e.cancelable) return
  const dx = pixels(e.deltaX, e.deltaMode)
  const dy = pixels(e.deltaY, e.deltaMode)
  for (let el = e.target instanceof Element ? e.target : null; el; el = el.parentElement) {
    if (canScroll(el, dx, dy)) return
  }
  e.preventDefault()
  tx -= dx
  ty -= dy
  clamp()
  schedule()
}

function onResize() {
  if (scale === 1) return
  clamp()
  schedule()
}

/** Converts a point on screen to #root's own (unmagnified) coordinates - for placing a
 * position: fixed element from getBoundingClientRect(), which while magnified is relative
 * to #root, not the window. */
export function toLayout(x: number, y: number) {
  return { x: (x - tx) / scale, y: (y - ty) / scale }
}

let installed = false

export function installPinchZoom() {
  if (installed) return
  installed = true
  window.addEventListener('wheel', onWheel, { passive: false })
  window.addEventListener('resize', onResize)
  // capture phase, so a handler that stops a key event can't hide it
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Control') ctrlHeld = true
  }, true)
  window.addEventListener('keyup', (e) => {
    if (e.key === 'Control') ctrlHeld = false
  }, true)
  // a Ctrl released in another window never sends its keyup here
  window.addEventListener('blur', () => {
    ctrlHeld = false
  })
}
