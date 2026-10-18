import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api/client'
import type { MemoryEntry, MemorySaveRequest, MemoryScope } from '../api/types'

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
      entries.value = res.results || []
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
      const res = await api.getMemory({
        memory_id: memoryId,
        scope
      })
      if (res.entry) {
        activeEntry.value = res.entry
        return res.entry
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

  return {
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
    saveEntry
  }
})
