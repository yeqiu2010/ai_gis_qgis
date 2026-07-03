<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useBridge } from './composables/useBridge'
import type {
  AppSettings,
  ChatMessage,
  PendingConfirmation,
  SessionSummary
} from './types/protocol'

const draft = ref('')
const messages = ref<ChatMessage[]>([])
const sessions = ref<SessionSummary[]>([])
const activeSessionId = ref('')
const isSending = ref(false)
const pendingConfirmation = ref<PendingConfirmation | null>(null)
const conversationRef = ref<HTMLElement | null>(null)
const conversationEndRef = ref<HTMLElement | null>(null)
const streamingRunId = ref('')
const streamingMessageIndex = ref<number | null>(null)
const processRunId = ref('')
const processMessageIndex = ref<number | null>(null)
const settingsOpen = ref(false)
const settingsSaving = ref(false)
const settingsMessage = ref('')
const settings = ref<AppSettings | null>(null)
const settingsForm = ref({
  provider: 'openai_compatible',
  base_url: '',
  model: '',
  api_key: '',
  temperature: 0.1,
  max_tokens: 4096
})
const providerOptions = [
  { value: 'openai_compatible', label: 'OpenAI Compatible' },
  { value: 'openai', label: 'OpenAI' },
  { value: 'ollama', label: 'Ollama' },
  { value: 'echo', label: 'Echo（离线测试）' }
]

const { status, request } = useBridge((event) => {
  if (event.type === 'run_start') {
    processRunId.value = event.run_id || ''
    processMessageIndex.value = null
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
  await bootstrapSession()
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
  await scrollConversation()
}

async function sendMessage() {
  const message = draft.value.trim()
  if (!message || !activeSessionId.value) {
    return
  }

  draft.value = ''
  messages.value.push({ role: 'user', content: message, event_type: 'user' })
  isSending.value = true
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
    max_tokens: Number(loaded.llm?.max_tokens ?? 4096)
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
        max_tokens: Number(settingsForm.value.max_tokens)
      }
    })
    settings.value = saved
    settingsForm.value = {
      provider: saved.llm?.provider || 'openai_compatible',
      base_url: saved.llm?.base_url || '',
      model: saved.llm?.model || '',
      api_key: saved.llm?.api_key || '',
      temperature: Number(saved.llm?.temperature ?? 0.1),
      max_tokens: Number(saved.llm?.max_tokens ?? 4096)
    }
    settingsMessage.value = '设置已保存。'
  } finally {
    settingsSaving.value = false
  }
}

async function resolveConfirmation(approved: boolean) {
  if (!pendingConfirmation.value || !activeSessionId.value) {
    return
  }
  const confirmation = pendingConfirmation.value
  pendingConfirmation.value = null
  await request('confirmToolCall', {
    session_id: activeSessionId.value,
    confirmation_id: confirmation.confirmation_id,
    approved
  })
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
      event_type: 'process'
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
      event_type: 'streaming'
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
      event_type: 'summary'
    }
  } else {
    messages.value.push({
      role: 'assistant',
      content,
      event_type: 'summary'
    })
  }
  streamingRunId.value = ''
  streamingMessageIndex.value = null
  void scrollConversation()
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
      </article>
      <div ref="conversationEndRef" class="conversation-end" aria-hidden="true" />
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
      </div>
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
