<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch, type CSSProperties } from 'vue'
import { ArrowDown, Download, FileText, GitBranch, Globe, Image as ImageIcon, KeyRound, LogOut, Maximize2, MessageCircle, Minimize2, PanelLeftClose, PanelLeftOpen, Paperclip, Pencil, Pin, PinOff, Plus, RefreshCw, Search, Send, Settings, ShieldCheck, Trash2, X } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import { ApiError, apiFetch, localizeApiMessage, readCookie } from '../api/client'
import AppSelect from '../components/AppSelect.vue'
import ChatMessage from '../components/ChatMessage.vue'
import SourceIcon from '../components/SourceIcon.vue'
import { useAuthStore } from '../stores/auth'
import type {
  ApiKeyEntry,
  ApiKeyGroup,
  Attachment,
  AttachmentChunkPreview,
  Conversation,
  ConversationAttachment,
  ConversationSearchResult,
  ImageGenerationSettings,
  ImageProgress,
  Message,
  ModelEndpoint,
  SendMessageResponse,
  User,
  WebSearchMode,
  WebSearchSettings,
  WebSearchSource,
  WebSearchStatus
} from '../types'
import { copyText } from '../utils/clipboard'
import { sourceOpenUrl, sourceSiteName } from '../utils/sources'

type ThemeMode = 'dark' | 'light'
type SettingsTab = 'appearance' | 'image' | 'web' | 'api' | 'groups' | 'account'
type GroupPurpose = 'none' | 'chat' | 'image'
type StreamProgressSnapshot = {
  startedAt?: number
  elapsedSeconds?: number
  progressDetail?: string
  progressPhase?: string
}

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
const FILE_TREE_PIN_STORAGE_KEY = 'private-gpt-file-tree-pinned'
const IMAGE_FINALIZATION_MIN_MS = 800
const ATTACHMENT_IMAGE_MAX_EDGE = 1920
const ATTACHMENT_IMAGE_QUALITY = 0.82
const ATTACHMENT_IMAGE_MIN_COMPRESS_BYTES = 450 * 1024

type ReasoningEffort = 'low' | 'medium' | 'high' | 'xhigh'
type FileTreeGroup = 'images' | 'documents'
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
const streamProgressByMessageId = new Map<string, StreamProgressSnapshot>()
const messagesLoading = ref(false)
const input = ref('')
const composerInput = ref<HTMLTextAreaElement | null>(null)
const composerExpanded = ref(false)
const models = ref<string[]>([])
const selectedModel = ref('')
const reasoningEffort = ref<ReasoningEffort>('medium')
const streaming = ref(false)
const pendingAttachments = ref<Attachment[]>([])
const conversationAttachments = ref<ConversationAttachment[]>([])
const draftConversationAttachments = ref<ConversationAttachment[]>([])
const fileTreeLoading = ref(false)
const fileTreePinned = ref(true)
const fileTreeGroupOpen = ref<Record<FileTreeGroup, boolean>>({ images: true, documents: true })
const questionNavExpanded = ref(false)
const activeQuestionMessageId = ref<string | null>(null)
const attachmentRenameOpen = ref(false)
const attachmentRenameSaving = ref(false)
const attachmentRenameDraft = ref('')
const attachmentRenameError = ref('')
const attachmentRenameTarget = ref<ConversationAttachment | null>(null)
const attachmentRenameInput = ref<HTMLInputElement | null>(null)
const attachmentDeleteOpen = ref(false)
const attachmentDeleting = ref(false)
const attachmentDeleteTarget = ref<ConversationAttachment | null>(null)
const uploadingAttachmentNames = ref<string[]>([])
const composerDragActive = ref(false)
const composerCompact = ref(true)
const composerScrollable = ref(false)
const composerAttachmentsScroller = ref<HTMLElement | null>(null)
const attachmentPreviewOpen = ref(false)
const attachmentPreview = ref<Attachment | null>(null)
const attachmentPreviewText = ref('')
const attachmentPreviewChunks = ref<AttachmentChunkPreview[]>([])
const attachmentPreviewLoading = ref(false)
const attachmentPreviewError = ref('')
const reindexingAttachment = ref(false)
const sourceDrawerOpen = ref(false)
const sourceDrawerMessageId = ref<string | null>(null)
const sourceDrawerSources = ref<WebSearchSource[]>([])
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
const questionNavScroll = ref<HTMLElement | null>(null)
const showScrollToBottom = ref(false)
const questionNavHasBefore = ref(false)
const questionNavHasAfter = ref(false)
const questionNavShortConversation = ref(false)
const chatFooterHeight = ref(0)
let userHasScrolledUp = false
let programmaticScrollUntil = 0
let userScrollSettlingUntil = 0
let questionNavLockUntil = 0
let questionNavLockedMessageId: string | null = null

const themeMode = ref<ThemeMode>('dark')
const bubbleColor = ref<BubbleColor>('blue')
const textSize = ref(15)
const codeSize = ref(12)
const welcomeMessage = ref('')
const welcomeFontSize = ref(52)
const settingsMenuOpen = ref(false)
const settingsOpen = ref(false)
const accessSwitchOpen = ref(false)
const accessSwitchLoading = ref(false)
const accessSwitchEndpointId = ref<string | null>(null)
const accessSwitchKeyId = ref<string | null>(null)
const accessSwitchNotice = ref('')
const accessSwitchError = ref('')
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
const modelEndpoints = ref<ModelEndpoint[]>([])
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
const activeModelEndpoint = computed(() => modelEndpoints.value.find((endpoint) => endpoint.isActive) || modelEndpoints.value[0] || null)
const activeScopedApiKeys = computed(() => apiKeys.value.filter((key) => key.isActive))
const apiKeyScopeSummary = computed(() => {
  if (!activeModelEndpoint.value) return '未选择 BaseURL'
  if (!apiKeys.value.length) return '当前 BaseURL 下暂无 API Key'
  return `${apiKeys.value.length} 个 API Key / ${activeScopedApiKeys.value.length} 个当前分组密钥`
})
const modelOptions = computed(() => models.value.map((model) => ({ value: model, label: model })))
const endpointOptions = computed(() =>
  modelEndpoints.value.map((endpoint) => ({
    value: endpoint.id,
    label: endpoint.isActive ? `${endpoint.name} (当前)` : endpoint.name,
    hint: endpoint.baseUrl
  }))
)
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
const defaultImageGroup = computed(() => apiKeyGroups.value.find((group) => group.purpose === 'image') || defaultChatGroup.value)
const keyDrafts = ref<Record<string, { name: string; groupId: string }>>({})
const endpointDrafts = ref<Record<string, { name: string; baseUrl: string }>>({})
const groupDrafts = ref<Record<string, { name: string; description: string; purpose: GroupPurpose }>>({})
const newApiKey = ref({ name: '默认密钥', apiKey: '', groupId: '', endpointId: '', makeActive: true })
const newEndpoint = ref({ name: 'Default BaseURL', baseUrl: '', makeActive: true })
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
const webSearchEnabled = ref(false)
const webSearchMode = ref<WebSearchMode>('auto')
const webSearchMaxRounds = ref(3)
const webSearchModeDialogOpen = ref(false)
const webSearchModeDraft = ref<WebSearchMode>('auto')
const webSearchRoundDraft = ref(3)
const webSearchStatus = ref<WebSearchStatus>({ enabled: false, configured: false })
const webSearchSettingsLoading = ref(false)
const webSearchSettingsSaving = ref(false)
const webSearchTesting = ref(false)
const webSearchTestQuery = ref('')
const webSearchTestResults = ref<Array<{
  title: string
  url: string
  snippet?: string
  evidence?: string
  provider?: string
  confidence?: number
  rerankStatus?: string
  rerank_status?: string
  sourceTier?: string
  source_tier?: string
  matchedTerms?: string[]
  matched_terms?: string[]
  supportLevel?: string
  support_level?: string
  searchDepth?: string
  search_depth?: string
  degraded?: boolean
  filterReason?: string
  filter_reason?: string
}>>([])
const webSearchSettings = ref<WebSearchSettings>({
  enabled: false,
  searxngBaseUrl: '',
  resultCount: 5,
  language: 'all',
  safesearch: '1',
  timeoutSeconds: 20,
  fetchTimeoutSeconds: 20,
  maxToolCalls: 4,
  fetchMaxChars: 12000,
  providerOrder: ['bocha', 'sougou', 'jina', 'searxng', 'serper'],
  searxngEngines: ['bing', 'baidu'],
  candidateCount: 20,
  fetchTopN: 5,
  chunkSize: 900,
  chunkOverlap: 120,
  maxEvidenceChunks: 8,
  rerankEnabled: true,
  rerankerModel: 'BAAI/bge-reranker-v2-m3',
  minRelevanceScore: 0.35,
  trustedDomains: [],
  blockedDomains: [],
  providerStatus: {}
})

let scrollFrame: number | null = null
let composerResizeFrame: number | null = null
let composerMeasureElement: HTMLDivElement | null = null
let chatFooterResizeObserver: ResizeObserver | null = null
let activeConversationLoad = 0
let imagePollingTimer: number | null = null
let conversationEventSource: EventSource | null = null
let conversationEventSourceId: string | null = null
const imageFinalizationTimers = new Map<string, number>()
const QUESTION_NAV_EDGE_THRESHOLD = 2
const QUESTION_NAV_SHORT_SCROLL_RANGE = 220
const QUESTION_NAV_BOTTOM_THRESHOLD_MAX = 180
const QUESTION_NAV_BOTTOM_THRESHOLD_RATIO = 0.18
const QUESTION_NAV_LAST_VISIBLE_INSET_MAX = 96
const QUESTION_NAV_LAST_VISIBLE_INSET_RATIO = 0.18
const USER_SCROLL_SETTLE_MS = 240

const currentConversation = computed(() => conversations.value.find((item) => item.id === currentId.value))
const currentConversationStreaming = computed(() => messages.value.some((message) => message.status === 'streaming'))
const webSearchAvailable = computed(() => webSearchStatus.value.enabled && webSearchStatus.value.configured && !selectedModelIsImageGeneration())
const webSearchToggleDisabled = computed(() => currentConversationStreaming.value || (!webSearchEnabled.value && !webSearchAvailable.value))
const webSearchToggleLabel = computed(() => {
  if (currentConversationStreaming.value) return '生成中无法切换联网搜索'
  if (webSearchEnabled.value) return webSearchAvailable.value ? '关闭联网搜索' : '关闭联网搜索（当前全局不可用）'
  return webSearchAvailable.value ? '开启联网搜索' : '联网搜索未配置'
})
const webSearchModeText = computed(() => {
  if (!webSearchEnabled.value) return ''
  if (webSearchMode.value === 'deep') return `深搜 ${webSearchMaxRounds.value}轮`
  if (webSearchMode.value === 'fast') return '快速'
  return '自动'
})
const activeFileTreeAttachments = computed(() => (currentId.value ? conversationAttachments.value : draftConversationAttachments.value))
const selectedFileTreeAttachments = computed(() => activeFileTreeAttachments.value.filter((item) => item.selected))
const selectedFileTreeAttachmentIds = computed(() => selectedFileTreeAttachments.value.map((item) => item.attachment.id))
const composerAttachmentItems = computed(() => selectedFileTreeAttachments.value)
const composerImageAttachments = computed(() => composerAttachmentItems.value.filter((item) => isImageAttachment(item.attachment)))
const composerDocumentAttachments = computed(() => composerAttachmentItems.value.filter((item) => !isImageAttachment(item.attachment)))
const fileTreeImages = computed(() => activeFileTreeAttachments.value.filter((item) => isImageAttachment(item.attachment)))
const fileTreeDocuments = computed(() => activeFileTreeAttachments.value.filter((item) => !isImageAttachment(item.attachment)))
const userQuestionNavItems = computed(() =>
  messages.value
    .filter((message) => message.role === 'user')
    .map((message, index) => {
      const text = message.content.trim().replace(/\s+/g, ' ')
      const fallback = message.attachments?.length ? '附件消息' : '空消息'
      return {
        id: message.id,
        index: index + 1,
        title: text || fallback,
        summary: text ? truncateQuestionTitle(text) : fallback
      }
    })
)
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
    uploadingAttachmentNames.value.length === 0 &&
    composerAttachmentItems.value.length === 0
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

function handleComposerAttachmentsWheel(event: WheelEvent) {
  const scroller = composerAttachmentsScroller.value
  if (!scroller) return
  const overflow = scroller.scrollWidth - scroller.clientWidth
  if (overflow <= 1) return
  const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX
  if (!delta) return
  const nextLeft = Math.max(0, Math.min(overflow, scroller.scrollLeft + delta))
  if (Math.abs(nextLeft - scroller.scrollLeft) < 1) return
  event.preventDefault()
  scroller.scrollLeft = nextLeft
}

function isReferenceDocument(item: Attachment) {
  return !isImageAttachment(item) && item.parseStatus === 'success'
}

function truncateQuestionTitle(text: string) {
  const normalized = text.trim().replace(/\s+/g, ' ')
  return normalized.length > 28 ? `${normalized.slice(0, 28)}...` : normalized
}

function truncateProgressText(value: unknown, limit = 72) {
  const normalized = String(value || '').trim().replace(/\s+/g, ' ')
  return normalized.length > limit ? `${normalized.slice(0, limit)}...` : normalized
}

function webSearchProgressDetail(data: any) {
  const query = truncateProgressText(data?.query)
  const url = truncateProgressText(data?.url, 84)
  const resultCount = Number(data?.result_count ?? data?.resultCount)
  const sourceCount = Number(data?.source_count ?? data?.sourceCount)
  if (data?.detail && data.detail !== '正在联网搜索...' && data.detail !== '正在读取网页...') return data.detail
  if (data?.phase === 'searching' && query) return `正在搜索：${query}`
  if (data?.phase === 'reading' && url) return `正在读取：${url}`
  if (data?.phase === 'completed' && Number.isFinite(sourceCount)) return `联网搜索已完成，找到 ${sourceCount} 个来源。`
  if (Number.isFinite(resultCount)) return `搜索返回 ${resultCount} 条结果`
  return data?.detail || '正在联网搜索...'
}

function openWebSearchSources(message: Message, sources: WebSearchSource[]) {
  sourceDrawerMessageId.value = message.id
  sourceDrawerSources.value = sources
  sourceDrawerOpen.value = true
}

function closeWebSearchSources() {
  sourceDrawerOpen.value = false
}

function openSourceUrl(source: WebSearchSource) {
  const url = sourceOpenUrl(source)
  if (!url) return
  window.open(url, '_blank', 'noopener,noreferrer')
}

function truncateSourceText(value: string, limit = 170) {
  const chars = Array.from(value)
  if (chars.length <= limit) return value
  return `${chars.slice(0, limit).join('').replace(/[，。、；：,.:\s]+$/u, '')}...`
}

function cleanSourceText(value: unknown) {
  return String(value || '')
    .replace(/https?:\/\/\S+/gi, ' ')
    .replace(/\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:\/[^\s，。；、]*)?/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function sourcePublishedLabel(source: WebSearchSource) {
  const raw = String(source.publishedAt || source.published_at || '').trim()
  if (!raw) return ''
  const timestamp = new Date(raw).getTime()
  if (Number.isFinite(timestamp)) {
    return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(timestamp)
  }
  return raw.replace(/T.*$/u, '').replace(/\s+\d{1,2}:\d{2}.*$/u, '')
}

function sourceSummaryText(source: WebSearchSource) {
  const title = cleanSourceText(source.title)
  let summary = cleanSourceText(source.snippet)
  if (!summary) return ''
  if (title && summary.toLowerCase().startsWith(title.toLowerCase())) {
    summary = summary.slice(title.length).replace(/^[\s:：,，。.-]+/u, '').trim()
  }
  if (!summary || summary === title) return ''
  return truncateSourceText(summary)
}

function sourceDiagnostics(source: WebSearchSource) {
  const parts: string[] = []
  const confidence = typeof source.confidence === 'number' ? source.confidence : null
  const tier = source.sourceTier || source.source_tier
  const support = source.supportLevel || source.support_level
  const rerank = source.rerankStatus || source.rerank_status
  const depth = source.searchDepth || source.search_depth
  const filterReason = source.filterReason || source.filter_reason
  const matched = source.matchedTerms || source.matched_terms || []
  if (source.provider) parts.push(source.provider)
  if (confidence !== null) parts.push(`${Math.round(confidence * 100)}%`)
  if (tier) parts.push(`tier:${tier}`)
  if (support) parts.push(`support:${support}`)
  if (rerank) parts.push(`rerank:${rerank}`)
  if (depth) parts.push(`mode:${depth}`)
  if (source.degraded) parts.push('degraded')
  if (matched.length) parts.push(`match:${matched.slice(0, 4).join(',')}`)
  if (filterReason) parts.push(filterReason)
  return parts.join(' · ')
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
  const createdAt = parseApiDateMs(message.createdAt ?? message.created_at)
  return createdAt !== undefined ? Math.min(createdAt, Date.now()) : Date.now()
}

function currentRuntimeElapsed(message: Message, live = message.status === 'streaming') {
  const startedAt = normalizeProgressTimestamp(message.startedAt ?? message.started_at)
  const baseElapsed = normalizeProgressElapsed(message.elapsedSeconds ?? message.elapsed_seconds) ?? 0
  if (!live || !startedAt) return baseElapsed
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

function earliestProgressTimestamp(...values: unknown[]) {
  const timestamps = values
    .map((value) => normalizeProgressTimestamp(value))
    .filter((value): value is number => value !== undefined)
  if (!timestamps.length) return undefined
  const now = Date.now()
  return Math.min(...timestamps.map((timestamp) => Math.min(timestamp, now)))
}

function forgetStreamProgress(messageId?: string | null) {
  if (!messageId) return
  streamProgressByMessageId.delete(messageId)
}

function migrateStreamProgress(fromId?: string | null, toId?: string | null) {
  if (!fromId || !toId || fromId === toId) return
  const cached = streamProgressByMessageId.get(fromId)
  if (!cached) return
  const target = streamProgressByMessageId.get(toId)
  streamProgressByMessageId.set(toId, {
    startedAt: earliestProgressTimestamp(target?.startedAt, cached.startedAt),
    elapsedSeconds: Math.max(target?.elapsedSeconds ?? 0, cached.elapsedSeconds ?? 0),
    progressDetail: target?.progressDetail || cached.progressDetail,
    progressPhase: target?.progressPhase || cached.progressPhase
  })
  streamProgressByMessageId.delete(fromId)
}

function rememberStreamProgress(message: Message, data: any = {}) {
  if (!message.id || message.role !== 'assistant') return
  const status = data.status || message.status
  if (status !== 'streaming') {
    forgetStreamProgress(message.id)
    return
  }
  const cached = streamProgressByMessageId.get(message.id)
  const startedAt =
    earliestProgressTimestamp(message.startedAt ?? message.started_at, data.startedAt ?? data.started_at, cached?.startedAt) ??
    messageCreatedAtMs(message)
  const elapsedSeconds = Math.max(
    currentRuntimeElapsed(message, true),
    normalizeProgressElapsed(data.elapsedSeconds ?? data.elapsed_seconds) ?? 0,
    cached?.elapsedSeconds ?? 0,
    Math.floor((Date.now() - startedAt) / 1000),
    0
  )
  streamProgressByMessageId.set(message.id, {
    startedAt,
    elapsedSeconds,
    progressDetail: data.detail || data.progressDetail || data.progress_detail || message.progressDetail || message.progress_detail || cached?.progressDetail,
    progressPhase: data.phase || data.progressPhase || data.progress_phase || message.progressPhase || message.progress_phase || cached?.progressPhase
  })
}

function restoreCachedStreamProgress(message: Message) {
  if (message.status !== 'streaming') {
    forgetStreamProgress(message.id)
    return
  }
  const cached = streamProgressByMessageId.get(message.id)
  if (!cached) return
  const startedAt = earliestProgressTimestamp(message.startedAt ?? message.started_at, cached.startedAt)
  if (startedAt !== undefined) {
    message.startedAt = startedAt
    message.started_at = startedAt
  }
  const elapsedSeconds = Math.max(
    normalizeProgressElapsed(message.elapsedSeconds ?? message.elapsed_seconds) ?? 0,
    cached.elapsedSeconds ?? 0,
    startedAt !== undefined ? Math.floor((Date.now() - startedAt) / 1000) : 0,
    0
  )
  message.elapsedSeconds = elapsedSeconds
  message.elapsed_seconds = elapsedSeconds
  message.progressDetail = message.progressDetail || message.progress_detail || cached.progressDetail
  message.progress_detail = message.progressDetail
  message.progressPhase = message.progressPhase || message.progress_phase || cached.progressPhase
  message.progress_phase = message.progressPhase
}

function rememberStreamingProgressForMessages(items: Message[]) {
  for (const message of items) {
    if (message.role === 'assistant' && message.status === 'streaming') {
      rememberStreamProgress(message)
    }
  }
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

function clearRuntimeProgress(message: Message) {
  message.elapsedSeconds = undefined
  message.elapsed_seconds = undefined
  message.startedAt = undefined
  message.started_at = undefined
}

function freezeFirstTokenSeconds(message: Message, data: any = {}) {
  const incomingFirstToken = incomingFirstTokenSeconds(data)
  if (incomingFirstToken !== undefined) {
    setMessageFirstTokenSeconds(message, incomingFirstToken)
    if (message.status !== 'streaming' && data.status !== 'streaming') clearRuntimeProgress(message)
    return
  }
  if (messageFirstTokenSeconds(message) !== undefined) return
  setMessageFirstTokenSeconds(message, currentRuntimeElapsed(message))
  if (message.status !== 'streaming' && data.status !== 'streaming') clearRuntimeProgress(message)
}

function applyRuntimeProgress(message: Message, data: any = {}) {
  const targetStatus = data.status || message.status
  const isStreamingProgress = targetStatus === 'streaming'
  if (isStreamingProgress) restoreCachedStreamProgress(message)
  const cached = message.id ? streamProgressByMessageId.get(message.id) : undefined
  const existingStartedAt = normalizeProgressTimestamp(message.startedAt ?? message.started_at)
  const incomingStartedAt = normalizeProgressTimestamp(data.startedAt ?? data.started_at)
  const fallbackStartedAt = isStreamingProgress ? messageCreatedAtMs(message) : undefined
  const now = Date.now()
  const startedAt = isStreamingProgress
    ? earliestProgressTimestamp(existingStartedAt, incomingStartedAt, cached?.startedAt, fallbackStartedAt) ?? now
    : existingStartedAt ?? incomingStartedAt
  const firstTokenSeconds = incomingFirstTokenSeconds(data)
  const incomingElapsed = normalizeProgressElapsed(data.elapsedSeconds ?? data.elapsed_seconds)
  const existingElapsed = currentRuntimeElapsed(message, isStreamingProgress)
  const elapsedSeconds =
    isStreamingProgress
      ? Math.max(existingElapsed, incomingElapsed ?? 0, cached?.elapsedSeconds ?? 0, startedAt ? Math.floor((now - startedAt) / 1000) : 0)
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
  rememberStreamProgress(message, { ...data, status: targetStatus })
}

function normalizeLoadedMessage(message: Message): Message {
  message.webSearchSources = message.webSearchSources || message.web_search_sources || []
  message.web_search_sources = message.webSearchSources
  const progress = message.imageProgress || message.image_progress
  restoreCachedStreamProgress(message)
  if (message.status === 'streaming') {
    applyRuntimeProgress(message)
  } else {
    clearRuntimeProgress(message)
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
      startedAt: earliestProgressTimestamp(progress.startedAt ?? progress.started_at, message.startedAt ?? message.started_at) || messageCreatedAtMs(message),
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
  const startedAt = earliestProgressTimestamp(message.startedAt ?? message.started_at, existing?.startedAt ?? existing?.started_at, data.startedAt ?? data.started_at) || Date.now()
  const elapsedSeconds = Math.max(
    currentRuntimeElapsed(message),
    normalizeProgressElapsed(existing?.elapsedSeconds ?? existing?.elapsed_seconds) ?? 0,
    normalizeProgressElapsed(data.elapsedSeconds ?? data.elapsed_seconds) ?? 0
  )
  const incomingIndex = Number(data.index || 0)
  const existingIndex = Number(existing?.index || 0)
  const incomingTotal = Number(data.total || 0)
  const existingTotal = Number(existing?.total || 0)
  const nextTotal = Math.max(incomingTotal, existingTotal, 1)
  message.imageProgress = {
    b64Json: data.b64Json || data.b64_json || existing?.b64Json || existing?.b64_json || '',
    index: Math.max(incomingIndex, existingIndex),
    total: nextTotal,
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

function normalizeListSetting(value: any, fallback: string[] = []): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || '').trim()).filter(Boolean)
  }
  if (typeof value === 'string') {
    return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean)
  }
  return [...fallback]
}

function listSettingText(value: any): string {
  return normalizeListSetting(value).join(', ')
}

function webSearchSettingsPayload(settings: WebSearchSettings) {
  return {
    ...settings,
    providerOrder: normalizeListSetting(settings.providerOrder),
    searxngEngines: normalizeListSetting(settings.searxngEngines),
    trustedDomains: normalizeListSetting(settings.trustedDomains),
    blockedDomains: normalizeListSetting(settings.blockedDomains),
    providerStatus: undefined,
    provider_status: undefined
  }
}

function normalizeWebSearchSettings(data: any): WebSearchSettings {
  return {
    enabled: Boolean(data?.enabled),
    searxngBaseUrl: data?.searxngBaseUrl ?? data?.searxng_base_url ?? '',
    resultCount: Number(data?.resultCount ?? data?.result_count ?? 5),
    language: data?.language || 'all',
    safesearch: data?.safesearch || '1',
    timeoutSeconds: Number(data?.timeoutSeconds ?? data?.timeout_seconds ?? 20),
    fetchTimeoutSeconds: Number(data?.fetchTimeoutSeconds ?? data?.fetch_timeout_seconds ?? 20),
    maxToolCalls: Number(data?.maxToolCalls ?? data?.max_tool_calls ?? 4),
    fetchMaxChars: Number(data?.fetchMaxChars ?? data?.fetch_max_chars ?? 12000),
    providerOrder: normalizeListSetting(data?.providerOrder ?? data?.provider_order, ['searxng', 'bocha', 'sougou', 'jina']),
    searxngEngines: normalizeListSetting(data?.searxngEngines ?? data?.searxng_engines, ['bing', 'baidu']),
    candidateCount: Number(data?.candidateCount ?? data?.candidate_count ?? 20),
    fetchTopN: Number(data?.fetchTopN ?? data?.fetch_top_n ?? 5),
    chunkSize: Number(data?.chunkSize ?? data?.chunk_size ?? 900),
    chunkOverlap: Number(data?.chunkOverlap ?? data?.chunk_overlap ?? 120),
    maxEvidenceChunks: Number(data?.maxEvidenceChunks ?? data?.max_evidence_chunks ?? 8),
    rerankEnabled: Boolean(data?.rerankEnabled ?? data?.rerank_enabled ?? true),
    rerankerModel: data?.rerankerModel ?? data?.reranker_model ?? 'BAAI/bge-reranker-v2-m3',
    minRelevanceScore: Number(data?.minRelevanceScore ?? data?.min_relevance_score ?? 0.35),
    trustedDomains: normalizeListSetting(data?.trustedDomains ?? data?.trusted_domains),
    blockedDomains: normalizeListSetting(data?.blockedDomains ?? data?.blocked_domains),
    providerStatus: data?.providerStatus ?? data?.provider_status ?? {}
  }
}

function attachmentPreviewUrl(id: string) {
  return `/api/attachments/${id}/preview`
}

function attachmentImageSrc(attachment: Attachment) {
  return attachment.previewDataUrl || attachmentPreviewUrl(attachment.id)
}

function normalizeAttachment(item: Attachment): Attachment {
  return {
    ...item,
    mimeSniffed: item.mimeSniffed || (item as any).mime_sniffed || '',
    sizeBytes: Number(item.sizeBytes ?? (item as any).size_bytes ?? 0),
    parseStatus: item.parseStatus || (item as any).parse_status || 'success',
    parseError: item.parseError ?? (item as any).parse_error ?? undefined,
    contextTextTokens: Number(item.contextTextTokens ?? (item as any).context_text_tokens ?? 0),
    chunkCount: Number(item.chunkCount ?? (item as any).chunk_count ?? 0),
    embeddingStatus: item.embeddingStatus ?? (item as any).embedding_status ?? null,
    previewText: item.previewText ?? (item as any).preview_text ?? null,
    createdAt: item.createdAt || (item as any).created_at
  }
}

function normalizeConversationAttachment(item: ConversationAttachment): ConversationAttachment {
  const attachment = normalizeAttachment(item.attachment)
  return {
    ...item,
    conversationId: item.conversationId || item.conversation_id || currentId.value || 'new',
    attachment,
    selected: item.selected !== false,
    displayName: item.displayName ?? item.display_name ?? null,
    createdAt: item.createdAt || item.created_at || attachment.createdAt || new Date().toISOString(),
    updatedAt: item.updatedAt || item.updated_at || item.createdAt || item.created_at || new Date().toISOString()
  }
}

function fileTreeDisplayName(item: ConversationAttachment) {
  return item.displayName?.trim() || item.attachment.filename
}

function fileTreeAttachmentForPreview(item: ConversationAttachment): Attachment {
  return { ...item.attachment, filename: fileTreeDisplayName(item) }
}

function draftConversationAttachmentFromAttachment(attachment: Attachment): ConversationAttachment {
  const now = new Date().toISOString()
  return {
    id: `draft-${attachment.id}`,
    conversationId: 'new',
    attachment: normalizeAttachment(attachment),
    selected: true,
    displayName: null,
    createdAt: now,
    updatedAt: now
  }
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

function loadFileTreePinned() {
  fileTreePinned.value = window.localStorage.getItem(FILE_TREE_PIN_STORAGE_KEY) !== 'false'
}

function saveFileTreePinned() {
  window.localStorage.setItem(FILE_TREE_PIN_STORAGE_KEY, String(fileTreePinned.value))
}

function toggleFileTreePinned() {
  fileTreePinned.value = !fileTreePinned.value
  saveFileTreePinned()
}

function toggleFileTreeGroup(group: FileTreeGroup) {
  fileTreeGroupOpen.value = {
    ...fileTreeGroupOpen.value,
    [group]: !fileTreeGroupOpen.value[group]
  }
}

async function loadConversationAttachments(conversationId = currentId.value) {
  if (!conversationId) {
    conversationAttachments.value = []
    return
  }
  fileTreeLoading.value = true
  try {
    const rows = await apiFetch<ConversationAttachment[]>(`/conversations/${conversationId}/attachments`)
    if (currentId.value !== conversationId) return
    conversationAttachments.value = rows.map(normalizeConversationAttachment)
  } catch (err) {
    if (currentId.value === conversationId && err instanceof ApiError) error.value = err.message
  } finally {
    if (currentId.value === conversationId) fileTreeLoading.value = false
  }
}

async function attachFilesToConversation(conversationId: string, attachments: Attachment[], selected = true) {
  const attachmentIds = Array.from(new Set(attachments.map((item) => item.id).filter(Boolean)))
  if (!attachmentIds.length) return
  const rows = await apiFetch<ConversationAttachment[]>(`/conversations/${conversationId}/attachments`, {
    method: 'POST',
    body: JSON.stringify({ attachmentIds, selected })
  })
  if (currentId.value === conversationId) {
    conversationAttachments.value = rows.map(normalizeConversationAttachment)
  }
}

async function addUploadedAttachmentToFileTree(attachment: Attachment) {
  const normalized = normalizeAttachment(attachment)
  pendingAttachments.value = [
    ...pendingAttachments.value.filter((item) => item.id !== normalized.id),
    normalized
  ]
  if (currentId.value) {
    await attachFilesToConversation(currentId.value, [normalized], true)
    return
  }
  const draft = draftConversationAttachmentFromAttachment(normalized)
  draftConversationAttachments.value = [
    ...draftConversationAttachments.value.filter((item) => item.attachment.id !== normalized.id),
    draft
  ]
}

async function bindDraftFileTreeToConversation(conversationId: string) {
  const drafts = draftConversationAttachments.value
  if (!drafts.length) return
  await attachFilesToConversation(conversationId, drafts.map((item) => item.attachment), true)
  const selectedIds = new Set(drafts.filter((item) => item.selected).map((item) => item.attachment.id))
  const renameTargets = drafts.filter((item) => item.displayName)
  await Promise.all([
    ...conversationAttachments.value.map((item) => {
      const selected = selectedIds.has(item.attachment.id)
      if (item.selected === selected) return Promise.resolve()
      return setFileTreeAttachmentSelected(item, selected, { silent: true })
    }),
    ...renameTargets.map((draft) => {
      const attached = conversationAttachments.value.find((item) => item.attachment.id === draft.attachment.id)
      if (!attached || !draft.displayName) return Promise.resolve()
      return renameFileTreeAttachment(attached, draft.displayName, { silent: true })
    })
  ])
  draftConversationAttachments.value = []
}

async function setFileTreeAttachmentSelected(item: ConversationAttachment, selected: boolean, options: { silent?: boolean } = {}) {
  const updateLocal = (rows: ConversationAttachment[]) =>
    rows.map((row) => (row.attachment.id === item.attachment.id ? { ...row, selected } : row))
  if (!currentId.value || item.conversationId === 'new') {
    draftConversationAttachments.value = updateLocal(draftConversationAttachments.value)
    return
  }
  conversationAttachments.value = updateLocal(conversationAttachments.value)
  try {
    const updated = await apiFetch<ConversationAttachment>(`/conversations/${currentId.value}/attachments/${item.attachment.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ selected })
    })
    conversationAttachments.value = conversationAttachments.value.map((row) =>
      row.attachment.id === item.attachment.id ? normalizeConversationAttachment(updated) : row
    )
  } catch (err) {
    conversationAttachments.value = updateLocal(conversationAttachments.value).map((row) =>
      row.attachment.id === item.attachment.id ? { ...row, selected: item.selected } : row
    )
    if (!options.silent) error.value = err instanceof Error ? err.message : '更新文件勾选状态失败'
  }
}

function handleFileTreeSelectionChange(item: ConversationAttachment, event: Event) {
  const input = event.target as HTMLInputElement | null
  void setFileTreeAttachmentSelected(item, input?.checked === true)
}

function openAttachmentRename(item: ConversationAttachment) {
  attachmentRenameTarget.value = item
  attachmentRenameDraft.value = fileTreeDisplayName(item)
  attachmentRenameError.value = ''
  attachmentRenameOpen.value = true
  void nextTick(() => {
    attachmentRenameInput.value?.focus()
    attachmentRenameInput.value?.select()
  })
}

function closeAttachmentRename() {
  if (attachmentRenameSaving.value) return
  attachmentRenameOpen.value = false
  attachmentRenameTarget.value = null
  attachmentRenameDraft.value = ''
  attachmentRenameError.value = ''
}

async function renameFileTreeAttachment(item: ConversationAttachment, displayName: string, options: { silent?: boolean } = {}) {
  const name = displayName.trim()
  if (!name) {
    if (!options.silent) error.value = '文件名不能为空'
    return
  }
  const updateLocal = (rows: ConversationAttachment[]) =>
    rows.map((row) => (row.attachment.id === item.attachment.id ? { ...row, displayName: name } : row))
  if (!currentId.value || item.conversationId === 'new') {
    draftConversationAttachments.value = updateLocal(draftConversationAttachments.value)
    return
  }
  const updated = await apiFetch<ConversationAttachment>(`/conversations/${currentId.value}/attachments/${item.attachment.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ displayName: name })
  })
  conversationAttachments.value = conversationAttachments.value.map((row) =>
    row.attachment.id === item.attachment.id ? normalizeConversationAttachment(updated) : row
  )
}

async function saveAttachmentRename() {
  const target = attachmentRenameTarget.value
  if (!target || attachmentRenameSaving.value) return
  const name = attachmentRenameDraft.value.trim()
  if (!name) {
    attachmentRenameError.value = '文件名不能为空'
    return
  }
  attachmentRenameSaving.value = true
  try {
    await renameFileTreeAttachment(target, name)
    attachmentRenameOpen.value = false
    attachmentRenameTarget.value = null
    attachmentRenameDraft.value = ''
    attachmentRenameError.value = ''
  } catch (err) {
    attachmentRenameError.value = err instanceof Error ? err.message : '重命名失败'
  } finally {
    attachmentRenameSaving.value = false
  }
}

function requestRemoveFileTreeAttachment(item: ConversationAttachment) {
  attachmentDeleteTarget.value = item
  attachmentDeleteOpen.value = true
}

function closeAttachmentDelete() {
  if (attachmentDeleting.value) return
  attachmentDeleteOpen.value = false
  attachmentDeleteTarget.value = null
}

async function confirmRemoveFileTreeAttachment() {
  const target = attachmentDeleteTarget.value
  if (!target || attachmentDeleting.value) return
  attachmentDeleting.value = true
  try {
    if (currentId.value && target.conversationId !== 'new') {
      await apiFetch(`/conversations/${currentId.value}/attachments/${target.attachment.id}`, { method: 'DELETE' })
      conversationAttachments.value = conversationAttachments.value.filter((item) => item.attachment.id !== target.attachment.id)
    } else {
      draftConversationAttachments.value = draftConversationAttachments.value.filter((item) => item.attachment.id !== target.attachment.id)
    }
    pendingAttachments.value = pendingAttachments.value.filter((item) => item.id !== target.attachment.id)
    attachmentDeleteOpen.value = false
    attachmentDeleteTarget.value = null
  } catch (err) {
    error.value = err instanceof Error ? err.message : '移除文件失败'
  } finally {
    attachmentDeleting.value = false
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
      '--welcome-font-size': `${welcomeFontSize.value}px`,
      '--chat-footer-height': `${chatFooterHeight.value}px`
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

function setNewApiKeyEndpoint(endpointId: string | number) {
  newApiKey.value.endpointId = String(endpointId)
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

function normalizeConversation(conversation: Conversation): Conversation {
  const rawMode = conversation.webSearchMode ?? conversation.web_search_mode
  const mode: WebSearchMode = rawMode === 'deep' || rawMode === 'fast' || rawMode === 'auto' ? rawMode : 'auto'
  const rounds = Number(conversation.webSearchMaxRounds ?? conversation.web_search_max_rounds ?? 3)
  return {
    ...conversation,
    webSearchEnabled: Boolean(conversation.webSearchEnabled ?? conversation.web_search_enabled),
    webSearchMode: mode,
    web_search_mode: mode,
    webSearchMaxRounds: Number.isFinite(rounds) ? Math.min(5, Math.max(1, Math.round(rounds))) : 3,
    web_search_max_rounds: Number.isFinite(rounds) ? Math.min(5, Math.max(1, Math.round(rounds))) : 3
  }
}

function activeConversationWebSearchEnabled() {
  return Boolean(currentConversation.value?.webSearchEnabled ?? false)
}

function syncWebSearchToggleFromConversation() {
  if (!currentId.value) {
    webSearchEnabled.value = false
    webSearchMode.value = 'auto'
    webSearchMaxRounds.value = 3
    return
  }
  const conversation = currentConversation.value ? normalizeConversation(currentConversation.value) : null
  webSearchEnabled.value = Boolean(conversation?.webSearchEnabled ?? false)
  webSearchMode.value = conversation?.webSearchMode || 'auto'
  webSearchMaxRounds.value = conversation?.webSearchMaxRounds || 3
}

function sortConversationsByUpdatedAt(items: Conversation[]) {
  return items.map(normalizeConversation).sort((a, b) => conversationUpdatedAtMs(b) - conversationUpdatedAtMs(a))
}

function lastVisibleConversation() {
  return conversations.value[conversations.value.length - 1]
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

function endpointDraftFor(endpoint: ModelEndpoint) {
  if (!endpointDrafts.value[endpoint.id]) {
    endpointDrafts.value[endpoint.id] = { name: endpoint.name, baseUrl: endpoint.baseUrl }
  }
  return endpointDrafts.value[endpoint.id]
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

async function loadApiSettings(options: { includeGroupKeys?: boolean } = {}) {
  const includeGroupKeys = options.includeGroupKeys ?? true
  settingsLoading.value = true
  try {
    const [keys, groups, endpoints] = await Promise.all([
      apiFetch<ApiKeyEntry[]>('/api-keys'),
      apiFetch<ApiKeyGroup[]>('/api-key-groups'),
      apiFetch<ModelEndpoint[]>('/model-endpoints')
    ])
    apiKeys.value = keys
    apiKeyGroups.value = groups
    modelEndpoints.value = endpoints
    if (!selectedGroupId.value || !groups.some((group) => group.id === selectedGroupId.value)) {
      selectedGroupId.value = groups[0]?.id || ''
    }
    const modelPurpose: GroupPurpose = selectedModelIsImageGeneration() ? 'image' : 'chat'
    const defaultGroupId =
      groups.find((group) => group.purpose === modelPurpose)?.id ||
      groups.find((group) => group.purpose === 'chat')?.id ||
      groups[0]?.id ||
      ''
    if (!newApiKey.value.groupId || !groups.some((group) => group.id === newApiKey.value.groupId)) {
      newApiKey.value.groupId = defaultGroupId
    }
    newApiKey.value.endpointId = endpoints.find((endpoint) => endpoint.isActive)?.id || endpoints[0]?.id || ''
    keyDrafts.value = Object.fromEntries(keys.map((key) => [key.id, { name: key.name, groupId: key.groupId || defaultGroupId }]))
    endpointDrafts.value = Object.fromEntries(endpoints.map((endpoint) => [endpoint.id, { name: endpoint.name, baseUrl: endpoint.baseUrl }]))
    groupDrafts.value = Object.fromEntries(
      groups.map((group) => [group.id, { name: group.name, description: group.description || '', purpose: group.purpose || 'none' }])
    )
    if (includeGroupKeys && auth.user?.role === 'admin' && selectedGroupId.value) await loadGroupKeys(selectedGroupId.value)
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '加载 API 管理失败'
  } finally {
    settingsLoading.value = false
  }
}

async function refreshApiSettings() {
  await loadApiSettings()
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

async function loadWebSearchStatus() {
  try {
    webSearchStatus.value = await apiFetch<WebSearchStatus>('/settings/web-search/status')
  } catch {
    webSearchStatus.value = { enabled: false, configured: false }
    webSearchEnabled.value = false
  }
}

async function loadWebSearchSettings() {
  if (auth.user?.role !== 'admin') return
  webSearchSettingsLoading.value = true
  try {
    webSearchSettings.value = normalizeWebSearchSettings(await apiFetch('/admin/settings/web-search'))
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '加载联网搜索设置失败'
  } finally {
    webSearchSettingsLoading.value = false
  }
}

async function saveWebSearchSettings() {
  resetSettingsMessages()
  webSearchSettingsSaving.value = true
  try {
    webSearchSettings.value = normalizeWebSearchSettings(await apiFetch('/admin/settings/web-search', {
      method: 'PATCH',
      body: JSON.stringify(webSearchSettingsPayload(webSearchSettings.value))
    }))
    settingsNotice.value = '联网搜索设置已保存'
    await loadWebSearchStatus()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '保存联网搜索设置失败'
  } finally {
    webSearchSettingsSaving.value = false
  }
}

async function testWebSearchSettings() {
  resetSettingsMessages()
  webSearchTesting.value = true
  webSearchTestResults.value = []
  try {
    const result = await apiFetch<{ results: Array<{ title: string; url: string; snippet?: string; evidence?: string; provider?: string; confidence?: number; rerankStatus?: string; rerank_status?: string }> }>('/admin/settings/web-search/test', {
      method: 'POST',
      body: JSON.stringify({ query: webSearchTestQuery.value.trim() || 'OpenAI news' })
    })
    webSearchTestResults.value = result.results || []
    settingsNotice.value = `测试完成，返回 ${webSearchTestResults.value.length} 条结果`
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '联网搜索测试失败'
  } finally {
    webSearchTesting.value = false
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
  const targetTab = (tab === 'groups' || tab === 'web') && auth.user?.role !== 'admin' ? 'appearance' : tab
  settingsMenuOpen.value = false
  settingsOpen.value = true
  settingsTab.value = targetTab
  profileUsername.value = auth.user?.username || ''
  resetSettingsMessages()
  if (targetTab === 'api' || targetTab === 'groups') await loadApiSettings()
  if (targetTab === 'image') await loadImageSettings()
  if (targetTab === 'web') await loadWebSearchSettings()
}

function resetAccessSwitchMessages() {
  accessSwitchNotice.value = ''
  accessSwitchError.value = ''
}

async function openAccessSwitch() {
  settingsMenuOpen.value = false
  accessSwitchOpen.value = true
  await refreshAccessSwitch()
}

async function refreshAccessSwitch() {
  resetSettingsMessages()
  resetAccessSwitchMessages()
  await loadApiSettings({ includeGroupKeys: false })
  if (settingsError.value) accessSwitchError.value = settingsError.value
}

function closeAccessSwitch() {
  if (accessSwitchLoading.value) return
  accessSwitchOpen.value = false
  accessSwitchEndpointId.value = null
  accessSwitchKeyId.value = null
  resetAccessSwitchMessages()
}

async function openApiSettingsFromSwitcher() {
  if (accessSwitchLoading.value) return
  accessSwitchOpen.value = false
  await openSettings('api')
}

async function switchAccessEndpoint(endpoint: ModelEndpoint) {
  if (endpoint.isActive || accessSwitchLoading.value) return
  resetAccessSwitchMessages()
  accessSwitchLoading.value = true
  accessSwitchEndpointId.value = endpoint.id
  try {
    await apiFetch<ModelEndpoint>(`/model-endpoints/${endpoint.id}/activate`, { method: 'POST' })
    resetSettingsMessages()
    await loadApiSettings({ includeGroupKeys: false })
    if (settingsError.value) {
      accessSwitchError.value = settingsError.value
    } else {
      accessSwitchNotice.value = `已切换到 ${endpoint.name}，API Key 列表已按该 BaseURL 刷新`
    }
    await loadModels()
  } catch (err) {
    accessSwitchError.value = err instanceof Error ? err.message : '切换 BaseURL 失败'
  } finally {
    accessSwitchEndpointId.value = null
    accessSwitchLoading.value = false
  }
}

async function switchAccessApiKey(key: ApiKeyEntry) {
  if (key.isActive || accessSwitchLoading.value) return
  resetAccessSwitchMessages()
  accessSwitchLoading.value = true
  accessSwitchKeyId.value = key.id
  try {
    await apiFetch<ApiKeyEntry>(`/api-keys/${key.id}/activate`, { method: 'POST' })
    resetSettingsMessages()
    await loadApiSettings({ includeGroupKeys: false })
    if (settingsError.value) {
      accessSwitchError.value = settingsError.value
    } else {
      accessSwitchNotice.value = `已在当前 BaseURL 下切换到 ${key.name}`
    }
    await loadModels()
  } catch (err) {
    accessSwitchError.value = err instanceof Error ? err.message : '切换 API Key 失败'
  } finally {
    accessSwitchKeyId.value = null
    accessSwitchLoading.value = false
  }
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
  if ((tab === 'groups' || tab === 'web') && auth.user?.role !== 'admin') return
  settingsTab.value = tab
  resetSettingsMessages()
  if ((tab === 'api' || tab === 'groups') && !apiKeys.value.length && !apiKeyGroups.value.length) await loadApiSettings()
  if (tab === 'groups' && apiKeyGroups.value.length) await loadGroupKeys(selectedGroupId.value)
  if (tab === 'image') await loadImageSettings()
  if (tab === 'web') await loadWebSearchSettings()
}

async function createModelEndpoint() {
  resetSettingsMessages()
  try {
    await apiFetch<ModelEndpoint>('/model-endpoints', {
      method: 'POST',
      body: JSON.stringify({
        name: newEndpoint.value.name,
        baseUrl: newEndpoint.value.baseUrl,
        makeActive: newEndpoint.value.makeActive
      })
    })
    newEndpoint.value = { name: 'Default BaseURL', baseUrl: '', makeActive: true }
    settingsNotice.value = 'BaseURL 已添加'
    await loadApiSettings()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '添加 BaseURL 失败'
  }
}

async function saveModelEndpoint(endpoint: ModelEndpoint) {
  resetSettingsMessages()
  const draft = endpointDraftFor(endpoint)
  try {
    await apiFetch<ModelEndpoint>(`/model-endpoints/${endpoint.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: draft.name, baseUrl: draft.baseUrl })
    })
    settingsNotice.value = 'BaseURL 已保存'
    await loadApiSettings()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '保存 BaseURL 失败'
  }
}

async function activateModelEndpoint(endpoint: ModelEndpoint) {
  resetSettingsMessages()
  try {
    await apiFetch<ModelEndpoint>(`/model-endpoints/${endpoint.id}/activate`, { method: 'POST' })
    settingsNotice.value = '当前 BaseURL 已切换'
    await loadApiSettings()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '切换 BaseURL 失败'
  }
}

async function deleteModelEndpoint(endpoint: ModelEndpoint) {
  resetSettingsMessages()
  if (!window.confirm(`删除 BaseURL「${endpoint.name}」？对应的聊天 key 和生图 key 也会删除。`)) return
  try {
    await apiFetch(`/model-endpoints/${endpoint.id}`, { method: 'DELETE' })
    settingsNotice.value = 'BaseURL 已删除'
    await loadApiSettings()
    await loadModels()
  } catch (err) {
    settingsError.value = err instanceof Error ? err.message : '删除 BaseURL 失败'
  }
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
        endpointId: activeModelEndpoint.value?.id || newApiKey.value.endpointId || null,
        makeActive: newApiKey.value.makeActive
      })
    })
    newApiKey.value = {
      name: '默认密钥',
      apiKey: '',
      groupId: defaultChatGroup.value?.id || '',
      endpointId: activeModelEndpoint.value?.id || '',
      makeActive: true
    }
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
    settingsNotice.value = '已切换该分组当前使用密钥'
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

function scrollBottomDistance(scroller: HTMLElement) {
  return Math.max(0, scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight)
}

function isNearBottom(threshold = 80): boolean {
  const scroller = messageScroller.value
  if (!scroller) return true
  return scrollBottomDistance(scroller) < threshold
}

function isAtScrollTop(scroller: HTMLElement) {
  return scroller.scrollTop <= QUESTION_NAV_EDGE_THRESHOLD
}

function isAtScrollBottom(scroller: HTMLElement, threshold = QUESTION_NAV_EDGE_THRESHOLD) {
  return scrollBottomDistance(scroller) <= threshold
}

function questionNavBottomThreshold(scroller: HTMLElement) {
  return Math.max(
    QUESTION_NAV_EDGE_THRESHOLD,
    Math.min(QUESTION_NAV_BOTTOM_THRESHOLD_MAX, scroller.clientHeight * QUESTION_NAV_BOTTOM_THRESHOLD_RATIO)
  )
}

function isNearQuestionNavBottom(scroller: HTMLElement) {
  return isAtScrollBottom(scroller, questionNavBottomThreshold(scroller))
}

function isProgrammaticScroll() {
  return Date.now() < programmaticScrollUntil
}

function markProgrammaticScroll(duration = 250) {
  programmaticScrollUntil = Date.now() + duration
}

function isUserScrollSettling() {
  return Date.now() < userScrollSettlingUntil
}

function markUserScrollSettling(duration = USER_SCROLL_SETTLE_MS) {
  userScrollSettlingUntil = Date.now() + duration
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

function clearQuestionNavLock() {
  questionNavLockUntil = 0
  questionNavLockedMessageId = null
}

function pauseAutoScrollFromUserScroll() {
  cancelPendingScroll()
  markUserScrollSettling()
  if (!streaming.value) return
  userHasScrolledUp = true
  showScrollToBottom.value = messages.value.length > 0
}

function handleScrollerWheel(event: WheelEvent) {
  clearQuestionNavLock()
  if (event.deltaY < 0) pauseAutoScrollFromUserScroll()
}

function handleScrollerTouchStart() {
  clearQuestionNavLock()
  pauseAutoScrollFromUserScroll()
}

function updateQuestionNavOverflow() {
  const scroller = questionNavScroll.value
  if (!scroller) {
    questionNavHasBefore.value = false
    questionNavHasAfter.value = false
    return
  }
  questionNavHasBefore.value = scroller.scrollTop > 3
  questionNavHasAfter.value = scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight - 3
}

function updateQuestionNavLayoutMode() {
  const scroller = messageScroller.value
  const items = userQuestionNavItems.value
  if (!scroller || items.length < 2) {
    questionNavShortConversation.value = false
    return
  }
  const scrollRange = Math.max(0, scroller.scrollHeight - scroller.clientHeight)
  const shortRange = Math.min(QUESTION_NAV_SHORT_SCROLL_RANGE, scroller.clientHeight * 0.35)
  questionNavShortConversation.value = scrollRange <= shortRange
}

function lockActiveQuestion(messageId: string, lockDuration = 0) {
  activeQuestionMessageId.value = messageId
  questionNavLockedMessageId = messageId
  questionNavLockUntil = lockDuration > 0 ? Date.now() + lockDuration : 0
  void syncActiveQuestionNavRow()
}

function setActiveQuestionToLast(lockDuration = 0) {
  const items = userQuestionNavItems.value
  const lastQuestion = items[items.length - 1]
  activeQuestionMessageId.value = lastQuestion?.id || null
  if (lastQuestion && lockDuration > 0) {
    lockActiveQuestion(lastQuestion.id, lockDuration)
    return
  }
  void syncActiveQuestionNavRow()
}

function isQuestionNavLocked() {
  if (!questionNavLockedMessageId) {
    return false
  }
  if (questionNavLockUntil > 0 && Date.now() >= questionNavLockUntil) {
    clearQuestionNavLock()
    return false
  }
  if (!userQuestionNavItems.value.some((item) => item.id === questionNavLockedMessageId)) {
    clearQuestionNavLock()
    return false
  }
  if (activeQuestionMessageId.value !== questionNavLockedMessageId) {
    activeQuestionMessageId.value = questionNavLockedMessageId
    void syncActiveQuestionNavRow()
  }
  return true
}

function messageTopInScroller(node: HTMLElement, scroller: HTMLElement) {
  return node.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
}

function lastQuestionIsVisible(scroller: HTMLElement, items: Array<{ id: string }>) {
  const lastQuestion = items[items.length - 1]
  if (!lastQuestion) return false
  const node = scroller.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(lastQuestion.id)}"]`)
  if (!node) return false
  const messageTop = messageTopInScroller(node, scroller)
  const messageBottom = messageTop + node.offsetHeight
  const viewTop = scroller.scrollTop
  const viewBottom = viewTop + scroller.clientHeight
  const activationInset = Math.min(QUESTION_NAV_LAST_VISIBLE_INSET_MAX, scroller.clientHeight * QUESTION_NAV_LAST_VISIBLE_INSET_RATIO)
  return messageBottom >= viewTop + QUESTION_NAV_EDGE_THRESHOLD && messageTop <= viewBottom - activationInset
}

async function syncActiveQuestionNavRow() {
  await nextTick()
  const scroller = questionNavScroll.value
  const activeId = activeQuestionMessageId.value
  if (!scroller || !activeId) {
    updateQuestionNavOverflow()
    return
  }
  const row = scroller.querySelector<HTMLElement>(`[data-question-nav-id="${CSS.escape(activeId)}"]`)
  if (!row) {
    updateQuestionNavOverflow()
    return
  }
  const rowTop = row.offsetTop
  const rowBottom = rowTop + row.offsetHeight
  const viewTop = scroller.scrollTop
  const viewBottom = viewTop + scroller.clientHeight
  if (rowTop < viewTop + 8) {
    scroller.scrollTo({ top: Math.max(rowTop - 8, 0), behavior: questionNavExpanded.value ? 'smooth' : 'auto' })
  } else if (rowBottom > viewBottom - 8) {
    scroller.scrollTo({ top: rowBottom - scroller.clientHeight + 8, behavior: questionNavExpanded.value ? 'smooth' : 'auto' })
  } else {
    updateQuestionNavOverflow()
  }
}

function refreshActiveQuestionFromScroll() {
  const scroller = messageScroller.value
  const items = userQuestionNavItems.value
  updateQuestionNavLayoutMode()
  if (!scroller || !items.length) {
    activeQuestionMessageId.value = null
    return
  }
  if (isQuestionNavLocked()) return
  if (isAtScrollTop(scroller)) {
    activeQuestionMessageId.value = items[0].id
    void syncActiveQuestionNavRow()
    return
  }
  if (isAtScrollBottom(scroller) || isNearQuestionNavBottom(scroller) || lastQuestionIsVisible(scroller, items)) {
    setActiveQuestionToLast()
    return
  }
  const anchorTop = scroller.scrollTop + scroller.clientHeight * 0.32
  let bestId = items[0].id
  for (const item of items) {
    const node = scroller.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(item.id)}"]`)
    if (!node) continue
    const messageTop = messageTopInScroller(node, scroller)
    if (messageTop <= anchorTop) {
      bestId = item.id
    }
  }
  activeQuestionMessageId.value = bestId
  void syncActiveQuestionNavRow()
}

function handleScrollerScroll() {
  const programmatic = isProgrammaticScroll()
  if (!programmatic) {
    cancelPendingScroll()
    markUserScrollSettling()
    clearQuestionNavLock()
  }
  const nearBottom = isNearBottom(80)
  refreshActiveQuestionFromScroll()
  const awayFromBottom = !nearBottom
  showScrollToBottom.value = messages.value.length > 0 && awayFromBottom
  if (!streaming.value || programmatic) return
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
  markProgrammaticScroll(behavior === 'smooth' ? 1000 : 300)
  scroller.scrollTo({ top: scroller.scrollHeight, behavior })
  setActiveQuestionToLast(behavior === 'smooth' ? 900 : 300)
  showScrollToBottom.value = false
}

async function scrollLoadedConversationToBottom(loadId: number) {
  await waitForMessageLayout()
  if (loadId !== activeConversationLoad) return
  await scrollMessagesToBottom('auto')
}

async function returnToBottom() {
  userHasScrolledUp = false
  userScrollSettlingUntil = 0
  await scrollMessagesToBottom('smooth')
}

async function jumpToQuestion(messageId: string) {
  questionNavExpanded.value = true
  lockActiveQuestion(messageId)
  await scrollToMessage(messageId, 'start')
}

function scheduleScrollToBottom(force = false) {
  // Skip auto-scroll when the user has intentionally scrolled up.
  if (userHasScrolledUp) return
  if (isUserScrollSettling()) return
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
  const previousId = existing.id
  if (existing.role === 'assistant' && existing.status === 'streaming') rememberStreamProgress(existing)
  const stableClientKey = existing.clientKey || preserve.clientKey || incoming.clientKey || existing.id
  Object.assign(existing, incoming, preserve, { clientKey: stableClientKey })
  migrateStreamProgress(previousId, existing.id)
  if (existing.role === 'assistant' && existing.status === 'streaming') {
    restoreCachedStreamProgress(existing)
    rememberStreamProgress(existing)
  } else {
    forgetStreamProgress(previousId)
    forgetStreamProgress(existing.id)
  }
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
  forgetStreamProgress(message.id)
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
    parentMessageId: data.user_message_id || data.userMessageId || data.parent_message_id || data.parentMessageId,
    webSearchSources: data.web_search_sources || data.webSearchSources || [],
    web_search_sources: data.web_search_sources || data.webSearchSources || []
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
    if (data.web_search_sources || data.webSearchSources) {
      message.webSearchSources = data.web_search_sources || data.webSearchSources || []
      message.web_search_sources = message.webSearchSources
    }
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
  if (event === 'web_search_status') {
    if (!message) {
      void refreshMessageById(messageId)
      return
    }
    message.status = 'streaming'
    message.progressDetail = webSearchProgressDetail(data)
    message.progress_detail = message.progressDetail
    message.progressPhase = data.phase || 'web_search'
    message.progress_phase = message.progressPhase
    applyRuntimeProgress(message, data)
    syncActiveRequestState()
    scheduleScrollToBottom()
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
        void addUploadedAttachmentToFileTree(data.attachment)
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
      message.status = data.status || (event === 'message_completed' ? 'completed' : 'failed_no_output')
      if (typeof data.content === 'string') message.content = data.content
      if (data.web_search_sources || data.webSearchSources) {
        message.webSearchSources = data.web_search_sources || data.webSearchSources || []
        message.web_search_sources = message.webSearchSources
      }
      applyRuntimeProgress(message, { ...data, status: message.status })
      if (message.content.trim()) freezeFirstTokenSeconds(message, { ...data, status: message.status })
      if (event === 'message_failed' && !message.content.trim() && typeof data.message === 'string' && data.message.trim()) {
        message.content = data.message.trim()
      }
      if (message.status !== 'streaming') {
        forgetStreamProgress(message.id)
        clearRuntimeProgress(message)
        message.imageProgress = undefined
      }
    } else {
      forgetStreamProgress(messageId)
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
    rememberStreamingProgressForMessages(messages.value)
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
    'web_search_status',
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

async function toggleWebSearch() {
  if (currentConversationStreaming.value) return
  if (!webSearchEnabled.value && !webSearchAvailable.value) return
  if (!webSearchEnabled.value) {
    webSearchModeDraft.value = webSearchMode.value || 'auto'
    webSearchRoundDraft.value = webSearchMaxRounds.value || 3
    webSearchModeDialogOpen.value = true
    return
  }
  await applyWebSearchChoice(false, webSearchMode.value, webSearchMaxRounds.value)
}

async function applyWebSearchChoice(enabled: boolean, mode: WebSearchMode, rounds: number) {
  const nextValue = enabled
  const nextMode: WebSearchMode = mode === 'deep' || mode === 'fast' ? mode : 'auto'
  const nextRounds = Math.min(5, Math.max(1, Math.round(Number(rounds) || 3)))
  const previousEnabled = webSearchEnabled.value
  const previousMode = webSearchMode.value
  const previousRounds = webSearchMaxRounds.value
  webSearchEnabled.value = nextValue
  webSearchMode.value = nextMode
  webSearchMaxRounds.value = nextRounds
  webSearchModeDialogOpen.value = false
  const conversationId = currentId.value
  if (!conversationId) return
  conversations.value = conversations.value.map((item) =>
    item.id === conversationId
      ? { ...item, webSearchEnabled: nextValue, webSearchMode: nextMode, webSearchMaxRounds: nextRounds }
      : item
  )
  try {
    const updated = normalizeConversation(await apiFetch<Conversation>(`/conversations/${conversationId}`, {
      method: 'PATCH',
      body: JSON.stringify({ webSearchEnabled: nextValue, webSearchMode: nextMode, webSearchMaxRounds: nextRounds })
    }))
    conversations.value = sortConversationsByUpdatedAt(conversations.value.map((item) => (item.id === updated.id ? { ...item, ...updated } : item)))
  } catch (err) {
    webSearchEnabled.value = previousEnabled
    webSearchMode.value = previousMode
    webSearchMaxRounds.value = previousRounds
    conversations.value = conversations.value.map((item) =>
      item.id === conversationId
        ? { ...item, webSearchEnabled: previousEnabled, webSearchMode: previousMode, webSearchMaxRounds: previousRounds }
        : item
    )
    error.value = err instanceof Error ? err.message : '联网搜索设置保存失败'
  }
}

function newChat() {
  activeConversationLoad++
  rememberStreamingProgressForMessages(messages.value)
  cancelPendingScroll()
  cancelPendingStreamFlush()
  clearAllImageFinalizationTimers()
  stopImagePolling()
  stopConversationEvents()
  currentId.value = null
  window.localStorage.removeItem(CURRENT_CONVERSATION_STORAGE_KEY)
  messages.value = []
  messagesLoading.value = false
  conversationAttachments.value = []
  draftConversationAttachments.value = []
  pendingAttachments.value = []
  fileTreeLoading.value = false
  showScrollToBottom.value = false
  userHasScrolledUp = false
  error.value = ''
  streaming.value = false
  webSearchEnabled.value = false
  webSearchMode.value = 'auto'
  webSearchMaxRounds.value = 3
  webSearchModeDialogOpen.value = false
}

async function loadConversations() {
  conversationsLoading.value = conversations.value.length === 0
  try {
    conversations.value = sortConversationsByUpdatedAt(await apiFetch<Conversation[]>('/conversations'))
    if (currentId.value) syncWebSearchToggleFromConversation()
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
  rememberStreamingProgressForMessages(messages.value)
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
    syncWebSearchToggleFromConversation()
    messages.value = loadedMessages
    await loadConversationAttachments(id)
    if (loadId !== activeConversationLoad) return
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

async function scrollToMessage(messageId: string, block: ScrollLogicalPosition = 'center') {
  await waitForMessageLayout()
  const target = Array.from(document.querySelectorAll<HTMLElement>('[data-message-id]')).find(
    (item) => item.dataset.messageId === messageId
  )
  if (!target) {
    await scrollMessagesToBottom('auto')
    return
  }
  userHasScrolledUp = true
  markProgrammaticScroll(block === 'start' ? 1600 : 1000)
  target.scrollIntoView({ behavior: 'smooth', block })
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
      const nextConversation = lastVisibleConversation()
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
    await addUploadedAttachmentToFileTree(attachment)
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
  if ((!input.value.trim() && !selectedFileTreeAttachments.value.length) || currentConversationStreaming.value) return
  const isImageGeneration = selectedModelIsImageGeneration()
  if (isImageGeneration && selectedFileTreeAttachments.value.length) {
    error.value = '图像生成暂不支持同时上传附件。'
    return
  }
  if (selectedFileTreeAttachments.value.some((item) => isImageAttachment(item.attachment)) && !selectedModelSupportsVision()) {
    error.value = '当前模型不支持图片理解，请切换到支持视觉的模型。'
    return
  }
  cancelPendingScroll()
  userHasScrolledUp = false
  error.value = ''
  const userText = input.value
  const selectedAttachments = selectedFileTreeAttachments.value.map((item) => fileTreeAttachmentForPreview(item))
  const selectedIds = selectedFileTreeAttachmentIds.value
  const pendingIds = new Set(pendingAttachments.value.map((item) => item.id))
  const attachmentIds = selectedIds.filter((id) => pendingIds.has(id))
  const referencedAttachmentIds = selectedIds
  let targetConversationId = currentId.value
  input.value = ''
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
    attachments: selectedAttachments,
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
  rememberStreamProgress(assistantDraft)
  syncActiveRequestState()
  await scrollMessagesToBottom('auto')
  try {
    if (!targetConversationId && draftConversationAttachments.value.length) {
      const created = await apiFetch<Conversation>('/conversations', {
        method: 'POST',
        body: JSON.stringify({
          title: (userText.trim() || '新对话').slice(0, 30),
          webSearchEnabled: webSearchEnabled.value,
          webSearchMode: webSearchMode.value,
          webSearchMaxRounds: webSearchMaxRounds.value
        })
      })
      targetConversationId = created.id
      currentId.value = created.id
      window.localStorage.setItem(CURRENT_CONVERSATION_STORAGE_KEY, created.id)
      await bindDraftFileTreeToConversation(created.id)
    }
    const path = targetConversationId ? `/conversations/${targetConversationId}/messages` : '/conversations/new/messages'
    const result = await apiFetch<SendMessageResponse>(path, {
      method: 'POST',
      body: JSON.stringify({
        content: userText,
        model: selectedModel.value,
        attachmentIds,
        referencedAttachmentIds,
        webSearchEnabled: webSearchEnabled.value,
        webSearchMode: webSearchMode.value,
        webSearchMaxRounds: webSearchMaxRounds.value,
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
    syncWebSearchToggleFromConversation()
    await loadContextStats()
    pendingAttachments.value = pendingAttachments.value.filter((item) => !selectedIds.includes(item.id))
  } catch (err) {
    cancelPendingScroll()
    const apiErr = err instanceof ApiError ? err : null
    const message = userFacingSendError(err)
    const assistant = findMessage(draftAssistantId)
    if (assistant) {
      forgetStreamProgress(assistant.id)
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

function updateChatFooterHeight() {
  const footer = chatFooter.value
  const scroller = messageScroller.value
  const previousBottomDistance = scroller ? scrollBottomDistance(scroller) : 0
  const nextHeight = footer ? Math.ceil(footer.getBoundingClientRect().height) : 0
  if (Math.abs(chatFooterHeight.value - nextHeight) <= 1) return
  const wasPinnedToBottom = previousBottomDistance <= 4 && !userHasScrolledUp
  chatFooterHeight.value = nextHeight
  if (wasPinnedToBottom && hasConversationFrame.value) scheduleScrollToBottom(true)
}

function observeChatFooter() {
  chatFooterResizeObserver?.disconnect()
  chatFooterResizeObserver = null
  const footer = chatFooter.value
  if (!footer || typeof ResizeObserver === 'undefined') {
    updateChatFooterHeight()
    return
  }
  chatFooterResizeObserver = new ResizeObserver(() => {
    updateChatFooterHeight()
  })
  chatFooterResizeObserver.observe(footer)
  updateChatFooterHeight()
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
  updateChatFooterHeight()
}

function scheduleComposerResize() {
  if (composerResizeFrame !== null) {
    window.cancelAnimationFrame(composerResizeFrame)
  }
  composerResizeFrame = window.requestAnimationFrame(() => {
    composerResizeFrame = null
    resizeComposerInput()
    void waitForMessageLayout().then(() => {
      updateChatFooterHeight()
      updateQuestionNavLayoutMode()
      refreshActiveQuestionFromScroll()
      updateQuestionNavOverflow()
    })
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

watch(
  () => [composerAttachmentItems.value.length, uploadingAttachmentNames.value.length] as const,
  async () => {
    await nextTick()
    scheduleComposerResize()
  }
)

watch(
  () => userQuestionNavItems.value.map((item) => item.id).join('|'),
  async () => {
    if (!userQuestionNavItems.value.length) {
      activeQuestionMessageId.value = null
      questionNavExpanded.value = false
      questionNavHasBefore.value = false
      questionNavHasAfter.value = false
      questionNavShortConversation.value = false
      clearQuestionNavLock()
      return
    }
    await waitForMessageLayout()
    updateQuestionNavLayoutMode()
    refreshActiveQuestionFromScroll()
    await syncActiveQuestionNavRow()
    updateQuestionNavOverflow()
  }
)

watch(questionNavExpanded, async () => {
  await syncActiveQuestionNavRow()
  updateQuestionNavOverflow()
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
  loadFileTreePinned()
  await Promise.all([loadModels(), loadConversations(), loadWebSearchStatus()])
  const storedConversationId = window.localStorage.getItem(CURRENT_CONVERSATION_STORAGE_KEY)
  if (storedConversationId && conversations.value.some((conversation) => conversation.id === storedConversationId)) {
    await openConversation(storedConversationId)
  } else {
    const fallbackConversation = lastVisibleConversation()
    if (fallbackConversation) await openConversation(fallbackConversation.id)
  }
  await nextTick()
  observeChatFooter()
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
  chatFooterResizeObserver?.disconnect()
  chatFooterResizeObserver = null
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
                <button type="button" @click="openSettings('appearance')">
                  <Settings :size="15" />
                  <span>设置</span>
                </button>
                <button type="button" @click="openAccessSwitch">
                  <KeyRound :size="15" />
                  <span>接入切换</span>
                </button>
                <button type="button" @click="openVersionControl">
                  <GitBranch :size="15" />
                  <span>版本控制</span>
                </button>
                <button v-if="auth.user?.role === 'admin'" type="button" @click="openAdminMonitor">
                  <ShieldCheck :size="15" />
                  <span>管理员监控</span>
                </button>
                <button class="sidebar-settings-danger" type="button" @click="requestLogout">
                  <LogOut :size="15" />
                  <span>退出登录</span>
                </button>
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
      <header class="chat-header" :class="{ 'sources-open': sourceDrawerOpen }">
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

          <button class="top-icon-button" type="button" title="BaseURL / API Key 切换" aria-label="BaseURL / API Key 切换" @click="openAccessSwitch">
            <KeyRound :size="15" />
          </button>

          <button class="top-icon-button" type="button" title="新对话" aria-label="新对话" @click="newChat">
            <Plus :size="15" />
          </button>
        </div>
      </header>

      <div class="chat-workspace" :class="{ 'file-tree-collapsed': !fileTreePinned }">
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
              @open-sources="openWebSearchSources"
            />
          </div>
        </section>

        <div v-if="sourceDrawerOpen" class="source-drawer-backdrop" @click="closeWebSearchSources" />
        <aside
          class="source-drawer"
          :class="{ open: sourceDrawerOpen }"
          aria-label="联网搜索来源"
          :aria-hidden="!sourceDrawerOpen"
        >
          <div class="source-drawer-header">
            <div>
              <span>搜索结果</span>
              <strong>已阅读 {{ sourceDrawerSources.length }} 个网页</strong>
            </div>
            <button class="source-drawer-close" type="button" title="关闭" aria-label="关闭来源面板" @click="closeWebSearchSources">
              <X :size="18" />
            </button>
          </div>
          <div class="source-drawer-list">
            <button
              v-for="source in sourceDrawerSources"
              :key="`${source.index}-${source.url}`"
              class="source-result-card"
              type="button"
              @click="openSourceUrl(source)"
            >
              <span class="source-result-index">{{ source.index }}</span>
              <span class="source-result-main">
                <span class="source-result-meta">
                  <SourceIcon :source="source" />
                  <span class="source-result-site">{{ sourceSiteName(source) }}</span>
                  <span v-if="sourcePublishedLabel(source)" class="source-result-date">{{ sourcePublishedLabel(source) }}</span>
                  <span v-if="sourceDiagnostics(source)" class="source-result-date">{{ sourceDiagnostics(source) }}</span>
                </span>
                <strong class="source-result-title">{{ source.title }}</strong>
                <em v-if="sourceSummaryText(source)" class="source-result-summary">{{ sourceSummaryText(source) }}</em>
              </span>
            </button>
          </div>
        </aside>

        <aside
          v-if="userQuestionNavItems.length > 0"
          class="question-nav-panel"
          :class="{
            expanded: questionNavExpanded,
            'has-before': questionNavHasBefore,
            'has-after': questionNavHasAfter,
            'short-conversation': questionNavShortConversation
          }"
          aria-label="当前对话问题导航"
          @mouseenter="questionNavExpanded = true"
          @mouseleave="questionNavExpanded = false"
        >
          <div class="question-nav-card">
            <div ref="questionNavScroll" class="question-nav-scroll" @scroll="updateQuestionNavOverflow">
              <div
                v-for="item in userQuestionNavItems"
                :key="item.id"
                class="question-nav-row"
                :class="{ active: item.id === activeQuestionMessageId }"
                :data-question-nav-id="item.id"
              >
                <button
                  class="question-nav-item"
                  :class="{ active: item.id === activeQuestionMessageId }"
                  type="button"
                  :aria-label="`跳转到问题：${item.title}`"
                  @click="jumpToQuestion(item.id)"
                >
                  <span>{{ item.index }}. {{ item.summary }}</span>
                </button>
                <button
                  class="question-nav-rail"
                  :class="{ active: item.id === activeQuestionMessageId }"
                  type="button"
                  aria-label="跳转到该问题"
                  @click="jumpToQuestion(item.id)"
                >
                  <span class="question-nav-mark" :class="{ active: item.id === activeQuestionMessageId }" aria-hidden="true" />
                </button>
              </div>
            </div>
          </div>
        </aside>

        <aside class="file-tree-panel" :class="{ collapsed: !fileTreePinned }" aria-label="对话文件树">
          <button class="file-tree-pin" type="button" :title="fileTreePinned ? '收起文件树' : '展开文件树'" @click="toggleFileTreePinned">
            <PinOff v-if="fileTreePinned" :size="17" />
            <Pin v-else :size="17" />
          </button>
          <Transition name="file-tree-body">
            <div v-if="fileTreePinned" class="file-tree-body">
              <header class="file-tree-header">
                <div>
                  <strong>文件树</strong>
                  <span>{{ activeFileTreeAttachments.length }} 个文件 · {{ selectedFileTreeAttachments.length }} 已选</span>
                </div>
              </header>
              <div v-if="fileTreeLoading" class="file-tree-skeleton" aria-label="正在加载文件树">
                <section v-for="group in ['images', 'documents']" :key="group" class="file-tree-skeleton-group">
                  <div class="file-tree-skeleton-head">
                    <span class="skeleton-pill file-tree-skeleton-icon" />
                    <span class="skeleton-line medium" />
                    <span class="skeleton-line short" />
                  </div>
                  <div
                    v-for="item in group === 'images' ? 2 : 3"
                    :key="`${group}-${item}`"
                    class="file-tree-skeleton-item"
                  >
                    <span class="skeleton-pill file-tree-skeleton-check" />
                    <span class="skeleton-pill file-tree-skeleton-thumb" />
                    <span class="file-tree-skeleton-meta">
                      <span class="skeleton-line" :class="item % 2 ? 'wide' : 'medium'" />
                      <span class="skeleton-line short" />
                    </span>
                  </div>
                </section>
              </div>
              <div v-else-if="!activeFileTreeAttachments.length" class="file-tree-empty">当前对话暂无文件</div>
              <div v-else class="file-tree-groups">
                <section class="file-tree-group">
                  <button class="file-tree-group-head" type="button" @click="toggleFileTreeGroup('images')">
                    <ImageIcon :size="16" />
                    <span>图片</span>
                    <em>{{ fileTreeImages.length }}</em>
                  </button>
                  <Transition name="file-tree-list">
                    <div v-if="fileTreeGroupOpen.images" class="file-tree-list">
                      <div class="file-tree-list-inner">
                        <article v-for="item in fileTreeImages" :key="item.id" class="file-tree-item" :class="{ selected: item.selected }">
                          <label class="file-tree-check" :title="item.selected ? '取消勾选' : '勾选引用'">
                            <input type="checkbox" :checked="item.selected" @change="handleFileTreeSelectionChange(item, $event)" />
                            <span aria-hidden="true" />
                          </label>
                          <button class="file-tree-thumb image" type="button" @click="openAttachmentPreview(fileTreeAttachmentForPreview(item))">
                            <img :src="attachmentPreviewUrl(item.attachment.id)" :alt="fileTreeDisplayName(item)" />
                          </button>
                          <button class="file-tree-meta" type="button" @click="openAttachmentPreview(fileTreeAttachmentForPreview(item))">
                            <strong>{{ fileTreeDisplayName(item) }}</strong>
                            <span>{{ formatBytes(item.attachment.sizeBytes) }} · {{ parseStatusText[item.attachment.parseStatus] || item.attachment.parseStatus }}</span>
                          </button>
                          <div class="file-tree-actions">
                            <button type="button" title="重命名" @click="openAttachmentRename(item)"><Pencil :size="14" /></button>
                            <button type="button" title="移除" @click="requestRemoveFileTreeAttachment(item)"><Trash2 :size="14" /></button>
                          </div>
                        </article>
                      </div>
                    </div>
                  </Transition>
                </section>
                <section class="file-tree-group">
                  <button class="file-tree-group-head" type="button" @click="toggleFileTreeGroup('documents')">
                    <FileText :size="16" />
                    <span>文档</span>
                    <em>{{ fileTreeDocuments.length }}</em>
                  </button>
                  <Transition name="file-tree-list">
                    <div v-if="fileTreeGroupOpen.documents" class="file-tree-list">
                      <div class="file-tree-list-inner">
                        <article v-for="item in fileTreeDocuments" :key="item.id" class="file-tree-item" :class="{ selected: item.selected }">
                          <label class="file-tree-check" :title="item.selected ? '取消勾选' : '勾选引用'">
                            <input type="checkbox" :checked="item.selected" @change="handleFileTreeSelectionChange(item, $event)" />
                            <span aria-hidden="true" />
                          </label>
                          <button class="file-tree-thumb document" type="button" @click="openAttachmentPreview(fileTreeAttachmentForPreview(item))">
                            <FileText :size="17" />
                          </button>
                          <button class="file-tree-meta" type="button" @click="openAttachmentPreview(fileTreeAttachmentForPreview(item))">
                            <strong>{{ fileTreeDisplayName(item) }}</strong>
                            <span>{{ attachmentKindLabel(item.attachment) }} · {{ parseStatusText[item.attachment.parseStatus] || item.attachment.parseStatus }}</span>
                          </button>
                          <div class="file-tree-actions">
                            <button type="button" title="重命名" @click="openAttachmentRename(item)"><Pencil :size="14" /></button>
                            <button type="button" title="移除" @click="requestRemoveFileTreeAttachment(item)"><Trash2 :size="14" /></button>
                          </div>
                        </article>
                      </div>
                    </div>
                  </Transition>
                </section>
              </div>
            </div>
          </Transition>
        </aside>
      </div>

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
          <div
            v-if="uploadingAttachmentNames.length || composerAttachmentItems.length"
            ref="composerAttachmentsScroller"
            class="composer-attachments"
            @wheel="handleComposerAttachmentsWheel"
          >
            <div v-for="name in uploadingAttachmentNames" :key="`uploading-${name}`" class="composer-attachment-card is-uploading">
              <div class="composer-attachment-loading" aria-hidden="true" />
              <div class="composer-attachment-meta">
                <strong>{{ name }}</strong>
                <span>上传中</span>
              </div>
            </div>
            <article
              v-for="item in composerImageAttachments"
              :key="`composer-image-${item.attachment.id}`"
              class="composer-attachment-card is-image is-selected"
            >
              <button
                class="composer-attachment-preview"
                type="button"
                :title="`查看图片：${fileTreeDisplayName(item)}`"
                :aria-label="`查看图片：${fileTreeDisplayName(item)}`"
                @click="openAttachmentPreview(fileTreeAttachmentForPreview(item))"
              >
                <img :src="attachmentPreviewUrl(item.attachment.id)" :alt="fileTreeDisplayName(item)" />
              </button>
              <button
                class="composer-attachment-remove"
                type="button"
                title="取消引用"
                aria-label="取消引用"
                @click.stop="setFileTreeAttachmentSelected(item, false)"
              >
                <X :size="13" />
              </button>
              <span class="composer-attachment-status">{{ parseStatusText[item.attachment.parseStatus] || item.attachment.parseStatus }}</span>
            </article>
            <article
              v-for="item in composerDocumentAttachments"
              :key="`composer-file-${item.attachment.id}`"
              class="composer-attachment-card is-file is-selected"
            >
              <button
                class="composer-attachment-preview"
                type="button"
                :title="`查看文件：${fileTreeDisplayName(item)}`"
                :aria-label="`查看文件：${fileTreeDisplayName(item)}`"
                @click="openAttachmentPreview(fileTreeAttachmentForPreview(item))"
              >
                <span class="composer-file-icon" :class="attachmentKindClass(item.attachment)"><FileText :size="20" /></span>
                <span class="composer-attachment-meta">
                  <strong>{{ fileTreeDisplayName(item) }}</strong>
                  <em>{{ attachmentKindLabel(item.attachment) }} · {{ parseStatusText[item.attachment.parseStatus] || item.attachment.parseStatus }}</em>
                </span>
              </button>
              <button
                class="composer-attachment-remove"
                type="button"
                title="取消引用"
                aria-label="取消引用"
                @click.stop="setFileTreeAttachmentSelected(item, false)"
              >
                <X :size="13" />
              </button>
            </article>
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
              <button
                class="composer-icon-button web-search-toggle"
                :class="{ active: webSearchEnabled }"
                type="button"
                :disabled="webSearchToggleDisabled"
                :title="webSearchToggleLabel"
                :aria-label="webSearchToggleLabel"
                :aria-pressed="webSearchEnabled"
                @click="toggleWebSearch"
              >
                <Globe :size="18" />
                <span v-if="webSearchModeText" class="web-search-mode-text">{{ webSearchModeText }}</span>
              </button>

            </div>

            <div class="composer-right-tools">
              <button class="send-button" type="submit" :disabled="currentConversationStreaming || (!input.trim() && !selectedFileTreeAttachments.length)" title="发送" aria-label="发送">
                <Send :size="18" />
              </button>
            </div>
          </div>
        </form>
      </footer>


    </main>

    <Transition name="modal-fade">
      <div v-if="webSearchModeDialogOpen" class="settings-modal-backdrop web-search-mode-backdrop" @click.self="webSearchModeDialogOpen = false">
        <section class="web-search-mode-modal" role="dialog" aria-modal="true" aria-label="选择联网搜索模式">
          <div class="web-search-mode-header">
            <div>
              <strong>选择联网搜索模式</strong>
              <span>本次会话会记住你的选择</span>
            </div>
            <button class="source-drawer-close" type="button" title="关闭" aria-label="关闭" @click="webSearchModeDialogOpen = false">
              <X :size="18" />
            </button>
          </div>
          <div class="web-search-mode-options">
            <button type="button" class="web-search-mode-option" :class="{ active: webSearchModeDraft === 'auto' }" @click="webSearchModeDraft = 'auto'">
              <strong>自动</strong>
              <span>按问题自动选择快速或深搜</span>
            </button>
            <button type="button" class="web-search-mode-option" :class="{ active: webSearchModeDraft === 'deep' }" @click="webSearchModeDraft = 'deep'">
              <strong>深度优先</strong>
              <span>多轮读取页面并补充关键词</span>
            </button>
            <button type="button" class="web-search-mode-option" :class="{ active: webSearchModeDraft === 'fast' }" @click="webSearchModeDraft = 'fast'">
              <strong>快速回答</strong>
              <span>单轮低延迟搜索</span>
            </button>
          </div>
          <label v-if="webSearchModeDraft === 'deep'" class="web-search-round-field">
            <span>最大深搜轮数</span>
            <input v-model.number="webSearchRoundDraft" type="number" min="1" max="5" />
          </label>
          <div class="web-search-mode-actions">
            <button class="settings-secondary" type="button" @click="webSearchModeDialogOpen = false">取消</button>
            <button class="settings-primary" type="button" @click="applyWebSearchChoice(true, webSearchModeDraft, webSearchModeDraft === 'deep' ? webSearchRoundDraft : 3)">
              开启联网搜索
            </button>
          </div>
        </section>
      </div>
    </Transition>

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
            <button v-if="auth.user?.role === 'admin'" class="settings-tab-button" :class="{ active: settingsTab === 'web' }" @click="selectSettingsTab('web')">联网搜索</button>
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

              <section v-else-if="settingsTab === 'web' && auth.user?.role === 'admin'" key="web" class="settings-pane">
                <div class="settings-pane-heading">
                  <h2>联网搜索</h2>
                  <p>配置 SearXNG 搜索源。用户在单个对话里开启后，模型可按需调用搜索和网页读取工具。</p>
                </div>
                <form class="settings-card" @submit.prevent="saveWebSearchSettings">
                  <div v-if="webSearchSettingsLoading" class="settings-empty">正在加载联网搜索设置...</div>
                  <div v-else class="settings-grid">
                    <label class="settings-check settings-field-wide">
                      <input v-model="webSearchSettings.enabled" type="checkbox" />
                      启用联网搜索
                    </label>
                    <label class="settings-field settings-field-wide">
                      <span>SearXNG URL</span>
                      <input v-model="webSearchSettings.searxngBaseUrl" class="settings-input" placeholder="https://searxng.example.com/search" />
                    </label>
                    <label class="settings-field settings-field-wide">
                      <span>搜索源顺序</span>
                      <input :value="listSettingText(webSearchSettings.providerOrder)" class="settings-input" placeholder="bocha, sougou, jina, searxng, serper" @input="webSearchSettings.providerOrder = normalizeListSetting(($event.target as HTMLInputElement).value)" />
                    </label>
                    <label class="settings-field settings-field-wide">
                      <span>SearXNG 引擎</span>
                      <input :value="listSettingText(webSearchSettings.searxngEngines)" class="settings-input" placeholder="bing, baidu" @input="webSearchSettings.searxngEngines = normalizeListSetting(($event.target as HTMLInputElement).value)" />
                    </label>
                    <div class="settings-field settings-field-wide">
                      <span>搜索源状态</span>
                      <div class="settings-provider-status">
                        <span v-for="provider in ['searxng', 'bocha', 'sougou', 'jina', 'serper']" :key="provider" :class="{ active: webSearchSettings.providerStatus?.[provider] }">
                          {{ provider }} {{ webSearchSettings.providerStatus?.[provider] ? '已配置' : '未配置' }}
                        </span>
                      </div>
                    </div>
                    <label class="settings-field">
                      <span>结果数</span>
                      <input v-model.number="webSearchSettings.resultCount" class="settings-input" type="number" min="1" max="10" />
                    </label>
                    <label class="settings-field">
                      <span>候选数</span>
                      <input v-model.number="webSearchSettings.candidateCount" class="settings-input" type="number" min="3" max="50" />
                    </label>
                    <label class="settings-field">
                      <span>语言</span>
                      <input v-model="webSearchSettings.language" class="settings-input" placeholder="all" />
                    </label>
                    <label class="settings-field">
                      <span>安全搜索</span>
                      <input v-model="webSearchSettings.safesearch" class="settings-input" placeholder="1" />
                    </label>
                    <label class="settings-field">
                      <span>搜索超时秒</span>
                      <input v-model.number="webSearchSettings.timeoutSeconds" class="settings-input" type="number" min="3" max="60" />
                    </label>
                    <label class="settings-field">
                      <span>读取网页超时秒</span>
                      <input v-model.number="webSearchSettings.fetchTimeoutSeconds" class="settings-input" type="number" min="3" max="60" />
                    </label>
                    <label class="settings-field">
                      <span>最大工具调用次数</span>
                      <input v-model.number="webSearchSettings.maxToolCalls" class="settings-input" type="number" min="1" max="10" />
                    </label>
                    <label class="settings-field">
                      <span>网页最大字符数</span>
                      <input v-model.number="webSearchSettings.fetchMaxChars" class="settings-input" type="number" min="1000" max="50000" />
                    </label>
                    <label class="settings-field">
                      <span>正文读取页数</span>
                      <input v-model.number="webSearchSettings.fetchTopN" class="settings-input" type="number" min="0" max="10" />
                    </label>
                    <label class="settings-field">
                      <span>切块大小</span>
                      <input v-model.number="webSearchSettings.chunkSize" class="settings-input" type="number" min="300" max="3000" />
                    </label>
                    <label class="settings-field">
                      <span>切块重叠</span>
                      <input v-model.number="webSearchSettings.chunkOverlap" class="settings-input" type="number" min="0" max="1000" />
                    </label>
                    <label class="settings-field">
                      <span>证据片段数</span>
                      <input v-model.number="webSearchSettings.maxEvidenceChunks" class="settings-input" type="number" min="1" max="20" />
                    </label>
                    <label class="settings-field">
                      <span>最低相关度</span>
                      <input v-model.number="webSearchSettings.minRelevanceScore" class="settings-input" type="number" min="0" max="1" step="0.05" />
                    </label>
                    <label class="settings-check">
                      <input v-model="webSearchSettings.rerankEnabled" type="checkbox" />
                      启用本地重排
                    </label>
                    <label class="settings-field settings-field-wide">
                      <span>重排模型</span>
                      <input v-model="webSearchSettings.rerankerModel" class="settings-input" placeholder="BAAI/bge-reranker-v2-m3" />
                    </label>
                    <label class="settings-field settings-field-wide">
                      <span>可信域名</span>
                      <textarea :value="listSettingText(webSearchSettings.trustedDomains)" class="settings-textarea" rows="2" placeholder="news.example.com, gov.cn" @input="webSearchSettings.trustedDomains = normalizeListSetting(($event.target as HTMLTextAreaElement).value)"></textarea>
                    </label>
                    <label class="settings-field settings-field-wide">
                      <span>屏蔽域名</span>
                      <textarea :value="listSettingText(webSearchSettings.blockedDomains)" class="settings-textarea" rows="2" placeholder="example.com, low-quality.example" @input="webSearchSettings.blockedDomains = normalizeListSetting(($event.target as HTMLTextAreaElement).value)"></textarea>
                    </label>
                  </div>
                  <div class="settings-api-card-footer">
                    <button class="settings-primary" type="submit" :disabled="webSearchSettingsLoading || webSearchSettingsSaving">
                      {{ webSearchSettingsSaving ? '保存中...' : '保存联网搜索设置' }}
                    </button>
                  </div>
                </form>
                <form class="settings-card" @submit.prevent="testWebSearchSettings">
                  <div class="settings-card-header">
                    <div>
                      <h3>测试搜索</h3>
                      <p>使用当前已保存配置测试 SearXNG 返回结果。</p>
                    </div>
                  </div>
                  <div class="settings-inline-field">
                    <input v-model="webSearchTestQuery" class="settings-input" placeholder="输入测试关键词" />
                    <button class="settings-secondary" type="submit" :disabled="webSearchTesting">
                      {{ webSearchTesting ? '测试中...' : '测试' }}
                    </button>
                  </div>
                  <div v-if="webSearchTestResults.length" class="settings-web-results">
                    <a v-for="item in webSearchTestResults" :key="item.url" :href="item.url" target="_blank" rel="noreferrer">
                      <strong>{{ item.title || item.url }}</strong>
                      <small>
                        {{ item.provider || 'source' }}
                        <template v-if="typeof item.confidence === 'number'"> · {{ Math.round(item.confidence * 100) }}%</template>
                        <template v-if="item.sourceTier || item.source_tier"> · tier:{{ item.sourceTier || item.source_tier }}</template>
                        <template v-if="item.supportLevel || item.support_level"> · support:{{ item.supportLevel || item.support_level }}</template>
                        <template v-if="item.searchDepth || item.search_depth"> · mode:{{ item.searchDepth || item.search_depth }}</template>
                        <template v-if="item.rerankStatus || item.rerank_status"> · {{ item.rerankStatus || item.rerank_status }}</template>
                        <template v-if="item.degraded"> · degraded</template>
                        <template v-if="(item.matchedTerms || item.matched_terms)?.length"> · match:{{ (item.matchedTerms || item.matched_terms || []).slice(0, 4).join(',') }}</template>
                        <template v-if="item.filterReason || item.filter_reason"> · {{ item.filterReason || item.filter_reason }}</template>
                      </small>
                      <span>{{ item.evidence || item.snippet || item.url }}</span>
                    </a>
                  </div>
                </form>
              </section>

              <section v-else-if="settingsTab === 'api'" key="api" class="settings-pane">
                <div class="settings-pane-heading settings-api-heading">
                  <div>
                    <h2>API 管理</h2>
                    <p>管理 BaseURL 和当前 BaseURL 下的 API Key；日常切换可使用独立接入切换窗口。</p>
                  </div>
                  <div class="settings-api-heading-actions">
                    <button class="settings-secondary" type="button" :disabled="settingsLoading" @click="refreshApiSettings">
                      <RefreshCw :size="14" />
                      刷新
                    </button>
                    <button class="settings-primary" type="button" @click="openAccessSwitch">
                      <KeyRound :size="15" />
                      接入切换
                    </button>
                  </div>
                </div>

                <div class="settings-api-overview">
                  <article class="settings-api-overview-item primary">
                    <span>当前 BaseURL</span>
                    <strong>{{ activeModelEndpoint?.name || '未配置' }}</strong>
                    <small>{{ activeModelEndpoint?.baseUrl || '暂无可用 BaseURL' }}</small>
                  </article>
                  <article class="settings-api-overview-item">
                    <span>BaseURL 数量</span>
                    <strong>{{ modelEndpoints.length }}</strong>
                    <small>{{ modelEndpoints.filter((endpoint) => endpoint.isActive).length ? '已有当前接入' : '尚未激活接入' }}</small>
                  </article>
                  <article class="settings-api-overview-item">
                    <span>当前范围内 API Key</span>
                    <strong>{{ apiKeys.length }}</strong>
                    <small>{{ activeScopedApiKeys.length }} 个当前分组密钥</small>
                  </article>
                </div>

                <div class="settings-api-layout">
                  <div class="settings-api-compose">
                    <form class="settings-card settings-api-create-card" @submit.prevent="createModelEndpoint">
                      <div class="settings-card-header">
                        <div>
                          <h3>新增 BaseURL</h3>
                          <p>添加一个 OpenAI 兼容模型服务地址。</p>
                        </div>
                      </div>
                      <div class="settings-api-form-grid">
                        <label class="settings-field">
                          <span>名称</span>
                          <input v-model="newEndpoint.name" class="settings-input" placeholder="例如：工作服务" />
                        </label>
                        <label class="settings-field">
                          <span>BaseURL</span>
                          <input v-model="newEndpoint.baseUrl" class="settings-input" placeholder="https://example.com/v1" />
                        </label>
                      </div>
                      <div class="settings-api-card-footer">
                        <label class="settings-check">
                          <input v-model="newEndpoint.makeActive" type="checkbox" />
                          添加后设为当前
                        </label>
                        <button class="settings-primary settings-api-action" type="submit">
                          <Plus :size="15" />
                          添加 BaseURL
                        </button>
                      </div>
                    </form>

                    <form class="settings-card settings-api-create-card" @submit.prevent="createApiKey">
                      <div class="settings-card-header">
                        <div>
                          <h3>新增 API Key</h3>
                          <p>默认绑定到当前 BaseURL，也可选择其他 BaseURL。</p>
                        </div>
                      </div>
                      <div class="settings-api-form-grid">
                        <label class="settings-field">
                          <span>密钥名称</span>
                          <input v-model="newApiKey.name" class="settings-input" placeholder="例如：工作 / 备用" />
                        </label>
                        <label class="settings-field">
                          <span>所属 BaseURL</span>
                          <AppSelect
                            v-model="newApiKey.endpointId"
                            class="settings-select"
                            button-class="settings-select-button"
                            menu-class="settings-select-menu"
                            option-class="settings-select-option"
                            :options="endpointOptions"
                            @change="setNewApiKeyEndpoint"
                          />
                        </label>
                        <label class="settings-field">
                          <span>用途分组</span>
                          <AppSelect
                            v-model="newApiKey.groupId"
                            class="settings-select"
                            button-class="settings-select-button"
                            menu-class="settings-select-menu"
                            option-class="settings-select-option"
                            :options="groupOptions"
                            @change="setNewApiKeyGroup"
                          />
                        </label>
                        <label class="settings-field">
                          <span>API Key</span>
                          <input v-model="newApiKey.apiKey" class="settings-input" type="password" placeholder="明文只提交一次" />
                        </label>
                      </div>
                      <div class="settings-api-card-footer">
                        <label class="settings-check">
                          <input v-model="newApiKey.makeActive" type="checkbox" />
                          添加后设为该分组当前密钥
                        </label>
                        <button class="settings-primary settings-api-action" type="submit">
                          <Plus :size="15" />
                          添加 API Key
                        </button>
                      </div>
                    </form>
                  </div>

                  <div class="settings-api-manage">
                    <section class="settings-card settings-api-list-card">
                      <div class="settings-card-header">
                        <div>
                          <h3>BaseURL</h3>
                          <p>保存服务地址，或把它设为当前接入。</p>
                        </div>
                      </div>
                      <div v-if="!modelEndpoints.length" class="settings-empty">暂无 BaseURL</div>
                      <div v-else class="settings-api-list">
                        <article
                          v-for="endpoint in modelEndpoints"
                          :key="endpoint.id"
                          class="settings-api-row"
                          :class="{ active: endpoint.isActive }"
                        >
                          <div class="settings-api-row-fields">
                            <input v-model="endpointDraftFor(endpoint).name" class="settings-input" aria-label="BaseURL 名称" />
                            <input v-model="endpointDraftFor(endpoint).baseUrl" class="settings-input" aria-label="BaseURL 地址" />
                          </div>
                          <div class="settings-api-row-meta">
                            <span>{{ endpoint.baseUrl }}</span>
                            <strong v-if="endpoint.isActive">当前 BaseURL</strong>
                            <em v-if="endpoint.lastProbeError">{{ endpoint.lastProbeError }}</em>
                          </div>
                          <div class="settings-api-row-actions">
                            <button class="settings-secondary" type="button" @click="saveModelEndpoint(endpoint)">保存</button>
                            <button class="settings-primary" type="button" :disabled="endpoint.isActive" @click="activateModelEndpoint(endpoint)">设为当前</button>
                            <button class="settings-danger" type="button" @click="deleteModelEndpoint(endpoint)">删除</button>
                          </div>
                        </article>
                      </div>
                    </section>

                    <section class="settings-card settings-api-list-card">
                      <div class="settings-card-header">
                        <div>
                          <h3>当前 BaseURL 的 API Key</h3>
                          <p>{{ activeModelEndpoint?.name || '未配置 BaseURL' }}</p>
                        </div>
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
                      <div v-else-if="!apiKeys.length" class="settings-empty">当前 BaseURL 下暂无 API Key</div>
                      <div v-else class="settings-api-list">
                        <article
                          v-for="key in apiKeys"
                          :key="key.id"
                          class="settings-api-row"
                          :class="{ active: key.isActive }"
                        >
                          <div class="settings-api-row-fields">
                            <input v-model="keyDraftFor(key).name" class="settings-input" aria-label="API Key 名称" />
                            <AppSelect
                              :model-value="keyDraftFor(key).groupId"
                              class="settings-select"
                              button-class="settings-select-button"
                              menu-class="settings-select-menu"
                              option-class="settings-select-option"
                              :options="groupOptions"
                              @update:model-value="setApiKeyDraftGroup(key, $event)"
                            />
                          </div>
                          <div class="settings-api-row-meta">
                            <span>{{ key.endpointName || key.baseUrl || '当前 BaseURL' }}</span>
                            <span>{{ key.groupName || 'gpt-chat' }}</span>
                            <span class="key-mask">{{ key.maskedKey || (key.last4 ? '****' + key.last4 : '未展示') }}</span>
                            <strong v-if="key.isActive">当前分组使用</strong>
                          </div>
                          <div class="settings-api-row-actions">
                            <button class="settings-secondary" type="button" @click="copyApiKeySecret(key)">复制</button>
                            <button class="settings-secondary" type="button" @click="saveApiKey(key)">保存</button>
                            <button class="settings-primary" type="button" :disabled="key.isActive" @click="activateApiKey(key)">设为当前</button>
                            <button class="settings-danger" type="button" @click="deleteApiKey(key)">删除</button>
                          </div>
                        </article>
                      </div>
                    </section>
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
                          <strong v-if="key.isActive">用户当前分组使用</strong>
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
      <div v-if="accessSwitchOpen" class="confirm-modal-backdrop access-switch-backdrop" @click.self="closeAccessSwitch">
        <section class="access-switch-modal" role="dialog" aria-modal="true" aria-label="BaseURL / API Key 切换">
          <header class="access-switch-header">
            <div class="access-switch-title">
              <KeyRound :size="20" />
              <div>
                <h2>接入切换</h2>
                <p>先切 BaseURL，右侧只显示该 BaseURL 下可切换的 API Key。</p>
              </div>
            </div>
            <button type="button" class="confirm-modal-close" title="关闭" aria-label="关闭" @click="closeAccessSwitch">
              <X :size="18" />
            </button>
          </header>

          <Transition name="soft-slide">
            <div v-if="accessSwitchNotice" class="access-switch-alert success">{{ accessSwitchNotice }}</div>
          </Transition>
          <Transition name="soft-slide">
            <div v-if="accessSwitchError" class="access-switch-alert error">{{ accessSwitchError }}</div>
          </Transition>

          <div class="access-switch-status">
            <div>
              <span>当前 BaseURL</span>
              <strong>{{ activeModelEndpoint?.name || '未配置' }}</strong>
              <small>{{ activeModelEndpoint?.baseUrl || '暂无 BaseURL' }}</small>
            </div>
            <div>
              <span>API Key 范围</span>
              <strong>{{ apiKeyScopeSummary }}</strong>
              <small>切换 Key 时不会跨 BaseURL 展示或激活</small>
            </div>
          </div>

          <div class="access-switch-grid">
            <section class="access-switch-section">
              <div class="access-switch-section-head">
                <h3>BaseURL</h3>
                <button type="button" class="access-switch-refresh" :disabled="settingsLoading || accessSwitchLoading" @click="refreshAccessSwitch">
                  <RefreshCw :size="14" />
                </button>
              </div>

              <div v-if="settingsLoading && !modelEndpoints.length" class="access-switch-list">
                <div v-for="item in 3" :key="item" class="access-switch-skeleton">
                  <span class="skeleton-line wide" />
                  <span class="skeleton-line medium" />
                </div>
              </div>
              <div v-else-if="!modelEndpoints.length" class="access-switch-empty">暂无 BaseURL，请先到 API 管理中添加。</div>
              <div v-else class="access-switch-list">
                <button
                  v-for="endpoint in modelEndpoints"
                  :key="endpoint.id"
                  type="button"
                  class="access-switch-row"
                  :class="{ active: endpoint.isActive }"
                  :disabled="settingsLoading || accessSwitchLoading || endpoint.isActive"
                  @click="switchAccessEndpoint(endpoint)"
                >
                  <span class="access-switch-row-main">
                    <strong>{{ endpoint.name }}</strong>
                    <small>{{ endpoint.baseUrl }}</small>
                    <em v-if="endpoint.lastProbeError">{{ endpoint.lastProbeError }}</em>
                  </span>
                  <span class="access-switch-badge">
                    {{ endpoint.isActive ? '当前' : accessSwitchEndpointId === endpoint.id ? '切换中' : '切换' }}
                  </span>
                </button>
              </div>
            </section>

            <section class="access-switch-section">
              <div class="access-switch-section-head">
                <h3>当前 BaseURL 的 API Key</h3>
                <span>{{ activeModelEndpoint?.name || '未配置' }}</span>
              </div>

              <div v-if="settingsLoading && !apiKeys.length" class="access-switch-list">
                <div v-for="item in 3" :key="item" class="access-switch-skeleton">
                  <span class="skeleton-line wide" />
                  <span class="skeleton-line short" />
                </div>
              </div>
              <div v-else-if="!activeModelEndpoint" class="access-switch-empty">请先选择或添加 BaseURL。</div>
              <div v-else-if="!apiKeys.length" class="access-switch-empty">当前 BaseURL 下暂无 API Key。</div>
              <div v-else class="access-switch-list">
                <button
                  v-for="key in apiKeys"
                  :key="key.id"
                  type="button"
                  class="access-switch-row"
                  :class="{ active: key.isActive }"
                  :disabled="settingsLoading || accessSwitchLoading || key.isActive"
                  @click="switchAccessApiKey(key)"
                >
                  <span class="access-switch-row-main">
                    <strong>{{ key.name }}</strong>
                    <small>{{ key.groupName || 'gpt-chat' }} · {{ key.maskedKey || (key.last4 ? '****' + key.last4 : '未展示') }}</small>
                    <em>{{ key.endpointName || key.baseUrl || activeModelEndpoint?.name || '当前 BaseURL' }}</em>
                  </span>
                  <span class="access-switch-badge">
                    {{ key.isActive ? '当前' : accessSwitchKeyId === key.id ? '切换中' : '切换' }}
                  </span>
                </button>
              </div>
            </section>
          </div>

          <footer class="access-switch-footer">
            <button type="button" class="confirm-secondary-button" :disabled="settingsLoading || accessSwitchLoading" @click="openApiSettingsFromSwitcher">
              管理配置
            </button>
            <button type="button" class="confirm-primary-button" :disabled="accessSwitchLoading" @click="closeAccessSwitch">完成</button>
          </footer>
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
      <div v-if="attachmentRenameOpen" class="confirm-modal-backdrop" @click.self="closeAttachmentRename">
        <form class="confirm-modal attachment-rename-modal" role="dialog" aria-modal="true" aria-label="重命名文件" @submit.prevent="saveAttachmentRename">
          <div class="confirm-modal-header">
            <h2>重命名文件</h2>
            <button type="button" class="confirm-modal-close" title="关闭" aria-label="关闭" @click="closeAttachmentRename">
              <X :size="18" />
            </button>
          </div>
          <label class="attachment-rename-field">
            <span>显示名称</span>
            <input
              ref="attachmentRenameInput"
              v-model="attachmentRenameDraft"
              class="attachment-rename-input"
              maxlength="255"
              placeholder="文件显示名"
              autocomplete="off"
              spellcheck="false"
              :title="attachmentRenameDraft"
              @input="attachmentRenameError = ''"
            />
          </label>
          <p v-if="attachmentRenameError" class="attachment-rename-error">{{ attachmentRenameError }}</p>
          <div class="confirm-modal-actions">
            <button type="button" class="confirm-secondary-button" :disabled="attachmentRenameSaving" @click="closeAttachmentRename">取消</button>
            <button type="submit" class="confirm-primary-button" :disabled="attachmentRenameSaving || !attachmentRenameDraft.trim()">
              {{ attachmentRenameSaving ? '保存中...' : '保存' }}
            </button>
          </div>
        </form>
      </div>
    </Transition>

    <Transition name="dialog-pop">
      <div v-if="attachmentDeleteOpen" class="confirm-modal-backdrop" @click.self="closeAttachmentDelete">
        <section class="confirm-modal" role="dialog" aria-modal="true" aria-label="移除文件">
          <div class="confirm-modal-header">
            <h2>从当前对话移除？</h2>
            <button type="button" class="confirm-modal-close" title="关闭" aria-label="关闭" @click="closeAttachmentDelete">
              <X :size="18" />
            </button>
          </div>
          <p>文件「{{ attachmentDeleteTarget ? fileTreeDisplayName(attachmentDeleteTarget) : '未命名文件' }}」将从当前对话文件树移除，其他对话不受影响。</p>
          <div class="confirm-modal-actions">
            <button type="button" class="confirm-secondary-button" :disabled="attachmentDeleting" @click="closeAttachmentDelete">取消</button>
            <button type="button" class="confirm-danger-button" :disabled="attachmentDeleting" @click="confirmRemoveFileTreeAttachment">
              {{ attachmentDeleting ? '移除中...' : '确认移除' }}
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
