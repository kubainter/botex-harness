// Mirrors the status taxonomy in botex/contracts.py (STATUS_FAILURE_KIND).
const LIMIT_STATUSES = new Set([
  'MAX_TURNS_REACHED', 'TOKEN_LIMIT', 'TIME_LIMIT', 'BUDGET_EXCEEDED', 'PRICE_EXCEEDED',
  'INCOMPLETE', 'STAGNANT_ROLLBACK', 'BLOCKED', 'UNSUPPORTED'
])
const FAILURE_STATUSES = new Set([
  'FAILED', 'API_ERROR', 'REQUEST_TIMEOUT', 'ERROR', 'CONFIG_ERROR',
  'ZDR_VIOLATION', 'VERIFICATION_FAILED', 'DONE_WITHOUT_WRITE', 'ABORTED'
])

export function statusDotColor(status: string): string {
  if (status === 'DONE') return 'bg-emerald-400'
  if (status === 'RUNNING') return 'bg-cyan-400 animate-pulse'
  if (status === 'QUEUED') return 'bg-amber-400 animate-pulse'
  if (FAILURE_STATUSES.has(status)) return 'bg-rose-500'
  if (LIMIT_STATUSES.has(status)) return 'bg-amber-400'
  return 'bg-slate-400'
}

export type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'purple'

export function statusBadgeVariant(status: string): BadgeVariant {
  if (status === 'DONE') return 'success'
  if (status === 'RUNNING') return 'info'
  if (FAILURE_STATUSES.has(status)) return 'danger'
  if (LIMIT_STATUSES.has(status) || status === 'QUEUED') return 'warning'
  return 'default'
}
