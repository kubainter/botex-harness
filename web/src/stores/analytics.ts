import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api/client'
import type { AnalyticsResponse } from '../api/types'

export type AnalyticsPeriod = 'week' | 'month' | 'all'

export const useAnalyticsStore = defineStore('analytics', () => {
  const stats = ref<AnalyticsResponse | null>(null)
  const period = ref<AnalyticsPeriod>('week')
  const isLoading = ref<boolean>(false)
  const isDigestRunning = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)
  const digestMessage = ref<string | null>(null)

  async function fetchStats(nextPeriod?: AnalyticsPeriod, forceRefresh = false) {
    if (nextPeriod) period.value = nextPeriod
    if (isLoading.value && !forceRefresh) return
    isLoading.value = true
    errorMessage.value = null
    try {
      stats.value = await api.getAnalytics(period.value)
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to fetch analytics'
    } finally {
      isLoading.value = false
    }
  }

  // Fetch a period without touching the selected `period`/`stats` state —
  // used by views that need a fixed window (e.g. the wallet ledger wants 'all').
  async function fetchPeriodStats(targetPeriod: AnalyticsPeriod): Promise<AnalyticsResponse | null> {
    try {
      return await api.getAnalytics(targetPeriod)
    } catch {
      return null
    }
  }

  async function runDigest() {
    if (isDigestRunning.value) return
    isDigestRunning.value = true
    digestMessage.value = null
    try {
      const res = await api.triggerAnalyticsDigest()
      if (res.ok) {
        digestMessage.value = 'Digest updated'
        await fetchStats(undefined, true)
      } else {
        digestMessage.value = res.error || 'Digest failed'
      }
    } catch (err: any) {
      digestMessage.value = err?.message || 'Digest failed'
    } finally {
      isDigestRunning.value = false
    }
  }

  return {
    stats,
    period,
    isLoading,
    isDigestRunning,
    errorMessage,
    digestMessage,
    fetchStats,
    fetchPeriodStats,
    runDigest
  }
})
