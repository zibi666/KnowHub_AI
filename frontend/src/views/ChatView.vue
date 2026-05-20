<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch, type CSSProperties } from 'vue'
import { ArrowDown, Download, FileText, Image as ImageIcon, Maximize2, MessageCircle, Minimize2, PanelLeftClose, PanelLeftOpen, Paperclip, Pencil, Plus, RefreshCw, Search, Send, Settings, X } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { ApiError, apiFetch, localizeApiMessage, readCookie, streamJsonLines } from '../api/client'
import AppSelect from '../components/AppSelect.vue'
import ChatMessage from '../components/ChatMessage.vue'
import { useAuthStore } from '../stores/auth'
import type {
  ApiKeyEntry,
  ApiKeyGroup,
  Attachment,
  AttachmentChunkPreview,
  Conversation,
  ConversationSearchResult,
  ImageGenerationSettings,
  Message,
  User
} from '../types'
import { copyText } from '../utils/clipboard'

type ThemeMode = 'dark' | 'light'
type SettingsTab = 'appearance' | 'image' | 'api' | 'groups' | 'account'

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
const VISION_MODEL_HINTS = ['gpt-4o', 'gpt-4.1', 'gpt-5', 'o3', 'o4', 'vision', 'vl', 'gemini', 'claude']
const THEME_STORAGE_KEY = 'private-gpt-theme'
const BUBBLE_STORAGE_KEY = 'private-gpt-bubble'
const TEXT_SIZE_STORAGE_KEY = 'private-gpt-text-size'
const CODE_SIZE_STORAGE_KEY = 'private-gpt-code-size'
const REASONING_STORAGE_KEY = 'private-gpt-reasoning-effort'
const SIDEBAR_STORAGE_KEY = 'private-gpt-sidebar-collapsed'
const WELCOME_STORAGE_KEY = 'private-gpt-welcome-message'
const WELCOME_SIZE_STORAGE_KEY = 'private-gpt-welcome-font-size'

type ReasoningEffort = 'low' | 'medium' | 'high' | 'xhigh'
const reasoningOptions: Array<{ value: ReasoningEffort; label: string; hint: string }> = [
  { value: 'low', label: '低', hint: '最快，适合闲聊 / 简短答疑' },
  { value: 'medium', label: '中', hint: '默认。日常使用平衡速度与质量' },
  { value: 'high', label: '高', hint: '更深思考，适合复杂分析' },
  { value: 'xhigh', label: '极致', hint: '最大推理预算，最慢，仅复杂难题' }
]

const textSizeOptions = [13, 14, 15, 16, 17, 18, 19]
const codeSizeOptions = [11, 12, 13, 14, 15, 16]
const welcomeSizeOptions = [36, 40, 44, 48, 52, 56, 60, 64]
const themeOptions: Array<{ value: ThemeMode; label: string }> = [
  { value: 'dark', label: '暗色' },
  { value: 'light', label: '浅色' }
]

const textSizeMenuOptions = textSizeOptions.map((size) => ({ value: size, label: `${size} px` }))
const codeSizeMenuOptions = codeSizeOptions.map((size) => ({ value: size, label: `${size} px` }))
const welcomeSizeMenuOptions = welcomeSizeOptions.map((size) => ({ value: size, label: `${size} px` }))
const imageSizeOptions = [
  { value: '1024x1024', label: '1024 x 1024' },
  { value: '1024x1536', label: '1024 x 1536' },
  { value: '1536x1024', label: '1536 x 1024' },
  { value: 'auto', label: '自动' }
]
const imageQualityOptions = [
  { value: 'high', label: '高' },
  { value: 'medium', label: '中' },
  { value: 'low', label: '低' },
  { value: 'auto', label: '自动' }
]
const imageBackgroundOptions = [
  { value: 'auto', label: '自动' },
  { value: 'opaque', label: '不透明' },
  { value: 'transparent', label: '透明' }
]
const imageFormatOptions = [
  { value: 'png', label: 'PNG' },
  { value: 'jpeg', label: 'JPEG' },
  { value: 'webp', label: 'WebP' }
]
const imageModerationOptions = [
  { value: 'auto', label: '自动' },
  { value: 'low', label: '低' }
]

const auth = useAuthStore()
const router = useRouter()

const conversations = ref<Conversation[]>([])
const conversationsLoading = ref(true)
const currentId = ref<string | null>(null)
const messages = ref<Message[]>([])
const messagesLoading = ref(false)
const input = ref('')
const composerInput = ref<HTMLTextAreaElement | null>(null)
const composerExpanded = ref(false)
const models = ref<string[]>([])
const selectedModel = ref('')
const reasoningEffort = ref<ReasoningEffort>('medium')
const streaming = ref(false)
const pendingAttachments = ref<Attachment[]>([])
const uploadingAttachmentNames = ref<string[]>([])
const composerDragActive = ref(false)
const attachmentPreviewOpen = ref(false)
const attachmentPreview = ref<Attachment | null>(null)
const attachmentPreviewText = ref('')
const attachmentPreviewChunks = ref<AttachmentChunkPreview[]>([])
const attachmentPreviewLoading = ref(false)
const attachmentPreviewError = ref('')
const reindexingAttachment = ref(false)
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
const welcomeMessage = ref('')
const welcomeFontSize = ref(52)
const settingsMenuOpen = ref(false)
const settingsOpen = ref(false)
const sidebarCollapsed = ref(false)
const searchOpen = ref(false)
const searchQuery = ref('')
const searchLoading = ref(false)
const searchResults = ref<ConversationSearchResult[]>([])
const searchError = ref('')
const logoutConfirmOpen = ref(false)
const logoutLoading = ref(false)
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
const renamingConversationId = ref<string | null>(null)
const renameSaving = ref(false)
const renameDialogOpen = ref(false)
const renameDraft = ref('')
const renameError = ref('')
const selectedApiKeyGroup = computed(() => apiKeyGroups.value.find((group) => group.id === selectedGroupId.value) || null)
const modelOptions = computed(() => models.value.map((model) => ({ value: model, label: model })))
const groupOptions = computed(() => [
  { value: '', label: '未分组' },
  ...apiKeyGroups.value.map((group) => ({ value: group.id, label: group.name, hint: group.description || undefined }))
])
const keyDrafts = ref<Record<string, { name: string; groupId: string }>>({})
const groupDrafts = ref<Record<string, { name: string; description: string }>>({})
const newApiKey = ref({ name: '默认密钥', apiKey: '', groupId: '', makeActive: true })
const newGroup = ref({ name: '', description: '' })
const profileUsername = ref('')
const profilePassword = ref('')
const currentPassword = ref('')
const replacementPassword = ref('')
const avatarUploading = ref(false)
const imageSettingsLoading = ref(false)
const imageSettingsSaving = ref(false)
const imageSettings = ref<ImageGenerationSettings>({
  size: '1024x1024',
  quality: 'high',
  background: 'auto',
  outputFormat: 'png',
  outputCompression: 100,
  moderation: 'auto'
})

let scrollFrame: number | null = null
let activeConversationLoad = 0

const currentConversation = computed(() => conversations.value.find((item) => item.id === currentId.value))
const recentSearchConversations = computed(() => conversations.value.slice(0, 10))
const conversationUsage = computed(() => {
  const assistantMessages = messages.value.filter((message) => message.role === 'assistant')
  return {
    tokens: assistantMessages.reduce((total, message) => total + (Number(message.totalTokens) || 0), 0),
    requests: assistantMessages.length
  }
})
const defaultWelcomeMessage = computed(() => {
  const name = auth.user?.username?.trim()
  return name ? `Hi ${name}, what can we work on?` : 'What can we work on?'
})
const effectiveWelcomeMessage = computed(() => welcomeMessage.value.trim() || defaultWelcomeMessage.value)
const isEmptyChat = computed(() => !messagesLoading.value && messages.value.length === 0)
const hasConversationFrame = computed(() => messagesLoading.value || messages.value.length > 0)
const composerClasses = computed(() => ({
  'is-expanded': composerExpanded.value,
  'is-drag-active': composerDragActive.value,
  'is-empty-composer':
    isEmptyChat.value &&
    !composerExpanded.value &&
    pendingAttachments.value.length === 0 &&
    uploadingAttachmentNames.value.length === 0
}))

function isImageAttachment(item: Attachment) {
  return item.mimeSniffed?.startsWith('image/')
}

function selectedModelSupportsVision() {
  const model = (selectedModel.value || '').toLowerCase()
  return VISION_MODEL_HINTS.some((hint) => model.includes(hint))
}

function selectedModelIsImageGeneration() {
  return ['image-2', 'image-1.5', 'image-1', 'gpt-image-2', 'gpt-image-1.5', 'gpt-image-1'].includes(
    (selectedModel.value || '').toLowerCase()
  )
}

function normalizeImageSettings(data: any): ImageGenerationSettings {
  return {
    size: data?.size || '1024x1024',
    quality: data?.quality || 'high',
    background: data?.background || 'auto',
    outputFormat: data?.outputFormat || data?.output_format || 'png',
    outputCompression: Number(data?.outputCompression ?? data?.output_compression ?? 100),
    moderation: data?.moderation || 'auto'
  }
}

function attachmentPreviewUrl(id: string) {
  return `/api/attachments/${id}/preview`
}

function attachmentKindLabel(item: Attachment | { filename: string; mimeSniffed?: string }) {
  const filename = item.filename || ''
  const mime = item.mimeSniffed || ''
  const extension = filename.split('.').pop()?.toLowerCase() || ''
  if (mime.includes('pdf') || extension === 'pdf') return 'PDF'
  if (mime.includes('wordprocessingml') || ['doc', 'docx'].includes(extension)) return '文档'
  if (['txt', 'md', 'csv'].includes(extension)) return '文本'
  if (['js', 'ts', 'tsx', 'vue', 'py', 'java', 'go', 'rs', 'toml', 'json', 'yaml', 'yml', 'sql', 'html', 'css'].includes(extension)) return '文件'
  return '文件'
}

function attachmentKindClass(item: Attachment | { filename: string; mimeSniffed?: string }) {
  const filename = item.filename || ''
  const mime = item.mimeSniffed || ''
  const extension = filename.split('.').pop()?.toLowerCase() || ''
  if (mime.includes('pdf') || extension === 'pdf') return 'kind-pdf'
  if (mime.includes('wordprocessingml') || ['doc', 'docx'].includes(extension)) return 'kind-doc'
  return 'kind-file'
}

function attachmentDownloadUrl(id: string) {
  return `/api/attachments/${id}/download`
}

const userInitial = computed(() => auth.user?.username?.slice(0, 1).toUpperCase() || 'U')

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size >= 10 || index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`
}

function timestampForUploadName(date = new Date()) {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
}

function extensionFromMime(mime: string | undefined) {
  if (mime === 'image/jpeg') return 'jpg'
  if (mime === 'image/webp') return 'webp'
  if (mime === 'image/gif') return 'gif'
  if (mime === 'image/png') return 'png'
  return 'png'
}

function renamePastedImage(file: File) {
  if (!file.type.startsWith('image/')) return file
  const extension = extensionFromMime(file.type)
  return new File([file], `pasted-image-${timestampForUploadName()}.${extension}`, {
    type: file.type || 'image/png',
    lastModified: Date.now()
  })
}

function removePendingAttachment(id: string) {
  pendingAttachments.value = pendingAttachments.value.filter((item) => item.id !== id)
}

function closeAttachmentPreview() {
  attachmentPreviewOpen.value = false
  attachmentPreview.value = null
  attachmentPreviewText.value = ''
  attachmentPreviewChunks.value = []
  attachmentPreviewError.value = ''
  attachmentPreviewLoading.value = false
}

async function openAttachmentPreview(item: Attachment) {
  attachmentPreview.value = item
  attachmentPreviewOpen.value = true
  attachmentPreviewText.value = ''
  attachmentPreviewChunks.value = []
  attachmentPreviewError.value = ''
  attachmentPreviewLoading.value = true
  try {
    if (!isImageAttachment(item)) {
      const preview = await apiFetch<{ attachmentId: string; filename: string; previewText: string }>(`/attachments/${item.id}/preview`)
      attachmentPreviewText.value = preview.previewText || ''
    }
    attachmentPreviewChunks.value = await apiFetch<AttachmentChunkPreview[]>(`/attachments/${item.id}/chunks`)
  } catch (err) {
    attachmentPreviewError.value = err instanceof Error ? err.message : '附件预览加载失败'
  } finally {
    attachmentPreviewLoading.value = false
  }
}

async function reindexPreviewAttachment() {
  if (!attachmentPreview.value || reindexingAttachment.value) return
  reindexingAttachment.value = true
  attachmentPreviewError.value = ''
  try {
    const updated = await apiFetch<Attachment>(`/attachments/${attachmentPreview.value.id}/reindex`, { method: 'POST' })
    attachmentPreview.value = updated
    pendingAttachments.value = pendingAttachments.value.map((item) => (item.id === updated.id ? updated : item))
    await openAttachmentPreview(updated)
  } catch (err) {
    attachmentPreviewError.value = err instanceof Error ? err.message : '重新索引失败'
  } finally {
    reindexingAttachment.value = false
  }
}
const shellClass = computed(() => (themeMode.value === 'dark' ? 'theme-dark' : 'theme-light'))
const selectedBubble = computed(() => bubbleOptions.find((item) => item.value === bubbleColor.value) || bubbleOptions[0])
const shellStyle = computed(
  () =>
    ({
      '--bubble-bg': selectedBubble.value.bg,
      '--bubble-hover': selectedBubble.value.hover,
      '--bubble-shadow': selectedBubble.value.shadow,
      '--message-font-size': `${Math.max(16, textSize.value)}px`,
      '--user-message-font-size': `${Math.max(15.5, textSize.value - 0.25)}px`,
      '--code-font-size': `${codeSize.value}px`,
      '--welcome-font-size': `${welcomeFontSize.value}px`
    }) as CSSProperties
)

const parseStatusText: Record<string, string> = {
  pending: '等待解析',
  parsing: '解析中',
  success: '解析完成',
  failed: '解析失败'
}

const embeddingStatusText: Record<string, string> = {
  pending: '向量等待中',
  ready: '向量可用',
  failed: '向量失败'
}

const MESSAGE_ROLE_ORDER: Record<Message['role'], number> = {
  system: 0,
  user: 1,
  assistant: 2
}

function sortMessagesForDisplay(items: Message[]) {
  const ordered = [...items].sort((a, b) => {
    const timeDelta = new Date(a.createdAt || 0).getTime() - new Date(b.createdAt || 0).getTime()
    if (timeDelta !== 0) return timeDelta
    const roleDelta = (MESSAGE_ROLE_ORDER[a.role] ?? 3) - (MESSAGE_ROLE_ORDER[b.role] ?? 3)
    if (roleDelta !== 0) return roleDelta
    return a.id.localeCompare(b.id)
  })
  const byId = new Map(ordered.map((message) => [message.id, message]))
  const parentIds = new Set(ordered.map((message) => message.parentMessageId).filter(Boolean) as string[])
  const head = [...ordered].reverse().find((message) => !parentIds.has(message.id)) || ordered[ordered.length - 1]
  const branch: Message[] = []
  const seen = new Set<string>()
  let current: Message | undefined = head
  while (current && !seen.has(current.id)) {
    branch.push(current)
    seen.add(current.id)
    current = current.parentMessageId ? byId.get(current.parentMessageId) : undefined
  }
  const branchChronological = branch.reverse()
  if (branchChronological.length > 1 || branchChronological.length === ordered.length) {
    const branchIds = new Set(branchChronological.map((message) => message.id))
    const olderLegacyMessages = ordered.filter((message) => !branchIds.has(message.id))
    return [...olderLegacyMessages, ...branchChronological]
  }
  return ordered
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

  const storedWelcomeMessage = window.localStorage.getItem(WELCOME_STORAGE_KEY)
  if (storedWelcomeMessage !== null) welcomeMessage.value = storedWelcomeMessage.slice(0, 90)

  const storedWelcomeSize = Number(window.localStorage.getItem(WELCOME_SIZE_STORAGE_KEY))
  if (welcomeSizeOptions.includes(storedWelcomeSize)) welcomeFontSize.value = storedWelcomeSize

  const storedReasoning = window.localStorage.getItem(REASONING_STORAGE_KEY)
  if (reasoningOptions.some((item) => item.value === storedReasoning)) {
    reasoningEffort.value = storedReasoning as ReasoningEffort
  }

  sidebarCollapsed.value = window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === 'true'
}

function setReasoningEffort(effort: ReasoningEffort) {
  reasoningEffort.value = effort
  window.localStorage.setItem(REASONING_STORAGE_KEY, effort)
}

const reasoningLabel = computed(() => reasoningOptions.find((item) => item.value === reasoningEffort.value)?.label || '中')

function setReasoningEffortFromSelect(effort: string | number) {
  if (reasoningOptions.some((item) => item.value === effort)) {
    setReasoningEffort(effort as ReasoningEffort)
  }
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

function setTextSize(size: number) {
  textSize.value = size
  window.localStorage.setItem(TEXT_SIZE_STORAGE_KEY, String(size))
}

function setCodeSize(size: number) {
  codeSize.value = size
  window.localStorage.setItem(CODE_SIZE_STORAGE_KEY, String(size))
}

function setWelcomeFontSize(size: number) {
  if (!welcomeSizeOptions.includes(size)) return
  welcomeFontSize.value = size
  window.localStorage.setItem(WELCOME_SIZE_STORAGE_KEY, String(size))
}

function setWelcomeMessage(value: string) {
  const normalized = value.slice(0, 90)
  if (welcomeMessage.value !== normalized) welcomeMessage.value = normalized
  if (normalized.trim()) {
    window.localStorage.setItem(WELCOME_STORAGE_KEY, normalized)
  } else {
    window.localStorage.removeItem(WELCOME_STORAGE_KEY)
  }
}

function handleWelcomeMessageInput(event: Event) {
  setWelcomeMessage((event.target as HTMLInputElement).value)
}

function resetWelcomeMessage() {
  welcomeMessage.value = ''
  window.localStorage.removeItem(WELCOME_STORAGE_KEY)
}

function setNewApiKeyGroup(groupId: string | number) {
  newApiKey.value.groupId = String(groupId)
}

function setApiKeyDraftGroup(key: ApiKeyEntry, groupId: string | number) {
  keyDraftFor(key).groupId = String(groupId)
}

function formatSearchDate(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
  const dayDelta = Math.round((startOfToday - startOfDate) / 86400000)
  if (dayDelta === 0) return '今天'
  if (dayDelta === 1) return '昨天'
  if (dayDelta < 7) return `${dayDelta} 天前`
  return date.toLocaleDateString()
}

function openSearchDialog() {
  settingsMenuOpen.value = false
  searchOpen.value = true
  searchError.value = ''
  void nextTick(() => {
    document.querySelector<HTMLInputElement>('.chat-search-input')?.focus()
  })
}

function closeSearchDialog() {
  searchOpen.value = false
  searchQuery.value = ''
  searchResults.value = []
  searchError.value = ''
  searchLoading.value = false
  if (searchDebounceTimer !== null) {
    window.clearTimeout(searchDebounceTimer)
    searchDebounceTimer = null
  }
}

let searchRequestId = 0
async function runConversationSearch() {
  const query = searchQuery.value.trim()
  const requestId = ++searchRequestId
  searchError.value = ''
  if (!query) {
    searchResults.value = []
    searchLoading.value = false
    return
  }
  searchLoading.value = true
  try {
    const params = new URLSearchParams({ q: query, limit: '30' })
    const results = await apiFetch<ConversationSearchResult[]>(`/conversations/search?${params.toString()}`)
    if (requestId === searchRequestId) searchResults.value = results
  } catch (err) {
    if (requestId === searchRequestId) {
      searchResults.value = []
      searchError.value = err instanceof Error ? err.message : '搜索失败'
    }
  } finally {
    if (requestId === searchRequestId) searchLoading.value = false
  }
}

let searchDebounceTimer: number | null = null
function scheduleConversationSearch() {
  if (searchDebounceTimer !== null) window.clearTimeout(searchDebounceTimer)
  searchDebounceTimer = window.setTimeout(() => {
    searchDebounceTimer = null
    void runConversationSearch()
  }, 180)
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

async function loadImageSettings() {
  imageSettingsLoading.value = true
  try {
    imageSettings.value = normalizeImageSettings(await apiFetch('/settings/image-generation'))
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '加载图像生成设置失败'
  } finally {
    imageSettingsLoading.value = false
  }
}

async function saveImageSettings() {
  resetSettingsMessages()
  imageSettingsSaving.value = true
  try {
    imageSettings.value = normalizeImageSettings(await apiFetch('/settings/image-generation', {
      method: 'PATCH',
      body: JSON.stringify(imageSettings.value)
    }))
    settingsNotice.value = '图像生成设置已保存'
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '保存图像生成设置失败'
  } finally {
    imageSettingsSaving.value = false
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
  if (targetTab === 'image') await loadImageSettings()
}

function toggleSettingsMenu() {
  settingsMenuOpen.value = !settingsMenuOpen.value
}

function toggleSidebarCollapsed() {
  sidebarCollapsed.value = !sidebarCollapsed.value
  settingsMenuOpen.value = false
  window.localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarCollapsed.value))
}

function openAdminMonitor() {
  settingsMenuOpen.value = false
  void router.push('/admin')
}

function openVersionControl() {
  settingsMenuOpen.value = false
  void router.push('/versions')
}

function requestLogout() {
  settingsMenuOpen.value = false
  logoutConfirmOpen.value = true
}

function closeLogoutConfirm() {
  if (logoutLoading.value) return
  logoutConfirmOpen.value = false
}

async function confirmLogout() {
  if (logoutLoading.value) return
  logoutLoading.value = true
  try {
    await auth.logout()
    logoutConfirmOpen.value = false
    await router.push('/login')
  } finally {
    logoutLoading.value = false
  }
}

async function selectSettingsTab(tab: SettingsTab) {
  if (tab === 'groups' && auth.user?.role !== 'admin') return
  settingsTab.value = tab
  resetSettingsMessages()
  if ((tab === 'api' || tab === 'groups') && !apiKeys.value.length && !apiKeyGroups.value.length) await loadApiSettings()
  if (tab === 'groups') await loadGroupKeys(selectedGroupId.value)
  if (tab === 'image') await loadImageSettings()
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
    await copyText(result.apiKey)
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

async function uploadAvatar(event: Event) {
  resetSettingsMessages()
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  avatarUploading.value = true
  try {
    const form = new FormData()
    form.append('file', file)
    const csrf = readCookie('csrf_token')
    const response = await fetch('/api/settings/avatar', {
      method: 'POST',
      body: form,
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : undefined
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new Error(payload?.detail?.message || '头像上传失败')
    }
    auth.user = await response.json()
    settingsNotice.value = '头像已更新'
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '头像上传失败'
  } finally {
    avatarUploading.value = false
    input.value = ''
  }
}

async function deleteAvatar() {
  resetSettingsMessages()
  avatarUploading.value = true
  try {
    auth.user = await apiFetch<User>('/settings/avatar', { method: 'DELETE' })
    settingsNotice.value = '头像已删除'
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '头像删除失败'
  } finally {
    avatarUploading.value = false
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
  message.content += text
  scheduleScrollToBottom()
}

function cancelPendingStreamFlush() {
  // Stream chunks are applied directly as they arrive; nothing is buffered.
}

function waitForStreamFlush(): Promise<void> {
  return Promise.resolve()
}

function cancelPendingScroll() {
  if (scrollFrame === null) return
  window.cancelAnimationFrame(scrollFrame)
  scrollFrame = null
}

function newChat() {
  activeConversationLoad++
  cancelPendingScroll()
  cancelPendingStreamFlush()
  currentId.value = null
  messages.value = []
  messagesLoading.value = false
  showScrollToBottom.value = false
  userHasScrolledUp = false
  error.value = ''
}

async function loadConversations() {
  conversationsLoading.value = conversations.value.length === 0
  try {
    conversations.value = await apiFetch<Conversation[]>('/conversations')
  } catch (err) {
    if (err instanceof ApiError && err.code === 'INVALID_CREDENTIALS') {
      await router.push('/login')
    } else if (err instanceof ApiError) {
      error.value = err.message
    }
  } finally {
    conversationsLoading.value = false
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
  await saveSelectedModel()
}

async function openConversation(id: string, focusMessageId?: string | null) {
  if (deletingConversationId.value === id) return
  const loadId = ++activeConversationLoad
  cancelPendingScroll()
  cancelPendingStreamFlush()
  currentId.value = id
  messages.value = []
  messagesLoading.value = true
  try {
    const params = focusMessageId ? `?aroundMessageId=${encodeURIComponent(focusMessageId)}&limit=120` : ''
    const loadedMessages = sortMessagesForDisplay(await apiFetch<Message[]>(`/conversations/${id}/messages${params}`))
    if (loadId !== activeConversationLoad) return
    messages.value = loadedMessages
    await loadContextStats()
    if (focusMessageId) {
      await scrollToMessage(focusMessageId)
    } else {
      await scrollMessagesToBottom('auto')
    }
  } catch (err) {
    if (loadId === activeConversationLoad && err instanceof ApiError) error.value = err.message
  } finally {
    if (loadId === activeConversationLoad) messagesLoading.value = false
  }
}

async function scrollToMessage(messageId: string) {
  await nextTick()
  const target = Array.from(document.querySelectorAll<HTMLElement>('[data-message-id]')).find(
    (item) => item.dataset.messageId === messageId
  )
  if (!target) {
    await scrollMessagesToBottom('auto')
    return
  }
  userHasScrolledUp = true
  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
  target.classList.add('message-search-highlight')
  window.setTimeout(() => target.classList.remove('message-search-highlight'), 1800)
}

async function openSearchResult(result: ConversationSearchResult) {
  closeSearchDialog()
  await openConversation(result.conversationId, result.messageId)
}

async function openRecentConversation(conversation: Conversation) {
  closeSearchDialog()
  await openConversation(conversation.id)
}

function openRenameConversation(conversation: Conversation) {
  if (streaming.value) return
  renamingConversationId.value = conversation.id
  renameDraft.value = conversation.title
  renameError.value = ''
  renameDialogOpen.value = true
}

function closeRenameDialog() {
  if (renameSaving.value) return
  renameDialogOpen.value = false
  renamingConversationId.value = null
  renameDraft.value = ''
  renameError.value = ''
}

async function saveConversationTitle() {
  const conversationId = renamingConversationId.value
  if (!conversationId) return
  const title = renameDraft.value.trim()
  if (!title) {
    renameError.value = '对话名称不能为空'
    return
  }
  renameSaving.value = true
  try {
    const updated = await apiFetch<Conversation>(`/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title })
    })
    conversations.value = conversations.value.map((item) => (item.id === updated.id ? { ...item, ...updated } : item))
    renameDialogOpen.value = false
    renamingConversationId.value = null
    renameDraft.value = ''
    renameError.value = ''
  } catch (err) {
    renameError.value = err instanceof Error ? err.message : '修改对话名称失败'
  } finally {
    renameSaving.value = false
  }
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

async function uploadAttachmentFile(file: File) {
  uploadingAttachmentNames.value.push(file.name)
  try {
    const presign = await apiFetch<{ uploadId: string; uploadUrl: string; method: string }>('/attachments/presign', {
      method: 'POST',
      body: JSON.stringify({ filename: file.name, contentType: file.type, sizeBytes: file.size })
    })
    const csrf = readCookie('csrf_token')
    const uploadResponse = await fetch(presign.uploadUrl, {
      method: presign.method,
      body: file,
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : undefined
    })
    if (!uploadResponse.ok) {
      throw new Error('附件上传失败')
    }
    const attachment = await apiFetch<Attachment>('/attachments/commit', {
      method: 'POST',
      body: JSON.stringify({ uploadId: presign.uploadId, filename: file.name, contentType: file.type })
    })
    pendingAttachments.value.push(attachment)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '附件上传失败'
  } finally {
    uploadingAttachmentNames.value = uploadingAttachmentNames.value.filter((name) => name !== file.name)
  }
}

async function uploadAttachmentFiles(files: File[]) {
  if (!files.length) return
  for (const file of files) {
    await uploadAttachmentFile(file)
  }
}

async function uploadFile(event: Event) {
  const inputEl = event.target as HTMLInputElement
  const files = Array.from(inputEl.files || [])
  if (!files.length) return
  try {
    await uploadAttachmentFiles(files)
  } finally {
    inputEl.value = ''
  }
}

function filesFromClipboard(event: ClipboardEvent) {
  const items = Array.from(event.clipboardData?.items || [])
  return items
    .filter((item) => item.kind === 'file')
    .map((item) => item.getAsFile())
    .filter((file): file is File => Boolean(file))
    .map(renamePastedImage)
}

function handleComposerPaste(event: ClipboardEvent) {
  const files = filesFromClipboard(event)
  if (!files.length) return
  event.preventDefault()
  void uploadAttachmentFiles(files)
}

function handleComposerDragEnter(event: DragEvent) {
  if (!event.dataTransfer?.types.includes('Files')) return
  composerDragActive.value = true
}

function handleComposerDragOver(event: DragEvent) {
  if (!event.dataTransfer?.types.includes('Files')) return
  event.preventDefault()
  composerDragActive.value = true
  event.dataTransfer.dropEffect = 'copy'
}

function handleComposerDragLeave(event: DragEvent) {
  const current = event.currentTarget as Node | null
  const related = event.relatedTarget as Node | null
  if (current && related && current.contains(related)) return
  composerDragActive.value = false
}

function handleComposerDrop(event: DragEvent) {
  const files = Array.from(event.dataTransfer?.files || [])
  if (!files.length) return
  event.preventDefault()
  composerDragActive.value = false
  void uploadAttachmentFiles(files)
}

async function send() {
  if ((!input.value.trim() && !pendingAttachments.value.length) || streaming.value) return
  const isImageGeneration = selectedModelIsImageGeneration()
  if (isImageGeneration && pendingAttachments.value.length) {
    error.value = '图像生成模型暂不支持同时发送附件，请只输入提示词。'
    return
  }
  if (pendingAttachments.value.some(isImageAttachment) && !selectedModelSupportsVision()) {
    error.value = '当前模型不支持图片理解，请切换到支持视觉的模型后再发送图片。'
    return
  }
  cancelPendingScroll()
  userHasScrolledUp = false
  error.value = ''
  streaming.value = true
  const userText = input.value
  const outgoingAttachments = [...pendingAttachments.value]
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
    attachments: outgoingAttachments,
    createdAt: new Date().toISOString()
  })
  const assistantDraft: Message = {
    id: `stream-${Date.now()}`,
    conversationId: currentId.value || 'new',
    role: 'assistant',
    content: isImageGeneration ? '正在生成图片' : '',
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
        } else if (event === 'image_status') {
          assistant.content = data.text || '正在生成图片'
          assistant.imageProgress = {
            b64Json: assistant.imageProgress?.b64Json || '',
            index: assistant.imageProgress?.index || 0,
            total: assistant.imageProgress?.total || 1,
            outputFormat: assistant.imageProgress?.outputFormat || 'png',
            detail: data.detail || '正在等待模型返回图片进度。',
            elapsedSeconds: Number(data.elapsed_seconds ?? data.elapsedSeconds ?? assistant.imageProgress?.elapsedSeconds ?? 0),
            startedAt: assistant.imageProgress?.startedAt || Date.now(),
            phase: data.phase || assistant.imageProgress?.phase || 'submitted',
            size: data.size || assistant.imageProgress?.size || imageSettings.value.size
          }
          scheduleScrollToBottom()
        } else if (event === 'image_progress') {
          assistant.content = '正在生成图片'
          assistant.imageProgress = {
            b64Json: data.b64_json || data.b64Json || '',
            index: Number(data.index || 1),
            total: Number(data.total || 1),
            outputFormat: data.output_format || data.outputFormat || 'png',
            detail: `已收到图像结果 ${Number(data.index || 1)}/${Number(data.total || 1)}，继续等待最终保存。`,
            elapsedSeconds: assistant.imageProgress?.elapsedSeconds,
            startedAt: assistant.imageProgress?.startedAt || Date.now(),
            phase: 'partial',
            size: data.size || assistant.imageProgress?.size || imageSettings.value.size
          }
          scheduleScrollToBottom()
        } else if (event === 'image_completed') {
          if (data.attachment) {
            assistant.attachments = [data.attachment]
            assistant.generatedImageSize = assistant.imageProgress?.size || imageSettings.value.size
            assistant.imageProgress = undefined
            assistant.content = '已根据提示词生成图片。'
          }
          scheduleScrollToBottom()
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
            const generatedImageSize = assistant.generatedImageSize || assistant.imageProgress?.size
            const canonical = await apiFetch<Message>(`/conversations/${currentId.value}/messages/${assistant.id}`)
            const typedContent = assistant.content
            const canonicalContent = canonical.content || ''
            Object.assign(assistant, { ...canonical, content: typedContent.trim() ? typedContent : canonicalContent })
            if (generatedImageSize) assistant.generatedImageSize = generatedImageSize
            assistant.imageProgress = undefined
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
    streaming.value = false
  }
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  void send()
}

function resizeComposerInput() {
  const textarea = composerInput.value
  if (!textarea) return
  textarea.style.height = 'auto'
  textarea.style.height = `${textarea.scrollHeight}px`
}

watch(input, async () => {
  await nextTick()
  resizeComposerInput()
})

watch(composerExpanded, async () => {
  await nextTick()
  resizeComposerInput()
})

function closeFloatingMenus() {
  settingsMenuOpen.value = false
}

watch(searchQuery, () => {
  if (!searchOpen.value) return
  scheduleConversationSearch()
})

onMounted(async () => {
  loadAppearance()
  await Promise.all([loadModels(), loadConversations()])
  await nextTick()
  resizeComposerInput()
})
</script>

<template>
  <div
    class="chat-shell h-screen overflow-hidden grid grid-cols-[280px_1fr]"
    :class="[shellClass, { 'sidebar-collapsed': sidebarCollapsed }]"
    :style="shellStyle"
    @click="closeFloatingMenus"
  >
    <aside class="chat-sidebar flex flex-col min-h-0">
      <div class="chat-sidebar-top">
        <div class="sidebar-top-actions">
          <div class="sidebar-title-row">
            <div class="sidebar-brand">KnowHub</div>
            <button
              class="sidebar-collapse-button"
              type="button"
              :title="sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
              :aria-label="sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'"
              :aria-pressed="sidebarCollapsed"
              @click.stop="toggleSidebarCollapsed"
            >
              <PanelLeftOpen v-if="sidebarCollapsed" :size="18" />
              <PanelLeftClose v-else :size="18" />
            </button>
          </div>
          <div class="sidebar-primary-actions">
            <button
              class="sidebar-new-chat-button"
              type="button"
              title="新对话"
              aria-label="新对话"
              @click="newChat"
            >
              <Plus :size="18" />
              <span>新对话</span>
            </button>
            <button class="sidebar-search-button" type="button" title="搜索聊天" aria-label="搜索聊天" @click="openSearchDialog">
              <Search :size="18" />
              <span>搜索聊天</span>
            </button>
          </div>
        </div>
      </div>

      <div class="conversation-list flex-1 min-h-0 overflow-auto px-2 py-2 space-y-1">
        <div v-if="conversationsLoading" class="sidebar-skeleton-list" aria-label="正在加载对话">
          <div v-for="item in 6" :key="item" class="sidebar-skeleton-row">
            <span class="skeleton-line" :class="`w-${item % 3}`" />
          </div>
        </div>
        <div
          v-else
          v-for="conversation in conversations"
          :key="conversation.id"
          class="conversation-row"
          :class="{ active: conversation.id === currentId }"
        >
          <button class="conversation-item" type="button" @click="openConversation(conversation.id)">
            {{ conversation.title }}
          </button>
          <div class="conversation-actions">
            <button
              class="conversation-action-button"
              type="button"
              title="修改名称"
              aria-label="修改名称"
              :disabled="streaming"
              @click.stop="openRenameConversation(conversation)"
            >
              <Pencil :size="14" />
            </button>
            <button
              class="conversation-action-button"
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
      </div>

      <div class="chat-sidebar-footer">
        <div class="sidebar-account-card">
          <div class="sidebar-avatar" aria-hidden="true">
            <img v-if="auth.user?.avatarUrl" :src="auth.user.avatarUrl" :alt="auth.user.username" />
            <span v-else>{{ userInitial }}</span>
          </div>
          <div class="sidebar-account-main">
            <span class="sidebar-username">{{ auth.user?.username }}</span>
            <span class="sidebar-account-role">{{ auth.user?.role === 'admin' ? '管理员' : '用户' }}</span>
          </div>
          <div class="sidebar-settings-menu" :class="{ open: settingsMenuOpen }" @pointerdown.stop @mousedown.stop @click.stop>
            <button class="sidebar-round-button" title="设置" aria-label="设置" type="button" @click.stop="toggleSettingsMenu">
              <Settings :size="17" />
            </button>
            <Transition name="menu-rise">
              <div v-if="settingsMenuOpen" class="sidebar-settings-popover" @pointerdown.stop @mousedown.stop @click.stop>
                <button type="button" @click="openSettings('appearance')">设置</button>
                <button type="button" @click="openVersionControl">版本控制</button>
                <button v-if="auth.user?.role === 'admin'" type="button" @click="openAdminMonitor">管理员监控</button>
                <button class="sidebar-settings-danger" type="button" @click="requestLogout">退出登录</button>
              </div>
            </Transition>
          </div>
        </div>
      </div>
    </aside>

    <main
      class="chat-main flex flex-col min-w-0 min-h-0 overflow-hidden"
      :class="{ 'has-messages': hasConversationFrame, 'is-empty-chat': isEmptyChat, 'composer-open': composerExpanded }"
    >
      <header class="chat-header">
        <div class="top-model-controls" @click.stop>
          <AppSelect
            v-model="selectedModel"
            class="model-picker model-picker-model"
            button-class="model-picker-button"
            menu-class="model-picker-menu"
            option-class="model-picker-option"
            :options="modelOptions"
            :placeholder="DEFAULT_MODEL"
            @change="chooseModel(String($event))"
          />

          <AppSelect
            v-model="reasoningEffort"
            class="model-picker model-picker-reasoning"
            button-class="model-picker-button"
            menu-class="model-picker-menu"
            option-class="model-picker-option"
            :title="reasoningLabel"
            :options="reasoningOptions"
            @change="setReasoningEffortFromSelect"
          />

          <button class="top-icon-button" type="button" title="新对话" aria-label="新对话" @click="newChat">
            <Plus :size="15" />
          </button>
        </div>
        <div class="chat-header-info">
          <span>{{ conversationUsage.tokens.toLocaleString() }} Tokens · {{ conversationUsage.requests }} requests</span>
          <span>{{ currentConversation?.title || '新对话' }}</span>
          <button
            v-if="currentConversation"
            class="chat-header-rename-button"
            type="button"
            title="修改对话名称"
            aria-label="修改对话名称"
            :disabled="streaming"
            @click.stop="openRenameConversation(currentConversation)"
          >
            <Pencil :size="13" />
          </button>
        </div>
      </header>

      <section ref="messageScroller" class="chat-surface flex-1 min-h-0 overflow-y-auto overscroll-contain px-6 py-8" @scroll="handleScrollerScroll">
        <div class="chat-flow mx-auto">
          <div v-if="messagesLoading" class="message-skeleton-stack" aria-label="正在加载消息">
            <div class="message-skeleton-row assistant">
              <div class="message-skeleton-block">
                <span class="skeleton-line wide" />
                <span class="skeleton-line medium" />
                <span class="skeleton-line narrow" />
              </div>
            </div>
            <div class="message-skeleton-row user">
              <div class="message-skeleton-bubble">
                <span class="skeleton-line medium" />
              </div>
            </div>
            <div class="message-skeleton-row assistant">
              <div class="message-skeleton-block">
                <span class="skeleton-line wide" />
                <span class="skeleton-line wide" />
                <span class="skeleton-line short" />
              </div>
            </div>
          </div>
          <ChatMessage
            v-else
            v-for="message in messages"
            :key="message.id"
            :message="message"
            @preview-attachment="openAttachmentPreview"
          />
        </div>
      </section>

      <footer class="chat-footer p-4">
        <Transition name="welcome-rise">
          <div v-if="isEmptyChat" class="empty-welcome">
            {{ effectiveWelcomeMessage }}
          </div>
        </Transition>
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

        <form
          class="composer-card"
          :class="composerClasses"
          @submit.prevent="send"
          @paste="handleComposerPaste"
          @dragenter.prevent="handleComposerDragEnter"
          @dragover="handleComposerDragOver"
          @dragleave="handleComposerDragLeave"
          @drop="handleComposerDrop"
        >
          <div v-if="uploadingAttachmentNames.length || pendingAttachments.length" class="composer-attachments">
            <div v-for="name in uploadingAttachmentNames" :key="`uploading-${name}`" class="composer-attachment-card is-uploading">
              <div class="composer-attachment-loading" aria-hidden="true" />
              <div class="composer-attachment-meta">
                <strong>{{ name }}</strong>
                <span>上传中</span>
              </div>
            </div>
            <div
              v-for="item in pendingAttachments"
              :key="item.id"
              class="composer-attachment-card"
              :class="{ 'is-image': isImageAttachment(item), 'is-file': !isImageAttachment(item) }"
            >
              <button class="composer-attachment-preview" type="button" @click="openAttachmentPreview(item)">
                <img v-if="isImageAttachment(item)" :src="attachmentPreviewUrl(item.id)" :alt="item.filename" />
                <template v-else>
                  <span class="composer-file-icon" :class="attachmentKindClass(item)"><FileText :size="21" /></span>
                  <span class="composer-attachment-meta">
                    <strong>{{ item.filename }}</strong>
                    <em>{{ attachmentKindLabel(item) }}</em>
                  </span>
                </template>
              </button>
              <button class="composer-attachment-remove" type="button" title="移除本次附件" aria-label="移除本次附件" @click.stop="removePendingAttachment(item.id)">
                <X :size="14" />
              </button>
              <span v-if="isImageAttachment(item)" class="composer-attachment-status">{{ parseStatusText[item.parseStatus] || item.parseStatus }}</span>
            </div>
          </div>
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
            ref="composerInput"
            v-model="input"
            class="composer-input"
            placeholder="输入消息，按 Enter 发送，Shift + Enter 换行"
            @keydown.enter="handleComposerKeydown"
          />
          <div class="composer-toolbar">
            <div class="composer-left-tools">
              <label class="composer-icon-button" title="添加附件" aria-label="添加附件">
                <Paperclip :size="18" />
                <input class="hidden" type="file" multiple @change="uploadFile" />
              </label>

            </div>

            <div class="composer-right-tools">
              <button class="send-button" type="submit" :disabled="streaming || (!input.trim() && !pendingAttachments.length)" title="发送" aria-label="发送">
                <Send :size="18" />
              </button>
            </div>
          </div>
        </form>
      </footer>


    </main>

    <Transition name="modal-fade">
      <div v-if="settingsOpen" class="settings-modal-backdrop" @click.self="settingsOpen = false">
        <section class="settings-modal" role="dialog" aria-modal="true" aria-label="设置">
          <nav class="settings-modal-nav">
            <div>
              <div class="settings-modal-title">设置</div>
              <div class="settings-modal-user">{{ auth.user?.username }}</div>
            </div>
            <button class="settings-tab-button" :class="{ active: settingsTab === 'appearance' }" @click="selectSettingsTab('appearance')">个性化</button>
            <button class="settings-tab-button" :class="{ active: settingsTab === 'image' }" @click="selectSettingsTab('image')">图像生成</button>
            <button class="settings-tab-button" :class="{ active: settingsTab === 'api' }" @click="selectSettingsTab('api')">API 管理</button>
            <button v-if="auth.user?.role === 'admin'" class="settings-tab-button" :class="{ active: settingsTab === 'groups' }" @click="selectSettingsTab('groups')">分组管理</button>
            <button class="settings-tab-button" :class="{ active: settingsTab === 'account' }" @click="selectSettingsTab('account')">账号安全</button>
            <button class="settings-close-button mt-auto" @click="settingsOpen = false">关闭</button>
          </nav>

          <div class="settings-modal-content">
            <Transition name="soft-slide">
              <div v-if="settingsNotice" class="settings-alert success">{{ settingsNotice }}</div>
            </Transition>
            <Transition name="soft-slide">
              <div v-if="settingsError" class="settings-alert error">{{ settingsError }}</div>
            </Transition>

            <Transition name="pane-swap" mode="out-in">
              <section v-if="settingsTab === 'appearance'" key="appearance" class="settings-pane">
                <div class="settings-pane-heading">
                  <h2>个性化设置</h2>
                  <p>调整主题、气泡颜色、正文和代码字号。</p>
                </div>
                <div class="settings-grid">
                  <label class="settings-field settings-field-wide">
                    <span>新对话顶部文字</span>
                    <div class="settings-inline-field">
                      <input
                        v-model="welcomeMessage"
                        class="settings-input"
                        maxlength="90"
                        :placeholder="defaultWelcomeMessage"
                        @input="handleWelcomeMessageInput"
                      />
                      <button class="settings-secondary" type="button" @click="resetWelcomeMessage">恢复默认</button>
                    </div>
                  </label>
                  <label class="settings-field">
                    <span>主题</span>
                    <AppSelect
                      v-model="themeMode"
                      class="settings-select"
                      button-class="settings-select-button"
                      menu-class="settings-select-menu"
                      option-class="settings-select-option"
                      :options="themeOptions"
                      @change="setTheme($event as ThemeMode)"
                    />
                  </label>
                  <label class="settings-field">
                    <span>气泡颜色</span>
                    <AppSelect
                      v-model="bubbleColor"
                      class="settings-select"
                      button-class="settings-select-button"
                      menu-class="settings-select-menu"
                      option-class="settings-select-option"
                      :options="bubbleOptions.map((option) => ({ value: option.value, label: option.label, swatch: option.bg }))"
                      @change="setBubbleColor($event as BubbleColor)"
                    />
                  </label>
                  <label class="settings-field">
                    <span>正文字号</span>
                    <AppSelect
                      v-model="textSize"
                      class="settings-select"
                      button-class="settings-select-button"
                      menu-class="settings-select-menu"
                      option-class="settings-select-option"
                      :options="textSizeMenuOptions"
                      @change="setTextSize(Number($event))"
                    />
                  </label>
                  <label class="settings-field">
                    <span>代码字号</span>
                    <AppSelect
                      v-model="codeSize"
                      class="settings-select"
                      button-class="settings-select-button"
                      menu-class="settings-select-menu"
                      option-class="settings-select-option"
                      :options="codeSizeMenuOptions"
                      @change="setCodeSize(Number($event))"
                    />
                  </label>
                  <label class="settings-field">
                    <span>欢迎语字号</span>
                    <AppSelect
                      v-model="welcomeFontSize"
                      class="settings-select"
                      button-class="settings-select-button"
                      menu-class="settings-select-menu"
                      option-class="settings-select-option"
                      :options="welcomeSizeMenuOptions"
                      @change="setWelcomeFontSize(Number($event))"
                    />
                  </label>
                </div>
              </section>

              <section v-else-if="settingsTab === 'image'" key="image" class="settings-pane">
                <div class="settings-pane-heading">
                  <h2>图像生成</h2>
                  <p>配置 image-2、image-1.5、image-1 的默认生成参数。</p>
                </div>
                <form class="settings-card" @submit.prevent="saveImageSettings">
                  <div v-if="imageSettingsLoading" class="settings-empty">正在加载图像设置...</div>
                  <div v-else class="settings-grid">
                    <label class="settings-field">
                      <span>尺寸</span>
                      <AppSelect
                        v-model="imageSettings.size"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="imageSizeOptions"
                      />
                    </label>
                    <label class="settings-field">
                      <span>质量</span>
                      <AppSelect
                        v-model="imageSettings.quality"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="imageQualityOptions"
                      />
                    </label>
                    <label class="settings-field">
                      <span>背景</span>
                      <AppSelect
                        v-model="imageSettings.background"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="imageBackgroundOptions"
                      />
                    </label>
                    <label class="settings-field">
                      <span>格式</span>
                      <AppSelect
                        v-model="imageSettings.outputFormat"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="imageFormatOptions"
                      />
                    </label>
                    <label class="settings-field">
                      <span>压缩质量</span>
                      <input
                        v-model.number="imageSettings.outputCompression"
                        class="settings-input"
                        type="number"
                        min="0"
                        max="100"
                        :disabled="imageSettings.outputFormat === 'png'"
                      />
                    </label>
                    <label class="settings-field">
                      <span>审核强度</span>
                      <AppSelect
                        v-model="imageSettings.moderation"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="imageModerationOptions"
                      />
                    </label>
                  </div>
                  <button class="settings-primary" type="submit" :disabled="imageSettingsLoading || imageSettingsSaving">
                    {{ imageSettingsSaving ? '保存中...' : '保存图像设置' }}
                  </button>
                </form>
              </section>

              <section v-else-if="settingsTab === 'api'" key="api" class="settings-pane">
                <div class="settings-pane-heading">
                  <h2>API 管理</h2>
                  <p>用户可以管理自己的密钥，并把密钥切换到管理员创建的分组。一个密钥只能属于一个分组。</p>
                </div>

                <form class="settings-card" @submit.prevent="createApiKey">
                  <h3>添加新密钥</h3>
                  <div class="settings-grid">
                    <input v-model="newApiKey.name" class="settings-input" placeholder="密钥名称，例如：工作 / 备用" />
                    <AppSelect
                      v-model="newApiKey.groupId"
                      class="settings-select"
                      button-class="settings-select-button"
                      menu-class="settings-select-menu"
                      option-class="settings-select-option"
                      :options="groupOptions"
                      @change="setNewApiKeyGroup"
                    />
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
                  <div v-if="settingsLoading" class="settings-skeleton-list" aria-label="正在加载密钥">
                    <div v-for="item in 3" :key="item" class="settings-key-skeleton">
                      <div class="settings-key-main">
                        <span class="skeleton-line wide" />
                        <span class="skeleton-line medium" />
                        <span class="skeleton-line short" />
                      </div>
                      <div class="settings-key-actions">
                        <span class="skeleton-pill" />
                        <span class="skeleton-pill" />
                      </div>
                    </div>
                  </div>
                  <div v-else-if="!apiKeys.length" class="settings-empty">暂无密钥</div>
                  <div v-for="key in apiKeys" v-else :key="key.id" class="settings-key-row">
                    <div class="settings-key-main">
                      <input v-model="keyDraftFor(key).name" class="settings-input" />
                      <AppSelect
                        :model-value="keyDraftFor(key).groupId"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="groupOptions"
                        @update:model-value="setApiKeyDraftGroup(key, $event)"
                      />
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

              <section v-else-if="settingsTab === 'groups' && auth.user?.role === 'admin'" key="groups" class="settings-pane">
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
                      <div v-if="groupKeysLoading" class="settings-skeleton-list compact" aria-label="正在加载分组密钥">
                        <div v-for="item in 4" :key="item" class="settings-admin-key-skeleton">
                          <span class="skeleton-line medium" />
                          <span class="skeleton-line wide" />
                          <span class="skeleton-line short" />
                        </div>
                      </div>
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

              <section v-else key="account" class="settings-pane">
                <div class="settings-pane-heading">
                  <h2>账号安全</h2>
                  <p>修改用户名和密码。修改用户名只需要输入当前密码确认。</p>
                </div>
                <div class="settings-card">
                  <div>
                    <h3>更换头像</h3>
                    <p class="settings-card-note">支持 PNG、JPG、WebP，最大 2MB，会自动裁切为方形头像。</p>
                  </div>
                  <div class="settings-avatar-editor">
                    <div class="settings-avatar-preview" aria-hidden="true">
                      <img v-if="auth.user?.avatarUrl" :src="auth.user.avatarUrl" :alt="auth.user.username" />
                      <span v-else>{{ userInitial }}</span>
                    </div>
                    <div class="settings-avatar-actions">
                      <label class="settings-primary" :class="{ disabled: avatarUploading }">
                        {{ avatarUploading ? '上传中...' : '上传头像' }}
                        <input
                          class="hidden"
                          type="file"
                          accept="image/png,image/jpeg,image/webp"
                          :disabled="avatarUploading"
                          @change="uploadAvatar"
                        />
                      </label>
                      <button class="settings-secondary" type="button" :disabled="avatarUploading || !auth.user?.avatarUrl" @click="deleteAvatar">
                        删除头像
                      </button>
                    </div>
                  </div>
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
            </Transition>
          </div>
        </section>
      </div>
    </Transition>

    <Transition name="dialog-pop">
      <div v-if="attachmentPreviewOpen" class="attachment-preview-backdrop" @click.self="closeAttachmentPreview">
        <section class="attachment-preview-modal" role="dialog" aria-modal="true" aria-label="附件预览">
          <header class="attachment-preview-header">
            <div class="attachment-preview-title">
              <ImageIcon v-if="attachmentPreview && isImageAttachment(attachmentPreview)" :size="19" />
              <FileText v-else :size="19" />
              <div>
                <h2>{{ attachmentPreview?.filename }}</h2>
                <p v-if="attachmentPreview">
                  {{ formatBytes(attachmentPreview.sizeBytes) }} · {{ parseStatusText[attachmentPreview.parseStatus] || attachmentPreview.parseStatus }}
                </p>
              </div>
            </div>
            <div class="attachment-preview-actions">
              <a
                v-if="attachmentPreview"
                class="attachment-preview-action"
                :href="attachmentDownloadUrl(attachmentPreview.id)"
                target="_blank"
                rel="noreferrer"
              >
                <Download :size="16" />
                下载
              </a>
              <button class="attachment-preview-close" type="button" title="关闭" aria-label="关闭" @click="closeAttachmentPreview">
                <X :size="20" />
              </button>
            </div>
          </header>

          <div class="attachment-preview-body">
            <div v-if="attachmentPreviewLoading" class="attachment-preview-empty">正在加载附件...</div>
            <div v-else-if="attachmentPreviewError" class="attachment-preview-empty error">{{ attachmentPreviewError }}</div>
            <template v-else-if="attachmentPreview">
              <div v-if="isImageAttachment(attachmentPreview)" class="attachment-preview-image-wrap">
                <img class="attachment-preview-image" :src="attachmentPreviewUrl(attachmentPreview.id)" :alt="attachmentPreview.filename" />
              </div>
              <template v-else>
                <section class="attachment-preview-section">
                  <div class="attachment-preview-section-head">
                    <h3>解析文本预览</h3>
                    <button class="attachment-preview-action subtle" type="button" :disabled="reindexingAttachment" @click="reindexPreviewAttachment">
                      <RefreshCw :size="15" />
                      {{ reindexingAttachment ? '重新索引中' : '重新索引' }}
                    </button>
                  </div>
                  <pre class="attachment-preview-text">{{ attachmentPreviewText || '暂无可预览文本' }}</pre>
                </section>

                <section class="attachment-preview-section">
                  <div class="attachment-preview-section-head">
                    <h3>RAG 分块状态</h3>
                    <span>{{ attachmentPreviewChunks.length }} 个分块</span>
                  </div>
                  <div v-if="!attachmentPreviewChunks.length" class="attachment-preview-empty compact">暂无分块，可能是图片、空文档或 embedding 未开始。</div>
                  <div v-else class="attachment-chunk-list">
                    <article
                      v-for="chunk in attachmentPreviewChunks"
                      :key="chunk.chunkIndex"
                      class="attachment-chunk-card"
                      :class="`status-${chunk.embeddingStatus}`"
                    >
                      <div class="attachment-chunk-meta">
                        <strong>#{{ chunk.chunkIndex + 1 }}</strong>
                        <span>{{ chunk.tokenCount }} tokens</span>
                        <em>{{ embeddingStatusText[chunk.embeddingStatus] || chunk.embeddingStatus }}</em>
                      </div>
                      <p>{{ chunk.contentPreview }}</p>
                      <small v-if="chunk.error">{{ chunk.error }}</small>
                    </article>
                  </div>
                </section>
              </template>
            </template>
          </div>
        </section>
      </div>
    </Transition>

    <Transition name="dialog-pop">
      <div v-if="searchOpen" class="chat-search-backdrop" @click.self="closeSearchDialog">
        <section class="chat-search-modal" role="dialog" aria-modal="true" aria-label="搜索聊天">
          <div class="chat-search-header">
            <div class="chat-search-field">
              <Search :size="19" />
              <input
                v-model="searchQuery"
                class="chat-search-input"
                placeholder="搜索聊天..."
                @keydown.esc="closeSearchDialog"
              />
              <button v-if="searchQuery" class="chat-search-clear" type="button" title="清空" aria-label="清空" @click="searchQuery = ''">
                <X :size="18" />
              </button>
            </div>
            <button class="chat-search-close" type="button" title="关闭" aria-label="关闭" @click="closeSearchDialog">
              <X :size="20" />
            </button>
          </div>

          <div class="chat-search-body">
            <button v-if="!searchQuery.trim()" class="chat-search-new" type="button" @click="newChat(); closeSearchDialog()">
              <Plus :size="18" />
              <span>新聊天</span>
            </button>

            <template v-if="!searchQuery.trim()">
              <div class="chat-search-section-title">最近</div>
              <button
                v-for="conversation in recentSearchConversations"
                :key="conversation.id"
                class="chat-search-row"
                type="button"
                @click="openRecentConversation(conversation)"
              >
                <MessageCircle :size="18" />
                <span>{{ conversation.title }}</span>
                <time>{{ formatSearchDate(conversation.updatedAt) }}</time>
              </button>
            </template>

            <template v-else>
              <div v-if="searchLoading" class="chat-search-empty">正在搜索...</div>
              <div v-else-if="searchError" class="chat-search-empty error">{{ searchError }}</div>
              <div v-else-if="!searchResults.length" class="chat-search-empty">没有找到相关聊天</div>
              <template v-else>
                <button
                  v-for="result in searchResults"
                  :key="`${result.conversationId}-${result.messageId}`"
                  class="chat-search-result"
                  type="button"
                  @click="openSearchResult(result)"
                >
                  <MessageCircle :size="18" />
                  <span class="chat-search-result-main">
                    <strong>{{ result.conversationTitle }}</strong>
                    <small>{{ result.snippet }}</small>
                  </span>
                  <time>{{ formatSearchDate(result.createdAt) }}</time>
                </button>
              </template>
            </template>
          </div>
        </section>
      </div>
    </Transition>

    <Transition name="dialog-pop">
      <div v-if="renameDialogOpen" class="rename-modal-backdrop" @click.self="closeRenameDialog">
        <form class="rename-modal" role="dialog" aria-modal="true" aria-label="修改对话名称" @submit.prevent="saveConversationTitle">
          <div class="rename-modal-header">
            <h2>修改对话名称</h2>
            <button type="button" class="rename-modal-close" title="关闭" aria-label="关闭" @click="closeRenameDialog">
              <X :size="18" />
            </button>
          </div>
          <input
            v-model="renameDraft"
            class="rename-modal-input"
            maxlength="100"
            placeholder="输入新的对话名称"
            autofocus
          />
          <Transition name="soft-slide">
            <p v-if="renameError" class="rename-modal-error">{{ renameError }}</p>
          </Transition>
          <div class="rename-modal-actions">
            <button type="button" class="rename-secondary-button" :disabled="renameSaving" @click="closeRenameDialog">取消</button>
            <button type="submit" class="rename-primary-button" :disabled="renameSaving || !renameDraft.trim()">保存</button>
          </div>
        </form>
      </div>
    </Transition>

    <Transition name="dialog-pop">
      <div v-if="logoutConfirmOpen" class="confirm-modal-backdrop" @click.self="closeLogoutConfirm">
        <section class="confirm-modal" role="dialog" aria-modal="true" aria-label="确认退出登录">
          <div class="confirm-modal-header">
            <h2>退出登录？</h2>
            <button type="button" class="confirm-modal-close" title="关闭" aria-label="关闭" @click="closeLogoutConfirm">
              <X :size="18" />
            </button>
          </div>
          <p>退出后需要重新登录才能继续使用。</p>
          <div class="confirm-modal-actions">
            <button type="button" class="confirm-secondary-button" :disabled="logoutLoading" @click="closeLogoutConfirm">取消</button>
            <button type="button" class="confirm-danger-button" :disabled="logoutLoading" @click="confirmLogout">确认退出</button>
          </div>
        </section>
      </div>
    </Transition>
  </div>
</template>
