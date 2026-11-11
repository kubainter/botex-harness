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
  const recommendedLoaded = ref<Record<string, boolean>>({})

  const ROLE_TASK_MAP: Record<RecommendedModel['role'], string> = {
    planner: 'architecture',
    coder: 'coding',
    chat: 'general',
    reviewer: 'review'
  }

  async function fetchRecommended(
    role: 'all' | RecommendedModel['role'] = 'all',
    forceRefresh: boolean = false
  ) {
    const roles = (role === 'all' ? Object.keys(ROLE_TASK_MAP) : [role]) as RecommendedModel['role'][]
    const pending = roles.filter((r) => forceRefresh || !recommendedLoaded.value[r])
    if (!pending.length) return
    await Promise.all(
      pending.map(async (r) => {
        try {
          const raw = await api.getRecommendedModels(ROLE_TASK_MAP[r], 5, 'auto')
          const mapped: RecommendedModel[] = raw
            .filter((m: any) => m && typeof m === 'object' && m.id && !m.error)
            .map((m: any) => ({
              id: String(m.id),
              name: String(m.name || m.id),
              provider: String(m.provider || ''),
              role: r,
              description: String(m.description || ''),
              context_length: Number(m.context_length || 0),
              input_cost_per_m: Number((m.pricing as any)?.prompt_1M ?? 0),
              output_cost_per_m: Number((m.pricing as any)?.completion_1M ?? 0)
            }))
          recommendedModels.value = [
            ...recommendedModels.value.filter((m) => m.role !== r),
            ...mapped
          ]
          recommendedLoaded.value[r] = true
        } catch {
          // Transient failure — leave unloaded so a later view can retry
        }
      })
    )
  }

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
    fetchRecommended,
    fetchWallet,
    fetchKeyStatus,
    setProvider
  }
})

