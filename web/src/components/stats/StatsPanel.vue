<template>
  <div class="space-y-6">
    <!-- Period selector + refresh -->
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div class="flex items-center bg-botex-950 p-0.5 rounded-lg border border-botex-800 space-x-1">
        <button
          v-for="p in periods"
          :key="p.value"
          type="button"
          :aria-pressed="analytics.period === p.value"
          :class="[
            'min-h-[44px] lg:min-h-[36px] min-w-[44px] px-3 py-1 rounded text-xs font-medium transition',
            analytics.period === p.value
              ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30'
              : 'text-slate-300 hover:text-white'
          ]"
          @click="analytics.fetchStats(p.value)"
        >
          {{ t(p.label) }}
        </button>
      </div>
      <button
        type="button"
        class="min-h-[44px] lg:min-h-[36px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5"
        @click="analytics.fetchStats(undefined, true)"
      >
        <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': analytics.isLoading }" />
        <span>{{ t('forceRefresh') }}</span>
      </button>
    </div>

    <p class="text-xs text-slate-400">{{ t('statsSubtitle') }}</p>

    <!-- Error -->
    <div v-if="analytics.errorMessage" class="p-3 rounded-xl bg-red-950/50 border border-red-700/60 text-xs text-red-200">
      {{ analytics.errorMessage }}
    </div>

    <!-- Empty -->
    <div
      v-else-if="stats && stats.runs === 0"
      class="p-6 rounded-xl border border-botex-800 bg-botex-900 text-sm text-slate-300 text-center"
    >
      {{ t('noStats') }}
    </div>

    <template v-if="stats && stats.runs > 0">
      <!-- KPI cards -->
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <div v-for="kpi in kpis" :key="kpi.label" class="rounded-xl border border-botex-800 bg-botex-900 p-3 min-w-0">
          <div class="flex items-center justify-between text-xs text-slate-300 mb-1">
            <span class="truncate">{{ kpi.label }}</span>
            <component :is="kpi.icon" class="w-4 h-4 shrink-0" :class="kpi.color" />
          </div>
          <div class="text-lg font-bold text-slate-100 font-mono truncate">{{ kpi.value }}</div>
          <div
            v-if="kpi.sub"
            class="text-xs text-slate-400 font-mono truncate mt-0.5"
            :title="kpi.subTitle || kpi.sub"
          >
            {{ kpi.sub }}
          </div>
        </div>
      </div>

      <!-- Daily chart -->
      <div class="rounded-xl border border-botex-800 bg-botex-900 p-4">
        <div class="text-xs text-slate-300 font-semibold uppercase tracking-wider font-mono mb-3">
          {{ t('dailyTitle') }}
        </div>
        <StatsBarChart :data="stats.daily" />
      </div>

      <!-- Per-model breakdown: cards on mobile, table on >=sm -->
      <div class="rounded-xl border border-botex-800 bg-botex-900 overflow-hidden">
        <div class="lg:hidden divide-y divide-botex-800">
          <div v-for="m in stats.by_model" :key="m.model" class="px-4 py-3 space-y-1.5">
            <div class="flex items-start justify-between gap-2">
              <span class="font-mono text-xs text-slate-100 break-all min-w-0">{{ m.model }}</span>
              <span class="text-emerald-300 font-mono text-xs font-semibold whitespace-nowrap shrink-0">${{ m.cost_usd.toFixed(4) }}</span>
            </div>
            <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs font-mono text-slate-300">
              <span>{{ t('colRuns') }}: {{ m.runs }}</span>
              <span :class="m.done_pct >= 80 ? 'text-emerald-300' : m.done_pct >= 50 ? 'text-amber-300' : 'text-red-300'">
                {{ t('colDone') }} {{ m.done_pct }}%
              </span>
              <span>{{ t('colTokens') }}: {{ m.tokens.toLocaleString() }}</span>
              <span v-if="(m.cache_hit_pct ?? 0) > 0" class="text-emerald-400/90">Cache {{ m.cache_hit_pct }}%</span>
            </div>
            <div v-if="m.top_failure" class="text-xs font-mono text-rose-300/90">
              {{ t('colTopFailure') }}: {{ m.top_failure }}
            </div>
          </div>
        </div>
        <div class="hidden lg:block overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs min-w-[560px]">
            <thead>
              <tr class="border-b border-botex-800 bg-botex-950/60 text-slate-300 font-mono">
                <th class="py-2.5 px-4 font-semibold">Model</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colRuns') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colDone') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colTopFailure') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colTokens') }}</th>
                <th class="py-2.5 px-4 font-semibold text-right">{{ t('colCost') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-botex-800 font-mono text-slate-200">
              <tr v-for="m in stats.by_model" :key="m.model" class="hover:bg-botex-800/40 transition">
                <td class="py-2.5 px-4 text-slate-100 break-all">{{ m.model }}</td>
                <td class="py-2.5 px-4">{{ m.runs }}</td>
                <td class="py-2.5 px-4">
                  <span :class="m.done_pct >= 80 ? 'text-emerald-300' : m.done_pct >= 50 ? 'text-amber-300' : 'text-red-300'">
                    {{ m.done_pct }}%
                  </span>
                </td>
                <td class="py-2.5 px-4 text-slate-300">{{ m.top_failure || '—' }}</td>
                <td class="py-2.5 px-4">{{ m.tokens.toLocaleString() }}</td>
                <td class="py-2.5 px-4 text-right text-emerald-300 font-semibold">${{ m.cost_usd.toFixed(4) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div
        v-if="analytics.errorMessage"
        class="p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
      >
        <span class="font-mono truncate">{{ analytics.errorMessage }}</span>
        <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="analytics.fetchStats()">{{ t('refresh') }}</button>
      </div>

      <!-- Recent runs -->

      <div class="rounded-xl border border-botex-800 bg-botex-900 overflow-hidden">
        <div class="px-4 py-2.5 text-xs text-slate-300 font-semibold uppercase tracking-wider font-mono border-b border-botex-800">
          {{ t('recentTitle') }}
        </div>
        <ul class="divide-y divide-botex-800">
          <li
            v-for="(r, i) in stats.recent"
            :key="r.task_id || i"
            class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-xs"
          >
            <Badge :variant="r.status === 'DONE' ? 'success' : 'danger'">{{ r.status || '?' }}</Badge>
            <span class="order-last w-full min-w-0 font-mono text-slate-100 break-all lg:order-none lg:w-auto lg:flex-1">{{ r.model || '—' }}</span>
            <span class="text-slate-400 font-mono whitespace-nowrap">{{ (r.timestamp || '').slice(0, 16).replace('T', ' ') }}</span>
            <span class="text-emerald-300 font-mono whitespace-nowrap">${{ (r.cost_usd || 0).toFixed(4) }}</span>
            <span v-if="r.duration_s" class="text-slate-400 font-mono whitespace-nowrap">{{ r.duration_s.toFixed(1) }}s</span>
          </li>
        </ul>
      </div>
    </template>

    <!-- Local AI digest -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="text-xs text-slate-300 font-semibold uppercase tracking-wider font-mono flex items-center space-x-2">
          <SparklesIcon class="w-4 h-4 text-amber-300" />
          <span>{{ t('digestTitle') }}</span>
        </div>
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-3 py-1 rounded-lg bg-botex-800 hover:bg-botex-700 border border-botex-700 text-xs font-medium text-slate-200 transition disabled:opacity-50"
          :disabled="analytics.isDigestRunning"
          @click="analytics.runDigest()"
        >
          {{ analytics.isDigestRunning ? '…' : t('runDigest') }}
        </button>
      </div>
      <p v-if="analytics.digestMessage" class="text-xs text-amber-300">{{ analytics.digestMessage }}</p>
      <div v-if="stats?.latest_digest" class="space-y-1">
        <div class="text-xs text-slate-400 font-mono">{{ stats.latest_digest.title }}</div>
        <p class="text-xs text-slate-200 whitespace-pre-wrap leading-relaxed">{{ stats.latest_digest.content }}</p>
      </div>
      <p v-else class="text-xs text-slate-400">{{ t('digestEmpty') }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useI18n } from '../../i18n'
import { useAnalyticsStore, type AnalyticsPeriod } from '../../stores/analytics'
import Badge from '../common/Badge.vue'
import StatsBarChart from './StatsBarChart.vue'
import {
  RefreshCw as RefreshCwIcon,
  Play as PlayIcon,
  CheckCircle as CheckIcon,
  DollarSign as DollarIcon,
  Zap as ZapIcon,
  Database as DatabaseIcon,
  Sparkles as SparklesIcon
} from 'lucide-vue-next'

const { t } = useI18n()
const analytics = useAnalyticsStore()
const stats = computed(() => analytics.stats)

const periods: { value: AnalyticsPeriod; label: string }[] = [
  { value: 'week', label: 'periodWeek' },
  { value: 'month', label: 'periodMonth' },
  { value: 'all', label: 'periodAll' }
]

const kpis = computed(() => {
  const s = stats.value
  if (!s) return []
  return [
    { label: t('totalRuns'), value: String(s.runs), icon: PlayIcon, color: 'text-cyan-400' },
    { label: t('successRate'), value: `${s.done_pct}%`, icon: CheckIcon, color: 'text-emerald-400' },
    { label: t('totalCost'), value: `$${s.cost_usd.toFixed(4)}`, icon: DollarIcon, color: 'text-amber-400' },
    {
      label: t('tokensUsed'),
      value: (s.prompt_tokens + s.completion_tokens).toLocaleString(),
      icon: ZapIcon,
      color: 'text-cyan-400'
    },
    {
      label: t('cacheHit'),
      value: `${s.cache_hit_pct}%`,
      sub: `~$${s.cache_savings_usd.toFixed(2)}`,
      subTitle: `~$${s.cache_savings_usd.toFixed(2)} ${t('cacheSaved')}`,
      icon: DatabaseIcon,
      color: 'text-emerald-400'
    }
  ]
})

onMounted(() => {
  if (!analytics.stats) analytics.fetchStats()
})
</script>
