import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * 修复 Radix UI 的一个已知问题：
 * DismissableLayer 在层栈变化时会设置/恢复 body 的 pointer-events，
 * 但在某些时序下（例如从 DropdownMenu 打开 Dialog/Sheet 再关闭），
 * body 上残留的 `pointer-events: none` 不会被清除，导致页面无法点击。
 *
 * 该函数会在"下一帧 + 再下一帧"检查并清理，确保跑在 Radix 所有内部
 * effect/cleanup 之后；同时会检查文档中是否还有其他处于 open 状态的
 * Radix overlay，避免误清掉合法的锁定。
 */
export function clearBodyPointerEventsNoneDeferred() {
  if (typeof document === 'undefined') return
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      // 仍有 open 状态的 Radix overlay 时不要清除——它们会接管 body 锁定
      const hasOpenOverlay =
        document.querySelector('[data-state="open"][role="dialog"]') != null ||
        document.querySelector('[data-state="open"][role="menu"]') != null
      if (!hasOpenOverlay && document.body.style.pointerEvents === 'none') {
        document.body.style.pointerEvents = ''
      }
    })
  })
}
