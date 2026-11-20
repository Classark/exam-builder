import { useEffect, useState } from 'react'

/** A number edited in place: saved on Enter or leaving the box, Escape puts it back. */
export function NumberCell({
  value,
  warn,
  label,
  onSave,
}: {
  value: number
  warn?: boolean
  label: string
  onSave: (n: number) => Promise<void>
}) {
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const commit = () => {
    const n = Number(text)
    if (text.trim() === '' || !Number.isInteger(n) || n < 0) return setText(String(value))
    if (n !== value) void onSave(n)
  }
  return (
    <input
      aria-label={label}
      inputMode="numeric"
      value={text}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        if (e.key === 'Escape') setText(String(value))
      }}
      className={`field w-12 shrink-0 px-1.5 py-0.5 text-center text-sm ${warn ? 'border-warning text-warning' : ''}`}
    />
  )
}

