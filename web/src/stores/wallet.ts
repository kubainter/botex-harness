import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api/client'
import type { WalletInfo, KeyStatusInfo, RecommendedModel } from '../api/types'

export const useWalletStore = defineStore('wallet', () => {
  const walletInfo = ref<WalletInfo | null>(null)
  const keyStatus = ref<KeyStatusInfo | null>(null)
  const isLoading = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)

  const isBudgetLow = computed<boolean>(() => {
    const bal = walletInfo.value?.balance_usd ?? keyStatus.value?.balance_usd
    return bal !== null && bal !== undefined && bal < 1.0
  })

  // Curated model roster based on BoteX benchmark recommendations
  const recommendedModels = ref<RecommendedModel[]>([
    {
      id: 'openrouter/pareto-code',
      name: 'OpenRouter Pareto Code',
      provider: 'OpenRouter Meta-Router',
      role: 'coder',
      description: 'Dynamic Pareto-optimal coding router balancing SWE-bench accuracy, latency, and cost.',
      context_length: 200000,
      input_cost_per_m: 1.50,
      output_cost_per_m: 6.00
    },
    {
      id: 'google/gemini-2.5-flash',
      name: 'Gemini 2.5 Flash',
      provider: 'Google / OpenRouter',
      role: 'chat',
      description: 'Low-latency 1M context model for repository-wide analysis and quick diffs.',
      context_length: 1048576,
      input_cost_per_m: 0.075,
      output_cost_per_m: 0.30
    },
    {
      id: 'qwen/qwen3-coder',
      name: 'Qwen 3 Coder',
      provider: 'Qwen / OpenRouter',
      role: 'coder',
      description: 'Primary autonomous coding model with high multi-file patch accuracy.',
      context_length: 131072,
      input_cost_per_m: 0.20,
      output_cost_per_m: 0.60
    },
    {
      id: 'deepseek/deepseek-r1',
      name: 'DeepSeek R1',
      provider: 'DeepSeek / OpenRouter',
      role: 'planner',
      description: 'Chain-of-thought reasoning model for system design, debugging, and TDD.',
      context_length: 65536,
      input_cost_per_m: 0.55,
      output_cost_per_m: 2.19
    },
    {
      id: 'anthropic/claude-sonnet-4',
      name: 'Claude Sonnet 4',
      provider: 'Anthropic / OpenRouter',
      role: 'reviewer',
      description: 'Flagship code review, static analysis fixes, and architectural security auditing.',
      context_length: 200000,
      input_cost_per_m: 3.00,
      output_cost_per_m: 15.00
    }
  ])

  async function fetchWallet(forceRefresh: boolean = false) {
    isLoading.value = true
    errorMessage.value = null
    try {
      const [wData, kData] = await Promise.all([
        api.getWallet('openrouter', forceRefresh),
        api.getKeyStatus('openrouter', forceRefresh).catch(() => null)
      ])
      walletInfo.value = wData
      if (kData) {
        keyStatus.value = kData
      }
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to fetch wallet info'
    } finally {
      isLoading.value = false
    }
  }

  async function fetchKeyStatus(forceRefresh: boolean = false) {
    try {
      keyStatus.value = await api.getKeyStatus('openrouter', forceRefresh)
    } catch {
      // keep previous state on transient network error
    }
  }

  return {
    walletInfo,
    keyStatus,
    isBudgetLow,
    isLoading,
    errorMessage,
    recommendedModels,
    fetchWallet,
    fetchKeyStatus
  }
})

