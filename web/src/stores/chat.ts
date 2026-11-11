import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api/client'
import { wsManager } from '../api/ws'
import type { ChatMessage, ChatChunk, CatalogModel, ChatSessionSummary } from '../api/types'

function generateId(): string {
  const c = globalThis.crypto
  if (typeof c?.randomUUID === 'function') return c.randomUUID()
  const buf = new Uint8Array(16)
  c.getRandomValues(buf)
  return Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('')
}

function inferProviderFromSlug(slug: string): string {
  if (!slug) return ''
  const s = slug.toLowerCase()
  if (s.includes('bielik') || s.startsWith('ollama:') || s.includes('speakleash')) {
    return 'ollama'
  }
  if (s.includes(':free') || s.includes('openrouter/free')) {
    return 'openrouter-free'
  }
  if (s.startsWith('nvidia/') || s.startsWith('meta/') || s.startsWith('poolside/')) {
    return 'nvidia'
  }
  // Bare "gemini-*" slugs (no vendor prefix) are served by the Google AI
  // Studio endpoint; "google/..." prefixed ones stay on OpenRouter.
  if (s.startsWith('gemini-') || s.startsWith('models/gemini-')) {
    return 'google'
  }
  if (!s.includes('/')) {
    return 'ollama'
  }
  return 'openrouter'
}

export const useChatStore = defineStore('chat', () => {
  const sessionId = ref<string>(localStorage.getItem('botex_chat_session') || generateId())
  const messages = ref<ChatMessage[]>([])
  const isStreaming = ref<boolean>(false)
  let activeAbortController: AbortController | null = null
  const activeTransport = ref<'ws' | 'sse' | null>(null)
  const activeMode = ref<'chat' | 'agent'>('chat')
  const selectedModel = ref<string>(
    localStorage.getItem('botex_selected_model') || ''
  )
  const selectedProvider = ref<string>(
    localStorage.getItem('botex_selected_provider') || inferProviderFromSlug(selectedModel.value)
  )
  const catalogModels = ref<CatalogModel[]>([])
  const isLoadingModels = ref<boolean>(false)
  // True when /api/models was served from an expired provider-metadata cache
  // (both the idle warmup and the lazy refresh failed — egress degradation).
  // TODO(PWA): render a stale badge in the model selector and auto-refresh the
  // catalog on a 400 model-not-found error — state is wired, UI pending.
  const catalogStale = ref<boolean>(false)
  const workspaceDir = ref<string>('.')
  const pendingTaskDraft = ref<string>('')
  const errorMessage = ref<string | null>(null)

  // Chat Session History
  const sessions = ref<ChatSessionSummary[]>([])
  const isLoadingSessions = ref<boolean>(false)
  const showHistoryDrawer = ref<boolean>(false)
  let initialized = false

  if (selectedModel.value.toLowerCase().includes(':free') && selectedProvider.value === 'openrouter') {
    selectedProvider.value = 'openrouter-free'
  }

  localStorage.setItem('botex_chat_session', sessionId.value)

  const activeModelMeta = computed<CatalogModel | undefined>(() => {
    return (
      catalogModels.value.find(
        (m) => m.id === selectedModel.value && m.provider === selectedProvider.value
      ) || catalogModels.value.find((m) => m.id === selectedModel.value)
    )
  })

  function selectModel(modelId: string, provider?: string) {
    selectedModel.value = modelId
    const found = catalogModels.value.find(
      (m) => m.id === modelId && (!provider || m.provider === provider)
    )
    let resolvedProvider = provider || found?.provider || inferProviderFromSlug(modelId)
    if (modelId.toLowerCase().includes(':free') && resolvedProvider === 'openrouter') {
      resolvedProvider = 'openrouter-free'
    }
    selectedProvider.value = resolvedProvider
    localStorage.setItem('botex_selected_model', modelId)
    localStorage.setItem('botex_selected_provider', resolvedProvider)
  }

  async function fetchCatalogModels(forceRefresh: boolean = false) {
    isLoadingModels.value = true
    const meta = { stale: false, fetchedAt: 0, ageSeconds: 0 }
    try {
      const list = await api.getModels(undefined, forceRefresh, meta)
      catalogStale.value = meta.stale
      if (Array.isArray(list) && list.length > 0) {
        catalogModels.value = list
        if (!selectedModel.value || !list.some((m) => m.id === selectedModel.value)) {
          selectedModel.value = list[0].id
          selectedProvider.value = list[0].provider
          localStorage.setItem('botex_selected_model', selectedModel.value)
          localStorage.setItem('botex_selected_provider', selectedProvider.value)
        } else {
          const exactMatch = catalogModels.value.find(
            (m) => m.id === selectedModel.value && m.provider === selectedProvider.value
          )
          if (!exactMatch) {
            const idMatch = catalogModels.value.find((m) => m.id === selectedModel.value)
            if (idMatch) {
              selectedProvider.value = idMatch.provider
              localStorage.setItem('botex_selected_provider', selectedProvider.value)
            }
          }
        }
      } else {
        catalogModels.value = []
      }
    } catch {
      catalogModels.value = []
      catalogStale.value = false
    } finally {
      isLoadingModels.value = false
    }
  }

  async function fetchSessions() {
    isLoadingSessions.value = true
    try {
      const res = await api.listChatSessions(workspaceDir.value)
      if (res && Array.isArray(res.sessions)) {
        sessions.value = res.sessions
      }
    } catch {
      // Offline or network fallback
    } finally {
      isLoadingSessions.value = false
    }
  }

  async function switchSession(targetId: string) {
    if (!targetId || targetId === sessionId.value) return
    isStreaming.value = false
    activeTransport.value = null
    errorMessage.value = null
    sessionId.value = targetId
    localStorage.setItem('botex_chat_session', targetId)

    try {
      const detail = await api.getChatSession(targetId, workspaceDir.value)
      if (detail && Array.isArray(detail.messages)) {
        messages.value = detail.messages
        if (detail.model) {
          selectedModel.value = detail.model
        }
        if (detail.provider) {
          selectedProvider.value = detail.provider
        }
      } else {
        messages.value = []
      }
    } catch {
      messages.value = []
    }
  }

  async function deleteSession(targetId: string) {
    try {
      await api.deleteChatSession(targetId, workspaceDir.value)
    } catch {
      // Ignore
    }
    sessions.value = sessions.value.filter((s) => s.session_id !== targetId)
    if (sessionId.value === targetId) {
      newSession()
    }
  }

  function newSession() {
    sessionId.value = generateId()
    localStorage.setItem('botex_chat_session', sessionId.value)
    messages.value = []
    isStreaming.value = false
    activeTransport.value = null
    errorMessage.value = null
    fetchSessions()
  }

  function appendChunkToLastAssistant(chunkText: string) {
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant') {
      last.content += chunkText
    } else {
      messages.value.push({
        id: generateId(),
        role: 'assistant',
        content: chunkText,
        timestamp: Date.now(),
        streaming: true,
        model: selectedModel.value
      })
    }
  }

  function finishStreaming() {
    isStreaming.value = false
    activeTransport.value = null
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant') {
      last.streaming = false
      last.is_thinking = false
    }
    fetchSessions()
  }

  function handleChunk(chunk: ChatChunk) {
    const last = messages.value[messages.value.length - 1]
    const isAssistant = last && last.role === 'assistant'

    if (chunk.type === 'turn_start') {
      return
    } else if (chunk.type === 'reasoning_chunk') {
      const text = chunk.chunk ?? chunk.delta ?? chunk.content ?? ''
      if (text && isAssistant) {
        last.reasoning = (last.reasoning || '') + text
        last.is_thinking = true
      }
      return
    } else if (chunk.type === 'content_chunk' || chunk.type === 'chunk') {
      if (isAssistant) {
        last.is_thinking = false
      }
      const text = chunk.delta ?? chunk.content ?? chunk.message ?? ''
      if (text) {
        appendChunkToLastAssistant(text)
      }
    } else if (chunk.type === 'tool_call_start') {
      const toolName = chunk.name || 'tool'
      appendChunkToLastAssistant(`\n\n> Running tool: \`${toolName}\`...\n\n`)
    } else if (chunk.type === 'tool_result') {
      // Tool executed
    } else if (chunk.type === 'done' || chunk.finish_reason) {
      if (chunk.content && isAssistant && !last.content.trim()) {
        last.content = chunk.content
      }
      finishStreaming()
    } else if (chunk.type === 'error' && chunk.error) {
      errorMessage.value = chunk.error
      if (isAssistant) {
        appendChunkToLastAssistant(`\n\n**[Error]** ${chunk.error}`)
        if (chunk.error_class === 'model_not_found') {
          appendChunkToLastAssistant(
            `\n\n> ⚠️ Model may be outdated or removed — refresh the model list (server catalog may be stale).`
          )
          void fetchCatalogModels(true)
        }
      }
      finishStreaming()
    } else if (chunk.delta) {
      appendChunkToLastAssistant(chunk.delta)
    } else if (chunk.content) {
      if (isAssistant && !last.content.trim()) {
        last.content = chunk.content
      } else {
        appendChunkToLastAssistant(chunk.content)
      }
    } else if (chunk.message) {
      appendChunkToLastAssistant(chunk.message)
    }
  }

  async function runSseFallback(promptText: string) {
    activeTransport.value = 'sse'
    activeAbortController = new AbortController()
    try {
      await api.streamChatSSE(
        {
          message: promptText,
          session_id: sessionId.value,
          model: selectedModel.value,
          provider: selectedProvider.value,
          workspace_dir: workspaceDir.value
        },
        handleChunk,
        activeAbortController.signal
      )
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to stream response'
      appendChunkToLastAssistant(`\n\n**[Error]** ${errorMessage.value}`)
    } finally {
      finishStreaming()
    }
  }

  async function sendMessage(text: string) {
    const promptText = text.trim()
    if (!promptText || isStreaming.value) return

    errorMessage.value = null
    const userMsg: ChatMessage = {
      id: generateId(),
      role: 'user',
      content: promptText,
      timestamp: Date.now()
    }
    messages.value.push(userMsg)
    isStreaming.value = true

    messages.value.push({
      id: generateId(),
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      streaming: true,
      model: selectedModel.value
    })

    const wsStatus = wsManager.getStatus()
    if (wsStatus === 'CONNECTED') {
      activeTransport.value = 'ws'
      activeAbortController = new AbortController()
      const sent = wsManager.sendChatMessage(
        promptText,
        sessionId.value,
        selectedModel.value,
        workspaceDir.value,
        selectedProvider.value
      )
      if (sent) return
    }

    await runSseFallback(promptText)
  }

  async function abortGeneration() {
    if (!isStreaming.value) return
    // 1. Abort SSE fetch if active
    if (activeAbortController) {
      try {
        activeAbortController.abort()
      } catch {}
      activeAbortController = null
    }

    // 2. Send WS abort message if using WebSocket
    if (activeTransport.value === 'ws') {
      wsManager.send({
        type: 'abort',
        session_id: sessionId.value,
        model: selectedModel.value,
        provider: selectedProvider.value
      })
    }

    // 3. Notify backend /api/chat/abort to unload VRAM immediately (e.g. Ollama)
    try {
      await api.abortChat(sessionId.value, selectedModel.value, selectedProvider.value)
    } catch {}

    // 4. Update UI message
    appendChunkToLastAssistant('\n\n*[Zatrzymano przez użytkownika]*')
    finishStreaming()
  }

  async function saveCurrentToMemory(
    title?: string,
    kind: string = 'context',
    scope: string = 'local',
    content?: string,
    tags?: string[]
  ): Promise<{ ok: boolean; memory_id?: string; error?: string }> {
    try {
      const res = await api.saveChatToMemory(sessionId.value, {
        title,
        kind,
        scope,
        workspace_dir: workspaceDir.value,
        content,
        tags
      })
      return res
    } catch (e: any) {
      return { ok: false, error: e?.message || 'Failed to save chat to memory' }
    }
  }

  function convertChatToTask(): string {
    const parts: string[] = []
    messages.value.forEach((m) => {
      if (m.role === 'user') {
        parts.push(`User Objective: ${m.content}`)
      } else if (m.role === 'assistant' && m.content) {
        parts.push(`Context / Plan:\n${m.content}`)
      }
    })
    const draft = parts.join('\n\n---\n\n')
    pendingTaskDraft.value = draft
    activeMode.value = 'agent'
    return draft
  }

  async function init() {
    fetchCatalogModels()
    await fetchSessions()

    // Restore existing session if stored in localStorage — skip the fetch
    // entirely when the backend no longer knows the id (avoids a 404 hit on
    // every launch after the session store was reset).
    const knownOnServer = sessions.value.some((s) => s.session_id === sessionId.value)
    if (sessionId.value && messages.value.length === 0 && knownOnServer) {
      try {
        const detail = await api.getChatSession(sessionId.value, workspaceDir.value)
        if (detail && Array.isArray(detail.messages) && detail.messages.length > 0) {
          messages.value = detail.messages
          if (detail.model) selectedModel.value = detail.model
          if (detail.provider) selectedProvider.value = detail.provider
        }
      } catch {
        // New session
      }
    }

    if (initialized) return
    initialized = true
    wsManager.onChunk(handleChunk)
    wsManager.onStatusChange((status) => {
      if (status === 'DISCONNECTED' && isStreaming.value && activeTransport.value === 'ws') {
        const last = messages.value[messages.value.length - 1]
        const prevUser = messages.value[messages.value.length - 2]
        if (last && last.role === 'assistant' && !last.content.trim() && prevUser?.role === 'user') {
          runSseFallback(prevUser.content)
          return
        }
        finishStreaming()
      }
    })
  }

  return {
    sessionId,
    messages,
    isStreaming,
    activeMode,
    selectedModel,
    selectedProvider,
    catalogModels,
    isLoadingModels,
    catalogStale,
    activeModelMeta,
    workspaceDir,
    pendingTaskDraft,
    errorMessage,
    sessions,
    isLoadingSessions,
    showHistoryDrawer,
    selectModel,
    fetchCatalogModels,
    fetchSessions,
    switchSession,
    deleteSession,
    saveCurrentToMemory,
    newSession,
    sendMessage,
    abortGeneration,
    convertChatToTask,
    finishStreaming,
    init
  }
})
