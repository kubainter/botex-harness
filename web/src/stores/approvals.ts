import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api/client'
import { wsManager } from '../api/ws'
import type { PendingAction, ApprovalDecisionRequest } from '../api/types'

export const useApprovalsStore = defineStore('approvals', () => {
  const pendingActions = ref<PendingAction[]>([])
  const activeModalAction = ref<PendingAction | null>(null)
  const isLoading = ref<boolean>(false)
  const isSubmitting = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)

  const count = computed(() => pendingActions.value.length)

  function openModal(action: PendingAction) {
    activeModalAction.value = action
  }

  function closeModal() {
    activeModalAction.value = null
  }

  function handleRealtimeApproval(action: PendingAction) {
    // Avoid duplicates
    const idx = pendingActions.value.findIndex((a) => a.action_id === action.action_id)
    if (idx >= 0) {
      pendingActions.value[idx] = action
    } else {
      pendingActions.value.unshift(action)
    }

    // Auto-open modal if none is currently focused
    if (!activeModalAction.value) {
      activeModalAction.value = action
    }

    // Try desktop notification if permitted
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      try {
        new Notification(`BoteX Approval Needed: ${action.tool_name}`, {
          body: `Risk: ${action.risk_level || 'UNKNOWN'}. Action requires operator sign-off.`,
          icon: '/icon-192.png'
        })
      } catch {}
    }
  }

  async function fetchApprovals(taskId?: string) {
    isLoading.value = true
    errorMessage.value = null
    try {
      const res = await api.listApprovals(taskId)
      pendingActions.value = res.pending_actions || []
      // If active modal action is no longer pending, close it
      if (activeModalAction.value) {
        const stillExists = pendingActions.value.some((a) => a.action_id === activeModalAction.value?.action_id)
        if (!stillExists) {
          activeModalAction.value = null
        }
      }
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to fetch pending approvals'
    } finally {
      isLoading.value = false
    }
  }

  async function decide(actionId: string, decision: ApprovalDecisionRequest): Promise<boolean> {
    isSubmitting.value = true
    try {
      await api.decideApproval(actionId, decision)
      pendingActions.value = pendingActions.value.filter((a) => a.action_id !== actionId)
      if (activeModalAction.value?.action_id === actionId) {
        activeModalAction.value = null
      }
      return true
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to submit decision'
      return false
    } finally {
      isSubmitting.value = false
    }
  }

  function init() {
    wsManager.onApproval((action) => {
      handleRealtimeApproval(action)
    })
    fetchApprovals()

    // Request notification permission if default
    if (typeof Notification !== 'undefined' && Notification.permission === 'default') {
      Notification.requestPermission().catch(() => {})
    }
  }

  return {
    pendingActions,
    activeModalAction,
    count,
    isLoading,
    isSubmitting,
    errorMessage,
    openModal,
    closeModal,
    fetchApprovals,
    decide,
    handleRealtimeApproval,
    init
  }
})
