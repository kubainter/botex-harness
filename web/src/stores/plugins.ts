import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { apiClient } from '../api/client'
import type { PluginDefinition } from '../api/types'

export const usePluginsStore = defineStore('plugins', () => {
  const localPlugins = ref<PluginDefinition[]>([])
  const globalPlugins = ref<PluginDefinition[]>([])
  const stagedPlugins = ref<PluginDefinition[]>([])
  const indexMarkdown = ref<{ local: string; global: string }>({
    local: '',
    global: ''
  })
  const isLoading = ref(false)
  const errorMessage = ref<string | null>(null)

  const stagedCount = computed(() => stagedPlugins.value.length)

  async function fetchPlugins(workspaceDir: string = '.'): Promise<void> {
    isLoading.value = true
    errorMessage.value = null
    try {
      const res = await apiClient.getPlugins(workspaceDir)
      if (!res.ok) throw new Error('Failed to load plugins catalog.')
      localPlugins.value = res.plugins?.local || []
      globalPlugins.value = res.plugins?.global || []
      stagedPlugins.value = res.plugins?.staging || []
      indexMarkdown.value = {
        local: res.index_markdown?.local || '',
        global: res.index_markdown?.global || ''
      }
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to load plugins catalog.'
    } finally {
      isLoading.value = false
    }
  }

  async function resolveStagedPlugin(
    name: string,
    action: 'local' | 'global' | 'discard',
    workspaceDir: string = '.'
  ): Promise<boolean> {
    errorMessage.value = null
    try {
      const res = await apiClient.resolvePlugin({
        name,
        action,
        workspace_dir: workspaceDir
      })
      if (res.ok) {
        await fetchPlugins(workspaceDir)
        return true
      }
      errorMessage.value = `Failed to resolve plugin '${name}': ${res.status || 'request rejected'}.`
      return false
    } catch (err: any) {
      errorMessage.value = err?.message || `Failed to resolve plugin '${name}'.`
      return false
    }
  }

  return {
    localPlugins,
    globalPlugins,
    stagedPlugins,
    indexMarkdown,
    stagedCount,
    isLoading,
    errorMessage,
    fetchPlugins,
    resolveStagedPlugin
  }
})