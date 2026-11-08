<template>
  <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 shadow-sm space-y-3">
    <!-- Header -->
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <h3 class="text-sm font-semibold text-slate-100 truncate">
          {{ displayTitle }}
        </h3>
        <p v-if="subtitle" class="text-xs text-slate-400 mt-0.5 truncate">
          {{ subtitle }}
        </p>
      </div>
      <Badge :variant="badgeVariant" class="shrink-0">
        {{ badgeLabel }}
      </Badge>
    </div>

    <!-- Primary Metrics Display (Type, not tiles) -->
    <div class="space-y-1">
      <div class="flex items-baseline justify-between">
        <span class="text-2xl font-bold text-slate-100 font-mono">
          {{ pct.toFixed(1) }}%
        </span>
        <span class="text-xs font-mono text-slate-300">
          {{ formatNumber(window.current_usage) }} / {{ formatNumber(window.limit) }}
        </span>
      </div>
      <div class="text-xs font-mono text-slate-400">
        {{ formatNumber(remaining) }} {{ t('wallet.quota.remaining') }}
      </div>
    </div>

    <!-- Accessible Progress Bar -->
    <div
      role="progressbar"
      :aria-valuenow="pct"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-label="displayTitle"
      class="h-2 w-full bg-botex-950 rounded-full border border-botex-800 overflow-hidden"
    >
      <div
        class="h-full rounded-full transition-all duration-300"
        :class="barColorClass"
        :style="{ width: `${pct}%` }"
      />
    </div>

    <!-- Countdown Timer to Reset -->
    <div
      v-if="countdownDisplay"
      class="flex items-center justify-between text-xs font-mono text-slate-400 pt-1 border-t border-botex-800/60"
    >
      <span class="flex items-center space-x-1">
        <ClockIcon class="w-3.5 h-3.5 text-cyan-400" />
        <span>{{ t('wallet.quota.resets_in') }}:</span>
      </span>
      <span class="text-slate-200 font-semibold">{{ countdownDisplay }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { Clock as ClockIcon } from 'lucide-vue-next'
import Badge from '../common/Badge.vue'
import { useI18n } from '../../i18n'
import type { QuotaWindow } from '../../api/types'

const props = defineProps<{
  window: QuotaWindow
  title?: string
  subtitle?: string
}>()

const { locale, t } = useI18n()

const displayTitle = computed(() => {
  if (props.title) return props.title
  if (props.window.name) return props.window.name
  if (props.window.unit !== undefined && props.window.number !== undefined) {
    const num = props.window.number
    const u = props.window.unit
    if (u === 3 || u === '3' || u === 'hours') {
      return locale.value === 'pl' ? `Okno ${num}-godzinne` : `${num}-Hour Window`
    }
    if (u === 6 || u === '6' || u === 'weeks') {
      return locale.value === 'pl' ? `Okno ${num}-tygodniowe` : `${num}-Week Window`
    }
    if (u === 5 || u === '5' || u === 'months') {
      return locale.value === 'pl' ? `Okno ${num}-miesięczne` : `${num}-Month Window`
    }
    return locale.value === 'pl' ? `Okno limitu (${num})` : `Quota Window (${num})`
  }
  return t('wallet.quota.title') || (locale.value === 'pl' ? 'Okno limitu' : 'Quota Window')
})

const pct = computed(() => {
  if (props.window.percentage !== undefined && !isNaN(props.window.percentage)) {
    return Math.max(0, Math.min(100, Number(props.window.percentage)))
  }
  if (props.window.limit > 0) {
    return Math.max(0, Math.min(100, (props.window.current_usage / props.window.limit) * 100))
  }
  return 0
})

const remaining = computed(() => {
  return Math.max(0, props.window.limit - props.window.current_usage)
})

const badgeVariant = computed<'success' | 'warning' | 'danger'>(() => {
  if (pct.value >= 95) return 'danger'
  if (pct.value >= 80) return 'warning'
  return 'success'
})

const badgeLabel = computed(() => {
  if (pct.value >= 95) return t('wallet.quota.status.exceeded')
  if (pct.value >= 80) return t('wallet.quota.status.warning')
  return t('wallet.quota.status.normal')
})

const barColorClass = computed(() => {
  if (pct.value >= 95) return 'bg-rose-500'
  if (pct.value >= 80) return 'bg-amber-500'
  return 'bg-emerald-500'
})

function formatNumber(val: number | null | undefined): string {
  if (val === null || val === undefined) return '0'
  return Number(val).toLocaleString()
}

function parseEpochMs(timeVal: number | string | null | undefined): number | null {
  if (timeVal === null || timeVal === undefined || timeVal === '') return null
  if (typeof timeVal === 'number') {
    return timeVal < 1e11 ? timeVal * 1000 : timeVal
  }
  const parsed = Date.parse(timeVal)
  return isNaN(parsed) ? null : parsed
}

const nowMs = ref(Date.now())
let timerId: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  timerId = setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
})

onUnmounted(() => {
  if (timerId !== null) {
    clearInterval(timerId)
    timerId = null
  }
})

const resetMs = computed(() => parseEpochMs(props.window.next_reset_time))

const countdownDisplay = computed(() => {
  if (resetMs.value === null) return null
  const diff = resetMs.value - nowMs.value
  if (diff <= 0) {
    return t('wallet.quota.reset_imminent')
  }
  const totalSec = Math.floor(diff / 1000)
  const days = Math.floor(totalSec / 86400)
  const hours = Math.floor((totalSec % 86400) / 3600)
  const minutes = Math.floor((totalSec % 3600) / 60)
  const seconds = totalSec % 60

  if (days > 0) {
    return `${days}d ${hours}h`
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`
  }
  return `${minutes}m ${seconds}s`
})
</script>
