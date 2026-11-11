export interface HealthStatus {
  status: 'healthy' | 'degraded' | 'offline' | string
  version: string
  uptime_s: number
  timestamp?: string
}

export interface QuotaWindow {
  limit: number
  current_usage: number
  percentage: number
  next_reset_time?: number | string | null
  unit?: number | string
  number?: number
  name?: string
}

export interface WalletInfo {
  supported: boolean
  provider: string
  units?: 'usd' | 'quota' | string
  quota_windows?: QuotaWindow[]
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
  units?: 'usd' | 'quota' | string
  quota_windows?: QuotaWindow[]
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

export interface ProviderRateLimitInfo {
  rpm: number
  tpm: number
  burst_multiplier: number
  requests_available: number | null
  tokens_available: number | null
  cooldown_s: number
  wait_s: number
}

export type RateLimitsMap = Record<string, ProviderRateLimitInfo>

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
  executor?: 'botex' | 'devin'
  agent_type?: string
}

export interface TaskSummary {
  task_id: string
  status: 'QUEUED' | 'RUNNING' | 'DONE' | 'FAILED' | 'CANCELLED' | string
  created_at: number
  finished_at: number | null
}

export interface TaskTurn {
  turn: number
  prompt: string
  summary: string
  ok: boolean
  run_id?: string | null
  session_id?: string | null
  resumed?: boolean | null
  resume_mode?: string | null
  ts?: number
}

export interface TaskDetail extends TaskSummary {
  result: string | null
  result_data: Record<string, any> | null
  task?: string | null
  request?: TaskCreateRequest | null
  progress?: TaskProgress | null
  executor?: 'botex' | 'devin' | string | null
  session_id?: string | null
  turns?: TaskTurn[]
}

export interface TaskFollowupResponse {
  ok: boolean
  task_id: string
  status: string
  session_id?: string | null
  detail?: string
}

// ---------------------------------------------------------------------------
// Live task-event observability
// ---------------------------------------------------------------------------

export const TASK_EVENT_KINDS = [
  'task_started',
  'turn_started',
  'turn_completed',
  'tool_call',
  'tool_result',
  'approval_requested',
  'approval_decided',
  'retry_scheduled',
  'waiting_for_input',
  'progress',
  'error',
  'warning',
  'task_finished'
] as const

export type TaskEventKind = (typeof TASK_EVENT_KINDS)[number]

export interface TaskEvent {
  seq: number
  ts: number
  kind: string
  data: Record<string, unknown>
}

export interface TaskEventsResponse {
  events: TaskEvent[]
  min_seq: number
  dropped: number
}

export type TaskWaitingFor = 'approval' | 'retry' | 'input' | 'model'

export interface TaskProgress {
  turn: number | null
  max_turns: number | null
  cost_usd: number | null
  waiting_for: TaskWaitingFor | null
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

export interface MemoryProjectItem {
  project: string
  count: number
}

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
  project?: string
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
  project?: string
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
  error_class?: string
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
  provider: 'openrouter-free' | 'openrouter' | 'nvidia' | 'google' | 'ollama' | string
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

// ---------------------------------------------------------------------------
// Knowledge Graph Curator Types
// ---------------------------------------------------------------------------

export interface CuratorStatus {
  last_run_id?: string | null
  last_run_timestamp?: string | null
  status: string
  is_healthy: boolean
  total_nodes: number
  total_edges: number
  orphan_count: number
  broken_count: number
  tier_used: string
  duration_seconds: number
  lock_active: boolean
  beacon_path?: string | null
  summary?: string
  last_telegram_status?: Record<string, any>
}

export interface CuratorRunRequest {
  workspace_dir?: string
  scope?: string
  force?: boolean
  model_override?: string
  notify_telegram?: boolean
}

// ---------------------------------------------------------------------------
// Creator Mode Dynamic Plugins & Vision Triage Types
// ---------------------------------------------------------------------------

export interface PluginDefinition {
  name: string
  description: string
  parameters_schema: Record<string, any>
  example_usage: {
    input?: any
    output?: any
    [key: string]: any
  }
  reusability: 'ephemeral' | 'reusable' | string
  requires_net: boolean
  scope: 'ephemeral' | 'staging' | 'local' | 'global' | string
  script_path: string
  sha256: string
}

export interface PluginsCatalogResponse {
  ok: boolean
  plugins: {
    local: PluginDefinition[]
    global: PluginDefinition[]
    staging: PluginDefinition[]
  }
  index_markdown: {
    local: string
    global: string
  }
}

export interface PluginResolveRequest {
  name: string
  action: 'local' | 'global' | 'discard'
  workspace_dir?: string
}

export interface PluginResolveResponse {
  ok: boolean
  status: string
  scope?: string
  name: string
  index_updated?: boolean
}

export interface VisionInspectRequest {
  image_base64: string
  prompt_hint?: string
  reference_hash?: number | null
  task_id?: string | null
  workspace_dir?: string
}

export interface VisionInspectResponse {
  ok: boolean
  status: 'NO_VISUAL_DIFF' | 'DEFECT_DETECTED' | 'CLEAN' | string
  skipped_llm: boolean
  dhash: number
  hamming_distance: number
  provider?: string | null
  model?: string | null
  finding: string
  observation_json: string
}