import { Warning } from '@phosphor-icons/react'
import type { Icon } from '@phosphor-icons/react'
import { useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { statusChip } from '../lib/activity'

export function SectionLabel({ children }: { children: ReactNode }) {
  return <span className="section-label">{children}</span>
}

export function StatusPill({ status }: { status: string | null | undefined }) {
  if (!status) return null
  const { color, bg } = statusChip(status)
  return (
    <span className="status-pill" style={{ color, background: bg }}>
      {status}
    </span>
  )
}

export function ConflictPill() {
  return (
    <span className="conflict-pill">
      <Warning size={12} weight="fill" />
      Conflict
    </span>
  )
}

export function IconTile({
  icon: IconCmp,
  color,
  size = 40,
  iconSize = 21,
}: {
  icon: Icon
  color: string
  size?: number
  iconSize?: number
}) {
  return (
    <span className="icon-tile" style={{ width: size, height: size, color }}>
      <IconCmp size={iconSize} />
    </span>
  )
}

export function EmptyState({
  icon: IconCmp,
  title,
  children,
}: {
  icon: Icon
  title: string
  children?: ReactNode
}) {
  return (
    <div className="empty-state">
      <IconCmp size={34} color="var(--faint)" />
      <div className="es-title">{title}</div>
      {children && <div className="es-body">{children}</div>}
    </div>
  )
}

export function Loading({ label }: { label?: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '40px 0', justifyContent: 'center' }}>
      <span className="spinner" />
      {label && <span style={{ fontSize: 13, color: 'var(--muted)' }}>{label}</span>}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  const msg = error instanceof Error ? error.message : 'Something went wrong'
  return (
    <div className="error-note" role="alert">
      <Warning size={16} weight="fill" />
      {msg}
    </div>
  )
}

export function Modal({
  onClose,
  children,
  label,
  width = 420,
  closeDisabled = false,
  focusDialog = false,
}: {
  onClose: () => void
  children: ReactNode
  label: string
  width?: number
  closeDisabled?: boolean
  focusDialog?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  const close = useRef(onClose)
  const locked = useRef(closeDisabled)
  useEffect(() => { close.current = onClose; locked.current = closeDisabled }, [onClose, closeDisabled])

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const focusable = () => Array.from(box.current?.querySelectorAll<HTMLElement>(
      'button:enabled, input:enabled, textarea:enabled, select:enabled, a[href], summary, [tabindex]:not([tabindex="-1"]), [contenteditable="true"]',
    ) ?? []).filter((element) => element.getClientRects().length > 0)
    const first = focusable()[0]
    ;(focusDialog ? box.current : first ?? box.current)?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        if (!locked.current) close.current()
      }
      if (event.key === 'Tab') {
        const elements = focusable()
        const first = elements[0]
        const last = elements[elements.length - 1]
        if (!first || !last) { event.preventDefault(); box.current?.focus(); return }
        if (event.shiftKey && (document.activeElement === first || document.activeElement === box.current || !box.current?.contains(document.activeElement))) {
          event.preventDefault(); last.focus()
        } else if (!event.shiftKey && (document.activeElement === last || !box.current?.contains(document.activeElement))) {
          event.preventDefault(); first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (previous?.isConnected) previous.focus()
    }
  }, [])

  return (
    <div className="modal-overlay" onClick={() => { if (!closeDisabled) onClose() }}>
      <div ref={box} className="modal-box" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}
        style={{ width }} onClick={(event) => event.stopPropagation()}>
        {children}
      </div>
    </div>
  )
}

export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  danger = true,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: {
  title: string
  body: ReactNode
  confirmLabel: string
  danger?: boolean
  busy?: boolean
  error?: unknown
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <Modal label={title} onClose={onCancel} closeDisabled={busy}>
      <div className="display" style={{ fontSize: 19, fontWeight: 600 }}>
        {title}
      </div>
      <div style={{ fontSize: 13.5, color: 'var(--text-3)', lineHeight: 1.55, margin: '10px 0 22px' }}>{body}</div>
      {error != null && <ErrorNote error={error} />}
      <div style={{ display: 'flex', gap: 10, marginTop: error != null ? 12 : 0 }}>
        <button className="btn-ghost" style={{ flex: 1, justifyContent: 'center' }} disabled={busy} onClick={onCancel}>
          Cancel
        </button>
        <button
          className={danger ? 'btn-danger-outline' : 'btn-accent'}
          style={{ flex: 1, justifyContent: 'center' }}
          disabled={busy}
          onClick={onConfirm}
        >
          {busy ? 'Working…' : confirmLabel}
        </button>
      </div>
    </Modal>
  )
}

/** 1-3 importance dots used on notes. */
export function ImportanceDots({ value, color }: { value: number; color: string }) {
  return (
    <span style={{ display: 'inline-flex', gap: 3 }}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          style={{
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: i < value ? color : '#2A2520',
          }}
        />
      ))}
    </span>
  )
}
