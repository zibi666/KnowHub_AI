<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch, type CSSProperties } from 'vue'
import { ArrowDown, Download, FileText, Image as ImageIcon, Maximize2, MessageCircle, Minimize2, PanelLeftClose, PanelLeftOpen, Paperclip, Pencil, Plus, RefreshCw, Search, Send, Settings, X } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { ApiError, apiFetch, localizeApiMessage, readCookie } from '../api/client'
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
  ImageProgress,
  Message,
  SendMessageResponse,
  User
} from '../types'
import { copyText } from '../utils/clipboard'

type ThemeMode = 'dark' | 'light'
type SettingsTab = 'appearance' | 'image' | 'api' | 'groups' | 'account'
type GroupPurpose = 'none' | 'chat' | 'image'

type ModelKeyChoice = {
  model: string
  purpose: GroupPurpose
  groupName: string
  candidateKeys: Array<{
    id: string
    name: string
    groupName?: string | null
    maskedKey?: string
    last4?: string
  }>
}

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
const CURRENT_CONVERSATION_STORAGE_KEY = 'private-gpt-current-conversation'
const IMAGE_FINALIZATION_MIN_MS = 800
const ATTACHMENT_IMAGE_MAX_EDGE = 1920
const ATTACHMENT_IMAGE_QUALITY = 0.82
const ATTACHMENT_IMAGE_MIN_COMPRESS_BYTES = 450 * 1024

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
const composerCompact = ref(true)
const composerScrollable = ref(false)
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
const chatFooter = ref<HTMLElement | null>(null)
const showScrollToBottom = ref(false)
let userHasScrolledUp = false
let programmaticScrollUntil = 0

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
const selectedGroupId = ref('')
const selectedGroupKeys = ref<ApiKeyEntry[]>([])
const groupKeysLoading = ref(false)
const modelKeyChoice = ref<ModelKeyChoice | null>(null)
const modelKeyChoiceSaving = ref(false)
const previousSelectedModel = ref(DEFAULT_MODEL)
const deletingConversationId = ref<string | null>(null)
const deleteConfirmOpen = ref(false)
const conversationPendingDelete = ref<Conversation | null>(null)
const renamingConversationId = ref<string | null>(null)
const renameSaving = ref(false)
const renameDialogOpen = ref(false)
const renameDraft = ref('')
const renameError = ref('')
const selectedApiKeyGroup = computed(() => apiKeyGroups.value.find((group) => group.id === selectedGroupId.value) || null)
const modelOptions = computed(() => models.value.map((model) => ({ value: model, label: model })))
const groupOptions = computed(() =>
  apiKeyGroups.value.map((group) => ({
    value: group.id,
    label: group.name,
    hint: `${groupPurposeLabel(group.purpose)}${group.description ? ` · ${group.description}` : ''}`
  }))
)
const groupPurposeOptions = [
  { value: 'none', label: '仅管理' },
  { value: 'chat', label: '聊天模型' },
  { value: 'image', label: '图像模型' }
]
const defaultChatGroup = computed(() => apiKeyGroups.value.find((group) => group.purpose === 'chat') || apiKeyGroups.value[0] || null)
const keyDrafts = ref<Record<string, { name: string; groupId: string }>>({})
const groupDrafts = ref<Record<string, { name: string; description: string; purpose: GroupPurpose }>>({})
const newApiKey = ref({ name: '默认密钥', apiKey: '', groupId: '', makeActive: true })
const newGroup = ref<{ name: string; description: string; purpose: GroupPurpose }>({ name: '', description: '', purpose: 'none' })
const profileUsername = ref('')
const profilePassword = ref('')
const currentPassword = ref('')
const replacementPassword = ref('')
const avatarUploading = ref(false)
const imageSettingsLoading = ref(false)
const imageSettingsSaving = ref(false)
const imageSettings = ref<ImageGenerationSettings>({
  size: 'auto',
  quality: 'auto',
  background: 'auto',
  outputFormat: 'png',
  outputCompression: 100,
  moderation: 'auto'
})

let scrollFrame: number | null = null
let composerResizeFrame: number | null = null
let composerMeasureElement: HTMLDivElement | null = null
let activeConversationLoad = 0
let imagePollingTimer: number | null = null
let conversationEventSource: EventSource | null = null
let conversationEventSourceId: string | null = null
const imageFinalizationTimers = new Map<string, number>()

const currentConversation = computed(() => conversations.value.find((item) => item.id === currentId.value))
const currentConversationStreaming = computed(() => messages.value.some((message) => message.status === 'streaming'))
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
const canUseCompactComposer = computed(
  () =>
    isEmptyChat.value &&
    !composerExpanded.value &&
    pendingAttachments.value.length === 0 &&
    uploadingAttachmentNames.value.length === 0
)
const composerClasses = computed(() => ({
  'is-expanded': composerExpanded.value,
  'is-drag-active': composerDragActive.value,
  'is-empty-composer': canUseCompactComposer.value && composerCompact.value,
  'is-scrollable': composerScrollable.value
}))

function isImageAttachment(item: Attachment) {
  return item.mimeSniffed?.startsWith('image/')
}

function selectedModelSupportsVision() {
  const model = (selectedModel.value || '').toLowerCase()
  return VISION_MODEL_HINTS.some((hint) => model.includes(hint))
}

function selectedModelIsImageGeneration() {
  const model = (selectedModel.value || '').toLowerCase()
  return model.startsWith('gpt-image-') || model.startsWith('image-')
}

function messageIsImageGeneration(message: Message) {
  return ['image-2', 'image-1.5', 'image-1', 'gpt-image-2', 'gpt-image-1.5', 'gpt-image-1'].includes((message.model || '').toLowerCase())
}

function parseApiDateMs(value: unknown) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : undefined
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  if (!trimmed) return undefined
  const hasTimezone = /(?:z|[+-]\d{2}:?\d{2})$/i.test(trimmed)
  const timestamp = new Date(hasTimezone ? trimmed : `${trimmed}Z`).getTime()
  return Number.isFinite(timestamp) ? timestamp : undefined
}

function messageCreatedAtMs(message: Message) {
  const createdAt = parseApiDateMs(message.createdAt)
  return createdAt !== undefined ? Math.min(createdAt, Date.now()) : Date.now()
}

function currentRuntimeElapsed(message: Message) {
  const startedAt = normalizeProgressTimestamp(message.startedAt ?? message.started_at)
  const baseElapsed = normalizeProgressElapsed(message.elapsedSeconds ?? message.elapsed_seconds) ?? 0
  if (!startedAt) return baseElapsed
  return Math.max(baseElapsed, Math.floor((Date.now() - startedAt) / 1000), 0)
}

function normalizeProgressTimestamp(value: unknown) {
  if (value === null || value === undefined) return undefined
  let timestamp = Number(value)
  if (!Number.isFinite(timestamp) || timestamp <= 0) return undefined
  if (timestamp < 10_000_000_000) timestamp *= 1000
  return timestamp
}

function normalizeProgressElapsed(value: unknown) {
  if (value === null || value === undefined) return undefined
  const elapsed = Number(value)
  return Number.isFinite(elapsed) && elapsed >= 0 ? Math.floor(elapsed) : undefined
}

function userFacingSendError(err: unknown) {
  const apiErr = err instanceof ApiError ? err : null
  if (apiErr?.code === 'INVALID_CREDENTIALS') return '登录已失效，请重新登录。'
  if (apiErr) return apiErr.message
  const raw = err instanceof Error ? err.message : ''
  const lowered = raw.toLowerCase()
  if (lowered.includes('server disconnected') || lowered.includes('without sending a response') || lowered.includes('failed to fetch')) {
    return '服务连接中断，请稍后重试。'
  }
  return raw || '请求失败，请稍后重试。'
}

function messageFirstTokenSeconds(message: Message) {
  return normalizeProgressElapsed(message.firstTokenSeconds ?? message.first_token_seconds)
}

function incomingFirstTokenSeconds(data: any = {}) {
  return normalizeProgressElapsed(data.firstTokenSeconds ?? data.first_token_seconds)
}

function setMessageFirstTokenSeconds(message: Message, seconds: number | undefined) {
  if (seconds === undefined) return
  message.firstTokenSeconds = seconds
  message.first_token_seconds = seconds
}

function freezeFirstTokenSeconds(message: Message, data: any = {}) {
  const incomingFirstToken = incomingFirstTokenSeconds(data)
  if (incomingFirstToken !== undefined) {
    setMessageFirstTokenSeconds(message, incomingFirstToken)
    message.elapsedSeconds = incomingFirstToken
    message.elapsed_seconds = incomingFirstToken
    return
  }
  if (messageFirstTokenSeconds(message) !== undefined) return
  setMessageFirstTokenSeconds(message, currentRuntimeElapsed(message))
}

function applyRuntimeProgress(message: Message, data: any = {}) {
  const existingStartedAt = normalizeProgressTimestamp(message.startedAt ?? message.started_at)
  const incomingStartedAt = normalizeProgressTimestamp(data.startedAt ?? data.started_at)
  const fallbackStartedAt = message.status === 'streaming' ? messageCreatedAtMs(message) : undefined
  const now = Date.now()
  const startedAt = existingStartedAt ?? incomingStartedAt ?? fallbackStartedAt ?? (message.status === 'streaming' ? now : undefined)
  const firstTokenSeconds = incomingFirstTokenSeconds(data)
  const incomingElapsed = normalizeProgressElapsed(data.elapsedSeconds ?? data.elapsed_seconds)
  const existingElapsed = currentRuntimeElapsed(message)
  const elapsedSeconds =
    message.status === 'streaming' || data.status === 'streaming'
      ? Math.max(existingElapsed, incomingElapsed ?? 0)
      : Math.max(existingElapsed, incomingElapsed ?? normalizeProgressElapsed(message.elapsedSeconds ?? message.elapsed_seconds) ?? 0)
  if (firstTokenSeconds !== undefined) {
    setMessageFirstTokenSeconds(message, firstTokenSeconds)
  }
  if (startedAt !== undefined) {
    const safeStartedAt = startedAt > now ? now : startedAt
    message.startedAt = safeStartedAt
    message.started_at = safeStartedAt
  }
  if (elapsedSeconds !== undefined) {
    message.elapsedSeconds = elapsedSeconds
    message.elapsed_seconds = message.elapsedSeconds
  }
  message.progressDetail = data.detail || data.progressDetail || data.progress_detail || message.progressDetail || message.progress_detail
  message.progress_detail = message.progressDetail
  message.progressPhase = data.phase || data.progressPhase || data.progress_phase || message.progressPhase || message.progress_phase
  message.progress_phase = message.progressPhase
}

function normalizeLoadedMessage(message: Message): Message {
  const progress = message.imageProgress || message.image_progress
  if (
    message.status === 'streaming' ||
    message.elapsedSeconds !== undefined ||
    message.elapsed_seconds !== undefined ||
    message.startedAt !== undefined ||
    message.started_at !== undefined
  ) {
    applyRuntimeProgress(message)
  }
  if (progress && message.status === 'streaming') {
    const elapsedSeconds = Math.max(
      currentRuntimeElapsed(message),
      normalizeProgressElapsed(progress.elapsedSeconds ?? progress.elapsed_seconds) ?? 0
    )
    message.imageProgress = {
      b64Json: '',
      index: Number(progress.index || 0),
      total: Number(progress.total || 1),
      outputFormat: progress.outputFormat || progress.output_format || 'png',
      detail: progress.detail,
      elapsedSeconds,
      startedAt: normalizeProgressTimestamp(progress.startedAt ?? progress.started_at) || message.startedAt || messageCreatedAtMs(message),
      phase: progress.phase || message.progressPhase || message.progress_phase || 'submitted',
      size: progress.size || imageSettings.value.size
    }
  } else {
    message.imageProgress = undefined
  }
  return message
}

function applyImageProgress(message: Message, data: any = {}) {
  message.status = data.status || 'streaming'
  applyRuntimeProgress(message, data)
  const existing = message.imageProgress || message.image_progress
  const startedAt = normalizeProgressTimestamp(message.startedAt ?? message.started_at) || normalizeProgressTimestamp(existing?.startedAt ?? existing?.started_at) || Date.now()
  const elapsedSeconds = Math.max(
    currentRuntimeElapsed(message),
    normalizeProgressElapsed(existing?.elapsedSeconds ?? existing?.elapsed_seconds) ?? 0,
    normalizeProgressElapsed(data.elapsedSeconds ?? data.elapsed_seconds) ?? 0
  )
  message.imageProgress = {
    b64Json: data.b64Json || data.b64_json || existing?.b64Json || existing?.b64_json || '',
    index: Number(data.index || existing?.index || 0),
    total: Number(data.total || existing?.total || 1),
    outputFormat: data.outputFormat || data.output_format || existing?.outputFormat || existing?.output_format || 'png',
    detail: data.detail || existing?.detail || message.progressDetail,
    elapsedSeconds,
    startedAt,
    phase: data.phase || existing?.phase || message.progressPhase || 'rendering',
    size: data.size || existing?.size || imageSettings.value.size
  }
  message.image_progress = message.imageProgress
}

function normalizeImageSettings(data: any): ImageGenerationSettings {
  return {
    size: data?.size || 'auto',
    quality: data?.quality || 'auto',
    background: data?.background || 'auto',
    outputFormat: data?.outputFormat || data?.output_format || 'png',
    outputCompression: Number(data?.outputCompression ?? data?.output_compression ?? 100),
    moderation: data?.moderation || 'auto'
  }
}

function attachmentPreviewUrl(id: string) {
  return `/api/attachments/${id}/preview`
}

function attachmentImageSrc(attachment: Attachment) {
  return attachment.previewDataUrl || attachmentPreviewUrl(attachment.id)
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

function compressedImageName(filename: string) {
  const base = filename.replace(/\.[^.]+$/, '') || 'image'
  return `${base}-compressed.jpg`
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality))
}

async function compressImageFile(file: File): Promise<File> {
  if (!file.type.startsWith('image/') || file.type === 'image/gif' || file.type === 'image/svg+xml') return file
  if (file.size < ATTACHMENT_IMAGE_MIN_COMPRESS_BYTES) return file

  const bitmap = await createImageBitmap(file)
  try {
    const scale = Math.min(1, ATTACHMENT_IMAGE_MAX_EDGE / Math.max(bitmap.width, bitmap.height))
    const width = Math.max(1, Math.round(bitmap.width * scale))
    const height = Math.max(1, Math.round(bitmap.height * scale))
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d')
    if (!context) return file
    context.drawImage(bitmap, 0, 0, width, height)
    const blob = await canvasToBlob(canvas, 'image/jpeg', ATTACHMENT_IMAGE_QUALITY)
    if (!blob || blob.size >= file.size) return file
    return new File([blob], compressedImageName(file.name), { type: 'image/jpeg', lastModified: file.lastModified })
  } catch {
    return file
  } finally {
    bitmap.close()
  }
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
  attachmentPreviewLoading.value = !isImageAttachment(item)
  if (isImageAttachment(item)) return
  try {
    const preview = await apiFetch<{ attachmentId: string; filename: string; previewText: string }>(`/attachments/${item.id}/preview`)
    attachmentPreviewText.value = preview.previewText || ''
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

function compareMessagesForDisplay(a: Message, b: Message) {
  const timeDelta = (parseApiDateMs(a.createdAt) ?? 0) - (parseApiDateMs(b.createdAt) ?? 0)
  if (timeDelta !== 0) return timeDelta
  const roleDelta = (MESSAGE_ROLE_ORDER[a.role] ?? 3) - (MESSAGE_ROLE_ORDER[b.role] ?? 3)
  if (roleDelta !== 0) return roleDelta
  return a.id.localeCompare(b.id)
}

function sortMessagesForDisplay(items: Message[]) {
  const ordered = [...items].sort(compareMessagesForDisplay)
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

function findDisplayInsertIndex(message: Message) {
  return messages.value.findIndex((item) => compareMessagesForDisplay(message, item) < 0)
}

function insertMessageInDisplayOrder(message: Message) {
  const insertIndex = findDisplayInsertIndex(message)
  if (insertIndex === -1) {
    messages.value.push(message)
  } else {
    messages.value.splice(insertIndex, 0, message)
  }
}

function moveMessageToDisplayPosition(message: Message) {
  const currentIndex = messages.value.indexOf(message)
  if (currentIndex === -1) return
  messages.value.splice(currentIndex, 1)
  insertMessageInDisplayOrder(message)
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

function groupPurposeLabel(purpose?: string) {
  if (purpose === 'chat') return '聊天模型'
  if (purpose === 'image') return '图像模型'
  return '仅管理'
}

function formatSearchDate(value: string) {
  const timestamp = parseApiDateMs(value)
  if (timestamp === undefined) return ''
  const date = new Date(timestamp)
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const startOfDate = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
  const dayDelta = Math.round((startOfToday - startOfDate) / 86400000)
  if (dayDelta === 0) return '今天'
  if (dayDelta === 1) return '昨天'
  if (dayDelta < 7) return `${dayDelta} 天前`
  return date.toLocaleDateString()
}

function conversationUpdatedAtMs(conversation: Conversation) {
  return parseApiDateMs(conversation.updatedAt) ?? 0
}

function sortConversationsByUpdatedAt(items: Conversation[]) {
  return [...items].sort((a, b) => conversationUpdatedAtMs(b) - conversationUpdatedAtMs(a))
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
    keyDrafts.value[key.id] = { name: key.name, groupId: key.groupId || defaultChatGroup.value?.id || '' }
  }
  return keyDrafts.value[key.id]
}

function groupDraftFor(group: ApiKeyGroup) {
  if (!groupDrafts.value[group.id]) {
    groupDrafts.value[group.id] = { name: group.name, description: group.description || '', purpose: group.purpose || 'none' }
  }
  return groupDrafts.value[group.id]
}

async function loadGroupKeys(groupId = selectedGroupId.value) {
  if (auth.user?.role !== 'admin' || !apiKeyGroups.value.length) return
  selectedGroupId.value = groupId && apiKeyGroups.value.some((group) => group.id === groupId) ? groupId : apiKeyGroups.value[0].id
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
    if (!selectedGroupId.value || !groups.some((group) => group.id === selectedGroupId.value)) {
      selectedGroupId.value = groups[0]?.id || ''
    }
    const chatGroupId = groups.find((group) => group.purpose === 'chat')?.id || groups[0]?.id || ''
    if (!newApiKey.value.groupId || !groups.some((group) => group.id === newApiKey.value.groupId)) {
      newApiKey.value.groupId = chatGroupId
    }
    keyDrafts.value = Object.fromEntries(keys.map((key) => [key.id, { name: key.name, groupId: key.groupId || chatGroupId }]))
    groupDrafts.value = Object.fromEntries(
      groups.map((group) => [group.id, { name: group.name, description: group.description || '', purpose: group.purpose || 'none' }])
    )
    if (auth.user?.role === 'admin' && selectedGroupId.value) await loadGroupKeys(selectedGroupId.value)
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
  if (tab === 'groups' && apiKeyGroups.value.length) await loadGroupKeys(selectedGroupId.value)
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
        groupId: newApiKey.value.groupId || defaultChatGroup.value?.id || null,
        makeActive: newApiKey.value.makeActive
      })
    })
    newApiKey.value = { name: '默认密钥', apiKey: '', groupId: defaultChatGroup.value?.id || '', makeActive: true }
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
      body: JSON.stringify({ name: draft.name, groupId: draft.groupId || defaultChatGroup.value?.id || null })
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
    newGroup.value = { name: '', description: '', purpose: 'none' }
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
  if (group.isSystem) {
    settingsError.value = '系统默认分组不能删除'
    return
  }
  if (!window.confirm(`删除分组「${group.name}」？该分组下的密钥会自动迁回 gpt-chat 或 gpt-image。`)) return
  try {
    await apiFetch(`/admin/api-key-groups/${group.id}`, { method: 'DELETE' })
    if (selectedGroupId.value === group.id) {
      selectedGroupId.value = apiKeyGroups.value.find((item) => item.id !== group.id)?.id || ''
      selectedGroupKeys.value = []
    }
    settingsNotice.value = '分组已删除，原分组密钥已迁回默认分组'
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

function isProgrammaticScroll() {
  return Date.now() < programmaticScrollUntil
}

function markProgrammaticScroll() {
  programmaticScrollUntil = Date.now() + 250
}

function waitForAnimationFrame(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => resolve())
  })
}

async function waitForMessageLayout() {
  await nextTick()
  await waitForAnimationFrame()
  await waitForAnimationFrame()
}

function pauseAutoScrollFromUserScroll() {
  if (!streaming.value) return
  userHasScrolledUp = true
  showScrollToBottom.value = messages.value.length > 0
  cancelPendingScroll()
}

function handleScrollerWheel(event: WheelEvent) {
  if (event.deltaY < 0) pauseAutoScrollFromUserScroll()
}

function handleScrollerTouchStart() {
  pauseAutoScrollFromUserScroll()
}

function handleScrollerScroll() {
  const nearBottom = isNearBottom(36)
  const awayFromBottom = !nearBottom
  showScrollToBottom.value = messages.value.length > 0 && awayFromBottom
  if (!streaming.value || isProgrammaticScroll()) return
  if (nearBottom) {
    userHasScrolledUp = false
  } else if (awayFromBottom) {
    userHasScrolledUp = true
  }
}

async function scrollMessagesToBottom(behavior: ScrollBehavior = 'smooth') {
  await nextTick()
  const scroller = messageScroller.value
  if (!scroller) return
  markProgrammaticScroll()
  scroller.scrollTo({ top: scroller.scrollHeight, behavior })
  showScrollToBottom.value = false
}

async function scrollLoadedConversationToBottom(loadId: number) {
  await waitForMessageLayout()
  if (loadId !== activeConversationLoad) return
  await scrollMessagesToBottom('auto')
}

async function returnToBottom() {
  userHasScrolledUp = false
  await scrollMessagesToBottom('smooth')
}

function scheduleScrollToBottom(force = false) {
  // Skip auto-scroll when the user has intentionally scrolled up.
  if (userHasScrolledUp) return
  if (!force && !isNearBottom(40)) return
  if (scrollFrame !== null) return
  scrollFrame = window.requestAnimationFrame(() => {
    scrollFrame = null
    void scrollMessagesToBottom('auto')
  })
}

function appendStreamText(message: Message, text: string) {
  if (!text) return
  const shouldFollow = !userHasScrolledUp && isNearBottom(40)
  message.content += text
  scheduleScrollToBottom(shouldFollow)
}

function findMessage(messageId?: string | null) {
  if (!messageId) return undefined
  return messages.value.find((message) => message.id === messageId)
}

function findMessageForMerge(message: Message) {
  return (
    findMessage(message.id) ||
    (message.clientKey ? messages.value.find((item) => item.clientKey === message.clientKey) : undefined) ||
    messages.value.find((item) => {
      if (!item.id.startsWith('stream-') && !item.id.startsWith('local-')) return false
      if (item.role !== message.role) return false
      if (item.role === 'assistant' && item.status !== 'streaming') return false
      if (item.role === 'user' && item.content !== message.content) return false
      return true
    })
  )
}

function mergeMessageIntoExisting(existing: Message, incoming: Message, preserve: Partial<Message> = {}) {
  const stableClientKey = existing.clientKey || preserve.clientKey || incoming.clientKey || existing.id
  Object.assign(existing, incoming, preserve, { clientKey: stableClientKey })
  moveMessageToDisplayPosition(existing)
}

function clearImageFinalizationTimer(messageId?: string | null) {
  if (!messageId) return
  const timer = imageFinalizationTimers.get(messageId)
  if (timer === undefined) return
  window.clearTimeout(timer)
  imageFinalizationTimers.delete(messageId)
}

function clearAllImageFinalizationTimers() {
  for (const timer of imageFinalizationTimers.values()) {
    window.clearTimeout(timer)
  }
  imageFinalizationTimers.clear()
}

function finishImageFinalization(messageId: string, data: any = {}) {
  imageFinalizationTimers.delete(messageId)
  const message = findMessage(messageId)
  if (!message) return
  message.status = data.status || 'completed'
  if (typeof data.content === 'string') message.content = data.content
  message.imageProgress = undefined
  message.image_progress = undefined
  syncActiveRequestState()
  stopImagePolling()
  void loadConversations()
  void loadContextStats()
  scheduleScrollToBottom(true)
}

function mergeImageMessageIntoExisting(existing: Message, incoming: Message) {
  const existingProgress = existing.imageProgress || existing.image_progress
  const incomingProgress = incoming.imageProgress || incoming.image_progress
  const startedAt =
    normalizeProgressTimestamp(existing.startedAt ?? existing.started_at) ||
    normalizeProgressTimestamp(existingProgress?.startedAt ?? existingProgress?.started_at) ||
    normalizeProgressTimestamp(incoming.startedAt ?? incoming.started_at) ||
    normalizeProgressTimestamp(incomingProgress?.startedAt ?? incomingProgress?.started_at)
  const elapsedSeconds = Math.max(
    currentRuntimeElapsed(existing),
    normalizeProgressElapsed(existingProgress?.elapsedSeconds ?? existingProgress?.elapsed_seconds) ?? 0,
    normalizeProgressElapsed(incoming.elapsedSeconds ?? incoming.elapsed_seconds) ?? 0,
    normalizeProgressElapsed(incomingProgress?.elapsedSeconds ?? incomingProgress?.elapsed_seconds) ?? 0
  )
  mergeMessageIntoExisting(existing, incoming)
  if (startedAt !== undefined) {
    existing.startedAt = startedAt
    existing.started_at = startedAt
  }
  existing.elapsedSeconds = elapsedSeconds
  existing.elapsed_seconds = elapsedSeconds
  if (existing.status === 'streaming') {
    applyImageProgress(existing, {
      ...(incomingProgress || {}),
      b64Json: existingProgress?.b64Json || existingProgress?.b64_json || undefined,
      b64_json: existingProgress?.b64_json || existingProgress?.b64Json || undefined,
      index: existingProgress?.index,
      total: existingProgress?.total,
      outputFormat: existingProgress?.outputFormat || existingProgress?.output_format || undefined,
      output_format: existingProgress?.output_format || existingProgress?.outputFormat || undefined,
      elapsedSeconds,
      startedAt,
      phase: incomingProgress?.phase || incoming.progressPhase || incoming.progress_phase || existingProgress?.phase,
      detail: incomingProgress?.detail || incoming.progressDetail || incoming.progress_detail || existingProgress?.detail,
      size: incomingProgress?.size || existingProgress?.size || incoming.generatedImageSize || existing.generatedImageSize
    })
  }
}

function upsertMessage(message: Message, preserve: Partial<Message> = {}) {
  const normalized = normalizeLoadedMessage(message)
  const existing = findMessageForMerge({ ...normalized, ...preserve })
  const shouldFollow = !userHasScrolledUp && isNearBottom(40)
  if (existing) {
    mergeMessageIntoExisting(existing, normalized, preserve)
  } else {
    insertMessageInDisplayOrder({
      ...normalized,
      ...preserve,
      clientKey: preserve.clientKey || normalized.clientKey || normalized.id
    })
  }
  scheduleScrollToBottom(shouldFollow)
}

function replaceDraftWithMessage(draftId: string, message: Message, preserve: Partial<Message> = {}) {
  const draft = findMessage(draftId)
  const normalized = normalizeLoadedMessage(message)
  const shouldFollow = !userHasScrolledUp && isNearBottom(40)
  if (!draft) {
    upsertMessage(normalized, preserve)
    return
  }
  mergeMessageIntoExisting(draft, normalized, preserve)
  scheduleScrollToBottom(shouldFollow)
}

async function refreshMessageById(messageId?: string | null) {
  const conversationId = currentId.value
  if (!conversationId || !messageId) return
  try {
    const updated = await apiFetch<Message>(`/conversations/${conversationId}/messages/${messageId}`)
    if (currentId.value !== conversationId) return
    upsertMessage(updated)
    syncActiveRequestState()
  } catch {
    // The event may arrive before the transaction is visible; the next snapshot reconciles it.
  }
}

function applyConversationEvent(event: string, data: any) {
  const conversationId = data?.conversation_id || data?.conversationId
  if (conversationId && currentId.value && conversationId !== currentId.value) return
  const messageId = data?.message_id || data?.messageId
  const eventMessage: Message = {
    id: messageId || `event-${Date.now()}`,
    conversationId: conversationId || currentId.value || 'new',
    role: 'assistant',
    content: typeof data.content === 'string' ? data.content : '',
    status: data.status || 'streaming',
    model: data.model,
    totalTokens: Number(data.total_tokens || data.totalTokens || 0),
    createdAt: data.created_at || data.createdAt || new Date().toISOString(),
    parentMessageId: data.user_message_id || data.userMessageId || data.parent_message_id || data.parentMessageId
  }
  const message = findMessage(messageId) || findMessageForMerge(eventMessage)
  if (event === 'message_delta') {
    if (!message) {
      void refreshMessageById(messageId)
      return
    }
    if (typeof data.content === 'string') {
      message.content = data.content
    } else {
      appendStreamText(message, data.text || '')
    }
    message.status = 'streaming'
    applyRuntimeProgress(message, data)
    if (message.content.trim()) freezeFirstTokenSeconds(message, data)
    syncActiveRequestState()
    scheduleScrollToBottom()
    return
  }
  if (event === 'message_snapshot') {
    if (!message) {
      void refreshMessageById(messageId)
      return
    }
    if (typeof data.content === 'string') message.content = data.content
    if (data.status) message.status = data.status
    applyRuntimeProgress(message, data)
    syncActiveRequestState()
    return
  }
  if (event === 'message_started') {
    if (message) {
      message.status = 'streaming'
      applyRuntimeProgress(message, data)
      syncActiveRequestState()
    } else {
      void refreshMessageById(messageId)
    }
    return
  }
  if (event === 'image_status' || event === 'image_progress') {
    if (!message) {
      void refreshMessageById(messageId)
      return
    }
    clearImageFinalizationTimer(message.id)
    applyImageProgress(message, data)
    syncActiveRequestState()
    syncImagePolling()
    scheduleScrollToBottom()
    return
  }
  if (event === 'image_completed') {
    if (message) {
      applyRuntimeProgress(message, data)
      if (data.attachment) {
        const attachments = message.attachments || []
        if (!attachments.some((attachment) => attachment.id === data.attachment.id)) {
          message.attachments = [...attachments, data.attachment]
        }
      }
      message.status = 'streaming'
      const progress = message.imageProgress || message.image_progress
      applyImageProgress(message, {
        b64Json: progress?.b64Json || progress?.b64_json || '',
        b64_json: progress?.b64_json || progress?.b64Json || '',
        outputFormat: progress?.outputFormat || progress?.output_format || 'png',
        output_format: progress?.output_format || progress?.outputFormat || 'png',
        index: progress?.index || 1,
        total: progress?.total || 1,
        phase: 'saving',
        detail: '最终图已保存，正在准备下载按钮。',
        size: data.size || progress?.size || message.generatedImageSize
      })
      clearImageFinalizationTimer(message.id)
      const activeLoad = activeConversationLoad
      const timer = window.setTimeout(() => {
        if (activeConversationLoad !== activeLoad || currentId.value !== (conversationId || currentId.value)) {
          imageFinalizationTimers.delete(message.id)
          return
        }
        finishImageFinalization(message.id, data)
      }, IMAGE_FINALIZATION_MIN_MS)
      imageFinalizationTimers.set(message.id, timer)
    }
    syncActiveRequestState()
    scheduleScrollToBottom(true)
    return
  }
  if (event === 'usage') {
    if (message) {
      message.promptTokens = Number(data.prompt_tokens || data.promptTokens || message.promptTokens || 0)
      message.completionTokens = Number(data.completion_tokens || data.completionTokens || message.completionTokens || 0)
      message.totalTokens = Number(data.total_tokens || data.totalTokens || message.totalTokens || 0)
      message.tokensSource = data.tokens_source || data.tokensSource || message.tokensSource
    }
    const actualPrompt = Number(data.prompt_tokens || data.promptTokens || 0)
    if (actualPrompt > 0) {
      contextStats.value = {
        ...contextStats.value,
        promptTokensEstimated: actualPrompt,
        tokensSource: 'actual'
      }
    }
    return
  }
  if (event === 'message_completed' || event === 'message_failed') {
    if (message) {
      applyRuntimeProgress(message, data)
      if (typeof data.content === 'string' && data.content.trim()) freezeFirstTokenSeconds(message, data)
      message.status = data.status || (event === 'message_completed' ? 'completed' : 'failed_no_output')
      if (typeof data.content === 'string') message.content = data.content
      if (event === 'message_failed' && !message.content.trim() && typeof data.message === 'string' && data.message.trim()) {
        message.content = data.message.trim()
      }
      if (message.status !== 'streaming') message.imageProgress = undefined
    }
    syncActiveRequestState()
    if (!messages.value.some((item) => item.role === 'assistant' && item.status === 'streaming' && messageIsImageGeneration(item))) {
      stopImagePolling()
    }
    void refreshMessageById(messageId)
    void loadConversations()
    void loadContextStats()
    return
  }
  if (event === 'context') {
    updateContextStats(data, 'estimated')
  }
}

function stopConversationEvents() {
  if (!conversationEventSource) return
  conversationEventSource.close()
  conversationEventSource = null
  conversationEventSourceId = null
}

async function reloadCurrentConversationMessages(conversationId: string) {
  const loadId = ++activeConversationLoad
  try {
    const loadedMessages = sortMessagesForDisplay(
      (await apiFetch<Message[]>(`/conversations/${conversationId}/messages`)).map(normalizeLoadedMessage)
    )
    if (loadId !== activeConversationLoad || currentId.value !== conversationId) return
    messages.value = loadedMessages
    syncActiveRequestState()
    syncImagePolling()
    syncConversationEvents()
    await loadContextStats()
  } catch {
    // Keep the current view; EventSource reconnect and later snapshots can fix it.
  }
}

function syncConversationEvents() {
  const conversationId = currentId.value
  if (!conversationId) {
    stopConversationEvents()
    return
  }
  if (conversationEventSource && conversationEventSourceId === conversationId) return
  stopConversationEvents()
  conversationEventSourceId = conversationId
  const source = new EventSource(`/api/conversations/${conversationId}/events`, { withCredentials: true })
  conversationEventSource = source
  const eventNames = [
    'message_started',
    'message_delta',
    'message_snapshot',
    'message_completed',
    'message_failed',
    'image_status',
    'image_progress',
    'image_completed',
    'usage',
    'context'
  ]
  for (const eventName of eventNames) {
    source.addEventListener(eventName, (event) => {
      try {
        applyConversationEvent(eventName, JSON.parse((event as MessageEvent).data || '{}'))
      } catch {
        // Ignore malformed event payloads; the next snapshot will reconcile.
      }
    })
  }
  source.onerror = () => {
    if (conversationEventSource !== source) return
    window.setTimeout(() => {
      if (conversationEventSource === source && currentId.value === conversationId) {
        void reloadCurrentConversationMessages(conversationId)
      }
    }, 5000)
  }
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

function syncActiveRequestState() {
  streaming.value = messages.value.some((message) => message.status === 'streaming')
}

function stopImagePolling() {
  if (imagePollingTimer === null) return
  window.clearInterval(imagePollingTimer)
  imagePollingTimer = null
}

async function refreshStreamingImages() {
  const conversationId = currentId.value
  if (!conversationId) return
  const streamingImages = messages.value.filter(
    (message) => message.role === 'assistant' && message.status === 'streaming' && messageIsImageGeneration(message)
  )
  if (!streamingImages.length) {
    stopImagePolling()
    return
  }
  await Promise.all(
    streamingImages.map(async (message) => {
      try {
        const updated = normalizeLoadedMessage(await apiFetch<Message>(`/conversations/${conversationId}/messages/${message.id}`))
        if (currentId.value !== conversationId) return
        mergeImageMessageIntoExisting(message, updated)
      } catch {
        // Keep the local timer alive; the next poll may succeed.
      }
    })
  )
  if (!messages.value.some((message) => message.role === 'assistant' && message.status === 'streaming' && messageIsImageGeneration(message))) {
    stopImagePolling()
    await loadConversations()
    await loadContextStats()
  }
}

function syncImagePolling() {
  const hasStreamingImage = messages.value.some(
    (message) => message.role === 'assistant' && message.status === 'streaming' && messageIsImageGeneration(message)
  )
  if (!hasStreamingImage) {
    stopImagePolling()
    return
  }
  if (imagePollingTimer !== null) return
  imagePollingTimer = window.setInterval(() => {
    void refreshStreamingImages()
  }, 3000)
}

function newChat() {
  activeConversationLoad++
  cancelPendingScroll()
  cancelPendingStreamFlush()
  clearAllImageFinalizationTimers()
  stopImagePolling()
  stopConversationEvents()
  currentId.value = null
  window.localStorage.removeItem(CURRENT_CONVERSATION_STORAGE_KEY)
  messages.value = []
  messagesLoading.value = false
  showScrollToBottom.value = false
  userHasScrolledUp = false
  error.value = ''
  streaming.value = false
}

async function loadConversations() {
  conversationsLoading.value = conversations.value.length === 0
  try {
    conversations.value = sortConversationsByUpdatedAt(await apiFetch<Conversation[]>('/conversations'))
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
    previousSelectedModel.value = selectedModel.value
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

function modelKeyChoiceFromError(err: ApiError, model: string): ModelKeyChoice {
  return {
    model,
    purpose: (err.detail?.purpose || 'none') as GroupPurpose,
    groupName: String(err.detail?.groupName || ''),
    candidateKeys: Array.isArray(err.detail?.candidateKeys) ? err.detail.candidateKeys : []
  }
}

async function refreshModelSelectionState() {
  await loadModels()
  if (settingsOpen.value && (settingsTab.value === 'api' || settingsTab.value === 'groups')) {
    await loadApiSettings()
  }
}

function closeModelKeyChoice() {
  modelKeyChoice.value = null
  modelKeyChoiceSaving.value = false
  selectedModel.value = previousSelectedModel.value
}

async function saveSelectedModel(apiKeyId?: string) {
  if (!selectedModel.value) return
  try {
    await apiFetch('/settings/model', {
      method: 'PATCH',
      body: JSON.stringify({ model: selectedModel.value, apiKeyId })
    })
    previousSelectedModel.value = selectedModel.value
    modelKeyChoice.value = null
    modelKeyChoiceSaving.value = false
    await Promise.all([loadContextStats(), refreshModelSelectionState()])
  } catch (err) {
    if (err instanceof ApiError && err.code === 'KEY_GROUP_CHOICE_REQUIRED') {
      modelKeyChoice.value = modelKeyChoiceFromError(err, selectedModel.value)
      error.value = ''
      return
    }
    if (err instanceof ApiError && err.code === 'KEY_GROUP_REQUIRED') {
      const groupName = String(err.detail?.groupName || '对应')
      error.value = `当前模型需要 ${groupName} 分组下的密钥，请先在 API 管理中添加。`
      selectedModel.value = previousSelectedModel.value
      await openSettings('api')
      return
    }
    if (err instanceof ApiError) {
      error.value = err.message
      selectedModel.value = previousSelectedModel.value
    }
  }
}

async function chooseModel(model: string) {
  selectedModel.value = model
  await saveSelectedModel()
}

async function chooseModelKey(keyId: string) {
  if (!modelKeyChoice.value) return
  modelKeyChoiceSaving.value = true
  selectedModel.value = modelKeyChoice.value.model
  await saveSelectedModel(keyId)
  if (modelKeyChoice.value) modelKeyChoiceSaving.value = false
}

async function openConversation(id: string, focusMessageId?: string | null) {
  if (deletingConversationId.value === id) return
  const loadId = ++activeConversationLoad
  cancelPendingScroll()
  cancelPendingStreamFlush()
  clearAllImageFinalizationTimers()
  stopImagePolling()
  currentId.value = id
  window.localStorage.setItem(CURRENT_CONVERSATION_STORAGE_KEY, id)
  messages.value = []
  messagesLoading.value = true
  userHasScrolledUp = false
  showScrollToBottom.value = false
  try {
    const params = focusMessageId ? `?aroundMessageId=${encodeURIComponent(focusMessageId)}&limit=120` : ''
    const loadedMessages = sortMessagesForDisplay(
      (await apiFetch<Message[]>(`/conversations/${id}/messages${params}`)).map(normalizeLoadedMessage)
    )
    if (loadId !== activeConversationLoad) return
    messages.value = loadedMessages
    messagesLoading.value = false
    syncActiveRequestState()
    syncImagePolling()
    syncConversationEvents()
    void loadContextStats()
    if (focusMessageId) {
      await scrollToMessage(focusMessageId)
    } else {
      await scrollLoadedConversationToBottom(loadId)
    }
  } catch (err) {
    if (loadId === activeConversationLoad && err instanceof ApiError) error.value = err.message
  } finally {
    if (loadId === activeConversationLoad) messagesLoading.value = false
  }
}

async function scrollToMessage(messageId: string) {
  await waitForMessageLayout()
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
  if (conversation.id === currentId.value && currentConversationStreaming.value) return
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
    conversations.value = sortConversationsByUpdatedAt(
      conversations.value.map((item) => (item.id === updated.id ? { ...item, ...updated } : item))
    )
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

function requestDeleteConversation(conversation: Conversation) {
  if (deletingConversationId.value || (conversation.id === currentId.value && currentConversationStreaming.value)) return
  conversationPendingDelete.value = conversation
  deleteConfirmOpen.value = true
}

function closeDeleteConfirm() {
  if (deletingConversationId.value) return
  deleteConfirmOpen.value = false
  conversationPendingDelete.value = null
}

async function confirmDeleteConversation() {
  const conversation = conversationPendingDelete.value
  if (!conversation || deletingConversationId.value || (conversation.id === currentId.value && currentConversationStreaming.value)) return
  deletingConversationId.value = conversation.id
  try {
    await apiFetch(`/conversations/${conversation.id}`, { method: 'DELETE' })
    conversations.value = conversations.value.filter((item) => item.id !== conversation.id)
    deleteConfirmOpen.value = false
    conversationPendingDelete.value = null
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
    const uploadFile = await compressImageFile(file)
    const presign = await apiFetch<{ uploadId: string; uploadUrl: string; method: string }>('/attachments/presign', {
      method: 'POST',
      body: JSON.stringify({ filename: uploadFile.name, contentType: uploadFile.type, sizeBytes: uploadFile.size })
    })
    const csrf = readCookie('csrf_token')
    const uploadResponse = await fetch(presign.uploadUrl, {
      method: presign.method,
      body: uploadFile,
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : undefined
    })
    if (!uploadResponse.ok) {
      throw new Error('附件上传失败')
    }
    const attachment = await apiFetch<Attachment>('/attachments/commit', {
      method: 'POST',
      body: JSON.stringify({ uploadId: presign.uploadId, filename: uploadFile.name, contentType: uploadFile.type })
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
  if ((!input.value.trim() && !pendingAttachments.value.length) || currentConversationStreaming.value) return
  const isImageGeneration = selectedModelIsImageGeneration()
  if (isImageGeneration && pendingAttachments.value.length) {
    error.value = '图像生成暂不支持同时上传附件。'
    return
  }
  if (pendingAttachments.value.some(isImageAttachment) && !selectedModelSupportsVision()) {
    error.value = '当前模型不支持图片理解，请切换到支持视觉的模型。'
    return
  }
  cancelPendingScroll()
  userHasScrolledUp = false
  error.value = ''
  const userText = input.value
  const outgoingAttachments = [...pendingAttachments.value]
  const attachmentIds = pendingAttachments.value.map((item) => item.id)
  input.value = ''
  pendingAttachments.value = []
  const draftConversationId = currentId.value || 'new'
  const draftUserId = `local-${Date.now()}`
  const draftAssistantId = `stream-${Date.now()}`
  const userClientKey = draftUserId
  const assistantClientKey = draftAssistantId
  const nowIso = new Date().toISOString()
  const userDraft: Message = {
    id: draftUserId,
    clientKey: userClientKey,
    conversationId: draftConversationId,
    role: 'user',
    content: userText,
    status: 'completed',
    totalTokens: 0,
    attachments: outgoingAttachments,
    createdAt: nowIso
  }
  const assistantDraft: Message = {
    id: draftAssistantId,
    clientKey: assistantClientKey,
    conversationId: draftConversationId,
    parentMessageId: userDraft.id,
    role: 'assistant',
    content: '',
    status: 'streaming',
    totalTokens: 0,
    createdAt: nowIso,
    startedAt: Date.now(),
    elapsedSeconds: 0
  }
  insertMessageInDisplayOrder(userDraft)
  insertMessageInDisplayOrder(assistantDraft)
  syncActiveRequestState()
  await scrollMessagesToBottom('auto')
  const path = currentId.value ? `/conversations/${currentId.value}/messages` : '/conversations/new/messages'
  try {
    const result = await apiFetch<SendMessageResponse>(path, {
      method: 'POST',
      body: JSON.stringify({
        content: userText,
        model: selectedModel.value,
        attachmentIds,
        referencedAttachmentIds: attachmentIds,
        reasoningEffort: reasoningEffort.value
      })
    })
    const newConversationId = result.conversationId || result.conversation_id
    const userMessage = result.userMessage || result.user_message
    const assistantMessage = result.assistantMessage || result.assistant_message
    if (newConversationId) {
      currentId.value = newConversationId
      window.localStorage.setItem(CURRENT_CONVERSATION_STORAGE_KEY, newConversationId)
    }
    if (userMessage) replaceDraftWithMessage(draftUserId, userMessage, { clientKey: userClientKey })
    if (assistantMessage) {
      const startedAt = assistantDraft.startedAt
      const elapsedSeconds = Math.max(
        assistantDraft.elapsedSeconds ?? 0,
        normalizeProgressElapsed(assistantMessage.elapsedSeconds ?? assistantMessage.elapsed_seconds) ?? 0
      )
      replaceDraftWithMessage(draftAssistantId, assistantMessage, {
        clientKey: assistantClientKey,
        startedAt,
        started_at: startedAt,
        elapsedSeconds,
        elapsed_seconds: elapsedSeconds,
        firstTokenSeconds: assistantDraft.firstTokenSeconds ?? assistantDraft.first_token_seconds ?? assistantMessage.firstTokenSeconds,
        first_token_seconds: assistantDraft.first_token_seconds ?? assistantDraft.firstTokenSeconds ?? assistantMessage.first_token_seconds
      })
    }
    syncActiveRequestState()
    syncConversationEvents()
    syncImagePolling()
    await loadConversations()
    await loadContextStats()
  } catch (err) {
    cancelPendingScroll()
    const apiErr = err instanceof ApiError ? err : null
    const message = userFacingSendError(err)
    const assistant = findMessage(draftAssistantId)
    if (assistant) {
      assistant.content = message
      assistant.status = 'failed_no_output'
    }
    error.value = message
    syncActiveRequestState()
    await scrollMessagesToBottom('smooth')
    if (apiErr?.code === 'INVALID_CREDENTIALS') await auth.loadMe().catch(() => router.push('/login'))
  }
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.shiftKey) return
  event.preventDefault()
  void send()
}

function ensureComposerMeasureElement() {
  if (composerMeasureElement) return composerMeasureElement
  composerMeasureElement = document.createElement('div')
  composerMeasureElement.setAttribute('aria-hidden', 'true')
  composerMeasureElement.className = 'composer-card is-empty-composer composer-measure-card'
  composerMeasureElement.style.position = 'fixed'
  composerMeasureElement.style.left = '-10000px'
  composerMeasureElement.style.top = '0'
  composerMeasureElement.style.visibility = 'hidden'
  composerMeasureElement.style.pointerEvents = 'none'
  composerMeasureElement.style.contain = 'layout style'
  const measureInput = document.createElement('textarea')
  measureInput.className = 'composer-input'
  measureInput.rows = 1
  measureInput.tabIndex = -1
  measureInput.readOnly = true
  composerMeasureElement.appendChild(measureInput)
  document.body.appendChild(composerMeasureElement)
  return composerMeasureElement
}

function isComposerTextSingleLine(textarea: HTMLTextAreaElement) {
  const value = textarea.value
  if (!value) return true
  const measure = ensureComposerMeasureElement()
  const measureInput = measure.querySelector<HTMLTextAreaElement>('.composer-input')
  if (!measureInput) return false

  measure.style.width = `${textarea.getBoundingClientRect().width || textarea.clientWidth}px`
  measureInput.value = value
  measureInput.style.height = 'auto'
  return measureInput.scrollHeight <= measureInput.clientHeight + 1
}

function updateEmptyFooterAnchorShift() {
  if (!isEmptyChat.value) return
  const footer = chatFooter.value
  if (!footer) return

  const footerHeight = footer.getBoundingClientRect().height
  if (footerHeight > 0) {
    footer.style.setProperty('--empty-footer-anchor-shift', `${-(footerHeight / 2)}px`)
  }
}

function composerMaxInputHeight(textarea: HTMLTextAreaElement, cssMaxHeight: number | null) {
  let maxHeight = cssMaxHeight ?? Number.POSITIVE_INFINITY
  if (!isEmptyChat.value || composerCompact.value) return maxHeight

  const card = textarea.closest<HTMLElement>('.composer-card')
  const cardRect = card?.getBoundingClientRect()
  const textareaRect = textarea.getBoundingClientRect()
  const bottomChrome = cardRect ? Math.max(0, cardRect.bottom - textareaRect.bottom) : 0
  const bottomGap = Math.max(48, Math.min(72, window.innerHeight * 0.07))
  const viewportMaxHeight = window.innerHeight - textareaRect.top - bottomChrome - bottomGap
  if (viewportMaxHeight > 0) {
    maxHeight = Math.min(maxHeight, viewportMaxHeight)
  }

  const minHeight = Number.parseFloat(getComputedStyle(textarea).minHeight)
  return Number.isFinite(minHeight) ? Math.max(minHeight, maxHeight) : maxHeight
}

function resizeComposerInput() {
  const textarea = composerInput.value
  if (!textarea) return

  const previousCompact = composerCompact.value
  if (canUseCompactComposer.value && previousCompact) {
    updateEmptyFooterAnchorShift()
  }
  if (!canUseCompactComposer.value) {
    composerCompact.value = false
  } else {
    composerCompact.value = isComposerTextSingleLine(textarea)
  }
  if (previousCompact !== composerCompact.value) {
    void nextTick().then(resizeComposerInput)
  }

  textarea.style.height = 'auto'
  const maxHeight = Number.parseFloat(getComputedStyle(textarea).maxHeight)
  const nextMaxHeight = composerMaxInputHeight(textarea, Number.isFinite(maxHeight) ? maxHeight : null)
  const nextHeight = Math.min(textarea.scrollHeight, nextMaxHeight)
  textarea.style.height = `${nextHeight}px`
  composerScrollable.value = textarea.scrollHeight > nextHeight
  textarea.style.overflowY = composerScrollable.value ? 'auto' : 'hidden'
}

function scheduleComposerResize() {
  if (composerResizeFrame !== null) {
    window.cancelAnimationFrame(composerResizeFrame)
  }
  composerResizeFrame = window.requestAnimationFrame(() => {
    composerResizeFrame = null
    resizeComposerInput()
  })
}

watch(input, async () => {
  await nextTick()
  resizeComposerInput()
})

watch(composerExpanded, async () => {
  await nextTick()
  resizeComposerInput()
})

watch(
  () => canUseCompactComposer.value,
  async () => {
    await nextTick()
    resizeComposerInput()
  }
)

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
  const storedConversationId = window.localStorage.getItem(CURRENT_CONVERSATION_STORAGE_KEY)
  if (storedConversationId && conversations.value.some((conversation) => conversation.id === storedConversationId)) {
    await openConversation(storedConversationId)
  }
  await nextTick()
  resizeComposerInput()
  window.addEventListener('resize', scheduleComposerResize)
})

onUnmounted(() => {
  clearAllImageFinalizationTimers()
  stopImagePolling()
  stopConversationEvents()
  cancelPendingScroll()
  if (composerResizeFrame !== null) {
    window.cancelAnimationFrame(composerResizeFrame)
    composerResizeFrame = null
  }
  composerMeasureElement?.remove()
  composerMeasureElement = null
  window.removeEventListener('resize', scheduleComposerResize)
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
            <div class="sidebar-brand">
              <img class="sidebar-brand-icon" src="/brand/knowhub-icon.png" alt="" aria-hidden="true" />
              <span>KnowHub</span>
            </div>
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
              :disabled="conversation.id === currentId && currentConversationStreaming"
              @click.stop="openRenameConversation(conversation)"
            >
              <Pencil :size="14" />
            </button>
            <button
              class="conversation-action-button"
              type="button"
              title="删除对话"
              aria-label="删除对话"
              :disabled="deletingConversationId === conversation.id || (conversation.id === currentId && currentConversationStreaming)"
              @click.stop="requestDeleteConversation(conversation)"
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
            v-if="!selectedModelIsImageGeneration()"
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
            :disabled="currentConversationStreaming"
            @click.stop="openRenameConversation(currentConversation)"
          >
            <Pencil :size="13" />
          </button>
        </div>
      </header>

      <section
        ref="messageScroller"
        class="chat-surface flex-1 min-h-0 overflow-y-auto overscroll-contain px-6 py-8"
        @scroll="handleScrollerScroll"
        @wheel.passive="handleScrollerWheel"
        @touchstart.passive="handleScrollerTouchStart"
      >
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
            :key="message.clientKey || message.id"
            :message="message"
            @preview-attachment="openAttachmentPreview"
          />
        </div>
      </section>

      <footer ref="chatFooter" class="chat-footer p-4">
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
            rows="1"
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
              <button class="send-button" type="submit" :disabled="currentConversationStreaming || (!input.trim() && !pendingAttachments.length)" title="发送" aria-label="发送">
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
                        <span>{{ key.groupName || 'gpt-chat' }}</span>
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
                      <AppSelect
                        v-model="newGroup.purpose"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="groupPurposeOptions"
                      />
                      <button class="settings-primary" type="submit">创建分组</button>
                    </form>

                    <div class="settings-group-list">
                      <button
                        v-for="group in apiKeyGroups"
                        :key="group.id"
                        class="settings-group-list-item"
                        :class="{ active: selectedGroupId === group.id }"
                        type="button"
                        @click="loadGroupKeys(group.id)"
                      >
                        <span>{{ group.name }}</span>
                        <small>{{ groupPurposeLabel(group.purpose) }}{{ group.isSystem ? ' · 默认' : '' }}</small>
                      </button>
                    </div>
                  </aside>

                  <section class="settings-group-detail">
                    <div class="settings-card-header">
                      <div>
                        <h3>{{ selectedApiKeyGroup?.name || '分组详情' }}</h3>
                        <p>当前分组下的密钥和所属用户。</p>
                      </div>
                      <button class="settings-secondary" :disabled="groupKeysLoading" @click="loadGroupKeys(selectedGroupId)">刷新密钥</button>
                    </div>

                    <div v-if="selectedApiKeyGroup" class="settings-group-editor">
                      <input
                        v-model="groupDraftFor(selectedApiKeyGroup).name"
                        class="settings-input"
                        placeholder="分组名称"
                        :disabled="selectedApiKeyGroup.isSystem"
                      />
                      <input
                        v-model="groupDraftFor(selectedApiKeyGroup).description"
                        class="settings-input"
                        placeholder="备注"
                      />
                      <AppSelect
                        :model-value="selectedApiKeyGroup.isSystem ? selectedApiKeyGroup.purpose : groupDraftFor(selectedApiKeyGroup).purpose"
                        class="settings-select"
                        button-class="settings-select-button"
                        menu-class="settings-select-menu"
                        option-class="settings-select-option"
                        :options="groupPurposeOptions"
                        :disabled="selectedApiKeyGroup.isSystem"
                        @update:model-value="groupDraftFor(selectedApiKeyGroup).purpose = String($event) as GroupPurpose"
                      />
                      <button class="settings-secondary" @click="saveApiKeyGroup(selectedApiKeyGroup)">保存</button>
                      <button v-if="!selectedApiKeyGroup.isSystem" class="settings-danger" @click="deleteApiKeyGroup(selectedApiKeyGroup)">删除</button>
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
                          <span>{{ key.groupName || 'gpt-chat' }}</span>
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
                <img class="attachment-preview-image" :src="attachmentImageSrc(attachmentPreview)" :alt="attachmentPreview.filename" />
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
      <div v-if="modelKeyChoice" class="confirm-modal-backdrop" @click.self="closeModelKeyChoice">
        <section class="confirm-modal model-key-modal" role="dialog" aria-modal="true" aria-label="选择模型密钥">
          <div class="confirm-modal-header">
            <h2>选择密钥</h2>
            <button type="button" class="confirm-modal-close" title="关闭" aria-label="关闭" @click="closeModelKeyChoice">
              <X :size="18" />
            </button>
          </div>
          <p>{{ modelKeyChoice.model }} 需要使用 {{ modelKeyChoice.groupName || groupPurposeLabel(modelKeyChoice.purpose) }} 分组下的密钥。</p>
          <div class="model-key-choice-list">
            <button
              v-for="key in modelKeyChoice.candidateKeys"
              :key="key.id"
              type="button"
              class="model-key-choice-row"
              :disabled="modelKeyChoiceSaving"
              @click="chooseModelKey(key.id)"
            >
              <strong>{{ key.name }}</strong>
              <span>{{ key.groupName || modelKeyChoice.groupName }}</span>
              <small>{{ key.maskedKey || (key.last4 ? `****${key.last4}` : '') }}</small>
            </button>
          </div>
          <div class="confirm-modal-actions">
            <button type="button" class="confirm-secondary-button" :disabled="modelKeyChoiceSaving" @click="closeModelKeyChoice">取消</button>
          </div>
        </section>
      </div>
    </Transition>

    <Transition name="dialog-pop">
      <div v-if="deleteConfirmOpen" class="confirm-modal-backdrop" @click.self="closeDeleteConfirm">
        <section class="confirm-modal" role="dialog" aria-modal="true" aria-label="确认删除对话">
          <div class="confirm-modal-header">
            <h2>删除对话？</h2>
            <button type="button" class="confirm-modal-close" title="关闭" aria-label="关闭" @click="closeDeleteConfirm">
              <X :size="18" />
            </button>
          </div>
          <p>对话「{{ conversationPendingDelete?.title || '未命名对话' }}」删除后将从侧边栏移除。</p>
          <div class="confirm-modal-actions">
            <button type="button" class="confirm-secondary-button" :disabled="Boolean(deletingConversationId)" @click="closeDeleteConfirm">取消</button>
            <button type="button" class="confirm-danger-button" :disabled="Boolean(deletingConversationId)" @click="confirmDeleteConversation">
              {{ deletingConversationId ? '删除中...' : '确认删除' }}
            </button>
          </div>
        </section>
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
