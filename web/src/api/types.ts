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
  task?: string | null
  request?: TaskCreateRequest | null
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
  status?: string
  compiled_truth?: boolean
  links?: string[]
  timestamp?: string
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
  reasoning?: string
  is_thinking?: boolean
  timestamp: number
  streaming?: boolean
  model?: string
  cost_usd?: number
  duration_ms?: number
}

export interface ChatChunk {
  type: 'chunk' | 'content_chunk' | 'reasoning_chunk' | 'message' | 'tool_call' | 'tool_call_start' | 'tool_result' | 'approval_required' | 'error' | 'done' | 'ping' | 'pong' | 'auth_ok' | string
  delta?: string
  chunk?: string
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

export interface GraphNode {
  id: string
  title: string
  kind: string
  tags: string[]
  status: string
  compiled_truth: boolean
  timestamp: string
  scope: string
  in_degree: number
  out_degree: number
}

export interface GraphEdge {
  source: string
  target: string
  kind: string
}

export interface GraphHealth {
  total_nodes: number
  total_edges: number
  broken_count: number
  orphan_count: number
  cycle_count: number
  is_healthy: boolean
  broken_links?: Array<{ source: string; target: string }>
  orphans?: any[]
  cycles?: string[][]
}

export interface MemoryGraphResponse {
  ok: boolean
  nodes: GraphNode[]
  edges: GraphEdge[]
  health: GraphHealth
}

export interface BacklinkItem {
  id: string
  title: string
  kind: string
  tags: string[]
  status: string
  compiled_truth: boolean
  timestamp: string
  scope: string
}

export interface BacklinksResponse {
  ok: boolean
  target_id: string
  count: number
  backlinks: BacklinkItem[]
}

// ---------------------------------------------------------------------------
// Analytics (run telemetry aggregated by backend from .agent_analytics.json)
// ---------------------------------------------------------------------------

export interface AnalyticsModelStat {
  model: string
  runs: number
  done_runs: number
  done_pct: number
  cost_usd: number
  tokens: number
  prompt_tokens?: number
  cached_tokens: number
  cache_hit_pct?: number
  cache_savings_usd: number
  failures: Record<string, number>
  top_failure: string | null
}

export interface AnalyticsDailyPoint {
  date: string
  runs: number
  cost_usd: number
  tokens: number
}

export interface AnalyticsRecentRun {
  task_id: string | null
  timestamp: string | null
  model: string | null
  provider: string | null
  status: string | null
  failure_kind: string | null
  cost_usd: number
  duration_s: number | null
  tokens: number
}

export interface AnalyticsDigest {
  id: string
  title: string
  timestamp: string
  content: string
}

export interface AnalyticsResponse {
  period: 'week' | 'month' | 'all' | string
  days: number | null
  runs: number
  done_runs: number
  failed_runs: number
  done_pct: number
  cost_usd: number
  prompt_tokens: number
  completion_tokens: number
  cached_tokens: number
  cache_hit_pct: number
  cache_savings_usd: number
  lines_added: number
  lines_removed: number
  avg_duration_s: number | null
  avg_generation_ms: number | null
  status_counts: Record<string, number>
  by_model: AnalyticsModelStat[]
  daily: AnalyticsDailyPoint[]
  recent: AnalyticsRecentRun[]
  latest_digest: AnalyticsDigest | null
}

// ---------------------------------------------------------------------------
// Server-Side Skills Catalog (Skill Vault)
// ---------------------------------------------------------------------------

export interface SkillCatalogItem {
  name: string
  display_name?: string
  description: string
  version: string
  origin_repo: string
  commit_sha?: string
  installed_at?: string
  taxonomy: string[]
  size_bytes: number
  path: string
  preview?: string
}

export interface SkillDetail extends SkillCatalogItem {
  content: string
}

export interface SkillInstallRequest {
  name?: string
  content?: string
  source_url?: string
  origin_repo?: string
  version?: string
  commit_sha?: string
  description?: string
  taxonomy?: string[]
}

