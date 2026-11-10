import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api/client'
import type { WalletInfo, KeyStatusInfo, RateLimitsMap, RecommendedModel } from '../api/types'

export const useWalletStore = defineStore('wallet', () => {
  const activeProvider = ref<string>('openrouter')
  const walletByProvider = ref<Record<string, WalletInfo>>({})
  const keyStatusByProvider = ref<Record<string, KeyStatusInfo>>({})
  const lastFetchByProvider = ref<Record<string, number>>({})
  const CACHE_TTL_MS = 30000 // 30s cache TTL

  const walletInfo = computed<WalletInfo | null>(() => walletByProvider.value[activeProvider.value] || null)
  const keyStatus = computed<KeyStatusInfo | null>(() => keyStatusByProvider.value[activeProvider.value] || null)
  const rateLimits = ref<RateLimitsMap | null>(null)
  const isLoading = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)

  const isBudgetLow = computed<boolean>(() => {
    const w = walletInfo.value
    const k = keyStatus.value
    const units = (w?.units ?? k?.units ?? 'usd').toLowerCase()
    if (units !== 'usd') {
      return false
    }
    const bal = w?.balance_usd ?? k?.balance_usd
    return bal !== null && bal !== undefined && bal < 1.0
  })

  // Curated model roster based on BoteX benchmark recommendations (empty by default, loaded from backend)
  const recommendedModels = ref<RecommendedModel[]>([])

  async function fetchWallet(forceRefresh: boolean = false, provider?: string) {
    const targetProv = provider || activeProvider.value
    const now = Date.now()
    const lastFetch = lastFetchByProvider.value[targetProv] || 0

    if (!forceRefresh && walletByProvider.value[targetProv] && now - lastFetch < CACHE_TTL_MS) {
      return
    }

    isLoading.value = true
    errorMessage.value = null
    try {
      const [wData, kData, rData] = await Promise.all([
        api.getWallet(targetProv, forceRefresh),
        api.getKeyStatus(targetProv, forceRefresh).catch(() => null),
        api.getRateLimits().catch(() => null)
      ])
      walletByProvider.value[targetProv] = wData
      if (kData) {
        keyStatusByProvider.value[targetProv] = kData
      }
      if (rData) {
        rateLimits.value = rData
      }
      lastFetchByProvider.value[targetProv] = Date.now()
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to fetch wallet info'
    } finally {
      isLoading.value = false
    }
  }

  async function fetchKeyStatus(forceRefresh: boolean = false, provider?: string) {
    const targetProv = provider || activeProvider.value
    try {
      const kData = await api.getKeyStatus(targetProv, forceRefresh)
      if (kData) {
        keyStatusByProvider.value[targetProv] = kData
      }
    } catch {
      // keep previous state on transient network error
    }
  }

  async function setProvider(provider: string) {
    if (!provider) return
    activeProvider.value = provider
    await fetchWallet(false, provider)
  }

  return {
    activeProvider,
    walletInfo,
    keyStatus,
    walletByProvider,
    keyStatusByProvider,
    lastFetchByProvider,
    rateLimits,
    isBudgetLow,
    isLoading,
    errorMessage,
    recommendedModels,
    fetchWallet,
    fetchKeyStatus,
    setProvider
  }
})

