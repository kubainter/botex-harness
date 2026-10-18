import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, getStoredAccessToken, getStoredBackendUrl, setStoredAccessToken, setStoredBackendUrl } from '../api/client'
import { wsManager, type ConnectionStatus } from '../api/ws'
import type { HealthStatus } from '../api/types'

export const useAuthStore = defineStore('auth', () => {
  const backendUrl = ref<string>(getStoredBackendUrl())
  const accessToken = ref<string>(getStoredAccessToken())
  const isOnline = ref<boolean>(navigator.onLine)
  const isServerHealthy = ref<boolean>(false)
  const healthData = ref<HealthStatus | null>(null)
  const wsStatus = ref<ConnectionStatus>('DISCONNECTED')
  const isChecking = ref<boolean>(false)
  const lastCheckTime = ref<number>(0)
  const errorMessage = ref<string | null>(null)

  function updateBackendUrl(url: string) {
    backendUrl.value = url
    setStoredBackendUrl(url)
    wsManager.reconnect()
    checkHealth()
  }

  function updateAccessToken(token: string) {
    accessToken.value = token
    setStoredAccessToken(token)
    wsManager.reconnect()
    checkHealth()
  }

  async function checkHealth(): Promise<boolean> {
    isChecking.value = true
    errorMessage.value = null
    try {
      const data = await api.getHealth()
      healthData.value = data
      isServerHealthy.value = data.status === 'healthy'
      lastCheckTime.value = Date.now()
      return true
    } catch (err: any) {
      isServerHealthy.value = false
      errorMessage.value = err?.message || 'Server unreachable'
      return false
    } finally {
      isChecking.value = false
    }
  }

  function init() {
    window.addEventListener('online', () => {
      isOnline.value = true
      checkHealth()
      wsManager.connect()
    })

    window.addEventListener('offline', () => {
      isOnline.value = false
      isServerHealthy.value = false
      wsManager.disconnect()
    })

    wsManager.onStatusChange((status) => {
      wsStatus.value = status
    })

    // Initial check and socket connection
    checkHealth()
    wsManager.connect()

    // Poll health periodically every 30s
    setInterval(() => {
      if (document.visibilityState === 'visible') {
        checkHealth()
      }
    }, 30000)
  }

  return {
    backendUrl,
    accessToken,
    isOnline,
    isServerHealthy,
    healthData,
    wsStatus,
    isChecking,
    lastCheckTime,
    errorMessage,
    updateBackendUrl,
    updateAccessToken,
    checkHealth,
    init
  }
})
