import { ref } from 'vue'
import type {
  HealthStatus,
  WalletInfo,
  KeyStatusInfo,
  TaskCreateRequest,
  TaskSummary,
  TaskDetail,
  PendingAction,
  ApprovalDecisionRequest,
  MemoryEntry,
  MemorySaveRequest,
  ChatChunk,
  CatalogModel,
  ChatSessionSummary,
  ChatSessionDetail,
  MemoryGraphResponse,
  BacklinksResponse,
  AnalyticsResponse,
  SkillCatalogItem,
  SkillDetail,
  SkillInstallRequest
} from './types'

const STORAGE_KEY_URL = 'botex_backend_url'
const STORAGE_KEY_TOKEN = 'botex_access_token'

export function getStoredBackendUrl(): string {
  const stored = localStorage.getItem(STORAGE_KEY_URL)
  if (stored) return stored.replace(/\/+$/, '')
  // Default to window origin if served from browser, unless running on vite dev port 5173
  if (typeof window !== 'undefined' && window.location && window.location.protocol.startsWith('http')) {
    if (window.location.port !== '5173') {
      return window.location.origin
    }
  }
  return 'http://127.0.0.1:8000'
}

export function setStoredBackendUrl(url: string): void {
  const clean = url.trim().replace(/\/+$/, '')
  localStorage.setItem(STORAGE_KEY_URL, clean)
}

export function getStoredAccessToken(): string {
  return (localStorage.getItem(STORAGE_KEY_TOKEN) || '').trim()
}

export function setStoredAccessToken(token: string): void {
  localStorage.setItem(STORAGE_KEY_TOKEN, token.trim())
}

// Reactive auth-failure signal: set when any API call returns 401/403 so the
// shell can warn instead of silently rendering empty states. Cleared on the
// next successful request.
export const authError = ref<string | null>(null)

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(`API Error ${status}: ${detail}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

async function request<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
  const baseUrl = getStoredBackendUrl()
  const token = getStoredAccessToken()
  const url = `${baseUrl}${endpoint.startsWith('/') ? endpoint : '/' + endpoint}`

  const headers = new Headers(options.headers || {})
  if (!headers.has('Content-Type') && !(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(url, {
    ...options,
    headers
  })

  if (!response.ok) {
    let errorDetail = response.statusText
    try {
      const errJson = await response.json()
      errorDetail = errJson.detail || errJson.message || JSON.stringify(errJson)
    } catch {
      try {
        const text = await response.text()
        if (text) errorDetail = text
      } catch {
        // use statusText
      }
    }
    if (response.status === 401 || response.status === 403) {
      authError.value = `HTTP ${response.status}`
    }
    throw new ApiError(response.status, errorDetail)
  }

  authError.value = null
  if (response.status === 204) {
    return {} as T
  }

  return response.json() as Promise<T>
}

export const api = {
  // 1. Health Probe
  async getHealth(): Promise<HealthStatus> {
    return request<HealthStatus>('/api/health')
  },

  // 2. Wallet & Credits
  async getWallet(provider: string = 'openrouter', forceRefresh: boolean = false): Promise<WalletInfo> {
    const params = new URLSearchParams({
      provider,
      force_refresh: String(forceRefresh)
    })
    return request<WalletInfo>(`/api/wallet?${params.toString()}`)
  },

  // 2a. Live Key Quota & Rate Limit Status
  async getKeyStatus(provider: string = 'openrouter', forceRefresh: boolean = false): Promise<KeyStatusInfo> {
    const params = new URLSearchParams({
      provider,
      force_refresh: String(forceRefresh)
    })
    return request<KeyStatusInfo>(`/api/key/status?${params.toString()}`)
  },

  // 2b. Consolidated Model Catalog
  async getModels(provider?: string, forceRefresh: boolean = false): Promise<CatalogModel[]> {
    const params = new URLSearchParams()
    if (provider) params.set('provider', provider)
    if (forceRefresh) params.set('force_refresh', 'true')
    const qs = params.toString()
    const data = await request<CatalogModel[] | { models: CatalogModel[] }>(
      `/api/models${qs ? '?' + qs : ''}`
    )
    return Array.isArray(data) ? data : (data.models || [])
  },

  // 3. Task Management
  async createTask(req: TaskCreateRequest): Promise<{ task_id: string; status: string }> {
    return request<{ task_id: string; status: string }>('/api/tasks', {
      method: 'POST',
      body: JSON.stringify(req)
    })
  },

  async listTasks(): Promise<{ tasks: TaskSummary[]; count: number }> {
    return request<{ tasks: TaskSummary[]; count: number }>('/api/tasks')
  },

  async getTask(taskId: string): Promise<TaskDetail> {
    return request<TaskDetail>(`/api/tasks/${encodeURIComponent(taskId)}`)
  },

  async cancelTask(taskId: string): Promise<{ ok: boolean; task_id: string; status: string; message?: string }> {
    return request<{ ok: boolean; task_id: string; status: string; message?: string }>(
      `/api/tasks/${encodeURIComponent(taskId)}/cancel`,
      { method: 'POST' }
    )
  },

  // 4. Approvals
  async listApprovals(taskId?: string): Promise<{ pending_actions: PendingAction[]; count: number }> {
    const query = taskId ? `?task_id=${encodeURIComponent(taskId)}` : ''
    return request<{ pending_actions: PendingAction[]; count: number }>(`/api/approvals${query}`)
  },

  async decideApproval(actionId: string, decision: ApprovalDecisionRequest): Promise<{ ok: boolean; action_id: string; approved: boolean; reason: string }> {
    return request<{ ok: boolean; action_id: string; approved: boolean; reason: string }>(
      `/api/approvals/${encodeURIComponent(actionId)}/decide`,
      {
        method: 'POST',
        body: JSON.stringify(decision)
      }
    )
  },

  // 5. Memory Vault
  async getMemory(params: {
    query?: string
    tag?: string
    kind?: string
    scope?: string
    workspace_dir?: string
    memory_id?: string
  }): Promise<{ ok: boolean; results?: MemoryEntry[]; memories?: MemoryEntry[]; entry?: MemoryEntry; total?: number; count?: number; error?: string }> {
    const q = new URLSearchParams()
    if (params.query) q.set('query', params.query)
    if (params.tag) q.set('tag', params.tag)
    if (params.kind) q.set('kind', params.kind)
    if (params.scope) q.set('scope', params.scope)
    if (params.workspace_dir) q.set('workspace_dir', params.workspace_dir)
    if (params.memory_id) q.set('memory_id', params.memory_id)

    return request<{ ok: boolean; results?: MemoryEntry[]; entry?: MemoryEntry; total?: number; error?: string }>(
      `/api/memory?${q.toString()}`
    )
  },

  async saveMemory(body: MemorySaveRequest): Promise<{ ok: boolean; memory_id?: string; path?: string; error?: string }> {
    return request<{ ok: boolean; memory_id?: string; path?: string; error?: string }>('/api/memory', {
      method: 'POST',
      body: JSON.stringify(body)
    })
  },

  async deleteMemory(memoryId: string, scope: string = 'local', workspaceDir?: string): Promise<{ ok: boolean; memory_id?: string; deleted?: boolean; error?: string }> {
    const q = new URLSearchParams({ scope })
    if (workspaceDir) q.set('workspace_dir', workspaceDir)
    return request<{ ok: boolean; memory_id?: string; deleted?: boolean; error?: string }>(
      `/api/memory/${encodeURIComponent(memoryId)}?${q.toString()}`,
      { method: 'DELETE' }
    )
  },

  async getMemoryGraph(params?: { scope?: string; workspace_dir?: string }): Promise<MemoryGraphResponse> {
    const q = new URLSearchParams()
    if (params?.scope) q.set('scope', params.scope)
    if (params?.workspace_dir) q.set('workspace_dir', params.workspace_dir)
    return request<MemoryGraphResponse>(`/api/memory/graph?${q.toString()}`)
  },

  async getMemoryBacklinks(memoryId: string, params?: { scope?: string; workspace_dir?: string }): Promise<BacklinksResponse> {
    const q = new URLSearchParams({ memory_id: memoryId })
    if (params?.scope) q.set('scope', params.scope)
    if (params?.workspace_dir) q.set('workspace_dir', params.workspace_dir)
    return request<BacklinksResponse>(`/api/memory/backlinks?${q.toString()}`)
  },

  async abortChat(sessionId: string, model?: string, provider?: string): Promise<{ ok: boolean }> {
    const q = new URLSearchParams({ session_id: sessionId })
    if (model) q.set('model', model)
    if (provider) q.set('provider', provider)
    return request<{ ok: boolean }>(`/api/chat/abort?${q.toString()}`, { method: 'POST' })
  },

  // 6. SSE Fallback for Chat
  async streamChatSSE(
    payload: {
      message: string
      session_id?: string
      workspace_dir?: string
      model?: string
      provider?: string
      profile?: string
      system_prompt?: string
    },
    onChunk: (chunk: ChatChunk) => void,
    signal?: AbortSignal
  ): Promise<void> {
    const baseUrl = getStoredBackendUrl()
    const token = getStoredAccessToken()
    const url = `${baseUrl}/api/chat`

    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream'
    }
    if (token) {
      headers['Authorization'] = `Bearer ${token}`
    }

    const res = await fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal
    })

    if (!res.ok) {
      let detail = res.statusText
      try {
        const j = await res.json()
        detail = j.detail || detail
      } catch {}
      throw new ApiError(res.status, detail)
    }

    const reader = res.body?.getReader()
    if (!reader) throw new Error('No readable stream received from /api/chat')

    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        const trimmed = line.trim()
        if (trimmed.startsWith('data:')) {
          const jsonStr = trimmed.slice(5).trim()
          if (!jsonStr) continue
          try {
            const parsed = JSON.parse(jsonStr) as ChatChunk
            onChunk(parsed)
          } catch (e) {
            console.error('Failed to parse SSE line', jsonStr, e)
          }
        }
      }
    }

    // Flush any remaining line in buffer
    if (buffer.trim()) {
      const trimmed = buffer.trim()
      if (trimmed.startsWith('data:')) {
        const jsonStr = trimmed.slice(5).trim()
        if (jsonStr) {
          try {
            const parsed = JSON.parse(jsonStr) as ChatChunk
            onChunk(parsed)
          } catch (e) {
            console.error('Failed to parse final SSE line', jsonStr, e)
          }
        }
      }
    }
  },

  // 7. Chat Session History & Memory Export
  async listChatSessions(workspaceDir?: string): Promise<{ sessions: ChatSessionSummary[]; count: number }> {
    const q = workspaceDir ? `?workspace_dir=${encodeURIComponent(workspaceDir)}` : ''
    return request<{ sessions: ChatSessionSummary[]; count: number }>(`/api/chat/sessions${q}`)
  },

  async getChatSession(sessionId: string, workspaceDir?: string): Promise<ChatSessionDetail> {
    const q = workspaceDir ? `?workspace_dir=${encodeURIComponent(workspaceDir)}` : ''
    return request<ChatSessionDetail>(`/api/chat/sessions/${encodeURIComponent(sessionId)}${q}`)
  },

  async deleteChatSession(sessionId: string, workspaceDir?: string): Promise<{ ok: boolean; session_id: string }> {
    const q = workspaceDir ? `?workspace_dir=${encodeURIComponent(workspaceDir)}` : ''
    return request<{ ok: boolean; session_id: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}${q}`,
      { method: 'DELETE' }
    )
  },

  // 8. Run Analytics & Local-LLM Digest
  async getAnalytics(period: 'week' | 'month' | 'all' = 'week', workspaceDir?: string): Promise<AnalyticsResponse> {
    const q = new URLSearchParams({ period })
    if (workspaceDir) q.set('workspace_dir', workspaceDir)
    return request<AnalyticsResponse>(`/api/analytics?${q.toString()}`)
  },

  async triggerAnalyticsDigest(params?: { provider?: string; model?: string; days?: number }): Promise<{ ok: boolean; text?: string; memory_id?: string; error?: string }> {
    const q = new URLSearchParams()
    if (params?.provider) q.set('provider', params.provider)
    if (params?.model) q.set('model', params.model)
    if (params?.days) q.set('days', String(params.days))
    const qs = q.toString()
    return request<{ ok: boolean; text?: string; memory_id?: string; error?: string }>(
      `/api/analytics/digest${qs ? '?' + qs : ''}`,
      { method: 'POST' }
    )
  },

  async saveChatToMemory(
    sessionId: string,
    body: {
      title?: string
      kind?: string
      scope?: string
      workspace_dir?: string
      content?: string
      tags?: string[]
    }
  ): Promise<{ ok: boolean; memory_id?: string; path?: string; error?: string }> {
    return request<{ ok: boolean; memory_id?: string; path?: string; error?: string }>(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/save_to_memory`,
      {
        method: 'POST',
        body: JSON.stringify(body)
      }
    )
  },

  // 9. Server-Side Skills Catalog (Skill Vault)
  async getSkills(): Promise<{ ok: boolean; count: number; skills: SkillCatalogItem[]; skills_dir?: string }> {
    return request<{ ok: boolean; count: number; skills: SkillCatalogItem[]; skills_dir?: string }>('/api/skills')
  },

  async getSkill(skillName: string): Promise<{ ok: boolean; skill: SkillDetail }> {
    return request<{ ok: boolean; skill: SkillDetail }>(`/api/skills/${encodeURIComponent(skillName)}`)
  },

  async installSkill(body: SkillInstallRequest): Promise<{ ok: boolean; name: string; version: string; origin_repo: string; skill?: SkillCatalogItem }> {
    return request<{ ok: boolean; name: string; version: string; origin_repo: string; skill?: SkillCatalogItem }>('/api/skills', {
      method: 'POST',
      body: JSON.stringify(body)
    })
  },

  async deleteSkill(skillName: string): Promise<{ ok: boolean; name: string; deleted: boolean }> {
    return request<{ ok: boolean; name: string; deleted: boolean }>(
      `/api/skills/${encodeURIComponent(skillName)}`,
      { method: 'DELETE' }
    )
  }
}

