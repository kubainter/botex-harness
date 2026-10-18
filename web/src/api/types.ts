export interface HealthStatus {
  status: 'healthy' | 'degraded' | 'offline' | string
  version: string
  uptime_s: number
  timestamp?: string
}

export interface WalletInfo {
  supported: boolean
  provider: string
  balance_usd: number | null
  usage_usd: number | null
  spend_usd?: number | null
  limit_usd: number | null
  limit_remaining_usd: number | null
  resets_at?: string | null
  is_free_tier: boolean
  free_model_daily_requests?: number | null
  label: string | null
  error: string | null
}

export interface KeyStatusInfo {
  supported: boolean
  provider: string
  limit: number | null
  limit_remaining: number | null
  spend_usd: number | null
  balance_usd: number | null
  resets_at: string | null
  is_free_tier: boolean
  free_model_daily_requests: number | null
  low_budget_warning: boolean
  label: string | null
  error: string | null
}

export interface TaskCreateRequest {
  task: string
  files?: string[]
  workspace_dir?: string
  model?: string
  profile?: string
  provider?: string
  mode?: string
  max_turns?: number
  max_tokens?: number
  max_duration_s?: number
  budget_limit_usd?: number
  allow_destructive?: boolean
  allow_exec?: boolean
  allow_net?: boolean
  net_allowed_hosts?: string[]
  net_allowed_urls?: string[]
  output_path?: string
  verify_command?: string
  recipe?: string
  allow_non_zdr?: boolean
  approval_policy?: string
}

export interface TaskSummary {
  task_id: string
  status: 'QUEUED' | 'RUNNING' | 'DONE' | 'FAILED' | 'CANCELLED' | string
  created_at: number
  finished_at: number | null
}

export interface TaskDetail extends TaskSummary {
  result: string | null
  result_data: Record<string, any> | null
}

export interface PendingAction {
  action_id: string
  tool_name: string
  risk_level: 'HIGH_RISK' | 'MUTATIONS' | 'SAFE' | string
  params: Record<string, any>
  preview_diff?: string
  timeout_s?: number
  created_at?: number
  task_id?: string
  description?: string
  status?: string
}

export interface ApprovalDecisionRequest {
  approved: boolean
  reason?: string
  remember?: boolean
}

export type MemoryKind = 'context' | 'decision' | 'handoff' | 'bug' | 'architecture' | string
export type MemoryScope = 'local' | 'global' | 'all'

export interface MemoryEntry {
  memory_id: string
  title: string
  content: string
  kind: MemoryKind
  tags: string[]
  origin?: string
  created_at?: string | number
  updated_at?: string | number
  scope?: string
  path?: string
}

export interface MemorySaveRequest {
  title: string
  content: string
  kind: MemoryKind
  tags: string[]
  memory_id?: string
  origin?: string
  scope: 'local' | 'global'
  workspace_dir?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: number
  streaming?: boolean
  model?: string
  cost_usd?: number
  duration_ms?: number
}

export interface ChatChunk {
  type: 'chunk' | 'content_chunk' | 'message' | 'tool_call' | 'tool_call_start' | 'tool_result' | 'approval_required' | 'error' | 'done' | 'ping' | 'pong' | 'auth_ok' | string
  delta?: string
  accumulated?: string
  content?: string
  message?: string
  name?: string
  arguments?: Record<string, any>
  result?: string
  action?: PendingAction
  error?: string
  finish_reason?: string
  usage?: Record<string, any>
  timestamp?: number
}

export interface RecommendedModel {
  id: string
  name: string
  provider: string
  role: 'planner' | 'coder' | 'chat' | 'reviewer'
  description: string
  context_length: number
  input_cost_per_m: number
  output_cost_per_m: number
}

export interface CatalogModelPricing {
  prompt_per_m: number
  completion_per_m: number
}

export interface CatalogModel {
  id: string
  name: string
  provider: 'openrouter-free' | 'openrouter' | 'nvidia' | string
  is_free: boolean
  context_length: number
  pricing: CatalogModelPricing
  supports_tools: boolean
  is_zdr_compliant: boolean
}

export interface ChatSessionSummary {
  session_id: string
  title: string
  workspace_dir: string
  model: string
  provider: string
  message_count: number
  created_at: number
  updated_at: number
}

export interface ChatSessionDetail extends ChatSessionSummary {
  messages: ChatMessage[]
}


