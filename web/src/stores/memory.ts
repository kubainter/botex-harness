import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api/client'
import type { MemoryEntry, MemorySaveRequest, MemoryScope, GraphNode, GraphEdge, GraphHealth, BacklinkItem } from '../api/types'

export const useMemoryStore = defineStore('memory', () => {
  const entries = ref<MemoryEntry[]>([])
  const activeEntry = ref<MemoryEntry | null>(null)
  const activeScope = ref<MemoryScope>('all')
  const searchQuery = ref<string>('')
  const filterKind = ref<string>('')
  const filterTag = ref<string>('')
  const isLoading = ref<boolean>(false)
  const isSaving = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)

  // Graph and Backlinks State
  const viewMode = ref<'cards' | 'graph'>('cards')
  const graphNodes = ref<GraphNode[]>([])
  const graphEdges = ref<GraphEdge[]>([])
  const graphHealth = ref<GraphHealth | null>(null)
  const isGraphLoading = ref<boolean>(false)
  const activeBacklinks = ref<BacklinkItem[]>([])

  async function fetchMemory() {
    isLoading.value = true
    errorMessage.value = null
    try {
      const res = await api.getMemory({
        query: searchQuery.value,
        tag: filterTag.value,
        kind: filterKind.value,
        scope: activeScope.value
      })
      const rawList = res.memories || res.results || []
      entries.value = rawList.map((m: any) => ({
        ...m,
        memory_id: m.memory_id || m.id || '',
        content: m.content || m.preview || '',
      }))
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to search Memory Vault'
      entries.value = []
    } finally {
      isLoading.value = false
    }
  }

  async function loadEntry(memoryId: string, scope: string = 'local'): Promise<MemoryEntry | null> {
    isLoading.value = true
    try {
      const res = (await api.getMemory({
        memory_id: memoryId,
        scope
      })) as any
      if (res && (res.entry || res.id || res.metadata)) {
        let cleanContent = res.content || res.entry?.content || ''
        cleanContent = cleanContent
          .replace(/<untrusted_memory_content[^>]*>\n?/, '')
          .replace(/\n?<\/untrusted_memory_content>/, '')

        const entry: MemoryEntry = res.entry || {
          memory_id: res.id || memoryId,
          title: res.metadata?.title || res.id || memoryId,
          content: cleanContent,
          kind: res.metadata?.kind || 'context',
          tags: res.metadata?.tags || [],
          links: res.metadata?.links || [],
          status: res.metadata?.status || 'active',
          compiled_truth: Boolean(res.metadata?.compiled_truth),
          timestamp: res.metadata?.timestamp || '',
          scope: res.scope || scope
        }
        entry.content = cleanContent
        activeEntry.value = entry
        return entry
      }
      return null
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to read memory entry'
      return null
    } finally {
      isLoading.value = false
    }
  }

  async function saveEntry(req: MemorySaveRequest): Promise<boolean> {
    isSaving.value = true
    errorMessage.value = null
    try {
      const res = await api.saveMemory(req)
      if (res.ok) {
        await fetchMemory()
        return true
      }
      errorMessage.value = res.error || 'Failed to save entry'
      return false
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to save memory entry'
      return false
    } finally {
      isSaving.value = false
    }
  }

  async function deleteEntry(memoryId: string, scope: string = 'local'): Promise<boolean> {
    errorMessage.value = null
    try {
      const res = await api.deleteMemory(memoryId, scope)
      if (res.ok) {
        entries.value = entries.value.filter((e) => e.memory_id !== memoryId)
        graphNodes.value = graphNodes.value.filter((n) => n.id !== memoryId)
        if (activeEntry.value?.memory_id === memoryId) {
          activeEntry.value = null
        }
        return true
      }
      errorMessage.value = res.error || 'Failed to delete entry'
      return false
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to delete memory entry'
      return false
    }
  }

  async function fetchGraph(scope?: string) {
    isGraphLoading.value = true
    try {
      const res = await api.getMemoryGraph({ scope: scope || activeScope.value })
      if (res.ok) {
        graphNodes.value = res.nodes || []
        graphEdges.value = res.edges || []
        graphHealth.value = res.health || null
      }
    } catch (err: any) {
      console.warn('Failed to fetch memory graph:', err)
    } finally {
      isGraphLoading.value = false
    }
  }

  async function fetchBacklinks(memoryId: string, scope?: string): Promise<BacklinkItem[]> {
    try {
      const res = await api.getMemoryBacklinks(memoryId, { scope: scope || activeScope.value })
      if (res.ok) {
        activeBacklinks.value = res.backlinks || []
        return res.backlinks || []
      }
      return []
    } catch (err) {
      console.warn('Failed to fetch backlinks:', err)
      return []
    }
  }

  return {
    viewMode,
    graphNodes,
    graphEdges,
    graphHealth,
    isGraphLoading,
    activeBacklinks,
    fetchGraph,
    fetchBacklinks,
    entries,
    activeEntry,
    activeScope,
    searchQuery,
    filterKind,
    filterTag,
    isLoading,
    isSaving,
    errorMessage,
    fetchMemory,
    loadEntry,
    saveEntry,
    deleteEntry
  }
})
