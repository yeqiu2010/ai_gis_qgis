export type RPCMethod =
  | 'createSession'
  | 'listSessions'
  | 'getMessages'
  | 'getTaskState'
  | 'listLoadedSkills'
  | 'chat'
  | 'confirmToolCall'
  | 'cancelRun'
  | 'getSettings'
  | 'saveSettings'

export interface RPCRequest {
  id: number
  method: RPCMethod
  params?: Record<string, unknown>
}

export interface RPCResponse {
  id: number
  result?: unknown
  error?: {
    code: string
    message: string
    details?: unknown
  }
}

export type AgentEventType =
  | 'run_start'
  | 'run_metrics'
  | 'thinking'
  | 'message_delta'
  | 'message'
  | 'tool_start'
  | 'tool_end'
  | 'stage_start'
  | 'stage_end'
  | 'code_generated'
  | 'confirm_request'
  | 'confirm_resolved'
  | 'error'
  | 'complete'

export interface AgentEvent {
  type: AgentEventType
  payload: Record<string, unknown>
  session_id?: string
  run_id?: string
}

export interface SessionSummary {
  id: string
  title: string
  started_at: number
  message_count: number
  model?: string
}

export interface ChatMessage {
  id?: number
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: string
  timestamp?: number
  event_type?: string
  run_id?: string
  duration_ms?: number
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  llm_calls?: number
  usage_estimated?: boolean | number
  metrics_status?: string
}

export interface RunMetrics {
  run_id: string
  started_at: number
  duration_ms: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  llm_calls: number
  usage_estimated: boolean
  status: string
  running: boolean
}

export interface PendingConfirmation {
  confirmation_id: string
  tool_name: string
  arguments: Record<string, unknown>
  description?: string
  destructive?: boolean
  writes_project?: boolean
}

export interface LoadedSkillSummary {
  name: string
  description: string
  version?: string
  tags: string[]
  related_skills: string[]
  available: boolean
  inputs?: Record<string, unknown>
  outputs?: Record<string, unknown>
  side_effects?: Record<string, unknown>
}

export interface PlanStepState {
  id: string
  position: number
  skill_name?: string
  instruction: string
  dependencies: string[]
  status: 'pending' | 'in_progress' | 'waiting_for_user' | 'completed' | 'failed' | 'skipped'
  inputs: Record<string, unknown>
  outputs: Record<string, unknown>
  error?: string
}

export interface TaskArtifact {
  id: string
  step_id?: string
  artifact_type: string
  name?: string
  uri?: string
  payload: Record<string, unknown>
  producer?: string
  verified: boolean
}

export interface TaskState {
  id: string
  session_id: string
  objective: string
  status: string
  plan_version: number
  summary?: string
  steps: PlanStepState[]
  artifacts: TaskArtifact[]
  skill_invocations: Array<Record<string, unknown>>
}

export interface AppSettings {
  llm: {
    provider?: string
    model?: string
    base_url?: string
    api_key?: string
    temperature?: number
    max_tokens?: number
    max_context_tokens?: number
    request_timeout_seconds?: number
  }
  database?: Record<string, unknown>
  executor?: Record<string, unknown>
  ui?: Record<string, unknown>
}
