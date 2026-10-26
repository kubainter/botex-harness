<template>
  <div class="h-full overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6">
    <!-- View Header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4">
      <div>
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <WalletIcon class="w-5 h-5 text-emerald-400" />
          <span>{{ t('walletTitle') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">
          {{ t('walletSubtitle') }}
        </p>
      </div>

      <div class="flex items-center gap-2 flex-wrap">
        <div class="flex items-center bg-botex-950 p-0.5 rounded-lg border border-botex-800 space-x-1">
          <button
            v-for="tab in tabs"
            :key="tab.value"
            type="button"
            :aria-pressed="activeTab === tab.value"
            :class="[
              'min-h-[44px] lg:min-h-[36px] min-w-[44px] px-3 py-1 rounded text-xs font-medium transition',
              activeTab === tab.value
                ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30'
                : 'text-slate-300 hover:text-white'
            ]"
            @click="activeTab = tab.value"
          >
            {{ t(tab.label) }}
          </button>
        </div>
        <button
          v-if="activeTab === 'budget'"
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5"
          @click="walletStore.fetchWallet(true)"
        >
          <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': walletStore.isLoading }" />
          <span>{{ t('forceRefresh') }}</span>
        </button>
      </div>
    </div>

    <StatsPanel v-if="activeTab === 'stats'" />

    <template v-else>
    <!-- Wallet Balance Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <!-- Balance USD -->
      <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 shadow-sm">
        <div class="flex items-center justify-between text-xs text-slate-300 mb-1">
          <span>{{ t('activeBalance') }}</span>
          <CreditCardIcon class="w-4 h-4 text-emerald-400" />
        </div>
        <div class="text-2xl font-bold text-slate-100 font-mono">
          <span v-if="walletInfo?.balance_usd !== null && walletInfo?.balance_usd !== undefined">
            ${{ Number(walletInfo.balance_usd).toFixed(4) }}
          </span>
          <span v-else class="text-slate-400 text-lg">N/A</span>
        </div>
        <div class="mt-2 flex items-center space-x-1.5">
          <Badge :variant="walletInfo?.is_free_tier ? 'warning' : 'success'">
            {{ walletInfo?.is_free_tier ? 'Free Tier' : 'Paid Quota' }}
          </Badge>
          <span class="text-xs text-slate-300 font-mono">{{ walletInfo?.label || 'OpenRouter' }}</span>
        </div>
      </div>

      <!-- Total Usage USD -->
      <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 shadow-sm">
        <div class="flex items-center justify-between text-xs text-slate-300 mb-1">
          <span>{{ t('totalUsage') }}</span>
          <TrendingUpIcon class="w-4 h-4 text-cyan-400" />
        </div>
        <div class="text-2xl font-bold text-slate-100 font-mono">
          <span v-if="walletInfo?.usage_usd !== null && walletInfo?.usage_usd !== undefined">
            ${{ Number(walletInfo.usage_usd).toFixed(4) }}
          </span>
          <span v-else class="text-slate-400 text-lg">N/A</span>
        </div>
        <p class="text-xs text-slate-300 mt-2">
          {{ locale === 'pl' ? 'Łączne zużycie na koncie' : 'Cumulative account spend' }}
        </p>
      </div>

      <!-- Limit USD -->
      <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 shadow-sm">
        <div class="flex items-center justify-between text-xs text-slate-300 mb-1">
          <span>{{ t('limitCap') }}</span>
          <ShieldIcon class="w-4 h-4 text-amber-400" />
        </div>
        <div class="text-2xl font-bold text-slate-100 font-mono">
          <span v-if="walletInfo?.limit_usd !== null && walletInfo?.limit_usd !== undefined">
            ${{ Number(walletInfo.limit_usd).toFixed(2) }}
          </span>
          <span v-else class="text-slate-400 text-lg">{{ locale === 'pl' ? 'Brak limitu' : 'No limit' }}</span>
        </div>
        <p class="text-xs text-slate-300 mt-2">
          {{ locale === 'pl' ? 'Skonfigurowany próg wydatków' : 'Configured spend cap' }}
        </p>
      </div>

      <!-- Remaining Limit -->
      <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 shadow-sm">
        <div class="flex items-center justify-between text-xs text-slate-300 mb-1">
          <span>{{ t('remainingQuota') }}</span>
          <ClockIcon class="w-4 h-4 text-cyan-400" />
        </div>
        <div class="text-2xl font-bold text-slate-100 font-mono">
          <span v-if="walletInfo?.limit_remaining_usd !== null && walletInfo?.limit_remaining_usd !== undefined">
            ${{ Number(walletInfo.limit_remaining_usd).toFixed(4) }}
          </span>
          <span v-else class="text-slate-400 text-lg">N/A</span>
        </div>
        <p class="text-xs text-slate-300 mt-2">
          {{ locale === 'pl' ? 'Dostępne środki przed blokadą' : 'Available before hard cap' }}
        </p>
      </div>
    </div>

    <!-- API error notice -->
    <div
      v-if="walletStore.errorMessage"
      class="p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ walletStore.errorMessage }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="walletStore.fetchWallet()">{{ t('refresh') }}</button>
    </div>

    <!-- Error Notice if any -->
    <div v-if="walletInfo?.error" class="p-3 rounded-xl bg-amber-950/50 border border-amber-700/60 text-xs text-amber-200">
      <strong>Provider Notice:</strong> {{ walletInfo.error }}
    </div>

    <!-- Token & Cost Analytics Ledger Table -->
    <div class="space-y-3">
      <div class="flex items-center justify-between text-xs text-slate-300 px-1 border-b border-botex-800 pb-2">
        <span class="font-semibold uppercase tracking-wider font-mono">{{ t('ledgerTitle') }}</span>
        <span class="text-slate-400">{{ t('ledgerSubtitle') }}</span>
      </div>

      <div class="rounded-xl border border-botex-800 bg-botex-900 overflow-hidden shadow-sm">
        <div v-if="ledgerRows.length === 0" class="p-6 text-center text-xs text-slate-400">
          {{ t('noTelemetry') }}
        </div>
        <!-- Mobile: stacked cards instead of a squeezed scrollable table -->
        <div v-else class="lg:hidden divide-y divide-botex-800">
          <div v-for="item in ledgerRows" :key="item.id" class="px-4 py-3 space-y-1.5">
            <div class="flex items-start justify-between gap-2">
              <div class="min-w-0">
                <div class="font-medium text-xs text-slate-100 break-all">{{ item.name }}</div>
                <div v-if="item.name !== item.id" class="text-xs text-slate-400 font-mono break-all">{{ item.id }}</div>
              </div>
              <Badge v-if="item.role" :variant="roleBadgeVariant(item.role)" class="shrink-0">{{ item.role }}</Badge>
            </div>
            <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs font-mono text-slate-300">
              <span>{{ t('colRuns') }}: <span class="text-slate-100">{{ item.runs }}</span></span>
              <span>{{ t('colTokens') }}: <span class="text-slate-100">{{ item.tokens.toLocaleString() }}</span></span>
              <span v-if="item.cachedTokens > 0" class="text-emerald-400/90">
                {{ item.cachePct }}% · ~${{ item.savings.toFixed(2) }}
              </span>
              <span>{{ t('colCost') }}: <span class="text-emerald-300 font-semibold">${{ item.cost.toFixed(4) }}</span></span>
            </div>
            <div v-if="item.inputCost !== null" class="text-xs font-mono text-slate-400">
              ${{ item.inputCost.toFixed(2) }} / ${{ item.outputCost!.toFixed(2) }} per 1M
            </div>
          </div>
        </div>
        <div v-if="ledgerRows.length > 0" class="hidden lg:block overflow-x-auto">
          <table class="w-full text-left border-collapse text-xs min-w-[640px]">
            <thead>
              <tr class="border-b border-botex-800 bg-botex-950/60 text-slate-300 font-mono text-xs">
                <th class="py-2.5 px-4 font-semibold">Model</th>
                <th class="py-2.5 px-4 font-semibold">Role</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colRuns') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colTokens') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colCacheSave') }}</th>
                <th class="py-2.5 px-4 font-semibold">{{ t('colCost') }}</th>
                <th class="py-2.5 px-4 font-semibold text-right">Rates (In/Out 1M)</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-botex-800 font-mono text-slate-200">
              <tr
                v-for="item in ledgerRows"
                :key="item.id"
                class="hover:bg-botex-800/40 transition"
              >
                <td class="py-2.5 px-4 min-w-[170px]">
                  <div class="font-medium text-slate-100 break-all">{{ item.name }}</div>
                  <div v-if="item.name !== item.id" class="text-xs text-slate-300 break-all">{{ item.id }}</div>
                </td>
                <td class="py-2.5 px-4">
                  <Badge v-if="item.role" :variant="roleBadgeVariant(item.role)">{{ item.role }}</Badge>
                  <span v-else class="text-slate-400">—</span>
                </td>
                <td class="py-2.5 px-4 text-slate-100">{{ item.runs }}</td>
                <td class="py-2.5 px-4 text-slate-100">{{ item.tokens.toLocaleString() }}</td>
                <td class="py-2.5 px-4 text-slate-300">
                  <template v-if="item.cachedTokens > 0">{{ item.cachePct }}% · ~${{ item.savings.toFixed(2) }}</template>
                  <template v-else>—</template>
                </td>
                <td class="py-2.5 px-4 text-emerald-300 font-semibold">${{ item.cost.toFixed(4) }}</td>
                <td class="py-2.5 px-4 text-right text-slate-300 text-xs">
                  <template v-if="item.inputCost !== null">${{ item.inputCost.toFixed(2) }} / ${{ item.outputCost!.toFixed(2) }}</template>
                  <template v-else>—</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Recommended Models by Role -->
    <div class="space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-300 px-1 border-b border-botex-800 pb-2">
        <span class="font-semibold uppercase tracking-wider font-mono">{{ t('recommendedRoster') }}</span>
        
        <!-- Role Filter Tabs -->
        <div class="flex items-center bg-botex-950 p-0.5 rounded-lg border border-botex-800 space-x-1">
          <button
            v-for="roleName in ['all', 'planner', 'coder', 'chat', 'reviewer'] as const"
            :key="roleName"
            type="button"
            :class="[
              'min-h-[44px] lg:min-h-[36px] min-w-[44px] justify-center px-2.5 py-1 rounded text-xs font-medium capitalize transition',
              selectedRole === roleName
                ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30'
                : 'text-slate-300 hover:text-white'
            ]"
            @click="selectedRole = roleName"
          >
            {{ roleName }}
          </button>
        </div>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div
          v-for="model in filteredModels"
          :key="model.id"
          class="rounded-xl border border-botex-800 bg-botex-900 p-4 hover:border-slate-600 transition shadow-sm space-y-3"
        >
          <div class="flex items-start justify-between">
            <div>
              <div class="flex items-center space-x-2">
                <h4 class="text-sm font-semibold text-slate-100">{{ model.name }}</h4>
                <Badge :variant="roleBadgeVariant(model.role)">{{ model.role }}</Badge>
              </div>
              <p class="text-xs font-mono text-cyan-300 mt-0.5">{{ model.id }}</p>
            </div>
            <span class="text-xs text-slate-300 font-mono">{{ model.provider }}</span>
          </div>

          <p class="text-xs text-slate-200">{{ model.description }}</p>

          <div class="pt-2 border-t border-botex-800 grid grid-cols-3 gap-2 text-center text-xs font-mono">
            <div class="bg-botex-950 p-2 rounded-lg border border-botex-800 min-w-0">
              <span class="block text-[10px] sm:text-xs text-slate-300 uppercase truncate">Context</span>
              <span class="text-slate-100 font-semibold">{{ formatContext(model.context_length) }}</span>
            </div>
            <div class="bg-botex-950 p-2 rounded-lg border border-botex-800 min-w-0">
              <span class="block text-[10px] sm:text-xs text-slate-300 uppercase truncate">Input / 1M</span>
              <span class="text-slate-100 font-semibold">${{ model.input_cost_per_m.toFixed(2) }}</span>
            </div>
            <div class="bg-botex-950 p-2 rounded-lg border border-botex-800 min-w-0">
              <span class="block text-[10px] sm:text-xs text-slate-300 uppercase truncate">Output / 1M</span>
              <span class="text-slate-100 font-semibold">${{ model.output_cost_per_m.toFixed(2) }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useWalletStore } from '../stores/wallet'
import { useAnalyticsStore } from '../stores/analytics'
import { useI18n } from '../i18n'
import type { AnalyticsResponse } from '../api/types'
import Badge from '../components/common/Badge.vue'
import StatsPanel from '../components/stats/StatsPanel.vue'
import {
  Wallet as WalletIcon,
  RefreshCw as RefreshCwIcon,
  CreditCard as CreditCardIcon,
  TrendingUp as TrendingUpIcon,
  Shield as ShieldIcon,
  Clock as ClockIcon
} from 'lucide-vue-next'

const walletStore = useWalletStore()
const analyticsStore = useAnalyticsStore()
const { walletInfo, recommendedModels } = storeToRefs(walletStore)
const { locale, t } = useI18n()

const selectedRole = ref<'all' | 'planner' | 'coder' | 'chat' | 'reviewer'>('all')

const activeTab = ref<'budget' | 'stats'>('budget')
const tabs: { value: 'budget' | 'stats'; label: string }[] = [
  { value: 'budget', label: 'tabBudget' },
  { value: 'stats', label: 'tabStats' }
]

const filteredModels = computed(() => {
  if (selectedRole.value === 'all') return recommendedModels.value
  return recommendedModels.value.filter((m) => m.role === selectedRole.value)
})

// Real per-model telemetry from the analytics ledger (lifetime), joined with
// the catalog for role badges and list pricing. Models never used never appear.
const ledgerStats = ref<AnalyticsResponse | null>(null)

const ledgerRows = computed(() => {
  const rows = (ledgerStats.value?.by_model || []).map((s) => {
    const catalog = recommendedModels.value.find((m) => m.id === s.model)
    return {
      id: s.model,
      name: catalog?.name || s.model,
      role: catalog?.role || null,
      runs: s.runs,
      tokens: s.tokens,
      cachedTokens: s.cached_tokens,
      cachePct: Math.round(
        s.cache_hit_pct ??
          ((s.prompt_tokens || 0) > 0 ? (s.cached_tokens / (s.prompt_tokens || 1)) * 100 : 0)
      ),
      savings: s.cache_savings_usd,
      cost: s.cost_usd,
      inputCost: catalog ? catalog.input_cost_per_m : null,
      outputCost: catalog ? catalog.output_cost_per_m : null
    }
  })
  return rows.sort((a, b) => b.cost - a.cost)
})

onMounted(async () => {
  walletStore.fetchWallet()
  ledgerStats.value = await analyticsStore.fetchPeriodStats('all')
})

function roleBadgeVariant(role: string) {
  switch (role) {
    case 'coder':
      return 'info'
    case 'planner':
      return 'success'
    case 'reviewer':
      return 'warning'
    default:
      return 'default'
  }
}

function formatContext(tokens: number) {
  if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`
  return `${Math.round(tokens / 1024)}k`
}
</script>
