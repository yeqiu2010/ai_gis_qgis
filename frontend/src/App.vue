<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useBridge } from './composables/useBridge'
import type {
  AppSettings,
  ChatMessage,
  LoadedSkillSummary,
  PendingConfirmation,
  RunMetrics,
  SessionSummary,
  TaskState
} from './types/protocol'

const draft = ref('')
const messages = ref<ChatMessage[]>([])
const sessions = ref<SessionSummary[]>([])
const activeSessionId = ref('')
const loadedSkills = ref<LoadedSkillSummary[]>([])
const taskState = ref<TaskState | null>(null)
const isSending = ref(false)
const pendingConfirmation = ref<PendingConfirmation | null>(null)
const conversationRef = ref<HTMLElement | null>(null)
const conversationEndRef = ref<HTMLElement | null>(null)
const streamingRunId = ref('')
const streamingMessageIndex = ref<number | null>(null)
const processRunId = ref('')
const processMessageIndex = ref<number | null>(null)
const runMetricsById = ref<Record<string, RunMetrics>>({})
const activeMetricsRunId = ref('')
const clockMs = ref(Date.now())
let clockTimer: number | undefined
const settingsOpen = ref(false)
const settingsSaving = ref(false)
const settingsMessage = ref('')
const sam3Testing = ref(false)
const settings = ref<AppSettings | null>(null)
const settingsForm = ref({
  provider: 'openai_compatible',
  base_url: '',
  model: '',
  api_key: '',
  temperature: 0.1,
  max_tokens: 16384,
  max_context_tokens: 32768,
  request_timeout_seconds: 300
})
const providerOptions = [
  { value: 'openai_compatible', label: 'OpenAI Compatible' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'ollama', label: 'Ollama' },
  { value: 'echo', label: 'Echo（离线测试）' }
]
const sam3Form = ref({
  enabled: true,
  base_url: 'http://127.0.0.1:8000',
  api_token: '',
  connect_timeout_seconds: 10,
  request_timeout_seconds: 1200,
  max_upload_mb: 512,
  max_pixels: 100000000,
  max_boxes_per_request: 64,
  verify_tls: true
})

const { status, request } = useBridge((event) => {
  if (event.type === 'run_start') {
    processRunId.value = event.run_id || ''
    processMessageIndex.value = null
    const runId = event.run_id || ''
    if (runId) {
      activeMetricsRunId.value = runId
      updateRunMetrics(runId, event.payload, true)
      for (let index = messages.value.length - 1; index >= 0; index -= 1) {
        if (messages.value[index].role === 'user' && !messages.value[index].run_id) {
          messages.value[index] = { ...messages.value[index], run_id: runId }
          break
        }
      }
    }
  }
  if (event.type === 'run_metrics') {
    const runId = event.run_id || String(event.payload.run_id || '')
    if (runId) {
      updateRunMetrics(runId, event.payload, Boolean(event.payload.running))
    }
  }
  if (event.type === 'stage_start') {
    addProcessMessage(`Pipeline 阶段开始：${stageLabel(String(event.payload.stage_name || ''))}`, event.run_id || '')
  }
  if (event.type === 'stage_end') {
    const artifact = (event.payload.artifact as Record<string, unknown> | undefined) ?? {}
    const state = event.payload.success === false ? '失败' : '完成'
    const summary = String(event.payload.summary || artifact.summary || '')
    addProcessMessage(
      [`Pipeline 阶段${state}：${stageLabel(String(event.payload.stage_name || ''))}`, summary]
        .filter(Boolean)
        .join('\n'),
      event.run_id || ''
    )
  }
  if (event.type === 'code_generated') {
    const code = String(event.payload.code || '')
    addProcessMessage(
      code ? `已生成待执行代码。\n${code}` : '已生成待执行代码。',
      event.run_id || ''
    )
  }
  if (event.type === 'thinking') {
    addProcessMessage(String(event.payload.message || '正在处理请求...'), event.run_id || '')
  }
  if (event.type === 'tool_start') {
    addProcessMessage(`调用工具：${String(event.payload.name || '')}`, event.run_id || '')
  }
  if (event.type === 'tool_end') {
    const result = event.payload.result as
      | { success?: boolean; error?: string; stderr?: string; stdout?: string; workspace_dir?: string }
      | undefined
    addProcessMessage(formatToolEndMessage(String(event.payload.name || ''), result), event.run_id || '')
  }
  if (event.type === 'confirm_request') {
    const payload = event.payload
    if (typeof payload.confirmation_id === 'string' && typeof payload.tool_name === 'string') {
      pendingConfirmation.value = {
        confirmation_id: payload.confirmation_id,
        tool_name: payload.tool_name,
        arguments: (payload.arguments as Record<string, unknown>) ?? {},
        description: typeof payload.description === 'string' ? payload.description : undefined,
        destructive: Boolean(payload.destructive),
        writes_project: Boolean(payload.writes_project)
      }
    }
  }
  if (event.type === 'confirm_resolved') {
    pendingConfirmation.value = null
    addProcessMessage(event.payload.approved ? '已确认工具操作。' : '已取消工具操作。', event.run_id || '')
  }
  if (event.type === 'error' || event.type === 'complete') {
    isSending.value = false
    if (event.type === 'complete') {
      void refreshAgentState()
    }
  }
  if (event.type === 'message_delta') {
    const delta = event.payload.delta
    if (typeof delta === 'string') {
      appendAssistantDelta(event.run_id || '', delta)
    }
  }
  if (event.type === 'message') {
    const content = event.payload.content
    if (typeof content === 'string') {
      finalizeAssistantMessage(event.run_id || '', content)
    }
  }
})

const canSubmit = computed(() => {
  if (isSending.value) {
    return activeSessionId.value.length > 0
  }
  return draft.value.trim().length > 0 && activeSessionId.value.length > 0
})

onMounted(async () => {
  clockTimer = window.setInterval(() => {
    clockMs.value = Date.now()
  }, 250)
  await bootstrapSession()
})

onUnmounted(() => {
  if (clockTimer !== undefined) {
    window.clearInterval(clockTimer)
  }
})

const activeRunMetrics = computed(() => {
  if (!activeMetricsRunId.value || !isSending.value) {
    return null
  }
  return runMetricsById.value[activeMetricsRunId.value] || null
})

async function bootstrapSession() {
  try {
    sessions.value = await request<SessionSummary[]>('listSessions', { limit: 20 })
    if (sessions.value.length > 0) {
      activeSessionId.value = sessions.value[0].id
      await loadMessages()
      await loadSettings()
      return
    }

    const session = await request<SessionSummary>('createSession', { title: 'QGIS 对话' })
    activeSessionId.value = session.id
    sessions.value = [session]
    await loadSettings()
  } catch (error) {
    console.error(error)
  }
}

async function loadMessages() {
  if (!activeSessionId.value) {
    return
  }
  const history = await request<ChatMessage[]>('getMessages', {
    session_id: activeSessionId.value,
    limit: 100
  })
  messages.value = normalizeHistoryMessages(history)
  streamingRunId.value = ''
  streamingMessageIndex.value = null
  processRunId.value = ''
  processMessageIndex.value = null
  runMetricsById.value = {}
  activeMetricsRunId.value = ''
  await refreshAgentState()
  await scrollConversation()
}

async function createSession() {
  const session = await request<SessionSummary>('createSession', { title: 'QGIS 对话' })
  sessions.value.unshift(session)
  activeSessionId.value = session.id
  messages.value = []
  pendingConfirmation.value = null
  streamingRunId.value = ''
  streamingMessageIndex.value = null
  processRunId.value = ''
  processMessageIndex.value = null
  runMetricsById.value = {}
  activeMetricsRunId.value = ''
  loadedSkills.value = []
  taskState.value = null
  await refreshAgentState()
  await scrollConversation()
}

async function refreshAgentState() {
  if (!activeSessionId.value) {
    loadedSkills.value = []
    taskState.value = null
    return
  }
  try {
    const [skills, task] = await Promise.all([
      request<LoadedSkillSummary[]>('listLoadedSkills', {
        session_id: activeSessionId.value
      }),
      request<TaskState | null>('getTaskState', {
        session_id: activeSessionId.value
      })
    ])
    loadedSkills.value = skills
    taskState.value = task
  } catch (error) {
    console.error(error)
  }
}

const completedPlanSteps = computed(() => {
  return taskState.value?.steps.filter((step) => ['completed', 'skipped'].includes(step.status)).length ?? 0
})

async function sendMessage() {
  const message = draft.value.trim()
  if (!message || !activeSessionId.value) {
    return
  }

  draft.value = ''
  messages.value.push({ role: 'user', content: message, event_type: 'user' })
  isSending.value = true
  activeMetricsRunId.value = ''
  processRunId.value = ''
  processMessageIndex.value = null
  await scrollConversation()
  await waitForPaint()
  try {
    await request('chat', { session_id: activeSessionId.value, message })
  } catch (error) {
    isSending.value = false
    throw error
  }
}

async function stopRun() {
  if (!activeSessionId.value) {
    return
  }
  isSending.value = false
  addProcessMessage('正在停止当前任务...', processRunId.value)
  await request('cancelRun', { session_id: activeSessionId.value })
}

async function handleComposerSubmit() {
  if (isSending.value) {
    await stopRun()
    return
  }
  await sendMessage()
}

async function loadSettings() {
  const loaded = await request<AppSettings>('getSettings')
  settings.value = loaded
  settingsForm.value = {
    provider: loaded.llm?.provider || 'openai_compatible',
    base_url: loaded.llm?.base_url || '',
    model: loaded.llm?.model || '',
    api_key: loaded.llm?.api_key || '',
    temperature: Number(loaded.llm?.temperature ?? 0.1),
    max_tokens: Number(loaded.llm?.max_tokens ?? 16384),
    max_context_tokens: Number(loaded.llm?.max_context_tokens ?? 32768),
    request_timeout_seconds: Number(loaded.llm?.request_timeout_seconds ?? 300)
  }
  sam3Form.value = {
    enabled: Boolean(loaded.sam3?.enabled ?? true),
    base_url: loaded.sam3?.base_url || 'http://127.0.0.1:8000',
    api_token: loaded.sam3?.api_token || '',
    connect_timeout_seconds: Number(loaded.sam3?.connect_timeout_seconds ?? 10),
    request_timeout_seconds: Number(loaded.sam3?.request_timeout_seconds ?? 1200),
    max_upload_mb: Number(loaded.sam3?.max_upload_mb ?? 512),
    max_pixels: Number(loaded.sam3?.max_pixels ?? 100000000),
    max_boxes_per_request: Number(loaded.sam3?.max_boxes_per_request ?? 64),
    verify_tls: Boolean(loaded.sam3?.verify_tls ?? true)
  }
}

async function openSettings() {
  settingsMessage.value = ''
  await loadSettings()
  settingsOpen.value = true
}

async function saveSettings() {
  const current = settings.value || { llm: {} }
  settingsSaving.value = true
  settingsMessage.value = ''
  try {
    const saved = await request<AppSettings>('saveSettings', {
      ...current,
      llm: {
        ...(current.llm || {}),
        provider: settingsForm.value.provider,
        base_url: settingsForm.value.base_url.trim(),
        model: settingsForm.value.model.trim(),
        api_key: settingsForm.value.api_key.trim(),
        temperature: Number(settingsForm.value.temperature),
        max_tokens: Number(settingsForm.value.max_tokens),
        max_context_tokens: Number(settingsForm.value.max_context_tokens),
        request_timeout_seconds: Number(settingsForm.value.request_timeout_seconds)
      },
      sam3: {
        ...(current.sam3 || {}),
        enabled: sam3Form.value.enabled,
        base_url: sam3Form.value.base_url.trim(),
        api_token: sam3Form.value.api_token.trim(),
        connect_timeout_seconds: Number(sam3Form.value.connect_timeout_seconds),
        request_timeout_seconds: Number(sam3Form.value.request_timeout_seconds),
        max_upload_mb: Number(sam3Form.value.max_upload_mb),
        max_pixels: Number(sam3Form.value.max_pixels),
        max_boxes_per_request: Number(sam3Form.value.max_boxes_per_request),
        verify_tls: sam3Form.value.verify_tls
      }
    })
    settings.value = saved
    settingsForm.value = {
      provider: saved.llm?.provider || 'openai_compatible',
      base_url: saved.llm?.base_url || '',
      model: saved.llm?.model || '',
      api_key: saved.llm?.api_key || '',
      temperature: Number(saved.llm?.temperature ?? 0.1),
      max_tokens: Number(saved.llm?.max_tokens ?? 16384),
      max_context_tokens: Number(saved.llm?.max_context_tokens ?? 32768),
      request_timeout_seconds: Number(saved.llm?.request_timeout_seconds ?? 300)
    }
    sam3Form.value = {
      enabled: Boolean(saved.sam3?.enabled ?? true),
      base_url: saved.sam3?.base_url || 'http://127.0.0.1:8000',
      api_token: saved.sam3?.api_token || '',
      connect_timeout_seconds: Number(saved.sam3?.connect_timeout_seconds ?? 10),
      request_timeout_seconds: Number(saved.sam3?.request_timeout_seconds ?? 1200),
      max_upload_mb: Number(saved.sam3?.max_upload_mb ?? 512),
      max_pixels: Number(saved.sam3?.max_pixels ?? 100000000),
      max_boxes_per_request: Number(saved.sam3?.max_boxes_per_request ?? 64),
      verify_tls: Boolean(saved.sam3?.verify_tls ?? true)
    }
    settingsMessage.value = '设置已保存。'
  } catch (error) {
    settingsMessage.value = `设置保存失败：${String(error)}`
  } finally {
    settingsSaving.value = false
  }
}

async function testSam3Connection() {
  sam3Testing.value = true
  settingsMessage.value = '正在检查 SAM3 服务...'
  try {
    const result = await request<Record<string, unknown>>('testSam3Connection', {
      sam3: {
        enabled: sam3Form.value.enabled,
        base_url: sam3Form.value.base_url.trim(),
        api_token: sam3Form.value.api_token.trim(),
        connect_timeout_seconds: Number(sam3Form.value.connect_timeout_seconds),
        request_timeout_seconds: Number(sam3Form.value.request_timeout_seconds),
        verify_tls: sam3Form.value.verify_tls
      }
    })
    settingsMessage.value = result.success
      ? `SAM3 已就绪：${String(result.device || '')}，${Number(result.latency_ms || 0)} ms`
      : `SAM3 连接失败：${String(result.error || '模型未就绪')}`
  } catch (error) {
    settingsMessage.value = `SAM3 连接失败：${String(error)}`
  } finally {
    sam3Testing.value = false
  }
}

async function resolveConfirmation(approved: boolean) {
  if (!pendingConfirmation.value || !activeSessionId.value) {
    return
  }
  const confirmation = pendingConfirmation.value
  pendingConfirmation.value = null
  isSending.value = true
  activeMetricsRunId.value = ''
  try {
    await request('confirmToolCall', {
      session_id: activeSessionId.value,
      confirmation_id: confirmation.confirmation_id,
      approved
    })
  } catch (error) {
    isSending.value = false
    throw error
  }
}

function addProcessMessage(content: string, runId = '') {
  const normalizedRunId = runId || processRunId.value
  const index = processMessageIndex.value
  if (
    processRunId.value === normalizedRunId &&
    index !== null &&
    messages.value[index]?.role === 'system' &&
    messages.value[index]?.event_type === 'process'
  ) {
    messages.value[index] = {
      ...messages.value[index],
      content: `${messages.value[index].content}\n${content}`
    }
  } else {
    messages.value.push({
      role: 'system',
      content,
      event_type: 'process',
      run_id: normalizedRunId
    })
    processRunId.value = normalizedRunId
    processMessageIndex.value = messages.value.length - 1
  }
  void scrollConversation()
}

function formatToolEndMessage(
  name: string,
  result?: { success?: boolean; error?: string; stderr?: string; stdout?: string; workspace_dir?: string }
) {
  const lines = [`${result?.success === false ? '工具失败' : '工具完成'}：${name}`]
  if (name === 'execute_gis_code') {
    if (result?.workspace_dir) {
      lines.push(`工作目录：${result.workspace_dir}`)
    }
    if (result?.error) {
      lines.push(`error：${result.error}`)
    }
    if (result?.stderr) {
      lines.push(`stderr：${result.stderr.slice(-1000)}`)
    }
    if (result?.stdout) {
      lines.push(`stdout：${result.stdout.slice(-500)}`)
    }
  }
  return lines.join('\n')
}

function formatConfirmationArguments(confirmation: PendingConfirmation) {
  return JSON.stringify(confirmation.arguments, null, 2)
}

function normalizeHistoryMessages(history: ChatMessage[]) {
  const normalized: ChatMessage[] = []
  for (const rawMessage of history) {
    const message = normalizeHistoryMessage(rawMessage)
    const previous = normalized[normalized.length - 1]
    if (
      message.role === 'system' &&
      message.event_type === 'process' &&
      previous?.role === 'system' &&
      previous.event_type === 'process'
    ) {
      normalized[normalized.length - 1] = {
        ...previous,
        content: `${previous.content}\n${message.content}`
      }
    } else {
      normalized.push(message)
    }
  }
  return normalized
}

function normalizeHistoryMessage(message: ChatMessage): ChatMessage {
  if (message.role === 'user') {
    return { ...message, event_type: 'user' }
  }
  if (
    message.role === 'system' ||
    message.event_type === 'process' ||
    message.event_type === 'stage_artifact'
  ) {
    return { ...message, role: 'system', event_type: 'process' }
  }
  if (message.role === 'assistant') {
    return {
      ...message,
      event_type: message.event_type === 'error' ? 'error' : 'summary'
    }
  }
  return message
}

function stageLabel(name: string) {
  const labels: Record<string, string> = {
    data_overview: '数据盘点',
    structured_query: '结构化需求',
    solution_plan: '处理方案',
    generated_code: '代码生成',
    execution_result: '执行结果'
  }
  return labels[name] || name
}

function appendAssistantDelta(runId: string, delta: string) {
  if (streamingRunId.value !== runId || streamingMessageIndex.value === null) {
    streamingRunId.value = runId
    messages.value.push({
      role: 'assistant',
      content: '',
      event_type: 'streaming',
      run_id: runId
    })
    streamingMessageIndex.value = messages.value.length - 1
  }
  const index = streamingMessageIndex.value
  messages.value[index] = {
    ...messages.value[index],
    content: `${messages.value[index].content}${delta}`
  }
  void scrollConversation()
}

function finalizeAssistantMessage(runId: string, content: string) {
  if (streamingRunId.value === runId && streamingMessageIndex.value !== null) {
    messages.value[streamingMessageIndex.value] = {
      role: 'assistant',
      content,
      event_type: 'summary',
      run_id: runId
    }
  } else {
    messages.value.push({
      role: 'assistant',
      content,
      event_type: 'summary',
      run_id: runId
    })
  }
  streamingRunId.value = ''
  streamingMessageIndex.value = null
  void scrollConversation()
}

function updateRunMetrics(
  runId: string,
  payload: Record<string, unknown>,
  running: boolean
) {
  const previous = runMetricsById.value[runId]
  const startedAt = Number(payload.started_at ?? previous?.started_at ?? Date.now() / 1000)
  const next: RunMetrics = {
    run_id: runId,
    started_at: Number.isFinite(startedAt) ? startedAt : Date.now() / 1000,
    duration_ms: metricNumber(payload.duration_ms, previous?.duration_ms),
    input_tokens: metricNumber(payload.input_tokens, previous?.input_tokens),
    output_tokens: metricNumber(payload.output_tokens, previous?.output_tokens),
    total_tokens: metricNumber(payload.total_tokens, previous?.total_tokens),
    llm_calls: metricNumber(payload.llm_calls, previous?.llm_calls),
    usage_estimated: Boolean(payload.usage_estimated ?? previous?.usage_estimated),
    status: String(payload.status ?? previous?.status ?? (running ? 'running' : 'completed')),
    running
  }
  runMetricsById.value = { ...runMetricsById.value, [runId]: next }
}

function metricNumber(value: unknown, fallback = 0) {
  const number = Number(value ?? fallback)
  return Number.isFinite(number) ? Math.max(0, number) : Math.max(0, fallback)
}

function metricsForMessage(message: ChatMessage): RunMetrics | null {
  if (message.role !== 'assistant') {
    return null
  }
  if (message.run_id && runMetricsById.value[message.run_id]) {
    return runMetricsById.value[message.run_id]
  }
  if (message.duration_ms == null && message.total_tokens == null) {
    return null
  }
  return {
    run_id: message.run_id || '',
    started_at: 0,
    duration_ms: metricNumber(message.duration_ms),
    input_tokens: metricNumber(message.input_tokens),
    output_tokens: metricNumber(message.output_tokens),
    total_tokens: metricNumber(message.total_tokens),
    llm_calls: metricNumber(message.llm_calls),
    usage_estimated: Boolean(message.usage_estimated),
    status: message.metrics_status || 'completed',
    running: false
  }
}

function elapsedMs(metrics: RunMetrics) {
  if (!metrics.running || !metrics.started_at) {
    return metrics.duration_ms
  }
  return Math.max(metrics.duration_ms, clockMs.value - metrics.started_at * 1000)
}

function formatDuration(durationMs: number) {
  const seconds = Math.max(0, durationMs) / 1000
  if (seconds < 60) {
    return `${seconds.toFixed(1)} 秒`
  }
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = Math.floor(seconds % 60)
  return `${minutes} 分 ${remainingSeconds} 秒`
}

function formatTokens(tokens: number) {
  return Math.max(0, Math.round(tokens)).toLocaleString()
}

async function scrollConversation() {
  await nextTick()
  scrollConversationNow()
  window.requestAnimationFrame(() => {
    scrollConversationNow()
    window.requestAnimationFrame(scrollConversationNow)
  })
  window.setTimeout(scrollConversationNow, 80)
}

function scrollConversationNow() {
  const element = conversationRef.value
  if (!element) {
    return
  }
  element.scrollTop = element.scrollHeight
  conversationEndRef.value?.scrollIntoView({ block: 'end' })
}

async function waitForPaint() {
  await new Promise<void>((resolve) => {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => resolve())
    })
  })
}
</script>

<template>
  <main class="app-shell">
    <header class="topbar">
      <div>
        <h1>AI GIS Agent</h1>
        <p>{{ status }} · {{ activeSessionId ? '会话已就绪' : '正在创建会话' }}</p>
      </div>
      <div class="topbar-actions">
        <button type="button" class="icon-action" title="设置" @click="openSettings">⚙</button>
        <button type="button" class="icon-action" title="新建会话" @click="createSession">+</button>
      </div>
    </header>

    <section v-if="loadedSkills.length || taskState" class="agent-state-bar" aria-label="Agent 状态">
      <span v-if="loadedSkills.length">
        Skills：{{ loadedSkills.map((skill) => skill.name).join(' · ') }}
      </span>
      <span v-if="taskState">
        计划：{{ completedPlanSteps }}/{{ taskState.steps.length }} · {{ taskState.status }}
      </span>
    </section>

    <section ref="conversationRef" class="conversation" aria-live="polite">
      <div v-if="messages.length === 0" class="empty-state">
        可以开始输入一个 GIS 任务。
      </div>
      <article
        v-for="(message, index) in messages"
        :key="message.id ?? `${message.role}-${index}`"
        class="message-row"
        :class="[message.role, message.event_type]"
      >
        <span>{{ message.role === 'user' ? '你' : message.role === 'system' ? '过程' : 'Agent' }}</span>
        <p>{{ message.content }}</p>
        <div v-if="metricsForMessage(message)" class="message-metrics">
          <span>耗时 {{ formatDuration(elapsedMs(metricsForMessage(message)!)) }}</span>
          <span>
            Token {{ formatTokens(metricsForMessage(message)!.total_tokens) }}
            （输入 {{ formatTokens(metricsForMessage(message)!.input_tokens) }} /
            输出 {{ formatTokens(metricsForMessage(message)!.output_tokens) }}）
          </span>
          <span>模型调用 {{ metricsForMessage(message)!.llm_calls }} 次</span>
          <em v-if="metricsForMessage(message)!.usage_estimated">估算</em>
        </div>
      </article>
      <div ref="conversationEndRef" class="conversation-end" aria-hidden="true" />
    </section>

    <section v-if="activeRunMetrics" class="live-metrics" aria-live="polite">
      <strong>本轮任务</strong>
      <span>耗时 {{ formatDuration(elapsedMs(activeRunMetrics)) }}</span>
      <span>
        Token {{ formatTokens(activeRunMetrics.total_tokens) }}
        （输入 {{ formatTokens(activeRunMetrics.input_tokens) }} /
        输出 {{ formatTokens(activeRunMetrics.output_tokens) }}）
      </span>
      <span>模型调用 {{ activeRunMetrics.llm_calls }} 次</span>
      <em v-if="activeRunMetrics.usage_estimated">Token 为估算值</em>
    </section>

    <section v-if="pendingConfirmation" class="confirm-bar" aria-live="polite">
      <div>
        <strong>{{ pendingConfirmation.tool_name }}</strong>
        <p>{{ pendingConfirmation.description || '该工具需要确认后执行。' }}</p>
        <pre
          v-if="pendingConfirmation.tool_name === 'execute_gis_code'"
          class="confirm-code-preview"
        >{{ formatConfirmationArguments(pendingConfirmation) }}</pre>
      </div>
      <button type="button" class="secondary-action" @click="resolveConfirmation(false)">取消</button>
      <button type="button" class="danger-action" @click="resolveConfirmation(true)">确认</button>
    </section>

    <section v-if="settingsOpen" class="settings-panel" aria-label="AI 设置">
      <header>
        <strong>AI 设置</strong>
        <button type="button" class="icon-action" title="关闭设置" @click="settingsOpen = false">×</button>
      </header>
      <label>
        <span>提供商</span>
        <select v-model="settingsForm.provider">
          <option
            v-for="option in providerOptions"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </option>
        </select>
      </label>
      <label>
        <span>Base URL</span>
        <input v-model="settingsForm.base_url" type="text" placeholder="http://10.0.19.214:11430/v1" />
      </label>
      <label>
        <span>模型名</span>
        <input v-model="settingsForm.model" type="text" placeholder="例如 gemma-4-31B-it-Q4:latest" />
      </label>
      <label>
        <span>API Key</span>
        <input v-model="settingsForm.api_key" type="password" placeholder="本地 Ollama 可留空" />
      </label>
      <div class="settings-grid">
        <label>
          <span>Temperature</span>
          <input
            v-model.number="settingsForm.temperature"
            type="number"
            min="0"
            max="2"
            step="0.1"
          />
        </label>
        <label>
          <span>Max Tokens</span>
          <input
            v-model.number="settingsForm.max_tokens"
            type="number"
            min="256"
            max="200000"
            step="512"
          />
        </label>
        <label>
          <span>Context Window</span>
          <input
            v-model.number="settingsForm.max_context_tokens"
            type="number"
            min="4096"
            max="1000000"
            step="4096"
          />
        </label>
        <label>
          <span>请求超时（秒）</span>
          <input
            v-model.number="settingsForm.request_timeout_seconds"
            type="number"
            min="10"
            max="3600"
            step="30"
          />
        </label>
      </div>
      <div class="settings-divider">
        <strong>SAM3 遥感分割服务</strong>
        <label class="settings-check">
          <input v-model="sam3Form.enabled" type="checkbox" />
          <span>启用</span>
        </label>
      </div>
      <label>
        <span>SAM3 Base URL</span>
        <input v-model="sam3Form.base_url" type="text" placeholder="http://127.0.0.1:8000" />
      </label>
      <label>
        <span>API Token（可选）</span>
        <input v-model="sam3Form.api_token" type="password" placeholder="无鉴权服务可留空" />
      </label>
      <div class="settings-grid">
        <label>
          <span>连接超时（秒）</span>
          <input v-model.number="sam3Form.connect_timeout_seconds" type="number" min="1" max="300" />
        </label>
        <label>
          <span>推理超时（秒）</span>
          <input v-model.number="sam3Form.request_timeout_seconds" type="number" min="10" max="7200" step="30" />
        </label>
        <label>
          <span>最大上传（MB）</span>
          <input v-model.number="sam3Form.max_upload_mb" type="number" min="1" max="10240" />
        </label>
        <label>
          <span>最大像元数</span>
          <input v-model.number="sam3Form.max_pixels" type="number" min="1" step="1000000" />
        </label>
        <label>
          <span>单次最大框数</span>
          <input v-model.number="sam3Form.max_boxes_per_request" type="number" min="1" max="10000" />
        </label>
        <label class="settings-check">
          <input v-model="sam3Form.verify_tls" type="checkbox" />
          <span>校验 HTTPS 证书</span>
        </label>
      </div>
      <button type="button" class="secondary-action" :disabled="sam3Testing" @click="testSam3Connection">
        {{ sam3Testing ? '检查中...' : '测试 SAM3 连接' }}
      </button>
      <footer>
        <p>{{ settingsMessage }}</p>
        <button type="button" class="secondary-action" @click="settingsOpen = false">取消</button>
        <button type="button" class="primary-action" :disabled="settingsSaving" @click="saveSettings">
          {{ settingsSaving ? '保存中' : '保存' }}
        </button>
      </footer>
    </section>

    <form class="composer" @submit.prevent="handleComposerSubmit">
      <textarea v-model="draft" rows="3" placeholder="输入 GIS 任务..." />
      <button
        type="submit"
        :class="{ stop: isSending }"
        :disabled="!canSubmit"
      >
        {{ isSending ? '停止' : '发送' }}
      </button>
    </form>
  </main>
</template>
