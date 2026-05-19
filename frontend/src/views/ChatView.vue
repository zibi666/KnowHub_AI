<script setup lang="ts">
import { computed, nextTick, onMounted, ref, type CSSProperties } from 'vue'
import { ArrowDown, ChevronDown, LogOut, Maximize2, Minimize2, Paperclip, Plus, Send, Settings, X } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { ApiError, apiFetch, localizeApiMessage, readCookie, streamJsonLines } from '../api/client'
import ChatMessage from '../components/ChatMessage.vue'
import { useAuthStore } from '../stores/auth'
import type { ApiKeyEntry, ApiKeyGroup, Attachment, Conversation, Message, User } from '../types'

type ThemeMode = 'dark' | 'light'
type SettingsTab = 'appearance' | 'api' | 'groups' | 'account'

const bubbleOptions = [
  { value: 'blue', label: '蓝色', bg: '#075a9f', hover: '#0b65b8', shadow: 'rgba(0,78,150,0.25)' },
  { value: 'green', label: '绿色', bg: '#047857', hover: '#059669', shadow: 'rgba(4,120,87,0.25)' },
  { value: 'purple', label: '紫色', bg: '#6d28d9', hover: '#7c3aed', shadow: 'rgba(109,40,217,0.24)' },
  { value: 'rose', label: '玫红', bg: '#be123c', hover: '#e11d48', shadow: 'rgba(190,18,60,0.24)' },
  { value: 'amber', label: '琥珀', bg: '#b45309', hover: '#d97706', shadow: 'rgba(180,83,9,0.24)' }
] as const

type BubbleColor = (typeof bubbleOptions)[number]['value']

const DEFAULT_MODEL = 'gpt-5.5'
const DEFAULT_CONTEXT_WINDOW_TOKENS = 258_000
const THEME_STORAGE_KEY = 'private-gpt-theme'
const BUBBLE_STORAGE_KEY = 'private-gpt-bubble'
const TEXT_SIZE_STORAGE_KEY = 'private-gpt-text-size'
const CODE_SIZE_STORAGE_KEY = 'private-gpt-code-size'
const REASONING_STORAGE_KEY = 'private-gpt-reasoning-effort'

type ReasoningEffort = 'low' | 'medium' | 'high' | 'xhigh'
const reasoningOptions: Array<{ value: ReasoningEffort; label: string; hint: string }> = [
  { value: 'low', label: '低', hint: '最快，适合闲聊 / 简短答疑' },
  { value: 'medium', label: '中', hint: '默认。日常使用平衡速度与质量' },
  { value: 'high', label: '高', hint: '更深思考，适合复杂分析' },
  { value: 'xhigh', label: '极致', hint: '最大推理预算，最慢，仅复杂难题' }
]

const textSizeOptions = [13, 14, 15, 16, 17, 18, 19]
const codeSizeOptions = [11, 12, 13, 14, 15, 16]
const themeOptions: Array<{ value: ThemeMode; label: string }> = [
  { value: 'dark', label: '暗色' },
  { value: 'light', label: '浅色' }
]

const auth = useAuthStore()
const router = useRouter()

const conversations = ref<Conversation[]>([])
const currentId = ref<string | null>(null)
const messages = ref<Message[]>([])
const input = ref('')
const composerExpanded = ref(false)
const models = ref<string[]>([])
const selectedModel = ref('')
const modelMenuOpen = ref(false)
const reasoningMenuOpen = ref(false)
const reasoningEffort = ref<ReasoningEffort>('medium')
const streaming = ref(false)
const pendingAttachments = ref<Attachment[]>([])
const error = ref('')
const contextStats = ref({
  promptTokensEstimated: 0,
  contextWindowTokens: DEFAULT_CONTEXT_WINDOW_TOKENS,
  promptBudgetTokens: Math.floor(DEFAULT_CONTEXT_WINDOW_TOKENS * 0.75),
  hasActiveCompaction: false,
  includedHistoryMessages: 0,
  includedAttachmentCount: 0,
  wasTrimmed: false,
  messagesToRefineCount: 0,
  remainingContextTokens: DEFAULT_CONTEXT_WINDOW_TOKENS,
  summaryUsed: false,
  branchMessageCount: 0,
  tokensSource: 'estimated' as 'estimated' | 'actual'
})
const messageScroller = ref<HTMLElement | null>(null)
const showScrollToBottom = ref(false)
let userHasScrolledUp = false

const themeMode = ref<ThemeMode>('dark')
const bubbleColor = ref<BubbleColor>('blue')
const textSize = ref(15)
const codeSize = ref(12)
const settingsMenuOpen = ref(false)
const settingsOpen = ref(false)
const settingsTab = ref<SettingsTab>('appearance')
const settingsNotice = ref('')
const settingsError = ref('')
const settingsLoading = ref(false)
const apiKeys = ref<ApiKeyEntry[]>([])
const apiKeyGroups = ref<ApiKeyGroup[]>([])
const selectedGroupId = ref('ungrouped')
const selectedGroupKeys = ref<ApiKeyEntry[]>([])
const groupKeysLoading = ref(false)
const deletingConversationId = ref<string | null>(null)
const selectedApiKeyGroup = computed(() => apiKeyGroups.value.find((group) => group.id === selectedGroupId.value) || null)
const keyDrafts = ref<Record<string, { name: string; groupId: string }>>({})
const groupDrafts = ref<Record<string, { name: string; description: string }>>({})
const newApiKey = ref({ name: '默认密钥', apiKey: '', groupId: '', makeActive: true })
const newGroup = ref({ name: '', description: '' })
const profileUsername = ref('')
const profilePassword = ref('')
const currentPassword = ref('')
const replacementPassword = ref('')

let scrollFrame: number | null = null
let streamFlushFrame: number | null = null
let streamTextBuffer = ''
let streamTargetMessage: Message | null = null

const currentConversation = computed(() => conversations.value.find((item) => item.id === currentId.value))
const conversationUsage = computed(() => {
  const assistantMessages = messages.value.filter((message) => message.role === 'assistant')
  return {
    tokens: assistantMessages.reduce((total, message) => total + (Number(message.totalTokens) || 0), 0),
    requests: assistantMessages.length
  }
})
const shellClass = computed(() => (themeMode.value === 'dark' ? 'theme-dark' : 'theme-light'))
const selectedBubble = computed(() => bubbleOptions.find((item) => item.value === bubbleColor.value) || bubbleOptions[0])
const contextTotalTokens = computed(() => contextStats.value.contextWindowTokens || DEFAULT_CONTEXT_WINDOW_TOKENS)
const contextUsedTokens = computed(() => {
  const pendingInputTokens = estimateDisplayTokens(input.value)
  return Math.min(contextTotalTokens.value, contextStats.value.promptTokensEstimated + pendingInputTokens)
})
const contextPercent = computed(() => Math.min(100, Math.round((contextUsedTokens.value / contextTotalTokens.value) * 100)))
const contextPercentLabel = computed(() => {
  if (contextUsedTokens.value > 0 && contextPercent.value < 1) return '<1%'
  return `${contextPercent.value}%`
})
const contextRingStyle = computed(
  () =>
    ({
      '--context-progress': `${contextPercent.value}%`
    }) as CSSProperties
)
const shellStyle = computed(
  () =>
    ({
      '--bubble-bg': selectedBubble.value.bg,
      '--bubble-hover': selectedBubble.value.hover,
      '--bubble-shadow': selectedBubble.value.shadow,
      '--message-font-size': `${textSize.value}px`,
      '--user-message-font-size': `${Math.max(13, textSize.value - 0.5)}px`,
      '--code-font-size': `${codeSize.value}px`
    }) as CSSProperties
)

const parseStatusText: Record<string, string> = {
  pending: '等待解析',
  parsing: '解析中',
  success: '解析完成',
  failed: '解析失败'
}

function estimateDisplayTokens(text: string) {
  let score = 0
  for (const char of text) {
    if (/\s/.test(char)) continue
    score += char.charCodeAt(0) > 127 ? 1 : 0.25
  }
  return Math.ceil(score)
}

function formatTokenCount(value: number) {
  if (value >= 1000) return `${Math.round(value / 1000)}k`
  return value.toLocaleString()
}

function updateContextStats(data: any, source: 'estimated' | 'actual' = 'estimated') {
  const promptTokens = data.prompt_tokens_estimated ?? data.promptTokensEstimated ?? data.prompt_tokens ?? data.promptTokens ?? 0
  const contextWindow = data.context_window_tokens ?? data.contextWindowTokens ?? contextStats.value.contextWindowTokens
  const promptBudget = data.prompt_budget_tokens ?? data.promptBudgetTokens ?? contextStats.value.promptBudgetTokens
  contextStats.value = {
    promptTokensEstimated: Number(promptTokens) || 0,
    contextWindowTokens: Number(contextWindow) || DEFAULT_CONTEXT_WINDOW_TOKENS,
    promptBudgetTokens: Number(promptBudget) || Math.floor(DEFAULT_CONTEXT_WINDOW_TOKENS * 0.75),
    hasActiveCompaction: Boolean(data.has_active_compaction ?? data.hasActiveCompaction ?? false),
    includedHistoryMessages: Number(data.included_history_messages ?? data.includedHistoryMessages ?? 0),
    includedAttachmentCount: Number(data.included_attachment_count ?? data.includedAttachmentCount ?? 0),
    wasTrimmed: Boolean(data.was_trimmed ?? data.wasTrimmed ?? false),
    messagesToRefineCount: Number(data.messages_to_refine_count ?? data.messagesToRefineCount ?? 0),
    remainingContextTokens: Number(data.remaining_context_tokens ?? data.remainingContextTokens ?? 0),
    summaryUsed: Boolean(data.summary_used ?? data.summaryUsed ?? false),
    branchMessageCount: Number(data.branch_message_count ?? data.branchMessageCount ?? 0),
    tokensSource: source
  }
}

function preferredModel(modelsList: string[], selected?: string) {
  if (selected) return selected
  if (modelsList.includes(DEFAULT_MODEL)) return DEFAULT_MODEL
  const gpt55 = modelsList.find((model) => model.toLowerCase().includes('gpt-5.5'))
  return gpt55 || modelsList[0] || DEFAULT_MODEL
}

function loadAppearance() {
  const storedTheme = window.localStorage.getItem(THEME_STORAGE_KEY)
  if (storedTheme === 'dark' || storedTheme === 'light') themeMode.value = storedTheme

  const storedBubble = window.localStorage.getItem(BUBBLE_STORAGE_KEY)
  if (bubbleOptions.some((item) => item.value === storedBubble)) bubbleColor.value = storedBubble as BubbleColor

  const storedTextSize = Number(window.localStorage.getItem(TEXT_SIZE_STORAGE_KEY))
  if (storedTextSize >= 13 && storedTextSize <= 19) textSize.value = storedTextSize

  const storedCodeSize = Number(window.localStorage.getItem(CODE_SIZE_STORAGE_KEY))
  if (storedCodeSize >= 11 && storedCodeSize <= 16) codeSize.value = storedCodeSize

  const storedReasoning = window.localStorage.getItem(REASONING_STORAGE_KEY)
  if (reasoningOptions.some((item) => item.value === storedReasoning)) {
    reasoningEffort.value = storedReasoning as ReasoningEffort
  }

}

function setReasoningEffort(effort: ReasoningEffort) {
  reasoningEffort.value = effort
  window.localStorage.setItem(REASONING_STORAGE_KEY, effort)
  reasoningMenuOpen.value = false
}

const reasoningLabel = computed(() => reasoningOptions.find((item) => item.value === reasoningEffort.value)?.label || '中')

function toggleModelMenu() {
  const shouldOpen = !modelMenuOpen.value
  closeFloatingMenus()
  modelMenuOpen.value = shouldOpen
}

function toggleReasoningMenu() {
  const shouldOpen = !reasoningMenuOpen.value
  closeFloatingMenus()
  reasoningMenuOpen.value = shouldOpen
}

function setTheme(mode: ThemeMode) {
  themeMode.value = mode
  window.localStorage.setItem(THEME_STORAGE_KEY, mode)
  document.querySelector('.app-root')?.classList.toggle('theme-light', mode === 'light')
  document.querySelector('.app-root')?.classList.toggle('theme-dark', mode === 'dark')
}

function setBubbleColor(color: BubbleColor) {
  bubbleColor.value = color
  window.localStorage.setItem(BUBBLE_STORAGE_KEY, color)
}

function saveTextSize() {
  window.localStorage.setItem(TEXT_SIZE_STORAGE_KEY, String(textSize.value))
}

function saveCodeSize() {
  window.localStorage.setItem(CODE_SIZE_STORAGE_KEY, String(codeSize.value))
}

function resetSettingsMessages() {
  settingsNotice.value = ''
  settingsError.value = ''
}

function keyDraftFor(key: ApiKeyEntry) {
  if (!keyDrafts.value[key.id]) {
    keyDrafts.value[key.id] = { name: key.name, groupId: key.groupId || '' }
  }
  return keyDrafts.value[key.id]
}

function groupDraftFor(group: ApiKeyGroup) {
  if (!groupDrafts.value[group.id]) {
    groupDrafts.value[group.id] = { name: group.name, description: group.description || '' }
  }
  return groupDrafts.value[group.id]
}

async function loadGroupKeys(groupId = selectedGroupId.value) {
  if (auth.user?.role !== 'admin') return
  selectedGroupId.value = groupId || 'ungrouped'
  groupKeysLoading.value = true
  try {
    selectedGroupKeys.value = await apiFetch<ApiKeyEntry[]>(
      `/admin/api-key-groups/${encodeURIComponent(selectedGroupId.value)}/api-keys`
    )
  } catch (err) {
    selectedGroupKeys.value = []
    settingsError.value = err instanceof Error ? err.message : '加载分组密钥失败'
  } finally {
    groupKeysLoading.value = false
  }
}

async function loadApiSettings() {
  settingsLoading.value = true
  try {
    const [keys, groups] = await Promise.all([apiFetch<ApiKeyEntry[]>('/api-keys'), apiFetch<ApiKeyGroup[]>('/api-key-groups')])
    apiKeys.value = keys
    apiKeyGroups.value = groups
    if (!selectedGroupId.value) selectedGroupId.value = 'ungrouped'
    if (selectedGroupId.value !== 'ungrouped' && !groups.some((group) => group.id === selectedGroupId.value)) {
      selectedGroupId.value = 'ungrouped'
    }
    keyDrafts.value = Object.fromEntries(keys.map((key) => [key.id, { name: key.name, groupId: key.groupId || '' }]))
    groupDrafts.value = Object.fromEntries(groups.map((group) => [group.id, { name: group.name, description: group.description || '' }]))
    if (auth.user?.role === 'admin') await loadGroupKeys(selectedGroupId.value)
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '加载 API 管理失败'
  } finally {
    settingsLoading.value = false
  }
}

async function openSettings(tab: SettingsTab = 'appearance') {
  const targetTab = tab === 'groups' && auth.user?.role !== 'admin' ? 'appearance' : tab
  settingsMenuOpen.value = false
  settingsOpen.value = true
  settingsTab.value = targetTab
  profileUsername.value = auth.user?.username || ''
  resetSettingsMessages()
  if (targetTab === 'api' || targetTab === 'groups') await loadApiSettings()
}

function toggleSettingsMenu() {
  settingsMenuOpen.value = !settingsMenuOpen.value
}

function openAdminMonitor() {
  settingsMenuOpen.value = false
  void router.push('/admin')
}

async function selectSettingsTab(tab: SettingsTab) {
  if (tab === 'groups' && auth.user?.role !== 'admin') return
  settingsTab.value = tab
  resetSettingsMessages()
  if ((tab === 'api' || tab === 'groups') && !apiKeys.value.length && !apiKeyGroups.value.length) await loadApiSettings()
  if (tab === 'groups') await loadGroupKeys(selectedGroupId.value)
}

async function createApiKey() {
  resetSettingsMessages()
  try {
    await apiFetch<ApiKeyEntry>('/api-keys', {
      method: 'POST',
      body: JSON.stringify({
        name: newApiKey.value.name,
        apiKey: newApiKey.value.apiKey,
        groupId: newApiKey.value.groupId || null,
        makeActive: newApiKey.value.makeActive
      })
    })
    newApiKey.value = { name: '默认密钥', apiKey: '', groupId: '', makeActive: true }
    settingsNotice.value = '密钥已添加'
    await loadApiSettings()
    await auth.loadMe()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '添加密钥失败'
  }
}

async function saveApiKey(key: ApiKeyEntry) {
  resetSettingsMessages()
  const draft = keyDraftFor(key)
  try {
    await apiFetch<ApiKeyEntry>(`/api-keys/${key.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: draft.name, groupId: draft.groupId || null })
    })
    settingsNotice.value = '密钥信息已保存'
    await loadApiSettings()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '保存密钥失败'
  }
}

async function activateApiKey(key: ApiKeyEntry) {
  resetSettingsMessages()
  try {
    await apiFetch<ApiKeyEntry>(`/api-keys/${key.id}/activate`, { method: 'POST' })
    settingsNotice.value = '已切换当前使用密钥'
    await loadApiSettings()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '切换密钥失败'
  }
}

async function deleteApiKey(key: ApiKeyEntry) {
  resetSettingsMessages()
  if (!window.confirm(`删除密钥「${key.name}」？`)) return
  try {
    await apiFetch(`/api-keys/${key.id}`, { method: 'DELETE' })
    settingsNotice.value = '密钥已删除'
    await loadApiSettings()
    await auth.loadMe()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '删除密钥失败'
  }
}

async function copyApiKeySecret(key: ApiKeyEntry, adminUserId?: string | null) {
  resetSettingsMessages()
  try {
    const path = adminUserId
      ? `/admin/users/${adminUserId}/api-keys/${key.id}/secret`
      : `/api-keys/${key.id}/secret`
    const result = await apiFetch<{ apiKey: string }>(path)
    await navigator.clipboard.writeText(result.apiKey)
    settingsNotice.value = '密钥已复制'
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '复制密钥失败'
  }
}

async function createApiKeyGroup() {
  resetSettingsMessages()
  try {
    const created = await apiFetch<ApiKeyGroup>('/admin/api-key-groups', {
      method: 'POST',
      body: JSON.stringify(newGroup.value)
    })
    selectedGroupId.value = created.id
    newGroup.value = { name: '', description: '' }
    settingsNotice.value = '分组已创建'
    await loadApiSettings()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '创建分组失败'
  }
}

async function saveApiKeyGroup(group: ApiKeyGroup) {
  resetSettingsMessages()
  try {
    await apiFetch<ApiKeyGroup>(`/admin/api-key-groups/${group.id}`, {
      method: 'PATCH',
      body: JSON.stringify(groupDraftFor(group))
    })
    settingsNotice.value = '分组已保存'
    await loadApiSettings()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '保存分组失败'
  }
}

async function deleteApiKeyGroup(group: ApiKeyGroup) {
  resetSettingsMessages()
  if (!window.confirm(`删除分组「${group.name}」？该分组下的密钥会变为未分组。`)) return
  try {
    await apiFetch(`/admin/api-key-groups/${group.id}`, { method: 'DELETE' })
    if (selectedGroupId.value === group.id) {
      selectedGroupId.value = 'ungrouped'
      selectedGroupKeys.value = []
    }
    settingsNotice.value = '分组已删除，原分组下的密钥已变为未分组'
    await loadApiSettings()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '删除分组失败'
  }
}

async function saveProfile() {
  resetSettingsMessages()
  try {
    const user = await apiFetch<User>('/settings/profile', {
      method: 'PATCH',
      body: JSON.stringify({ username: profileUsername.value, password: profilePassword.value })
    })
    auth.user = user
    profilePassword.value = ''
    settingsNotice.value = '用户名已保存'
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '保存用户名失败'
  }
}

async function savePassword() {
  resetSettingsMessages()
  try {
    await auth.changePassword(currentPassword.value, replacementPassword.value)
    currentPassword.value = ''
    replacementPassword.value = ''
    settingsNotice.value = '密码已修改'
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '修改密码失败'
  }
}

function isNearBottom(threshold = 80): boolean {
  const scroller = messageScroller.value
  if (!scroller) return true
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < threshold
}

function handleScrollerScroll() {
  const awayFromBottom = !isNearBottom(120)
  showScrollToBottom.value = messages.value.length > 0 && awayFromBottom
  // When the user scrolls during streaming, detect whether they moved
  // away from the bottom.  If they did, stop auto-scrolling so they can
  // read earlier content undisturbed.  Auto-scroll resumes once they
  // scroll back near the bottom.
  if (streaming.value) {
    userHasScrolledUp = awayFromBottom
  }
}

async function scrollMessagesToBottom(behavior: ScrollBehavior = 'smooth') {
  await nextTick()
  const scroller = messageScroller.value
  if (!scroller) return
  scroller.scrollTo({ top: scroller.scrollHeight, behavior })
  showScrollToBottom.value = false
}

async function returnToBottom() {
  userHasScrolledUp = false
  await scrollMessagesToBottom('smooth')
}

function scheduleScrollToBottom() {
  // Skip auto-scroll when the user has intentionally scrolled up.
  if (userHasScrolledUp) return
  if (scrollFrame !== null) return
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = null
    void scrollMessagesToBottom('auto')
  })
}

function appendStreamText(message: Message, text: string) {
  if (!text) return
  streamTargetMessage = message
  streamTextBuffer += text
  scheduleStreamFlush()
}

function flushStreamText() {
  streamFlushFrame = null
  if (!streamTargetMessage || !streamTextBuffer) return

  // --- typewriter effect ---
  // Drain characters per animation frame (~60 fps). Adaptive rate:
  //   small buffer → slow & visible typing
  //   large buffer → faster, but capped at ~28 chars/frame (~1680 chars/sec)
  //   so a 6000-char "completed_text" payload still animates over ~3-4s
  //   instead of flashing on screen.
  const len = streamTextBuffer.length
  const charsThisFrame = Math.max(2, Math.min(28, Math.ceil(len / 60)))
  const chunk = streamTextBuffer.slice(0, charsThisFrame)
  streamTextBuffer = streamTextBuffer.slice(charsThisFrame)
  streamTargetMessage.content += chunk
  scheduleScrollToBottom()

  // If there is still text waiting, schedule another frame immediately.
  if (streamTextBuffer) {
    streamFlushFrame = window.requestAnimationFrame(flushStreamText)
  } else {
    streamTargetMessage = null
  }
}

function scheduleStreamFlush() {
  if (streamFlushFrame !== null) return
  streamFlushFrame = window.requestAnimationFrame(flushStreamText)
}

function cancelPendingStreamFlush() {
  if (streamFlushFrame !== null) {
    window.cancelAnimationFrame(streamFlushFrame)
    streamFlushFrame = null
  }
  if (streamTargetMessage && streamTextBuffer) {
    streamTargetMessage.content += streamTextBuffer
  }
  streamTargetMessage = null
  streamTextBuffer = ''
}

function waitForStreamFlush(): Promise<void> {
  if (!streamTextBuffer && streamFlushFrame === null) {
    streamTargetMessage = null
    return Promise.resolve()
  }
  if (streamFlushFrame === null) scheduleStreamFlush()
  return new Promise((resolve) => {
    const tick = () => {
      if (!streamTextBuffer && streamFlushFrame === null) {
        streamTargetMessage = null
        resolve()
        return
      }
      window.requestAnimationFrame(tick)
    }
    window.requestAnimationFrame(tick)
  })
}

function cancelPendingScroll() {
  if (scrollFrame === null) return
  window.cancelAnimationFrame(scrollFrame)
  scrollFrame = null
}

function newChat() {
  cancelPendingScroll()
  cancelPendingStreamFlush()
  currentId.value = null
  messages.value = []
  showScrollToBottom.value = false
  userHasScrolledUp = false
  error.value = ''
}

async function loadConversations() {
  try {
    conversations.value = await apiFetch<Conversation[]>('/conversations')
    if (!currentId.value && conversations.value[0]) await openConversation(conversations.value[0].id)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'INVALID_CREDENTIALS') {
      await router.push('/login')
    } else if (err instanceof ApiError) {
      error.value = err.message
    }
  }
}

async function loadModels() {
  try {
    const result = await apiFetch<{ models: string[]; selectedModel?: string }>('/models')
    models.value = result.models
    selectedModel.value = preferredModel(result.models, result.selectedModel)
  } catch (err) {
    models.value = [DEFAULT_MODEL]
    selectedModel.value = DEFAULT_MODEL
    if (err instanceof ApiError && err.code === 'INVALID_CREDENTIALS') {
      await router.push('/login')
    } else if (err instanceof ApiError) {
      error.value = err.message
    }
  }
}

async function saveSelectedModel() {
  if (!selectedModel.value) return
  try {
    await apiFetch('/settings/model', {
      method: 'PATCH',
      body: JSON.stringify({ model: selectedModel.value })
    })
    await loadContextStats()
  } catch (err) {
    if (err instanceof ApiError) error.value = err.message
  }
}

async function chooseModel(model: string) {
  selectedModel.value = model
  modelMenuOpen.value = false
  await saveSelectedModel()
}

async function openConversation(id: string) {
  if (deletingConversationId.value === id) return
  cancelPendingScroll()
  cancelPendingStreamFlush()
  currentId.value = id
  messages.value = await apiFetch<Message[]>(`/conversations/${id}/messages`)
  await loadContextStats()
  await scrollMessagesToBottom('auto')
}

async function deleteConversation(conversation: Conversation) {
  if (deletingConversationId.value || streaming.value) return
  deletingConversationId.value = conversation.id
  try {
    await apiFetch(`/conversations/${conversation.id}`, { method: 'DELETE' })
    conversations.value = conversations.value.filter((item) => item.id !== conversation.id)
    if (currentId.value === conversation.id) {
      const nextConversation = conversations.value[0]
      if (nextConversation) {
        await openConversation(nextConversation.id)
      } else {
        newChat()
      }
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除对话失败'
  } finally {
    deletingConversationId.value = null
  }
}

async function loadContextStats() {
  if (!currentId.value) {
    updateContextStats({ prompt_tokens_estimated: 0, context_window_tokens: DEFAULT_CONTEXT_WINDOW_TOKENS })
    return
  }
  try {
    const params = selectedModel.value ? `?model=${encodeURIComponent(selectedModel.value)}` : ''
    const stats = await apiFetch<any>(`/conversations/${currentId.value}/context${params}`)
    updateContextStats(stats)
  } catch {
    updateContextStats({ prompt_tokens_estimated: 0, context_window_tokens: DEFAULT_CONTEXT_WINDOW_TOKENS })
  }
}

async function uploadFile(event: Event) {
  const inputEl = event.target as HTMLInputElement
  const file = inputEl.files?.[0]
  if (!file) return
  const presign = await apiFetch<{ uploadId: string; uploadUrl: string; method: string }>('/attachments/presign', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, contentType: file.type, sizeBytes: file.size })
  })
  const csrf = readCookie('csrf_token')
  await fetch(presign.uploadUrl, {
    method: presign.method,
    body: file,
    credentials: 'include',
    headers: csrf ? { 'X-CSRF-Token': csrf } : undefined
  })
  const attachment = await apiFetch<Attachment>('/attachments/commit', {
    method: 'POST',
    body: JSON.stringify({ uploadId: presign.uploadId, filename: file.name, contentType: file.type })
  })
  pendingAttachments.value.push(attachment)
  inputEl.value = ''
}

async function send() {
  if (!input.value.trim() || streaming.value) return
  cancelPendingScroll()
  userHasScrolledUp = false
  modelMenuOpen.value = false
  error.value = ''
  streaming.value = true
  const userText = input.value
  const attachmentIds = pendingAttachments.value.map((item) => item.id)
  input.value = ''
  pendingAttachments.value = []
  messages.value.push({
    id: `local-${Date.now()}`,
    conversationId: currentId.value || 'new',
    role: 'user',
    content: userText,
    status: 'completed',
    totalTokens: 0,
    createdAt: new Date().toISOString()
  })
  const assistantDraft: Message = {
    id: `stream-${Date.now()}`,
    conversationId: currentId.value || 'new',
    role: 'assistant',
    content: '',
    status: 'streaming',
    totalTokens: 0,
    createdAt: new Date().toISOString()
  }
  messages.value.push(assistantDraft)
  const assistant = messages.value[messages.value.length - 1]
  await scrollMessagesToBottom('smooth')
  const path = currentId.value ? `/conversations/${currentId.value}/messages` : '/conversations/new/messages'
  try {
    await streamJsonLines(
      path,
      {
        content: userText,
        model: selectedModel.value,
        attachmentIds,
        referencedAttachmentIds: attachmentIds,
        reasoningEffort: reasoningEffort.value
      },
      async (event, data) => {
        if (event === 'conversation_created') {
          const newId = data.conversation_id || data.conversationId
          currentId.value = newId
          assistant.conversationId = newId
          await loadConversations()
        } else if (event === 'token') {
          appendStreamText(assistant, data.text || '')
        } else if (event === 'context') {
          updateContextStats(data, 'estimated')
        } else if (event === 'usage') {
          assistant.totalTokens = data.total_tokens || data.totalTokens || 0
          // Prefer actual prompt tokens from API over the earlier estimate.
          // This prevents the context meter from jumping when loadContextStats
          // recalculates with a different token count after the stream ends.
          const actualPrompt = Number(data.prompt_tokens || data.promptTokens || 0)
          if (actualPrompt > 0) {
            contextStats.value = {
              ...contextStats.value,
              promptTokensEstimated: actualPrompt,
              tokensSource: 'actual'
            }
          }
        } else if (event === 'done') {
          await waitForStreamFlush()
          assistant.id = data.message_id || data.messageId
          if (currentId.value) {
            const canonical = await apiFetch<Message>(`/conversations/${currentId.value}/messages/${assistant.id}`)
            const typedContent = assistant.content
            const canonicalContent = canonical.content || ''
            Object.assign(assistant, { ...canonical, content: canonicalContent.trim() ? canonicalContent : typedContent })
          }
          if (!assistant.content.trim() && typeof data.content === 'string') assistant.content = data.content
          assistant.status = data.status || 'completed'
          await scrollMessagesToBottom('smooth')
          await loadConversations()
          await loadContextStats()
        } else if (event === 'error') {
          cancelPendingStreamFlush()
          cancelPendingScroll()
          assistant.status = 'failed_partial'
          error.value = localizeApiMessage(data.code, data.message || data.code)
          assistant.content = error.value
          await scrollMessagesToBottom('smooth')
          await loadContextStats()
        }
      }
    )
  } catch (err) {
    cancelPendingStreamFlush()
    cancelPendingScroll()
    const apiErr = err instanceof ApiError ? err : null
    const message =
      apiErr?.code === 'INVALID_CREDENTIALS'
        ? '登录已失效或密钥无效，请重新登录或到设置中更新密钥'
        : apiErr?.message || (err instanceof Error ? err.message : '请求失败')
    assistant.content = message
    assistant.status = 'failed_no_output'
    error.value = message
    await scrollMessagesToBottom('smooth')
    if (apiErr?.code === 'INVALID_CREDENTIALS') await auth.loadMe().catch(() => router.push('/login'))
  } finally {
    cancelPendingStreamFlush()
    streaming.value = false
  }
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  void send()
}

async function compact() {
  if (!currentId.value) return
  await apiFetch(`/conversations/${currentId.value}/compact`, { method: 'POST' })
  await openConversation(currentId.value)
}

function closeFloatingMenus() {
  modelMenuOpen.value = false
  settingsMenuOpen.value = false
  reasoningMenuOpen.value = false
}

async function logout() {
  await auth.logout()
  await router.push('/login')
}

onMounted(async () => {
  loadAppearance()
  await Promise.all([loadModels(), loadConversations()])
})
</script>

<template>
  <div class="chat-shell h-screen overflow-hidden grid grid-cols-[280px_1fr]" :class="shellClass" :style="shellStyle" @click="closeFloatingMenus">
    <aside class="chat-sidebar flex flex-col min-h-0">
      <div class="conversation-list flex-1 min-h-0 overflow-auto px-2 py-3 space-y-1">
        <div
          v-for="conversation in conversations"
          :key="conversation.id"
          class="conversation-row"
          :class="{ active: conversation.id === currentId }"
        >
          <button class="conversation-item" type="button" @click="openConversation(conversation.id)">
            {{ conversation.title }}
          </button>
          <button
            class="conversation-delete-button"
            type="button"
            title="删除对话"
            aria-label="删除对话"
            :disabled="deletingConversationId === conversation.id || streaming"
            @click.stop="deleteConversation(conversation)"
          >
            <X :size="15" />
          </button>
        </div>
      </div>

      <div class="chat-sidebar-footer">
        <div class="sidebar-primary-actions">
          <button class="chat-control flex-1 px-3 py-2 text-sm" @click="newChat">新对话</button>
          <div class="sidebar-settings-menu" :class="{ open: settingsMenuOpen }" @pointerdown.stop @mousedown.stop @click.stop>
            <button class="chat-icon-button" title="设置" aria-label="设置" type="button" @click.stop="toggleSettingsMenu">
              <Settings :size="18" />
            </button>
            <div v-if="settingsMenuOpen" class="sidebar-settings-popover" @pointerdown.stop @mousedown.stop @click.stop>
              <button type="button" @click="openSettings('appearance')">设置</button>
              <button v-if="auth.user?.role === 'admin'" type="button" @click="openAdminMonitor">管理员监控</button>
            </div>
          </div>
        </div>
        <div class="sidebar-account-row">
          <span class="sidebar-username">{{ auth.user?.username }}</span>
          <button class="chat-icon-button" title="退出登录" aria-label="退出登录" @click="logout"><LogOut :size="18" /></button>
        </div>
      </div>
    </aside>

    <main class="chat-main flex flex-col min-w-0 min-h-0 overflow-hidden">
      <header class="chat-header">
        <div class="top-model-controls" @click.stop>
          <div class="model-picker model-picker-model" @click.stop>
            <button type="button" class="model-picker-button" @click="toggleModelMenu">
              <strong>{{ selectedModel || DEFAULT_MODEL }}</strong>
            </button>
            <ChevronDown class="model-picker-chevron" :size="16" />
            <div v-if="modelMenuOpen" class="model-picker-menu">
              <button
                v-for="model in models"
                :key="model"
                type="button"
                class="model-picker-option"
                :class="{ active: model === selectedModel }"
                @click="chooseModel(model)"
              >
                {{ model }}
              </button>
            </div>
          </div>

          <div class="model-picker model-picker-reasoning" @click.stop>
            <button type="button" class="model-picker-button" :title="reasoningLabel" @click="toggleReasoningMenu">
              <strong>{{ reasoningLabel }}</strong>
            </button>
            <ChevronDown class="model-picker-chevron" :size="16" />
            <div v-if="reasoningMenuOpen" class="model-picker-menu">
              <button
                v-for="option in reasoningOptions"
                :key="option.value"
                type="button"
                class="model-picker-option"
                :class="{ active: option.value === reasoningEffort }"
                :title="option.hint"
                @click="setReasoningEffort(option.value)"
              >
                {{ option.label }} <span style="opacity:0.55;font-size:12px;margin-left:6px">{{ option.hint }}</span>
              </button>
            </div>
          </div>

          <button class="top-icon-button" type="button" title="新对话" aria-label="新对话" @click="newChat">
            <Plus :size="15" />
          </button>
        </div>
        <div class="chat-header-info">
          <span>{{ conversationUsage.tokens.toLocaleString() }} Tokens · {{ conversationUsage.requests }} requests</span>
          <span>{{ currentConversation?.title || '新对话' }}</span>
        </div>
        <button v-if="currentId" class="chat-glass-button" @click="compact">压缩上下文</button>
      </header>

      <section ref="messageScroller" class="chat-surface flex-1 min-h-0 overflow-y-auto overscroll-contain px-6 py-8" @scroll="handleScrollerScroll">
        <div v-if="!messages.length" class="empty-chat-state flex items-center justify-center chat-muted">开始一个新问题吧</div>
        <ChatMessage v-for="message in messages" :key="message.id" :message="message" />

        <footer class="chat-footer p-4">
        <button
          v-if="showScrollToBottom"
          class="scroll-bottom-button"
          type="button"
          title="返回底部"
          aria-label="返回底部"
          @click="returnToBottom"
        >
          <ArrowDown :size="20" />
        </button>

        <div v-if="pendingAttachments.length" class="max-w-3xl mx-auto mb-2 flex flex-wrap gap-2">
          <span v-for="item in pendingAttachments" :key="item.id" class="attachment-pill">
            {{ item.filename }} - {{ parseStatusText[item.parseStatus] || item.parseStatus }}
          </span>
        </div>

        <form class="composer-card mx-auto" :class="{ 'is-expanded': composerExpanded }" @submit.prevent="send">
          <button
            class="composer-expand-button"
            type="button"
            :title="composerExpanded ? '收起输入框' : '展开输入框'"
            :aria-label="composerExpanded ? '收起输入框' : '展开输入框'"
            :aria-pressed="composerExpanded"
            @click="composerExpanded = !composerExpanded"
          >
            <Minimize2 v-if="composerExpanded" :size="17" />
            <Maximize2 v-else :size="17" />
          </button>
          <textarea
            v-model="input"
            class="composer-input"
            placeholder="输入消息，按 Enter 发送，Shift + Enter 换行"
            @keydown.enter="handleComposerKeydown"
          />
          <div class="composer-toolbar">
            <div class="composer-left-tools">
              <label class="composer-icon-button" title="添加附件" aria-label="添加附件">
                <Paperclip :size="18" />
                <input class="hidden" type="file" @change="uploadFile" />
              </label>

            </div>

            <div class="composer-right-tools">
              <div class="context-meter">
                <span class="context-label">上下文</span>
                <span class="context-ring" :style="contextRingStyle"><span class="context-ring-inner">{{ contextPercentLabel }}</span></span>
                <span class="context-text">已用 {{ formatTokenCount(contextUsedTokens) }} 标记，共 {{ formatTokenCount(contextTotalTokens) }}</span>
                <span v-if="contextStats.summaryUsed" class="context-text">· 已用摘要</span>
                <span v-else-if="contextStats.hasActiveCompaction" class="context-text">· 有摘要</span>
                <span v-if="contextStats.wasTrimmed" class="context-text">· 已裁剪</span>
              </div>
              <button class="send-button" type="submit" :disabled="streaming || !input.trim()" title="发送" aria-label="发送">
                <Send :size="18" />
              </button>
            </div>
          </div>
        </form>
        </footer>
      </section>


    </main>

    <div v-if="settingsOpen" class="settings-modal-backdrop" @click.self="settingsOpen = false">
      <section class="settings-modal" role="dialog" aria-modal="true" aria-label="设置">
        <nav class="settings-modal-nav">
          <div>
            <div class="settings-modal-title">设置</div>
            <div class="settings-modal-user">{{ auth.user?.username }}</div>
          </div>
          <button class="settings-tab-button" :class="{ active: settingsTab === 'appearance' }" @click="selectSettingsTab('appearance')">个性化</button>
          <button class="settings-tab-button" :class="{ active: settingsTab === 'api' }" @click="selectSettingsTab('api')">API 管理</button>
          <button v-if="auth.user?.role === 'admin'" class="settings-tab-button" :class="{ active: settingsTab === 'groups' }" @click="selectSettingsTab('groups')">分组管理</button>
          <button class="settings-tab-button" :class="{ active: settingsTab === 'account' }" @click="selectSettingsTab('account')">账号安全</button>
          <button class="settings-close-button mt-auto" @click="settingsOpen = false">关闭</button>
        </nav>

        <div class="settings-modal-content">
          <div v-if="settingsNotice" class="settings-alert success">{{ settingsNotice }}</div>
          <div v-if="settingsError" class="settings-alert error">{{ settingsError }}</div>

          <section v-if="settingsTab === 'appearance'" class="settings-pane">
            <div class="settings-pane-heading">
              <h2>个性化设置</h2>
              <p>调整主题、气泡颜色、正文和代码字号。</p>
            </div>
            <div class="settings-grid">
              <label class="settings-field">
                <span>主题</span>
                <select v-model="themeMode" class="settings-input" @change="setTheme(themeMode)">
                  <option v-for="option in themeOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
              </label>
              <label class="settings-field">
                <span>气泡颜色</span>
                <select v-model="bubbleColor" class="settings-input" @change="setBubbleColor(bubbleColor)">
                  <option v-for="option in bubbleOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
                </select>
              </label>
              <label class="settings-field">
                <span>正文字号</span>
                <select v-model.number="textSize" class="settings-input" @change="saveTextSize">
                  <option v-for="size in textSizeOptions" :key="size" :value="size">{{ size }} px</option>
                </select>
              </label>
              <label class="settings-field">
                <span>代码字号</span>
                <select v-model.number="codeSize" class="settings-input" @change="saveCodeSize">
                  <option v-for="size in codeSizeOptions" :key="size" :value="size">{{ size }} px</option>
                </select>
              </label>
            </div>
          </section>

          <section v-else-if="settingsTab === 'api'" class="settings-pane">
            <div class="settings-pane-heading">
              <h2>API 管理</h2>
              <p>用户可以管理自己的密钥，并把密钥切换到管理员创建的分组。一个密钥只能属于一个分组。</p>
            </div>

            <form class="settings-card" @submit.prevent="createApiKey">
              <h3>添加新密钥</h3>
              <div class="settings-grid">
                <input v-model="newApiKey.name" class="settings-input" placeholder="密钥名称，例如：工作 / 备用" />
                <select v-model="newApiKey.groupId" class="settings-input">
                  <option value="">未分组</option>
                  <option v-for="group in apiKeyGroups" :key="group.id" :value="group.id">{{ group.name }}</option>
                </select>
                <input v-model="newApiKey.apiKey" class="settings-input" type="password" placeholder="API Key 明文只提交一次" />
              </div>
              <label class="settings-check">
                <input v-model="newApiKey.makeActive" type="checkbox" />
                添加后立即设为当前使用密钥
              </label>
              <button class="settings-primary" type="submit">添加密钥</button>
            </form>

            <div class="settings-card">
              <div class="settings-card-header">
                <h3>我的密钥</h3>
                <button class="settings-secondary" :disabled="settingsLoading" @click="loadApiSettings">刷新</button>
              </div>
              <div v-if="!apiKeys.length" class="settings-empty">暂无密钥</div>
              <div v-for="key in apiKeys" :key="key.id" class="settings-key-row">
                <div class="settings-key-main">
                  <input v-model="keyDraftFor(key).name" class="settings-input" />
                  <select v-model="keyDraftFor(key).groupId" class="settings-input">
                    <option value="">未分组</option>
                    <option v-for="group in apiKeyGroups" :key="group.id" :value="group.id">{{ group.name }}</option>
                  </select>
                  <div class="settings-key-meta">
                    <span>{{ key.groupName || '未分组' }}</span>
                    <span class="key-mask">{{ key.maskedKey }}</span>
                    <strong v-if="key.isActive">当前使用</strong>
                  </div>
                </div>
                <div class="settings-key-actions">
                  <button class="settings-secondary" @click="copyApiKeySecret(key)">复制</button>
                  <button class="settings-secondary" @click="saveApiKey(key)">保存</button>
                  <button class="settings-primary" :disabled="key.isActive" @click="activateApiKey(key)">切换</button>
                  <button class="settings-danger" @click="deleteApiKey(key)">删除</button>
                </div>
              </div>
            </div>

          </section>

          <section v-else-if="settingsTab === 'groups' && auth.user?.role === 'admin'" class="settings-pane">
            <div class="settings-pane-heading">
              <h2>分组管理</h2>
              <p>维护密钥分组，并查看每个分组下所有用户的密钥。</p>
            </div>

            <div class="settings-group-layout">
              <aside class="settings-group-sidebar">
                <form class="settings-group-create-panel" @submit.prevent="createApiKeyGroup">
                  <h3>新建分组</h3>
                  <input v-model="newGroup.name" class="settings-input" placeholder="分组名称" />
                  <input v-model="newGroup.description" class="settings-input" placeholder="备注，可选" />
                  <button class="settings-primary" type="submit">创建分组</button>
                </form>

                <div class="settings-group-list">
                  <button
                    class="settings-group-list-item"
                    :class="{ active: selectedGroupId === 'ungrouped' }"
                    type="button"
                    @click="loadGroupKeys('ungrouped')"
                  >
                    <span>未分组</span>
                    <small>系统默认</small>
                  </button>
                  <button
                    v-for="group in apiKeyGroups"
                    :key="group.id"
                    class="settings-group-list-item"
                    :class="{ active: selectedGroupId === group.id }"
                    type="button"
                    @click="loadGroupKeys(group.id)"
                  >
                    <span>{{ group.name }}</span>
                    <small>{{ group.description || '无备注' }}</small>
                  </button>
                </div>
              </aside>

              <section class="settings-group-detail">
                <div class="settings-card-header">
                  <div>
                    <h3>{{ selectedGroupId === 'ungrouped' ? '未分组' : selectedApiKeyGroup?.name || '分组详情' }}</h3>
                    <p>当前分组下的密钥和所属用户。</p>
                  </div>
                  <button class="settings-secondary" :disabled="groupKeysLoading" @click="loadGroupKeys(selectedGroupId)">刷新密钥</button>
                </div>

                <div v-if="selectedApiKeyGroup" class="settings-group-editor">
                  <input
                    v-model="groupDraftFor(selectedApiKeyGroup).name"
                    class="settings-input"
                    placeholder="分组名称"
                  />
                  <input
                    v-model="groupDraftFor(selectedApiKeyGroup).description"
                    class="settings-input"
                    placeholder="备注"
                  />
                  <button class="settings-secondary" @click="saveApiKeyGroup(selectedApiKeyGroup)">保存</button>
                  <button class="settings-danger" @click="deleteApiKeyGroup(selectedApiKeyGroup)">删除</button>
                </div>

                <div class="settings-group-key-list">
                  <div v-if="groupKeysLoading" class="settings-empty">正在加载该分组密钥...</div>
                  <div v-else-if="!selectedGroupKeys.length" class="settings-empty">当前分组暂无密钥</div>
                  <div v-for="key in selectedGroupKeys" v-else :key="key.id" class="settings-admin-key-row">
                    <div class="settings-admin-key-top">
                      <strong>{{ key.name }}</strong>
                      <span>{{ key.username || key.userId || '未知用户' }}</span>
                    </div>
                    <div class="settings-admin-key-meta">
                      <span>{{ key.groupName || '未分组' }}</span>
                      <span class="key-mask">{{ key.maskedKey }}</span>
                      <strong v-if="key.isActive">用户当前使用</strong>
                      <button class="settings-inline-action" @click="copyApiKeySecret(key, key.userId)">复制</button>
                    </div>
                  </div>
                </div>
              </section>
            </div>
          </section>

          <section v-else class="settings-pane">
            <div class="settings-pane-heading">
              <h2>账号安全</h2>
              <p>修改用户名和密码。修改用户名只需要输入当前密码确认。</p>
            </div>
            <form class="settings-card" @submit.prevent="saveProfile">
              <h3>修改用户名</h3>
              <input v-model="profileUsername" class="settings-input" placeholder="新用户名" />
              <input v-model="profilePassword" class="settings-input" type="password" placeholder="当前密码" />
              <button class="settings-primary" type="submit">保存用户名</button>
            </form>
            <form class="settings-card" @submit.prevent="savePassword">
              <h3>修改密码</h3>
              <input v-model="currentPassword" class="settings-input" type="password" placeholder="当前密码" />
              <input v-model="replacementPassword" class="settings-input" type="password" placeholder="新密码" />
              <button class="settings-primary" type="submit">保存密码</button>
            </form>
          </section>
        </div>
      </section>
    </div>
  </div>
</template>
