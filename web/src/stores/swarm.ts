import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, ApiError } from '../api/client'
import type {
  BotInfo,
  BotBrief,
  BotLease,
  ConnectedDeviceInfo,
  HostHarnessInfo,
  WorkOrderInfo
} from '../api/types'

export type BriefSaveResult =
  | { ok: true; brief: BotBrief }
  | { ok: false; conflict: boolean; code?: string; currentEtag?: string; message: string }

export type ActionResult = { ok: boolean; message?: string }

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.detail || e.message
  if (e instanceof Error) return e.message
  return String(e)
}

export const useSwarmStore = defineStore('swarm', () => {
  // --- Teammates (bots) -----------------------------------------------------
  const bots = ref<BotInfo[]>([])
  const activeLeasesCount = ref<number>(0)
  const selectedBotSlug = ref<string | null>(null)
  const selectedBrief = ref<BotBrief | null>(null)
  const selectedLeases = ref<BotLease[]>([])
  const briefMissing = ref<boolean>(false)
  const loadingBots = ref<boolean>(false)
  const loadingBrief = ref<boolean>(false)
  const savingBrief = ref<boolean>(false)
  const refreshingBrief = ref<boolean>(false)
  const togglingBotSlug = ref<string | null>(null)

  // --- Edge hosts -----------------------------------------------------------
  const devices = ref<ConnectedDeviceInfo[]>([])
  const harnessesByHost = ref<Record<string, HostHarnessInfo[]>>({})
  const leasesByHost = ref<Record<string, BotLease[]>>({})
  const loadingHosts = ref<boolean>(false)
  const loadingLeases = ref<boolean>(false)
  const loadingHarnessHost = ref<Set<string>>(new Set())
  const harnessErrors = ref<Record<string, string>>({})

  // --- Work orders ----------------------------------------------------------
  const orders = ref<WorkOrderInfo[]>([])
  const loadingOrders = ref<boolean>(false)
  const mutatingOrderId = ref<string | null>(null)
  let lastOrdersParams: { status?: string; bot_slug?: string; limit?: number } | undefined

  const error = ref<string | null>(null)
  const briefError = ref<string | null>(null)

  const selectedBot = computed<BotInfo | null>(
    () => bots.value.find((b) => b.slug === selectedBotSlug.value) ?? null
  )

  function upsertBotRow(row: BotInfo): void {
    const idx = bots.value.findIndex((b) => b.slug === row.slug)
    if (idx >= 0) bots.value[idx] = { ...bots.value[idx], ...row }
    else bots.value.push(row)
    bots.value.sort((a, b) => a.slug.localeCompare(b.slug))
  }

  function clearSelection(): void {
    selectedBotSlug.value = null
    selectedBrief.value = null
    selectedLeases.value = []
    briefMissing.value = false
    briefError.value = null
  }

  async function fetchBots(params?: { namespace?: string; status?: string }): Promise<void> {
    loadingBots.value = true
    error.value = null
    try {
      const res = await api.listBots(params)
      bots.value = res.bots ?? []
      activeLeasesCount.value = res.active_leases_count ?? 0
      if (selectedBotSlug.value && !bots.value.some((b) => b.slug === selectedBotSlug.value)) {
        clearSelection()
      }
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      loadingBots.value = false
    }
  }

  async function selectBot(slug: string | null, opts?: { force?: boolean }): Promise<void> {
    if (!opts?.force && slug === selectedBotSlug.value && (selectedBrief.value || briefMissing.value)) return
    selectedBotSlug.value = slug
    selectedBrief.value = null
    selectedLeases.value = []
    briefMissing.value = false
    briefError.value = null
    if (!slug) return

    loadingBrief.value = true
    try {
      const [detailRes, briefRes] = await Promise.allSettled([api.getBot(slug), api.getBotBrief(slug)])
      if (selectedBotSlug.value !== slug) return
      if (detailRes.status === 'fulfilled') {
        upsertBotRow(detailRes.value.bot)
        selectedLeases.value = detailRes.value.leases ?? []
      }
      if (briefRes.status === 'fulfilled') {
        selectedBrief.value = briefRes.value
      } else {
        const e = briefRes.reason
        if (e instanceof ApiError && e.status === 404) briefMissing.value = true
        else briefError.value = errorMessage(e)
      }
    } finally {
      if (selectedBotSlug.value === slug) loadingBrief.value = false
    }
  }

  async function saveBrief(content: string, pinned: boolean, opts?: { force?: boolean }): Promise<BriefSaveResult> {
    const slug = selectedBotSlug.value
    if (!slug) return { ok: false, conflict: false, message: 'No teammate selected.' }
    savingBrief.value = true
    try {
      const brief = await api.updateBotBrief(slug, { content, pinned }, opts?.force ? undefined : selectedBrief.value?.etag)
      if (selectedBotSlug.value === slug) {
        selectedBrief.value = brief
        briefMissing.value = false
      }
      return { ok: true, brief }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const payload = e.payload as { error?: string; current_etag?: string } | undefined
        return {
          ok: false,
          conflict: true,
          code: payload?.error,
          currentEtag: payload?.current_etag,
          message: e.detail || 'The brief changed on the server while you were editing.'
        }
      }
      if (e instanceof ApiError && e.status === 422) {
        const payload = e.payload as { error?: string; tokens?: number; budget?: number } | undefined
        let message = e.detail || 'The server rejected the brief content.'
        if (payload?.error === 'BRIEF_TOO_LARGE' && typeof payload.tokens === 'number' && typeof payload.budget === 'number') {
          message = `Brief too large: ~${payload.tokens} tokens vs budget ${payload.budget}.`
        }
        return { ok: false, conflict: false, code: payload?.error, message }
      }
      return { ok: false, conflict: false, message: errorMessage(e) }
    } finally {
      savingBrief.value = false
    }
  }

  async function refreshBrief(force: boolean = false): Promise<ActionResult> {
    const slug = selectedBotSlug.value
    if (!slug) return { ok: false, message: 'No teammate selected.' }
    refreshingBrief.value = true
    try {
      const brief = await api.refreshBotBrief(slug, force)
      if (selectedBotSlug.value === slug) {
        selectedBrief.value = brief
        briefMissing.value = false
      }
      return { ok: true }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const payload = e.payload as { status?: string } | undefined
        if (payload?.status === 'already_running') {
          return { ok: false, message: 'A synthesis is already running for this teammate — check back shortly.' }
        }
      }
      return { ok: false, message: errorMessage(e) }
    } finally {
      refreshingBrief.value = false
    }
  }

  async function toggleBotPause(bot: BotInfo): Promise<ActionResult> {
    togglingBotSlug.value = bot.slug
    error.value = null
    try {
      const { ok: _ok, ...row } =
        bot.status === 'active' ? await api.pauseBot(bot.slug) : await api.resumeBot(bot.slug)
      upsertBotRow(row)
      return { ok: true }
    } catch (e) {
      return { ok: false, message: errorMessage(e) }
    } finally {
      togglingBotSlug.value = null
    }
  }

  // --- Edge hosts -----------------------------------------------------------

  async function fetchHosts(): Promise<void> {
    loadingHosts.value = true
    error.value = null
    try {
      const res = await api.listConnectedDevices()
      devices.value = res.devices ?? []
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      loadingHosts.value = false
    }
  }

  async function fetchHostHarnesses(hostId: string, staleAfterS: number = 120): Promise<void> {
    loadingHarnessHost.value.add(hostId)
    const { [hostId]: _prev, ...restErrors } = harnessErrors.value
    harnessErrors.value = restErrors
    try {
      const res = await api.listHostHarnesses(hostId, staleAfterS)
      harnessesByHost.value = { ...harnessesByHost.value, [hostId]: res.harnesses ?? [] }
    } catch (e) {
      harnessesByHost.value = { ...harnessesByHost.value, [hostId]: [] }
      harnessErrors.value = { ...harnessErrors.value, [hostId]: errorMessage(e) }
    } finally {
      loadingHarnessHost.value.delete(hostId)
    }
  }

  // Aggregates per-bot leases into a host-keyed summary. Non-fatal: hosts
  // render fine without it when the roster lookup fails.
  async function fetchLeases(): Promise<void> {
    loadingLeases.value = true
    try {
      if (!bots.value.length) await fetchBots()
      const lists = await Promise.all(
        bots.value.slice(0, 50).map(async (b) => {
          try {
            const detail = await api.getBot(b.slug)
            return detail.leases ?? []
          } catch {
            return [] as BotLease[]
          }
        })
      )
      const byHost: Record<string, BotLease[]> = {}
      for (const leases of lists) {
        for (const lease of leases) {
          ;(byHost[lease.host_id] ||= []).push(lease)
        }
      }
      leasesByHost.value = byHost
      const total = Object.values(byHost).reduce((n, ls) => n + ls.length, 0)
      if (total > 0 || activeLeasesCount.value === 0) activeLeasesCount.value = total
    } catch {
      // keep previous state on transient failure
    } finally {
      loadingLeases.value = false
    }
  }

  // --- Work orders ----------------------------------------------------------

  function applyOrderPatch(orderId: string, patch: Partial<WorkOrderInfo>): void {
    const idx = orders.value.findIndex((o) => o.order_id === orderId)
    if (idx >= 0) orders.value[idx] = { ...orders.value[idx], ...patch }
  }

  async function fetchOrders(params?: { status?: string; bot_slug?: string; limit?: number }): Promise<void> {
    loadingOrders.value = true
    error.value = null
    lastOrdersParams = params
    try {
      const res = await api.listWorkOrders(params)
      orders.value = res.orders ?? []
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      loadingOrders.value = false
    }
  }

  async function mutateOrder(
    orderId: string,
    fn: () => Promise<{ ok: boolean; order?: WorkOrderInfo; control_dispatch?: unknown }>
  ): Promise<ActionResult> {
    mutatingOrderId.value = orderId
    error.value = null
    try {
      const res = await fn()
      if (res.order) applyOrderPatch(orderId, res.order)
      else await fetchOrders(lastOrdersParams)
      return { ok: true }
    } catch (e) {
      return { ok: false, message: errorMessage(e) }
    } finally {
      mutatingOrderId.value = null
    }
  }

  async function pauseOrder(orderId: string, body?: { reason?: string; host_id?: string; fencing_token?: string; fencing_epoch?: number }): Promise<ActionResult> {
    return mutateOrder(orderId, () => api.pauseWorkOrder(orderId, body))
  }

  async function resumeOrder(orderId: string, body?: { reason?: string; host_id?: string; fencing_token?: string; fencing_epoch?: number }): Promise<ActionResult> {
    return mutateOrder(orderId, () => api.resumeWorkOrder(orderId, body))
  }

  async function cancelOrder(orderId: string, body?: { reason?: string; host_id?: string; fencing_token?: string; fencing_epoch?: number }): Promise<ActionResult> {
    return mutateOrder(orderId, () => api.cancelWorkOrder(orderId, body))
  }

  async function fenceOrder(orderId: string, body: { expected_epoch: number; reason?: string; status?: string }): Promise<ActionResult> {
    return mutateOrder(orderId, () => api.fenceWorkOrder(orderId, body))
  }

  return {
    // teammates
    bots,
    activeLeasesCount,
    selectedBot,
    selectedBotSlug,
    selectedBrief,
    selectedLeases,
    briefMissing,
    loadingBots,
    loadingBrief,
    savingBrief,
    refreshingBrief,
    togglingBotSlug,
    briefError,
    // hosts
    devices,
    harnessesByHost,
    leasesByHost,
    loadingHosts,
    loadingLeases,
    loadingHarnessHost,
    harnessErrors,
    // orders
    orders,
    loadingOrders,
    mutatingOrderId,
    // shared
    error,
    fetchBots,
    selectBot,
    saveBrief,
    refreshBrief,
    toggleBotPause,
    fetchHosts,
    fetchHostHarnesses,
    fetchLeases,
    fetchOrders,
    pauseOrder,
    resumeOrder,
    cancelOrder,
    fenceOrder
  }
})
