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
    <div
      v-if="analytics.errorMessage"
      class="p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ analytics.errorMessage }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="analytics.fetchStats(undefined, true)">
        {{ t('refresh') }}
      </button>
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
          <div class="text-lg font-bold text-slate-100 font-mono tabular-nums truncate">{{ kpi.value }}</div>
          <div
            v-if="kpi.sub"
            class="text-xs text-slate-400 font-mono truncate mt-0.5"
            :title="kpi.subTitle || kpi.sub"
          >
            {{ kpi.sub }}
          </div>
        </div>
      </div>

      <!-- Execution & Code Telemetry strip -->
      <div class="rounded-xl border border-botex-800 bg-botex-900/70 px-4 py-3">
        <div class="text-[11px] text-slate-400 font-semibold uppercase tracking-wider font-mono mb-2">
          {{ t('execTelemetryTitle') }}
        </div>
        <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 text-xs font-mono">
          <div>
            <span class="text-slate-400 block text-[11px]">{{ t('avgDuration') }}</span>
            <span class="text-slate-100 font-semibold">{{ stats.avg_duration_s !== null ? `${stats.avg_duration_s.toFixed(1)}s` : '—' }}</span>
          </div>
          <div>
            <span class="text-slate-400 block text-[11px]">{{ t('avgGenLatency') }}</span>
            <span class="text-slate-100 font-semibold">{{ stats.avg_generation_ms !== null ? `${Math.round(stats.avg_generation_ms)}ms` : '—' }}</span>
          </div>
          <div>
            <span class="text-slate-400 block text-[11px]">{{ t('codeDiff') }}</span>
            <span class="text-slate-100 font-semibold">
              <span class="text-emerald-300">+{{ stats.lines_added }}</span>
              <span class="text-slate-500"> / </span>
              <span class="text-rose-300">-{{ stats.lines_removed }}</span>
            </span>
          </div>
          <div>
            <span class="text-slate-400 block text-[11px]">{{ t('avgSteps') }}</span>
            <span class="text-slate-100 font-semibold">{{ stats.avg_steps !== undefined ? stats.avg_steps.toFixed(1) : '—' }}</span>
          </div>
          <div>
            <span class="text-slate-400 block text-[11px]">{{ t('syntaxRejections') }}</span>
            <span :class="(stats.syntax_rejections ?? 0) > 0 ? 'text-amber-300 font-semibold' : 'text-slate-100 font-semibold'">
              {{ stats.syntax_rejections ?? 0 }}
            </span>
          </div>
          <div>
            <span class="text-slate-400 block text-[11px]">{{ t('rollbacks') }}</span>
            <span :class="(stats.rollback_count ?? 0) > 0 ? 'text-rose-300 font-semibold' : 'text-slate-100 font-semibold'">
              {{ stats.rollback_count ?? 0 }}
            </span>
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

      <!-- Per-provider breakdown -->
      <div v-if="stats.by_provider && stats.by_provider.length > 0" class="rounded-xl border border-botex-800 bg-botex-900 overflow-hidden">
        <div class="px-4 py-2.5 text-xs text-slate-300 font-semibold uppercase tracking-wider font-mono border-b border-botex-800">
          {{ t('byProviderTitle') }}
        </div>
        <div class="lg:hidden divide-y divide-botex-800">
          <div v-for="p in stats.by_provider" :key="p.provider" class="px-4 py-3 space-y-1.5">
            <div class="flex items-center justify-between gap-2">
              <span class="font-mono text-xs font-semibold text-cyan-200 uppercase">{{ p.provider }}</span>
              <span class="text-emerald-300 font-mono text-xs font-semibold">${{ p.cost_usd.toFixed(4) }}</span>
            </div>
            <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs font-mono text-slate-300">
              <span>{{ t('colRuns') }}: {{ p.runs }}</span>
              <span :class="p.done_pct >= 80 ? 'text-emerald-300' : p.done_pct >= 50 ? 'text-amber-300' : 'text-red-300'">
                {{ t('colDone') }} {{ p.done_pct }}%
              </span>
              <span>{{ t('colTokens') }}: {{ p.tokens.toLocaleString() }}</span>
              <span v-if="p.avg_duration_s !== null && p.avg_duration_s !== undefined">{{ t('colAvgTime') }}: {{ p.avg_duration_s.toFixed(1) }}s</span>
            </div>
            <div v-if="p.top_failure" class="text-xs font-mono text-rose-300/90">
              {{ t('colTopFailure') }}: {{ p.top_failure }}
            </div>
          </div>
        </div>
        <div class="hidden lg:block overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs min-w-[560px]">
            <thead>
              <tr class="border-b border-botex-800 bg-botex-950/60 text-slate-300 font-mono">
                <th class="py-2.5 px-4 font-semibold">{{ t('colProvider') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colRuns') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colDone') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colAvgTime') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colTopFailure') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colTokens') }}</th>
                <th class="py-2.5 px-4 font-semibold text-right">{{ t('colCost') }}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-botex-800 font-mono text-slate-200">
              <tr v-for="p in stats.by_provider" :key="p.provider" class="hover:bg-botex-800/40 transition">
                <td class="py-2.5 px-4 text-cyan-200 font-semibold uppercase">{{ p.provider }}</td>
                <td class="py-2.5 px-4">{{ p.runs }}</td>
                <td class="py-2.5 px-4">
                  <span :class="p.done_pct >= 80 ? 'text-emerald-300' : p.done_pct >= 50 ? 'text-amber-300' : 'text-red-300'">
                    {{ p.done_pct }}%
                  </span>
                </td>
                <td class="py-2.5 px-4 text-slate-300">{{ p.avg_duration_s !== null && p.avg_duration_s !== undefined ? `${p.avg_duration_s.toFixed(1)}s` : '—' }}</td>
                <td class="py-2.5 px-4 text-slate-300">{{ p.top_failure || '—' }}</td>
                <td class="py-2.5 px-4">{{ p.tokens.toLocaleString() }}</td>
                <td class="py-2.5 px-4 text-right text-emerald-300 font-semibold">${{ p.cost_usd.toFixed(4) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Task taxonomy breakdown -->
      <div v-if="stats.by_taxonomy && stats.by_taxonomy.length > 0" class="rounded-xl border border-botex-800 bg-botex-900 overflow-hidden">
        <div class="px-4 py-2.5 text-xs text-slate-300 font-semibold uppercase tracking-wider font-mono border-b border-botex-800">
          {{ t('byTaxonomyTitle') }}
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-px bg-botex-800">
          <div v-for="g in stats.by_taxonomy" :key="g.key" class="bg-botex-900 px-4 py-3 text-xs font-mono space-y-1">
            <div class="flex items-center justify-between gap-2">
              <span class="text-slate-100 font-semibold uppercase">{{ g.key }}</span>
              <span class="text-emerald-300 font-semibold">${{ g.cost_usd.toFixed(4) }}</span>
            </div>
            <div class="flex flex-wrap gap-x-3 text-slate-400">
              <span>{{ t('colRuns') }}: <span class="text-slate-200">{{ g.runs }}</span></span>
              <span :class="g.done_pct >= 80 ? 'text-emerald-300' : g.done_pct >= 50 ? 'text-amber-300' : 'text-red-300'">
                {{ g.done_pct }}%
              </span>
              <span>{{ g.tokens.toLocaleString() }} tok</span>
            </div>
          </div>
        </div>
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

      <!-- Recent runs -->

      <div class="rounded-xl border border-botex-800 bg-botex-900 overflow-hidden">
        <div class="px-4 py-2.5 text-xs text-slate-300 font-semibold uppercase tracking-wider font-mono border-b border-botex-800">
          {{ t('recentTitle') }}
        </div>
        <ul class="divide-y divide-botex-800">
          <li
            v-for="(r, i) in stats.recent"
            :key="r.task_id || i"
            class="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2.5 text-xs tabular-nums"
          >
            <Badge :variant="r.status === 'DONE' ? 'success' : 'danger'">{{ r.status || '?' }}</Badge>
            <span class="order-last w-full min-w-0 font-mono text-slate-100 break-all lg:order-none lg:w-auto lg:flex-1">{{ r.model || '—' }}</span>
            <span class="text-slate-300 font-mono whitespace-nowrap">{{ (r.timestamp || '').slice(0, 16).replace('T', ' ') }}</span>
            <span class="text-emerald-300 font-mono whitespace-nowrap">${{ (r.cost_usd || 0).toFixed(4) }}</span>
            <span v-if="r.duration_s" class="text-slate-300 font-mono whitespace-nowrap">{{ r.duration_s.toFixed(1) }}s</span>
          </li>
        </ul>
      </div>
    </template>

    <!-- Self-Improvement Loop (Layers A-C) -->
    <div v-if="learning" class="rounded-xl border border-botex-800 bg-botex-900 p-4 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-botex-800 pb-2.5">
        <div class="text-xs text-slate-200 font-semibold uppercase tracking-wider font-mono">
          {{ t('learningLoopTitle') }}
        </div>
        <div class="flex flex-wrap items-center gap-2 text-[11px] font-mono tabular-nums">
          <span class="px-2 py-0.5 rounded bg-emerald-950/60 border border-emerald-700/50 text-emerald-300">
            {{ learning.lessons.counts.active }} {{ t('learningActive') }}
          </span>
          <span class="px-2 py-0.5 rounded bg-amber-950/60 border border-amber-700/50 text-amber-300">
            {{ learning.lessons.counts.candidate }} {{ t('learningCandidate') }}
          </span>
          <span v-if="learning.lessons.counts.deprecated > 0" class="px-2 py-0.5 rounded bg-botex-950 border border-botex-800 text-slate-300">
            {{ learning.lessons.counts.deprecated }} {{ t('learningDeprecated') }}
          </span>
        </div>
      </div>

      <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs font-mono tabular-nums">
        <div class="rounded-lg border border-botex-800 bg-botex-950/60 p-2.5">
          <span class="text-slate-300 block text-[11px]">{{ t('learningAvgConf') }}</span>
          <span class="text-slate-100 font-semibold text-sm">
            {{ learning.lessons.avg_confidence !== null ? `${Math.round(learning.lessons.avg_confidence * 100)}%` : '—' }}
          </span>
        </div>
        <div class="rounded-lg border border-botex-800 bg-botex-950/60 p-2.5">
          <span class="text-slate-300 block text-[11px]">{{ t('learningAppliedRuns') }}</span>
          <span class="text-slate-100 font-semibold text-sm">
            {{ learning.ledger.runs_with_applied_lessons }} / {{ learning.ledger.runs_total }}
          </span>
        </div>
        <div class="rounded-lg border border-botex-800 bg-botex-950/60 p-2.5">
          <span class="text-slate-300 block text-[11px]">{{ t('learningSkillRuns') }}</span>
          <span class="text-slate-100 font-semibold text-sm">
            {{ learning.ledger.runs_with_applied_skills }} / {{ learning.ledger.runs_total }}
          </span>
        </div>
        <div class="rounded-lg border border-botex-800 bg-botex-950/60 p-2.5">
          <span class="text-slate-300 block text-[11px]">{{ t('learningShadowRuns') }}</span>
          <span class="text-slate-100 font-semibold text-sm">
            {{ learning.ledger.runs_with_shadow_advice }} / {{ learning.ledger.runs_total }}
          </span>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <!-- Recent lessons -->
        <div class="space-y-2">
          <div class="text-[11px] text-slate-300 font-semibold uppercase tracking-wider font-mono">
            {{ t('learningLessonsTitle') }}
          </div>
          <p v-if="learning.lessons.items.length === 0" class="text-xs text-slate-300">
            {{ t('learningNoLessons') }}
          </p>
          <ul v-else class="divide-y divide-botex-800 rounded-lg border border-botex-800 bg-botex-950/50 max-h-56 overflow-y-auto">
            <li
              v-for="item in learning.lessons.items.slice(0, 8)"
              :key="item.memory_id"
              class="px-3 py-2 text-xs flex items-center justify-between gap-2"
            >
              <span class="text-slate-100 truncate min-w-0">{{ item.title }}</span>
              <div class="flex items-center gap-2 shrink-0 font-mono text-[11px] tabular-nums">
                <span v-if="item.confidence !== null" class="text-slate-300">{{ Math.round(item.confidence * 100) }}%</span>
                <Badge :variant="item.status === 'active' ? 'success' : item.status === 'candidate' ? 'warning' : 'default'">
                  {{ item.status }}
                </Badge>
              </div>
            </li>
          </ul>
        </div>

        <!-- Proposals & Shadow routing -->
        <div class="space-y-3">
          <div class="space-y-1.5">
            <div class="text-[11px] text-slate-300 font-semibold uppercase tracking-wider font-mono">
              {{ t('learningProposalsTitle') }}
            </div>
            <p v-if="learning.proposals.length === 0" class="text-xs text-slate-300">
              {{ t('learningNoProposals') }}
            </p>
            <ul v-else class="divide-y divide-botex-800 rounded-lg border border-botex-800 bg-botex-950/50">
              <li v-for="prop in learning.proposals" :key="prop.name" class="px-3 py-2 text-xs flex items-center justify-between gap-2 font-mono">
                <span class="text-cyan-200 truncate">{{ prop.name }}</span>
                <span class="text-slate-300 text-[11px] shrink-0">{{ prop.files.join(', ') }}</span>
              </li>
            </ul>
          </div>

          <div class="space-y-1.5">
            <div class="text-[11px] text-slate-300 font-semibold uppercase tracking-wider font-mono">
              {{ t('learningShadowTitle') }}
            </div>
            <p v-if="learning.ledger.shadow_recommendations.length === 0" class="text-xs text-slate-300">
              {{ t('learningNoShadow') }}
            </p>
            <ul v-else class="divide-y divide-botex-800 rounded-lg border border-botex-800 bg-botex-950/50">
              <li
                v-for="(s, idx) in learning.ledger.shadow_recommendations.slice(0, 5)"
                :key="idx"
                class="px-3 py-2 text-xs flex items-center justify-between gap-2 font-mono tabular-nums"
              >
                <span class="text-slate-200 truncate">{{ s.recipe || 'general' }} · {{ s.taxonomy || 'general' }} → <strong class="text-cyan-200">{{ s.model }}</strong></span>
                <span class="text-slate-300 shrink-0">{{ s.count }}x</span>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>

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
const learning = computed(() => analytics.learningStatus)

const periods: { value: AnalyticsPeriod; label: string }[] = [
  { value: 'week', label: 'periodWeek' },
  { value: 'month', label: 'periodMonth' },
  { value: 'all', label: 'periodAll' }
]

const kpis = computed(() => {
  const s = stats.value
  if (!s) return []
  const estUsd = s.estimated_cost_usd ?? 0
  const hasSources = Boolean(s.cost_source_counts && Object.values(s.cost_source_counts).some((v) => v > 0))
  const costSub =
    estUsd > 0
      ? `${t('costEstimatedSub')} $${estUsd.toFixed(4)}`
      : hasSources && s.cost_usd > 0
        ? t('costApiBilled')
        : undefined
  return [
    { label: t('totalRuns'), value: String(s.runs), icon: PlayIcon, color: 'text-cyan-400' },
    { label: t('successRate'), value: `${s.done_pct}%`, icon: CheckIcon, color: 'text-emerald-400' },
    { label: t('totalCost'), value: `$${s.cost_usd.toFixed(4)}`, sub: costSub, icon: DollarIcon, color: 'text-amber-400' },
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
