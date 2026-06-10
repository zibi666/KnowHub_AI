export interface User {
  id: string
  username: string
  role: 'admin' | 'user'
  status: string
  mustChangePassword: boolean
  hasApiKey: boolean
  avatarUrl?: string | null
}

export interface Conversation {
  id: string
  title: string
  autoCompactionEnabled: boolean
  webSearchEnabled?: boolean
  web_search_enabled?: boolean
  webSearchMode?: WebSearchMode
  web_search_mode?: WebSearchMode
  webSearchMaxRounds?: number
  web_search_max_rounds?: number
  createdAt: string
  updatedAt: string
}

export type WebSearchMode = 'auto' | 'deep' | 'fast'

export interface Message {
  id: string
  clientKey?: string
  conversationId: string
  parentMessageId?: string | null
  retryOfMessageId?: string | null
  role: 'user' | 'assistant' | 'system'
  content: string
  status: string
  model?: string
  promptTokens?: number
  completionTokens?: number
  totalTokens: number
  tokensSource?: string
  firstTokenSeconds?: number | null
  first_token_seconds?: number | null
  createdAt: string
  created_at?: string
  attachments?: Attachment[]
  generatedImageSize?: string
  imageProgress?: ImageProgress
  image_progress?: ImageProgress
  elapsedSeconds?: number
  elapsed_seconds?: number
  startedAt?: number
  started_at?: number
  progressDetail?: string
  progress_detail?: string
  progressPhase?: string
  progress_phase?: string
  webSearchSources?: WebSearchSource[]
  web_search_sources?: WebSearchSource[]
  webSearchTrace?: WebSearchTrace | null
  web_search_trace?: WebSearchTrace | null
}

export interface WebSearchSource {
  index: number
  title: string
  url: string
  snippet?: string
  evidence?: string
  siteName?: string | null
  site_name?: string | null
  publishedAt?: string | null
  published_at?: string | null
  faviconUrl?: string | null
  favicon_url?: string | null
  displayUrl?: string
  display_url?: string
  provider?: string | null
  confidence?: number | null
  rerankStatus?: string | null
  rerank_status?: string | null
  sourceTier?: string | null
  source_tier?: string | null
  matchedTerms?: string[]
  matched_terms?: string[]
  supportLevel?: string | null
  support_level?: string | null
  searchDepth?: string | null
  search_depth?: string | null
  degraded?: boolean
  filterReason?: string | null
  filter_reason?: string | null
}

export interface WebSearchStatus {
  enabled: boolean
  configured: boolean
}

export interface WebSearchTraceSource {
  title?: string
  url?: string
  provider?: string | null
  confidence?: number | null
  sourceTier?: string | null
  source_tier?: string | null
  supportLevel?: string | null
  support_level?: string | null
  searchDepth?: string | null
  search_depth?: string | null
  degraded?: boolean
  filterReason?: string | null
  filter_reason?: string | null
}

export interface WebSearchTraceEvent {
  round?: number
  phase?: string
  type?: 'search' | 'read' | 'review' | string
  tool?: string
  ok?: boolean
  cached?: boolean
  query?: string
  url?: string
  result_count?: number
  resultCount?: number
  sources?: WebSearchTraceSource[]
  needs_more?: boolean
  needsMore?: boolean
  new_queries?: string[]
  newQueries?: string[]
  urls_to_fetch?: string[]
  urlsToFetch?: string[]
  searched_queries?: string[]
  searchedQueries?: string[]
  read_urls?: string[]
  readUrls?: string[]
  failed_urls?: string[]
  failedUrls?: string[]
  source_domains?: string[]
  sourceDomains?: string[]
  evidence_gaps?: string[]
  evidenceGaps?: string[]
  relevance_notes?: string[]
  relevanceNotes?: string[]
  accuracy_notes?: string[]
  accuracyNotes?: string[]
  reason_codes?: string[]
  reasonCodes?: string[]
  decision_summary?: string
  decisionSummary?: string
  attempts?: number
  soft_max_rounds_reached?: boolean
  softMaxRoundsReached?: boolean
  stop_reason?: string
  stopReason?: string
  error?: string
}

export interface WebSearchTrace {
  mode?: WebSearchMode | string
  effective_depth?: string
  effectiveDepth?: string
  max_rounds?: number
  maxRounds?: number
  requested_max_rounds?: number
  requestedMaxRounds?: number
  executed_rounds?: number
  executedRounds?: number
  planning_attempts?: number
  planningAttempts?: number
  planner_reason_codes?: string[]
  plannerReasonCodes?: string[]
  planner_summary?: string
  plannerSummary?: string
  review_attempts?: number
  reviewAttempts?: number
  soft_max_rounds_reached?: boolean
  softMaxRoundsReached?: boolean
  stop_code?: string
  stopCode?: string
  source_count?: number
  sourceCount?: number
  early_stop?: boolean
  earlyStop?: boolean
  stop_reason?: string
  stopReason?: string
  events?: WebSearchTraceEvent[]
}

export interface WebSearchSettings {
  enabled: boolean
  searxngBaseUrl?: string | null
  searxng_base_url?: string | null
  resultCount: number
  result_count?: number
  language: string
  safesearch: string
  timeoutSeconds: number
  timeout_seconds?: number
  fetchTimeoutSeconds: number
  fetch_timeout_seconds?: number
  maxToolCalls: number
  max_tool_calls?: number
  fetchMaxChars: number
  fetch_max_chars?: number
  providerOrder: string[]
  provider_order?: string[]
  searxngEngines: string[]
  searxng_engines?: string[]
  candidateCount: number
  candidate_count?: number
  fetchTopN: number
  fetch_top_n?: number
  chunkSize: number
  chunk_size?: number
  chunkOverlap: number
  chunk_overlap?: number
  maxEvidenceChunks: number
  max_evidence_chunks?: number
  rerankEnabled: boolean
  rerank_enabled?: boolean
  rerankerModel: string
  reranker_model?: string
  minRelevanceScore: number
  min_relevance_score?: number
  trustedDomains: string[]
  trusted_domains?: string[]
  blockedDomains: string[]
  blocked_domains?: string[]
  providerStatus?: Record<string, boolean>
  provider_status?: Record<string, boolean>
}

export interface SendMessageResponse {
  conversationId: string
  conversation_id?: string
  userMessage: Message
  user_message?: Message
  assistantMessage: Message
  assistant_message?: Message
  status: string
  background: boolean
}

export interface ImageProgress {
  b64Json?: string
  b64_json?: string
  index: number
  total: number
  outputFormat?: string
  output_format?: string
  detail?: string
  elapsedSeconds?: number
  elapsed_seconds?: number
  startedAt?: number
  started_at?: number
  phase?: string
  size?: string
}

export interface ConversationSearchResult {
  conversationId: string
  conversationTitle: string
  messageId: string
  role: 'user' | 'assistant' | 'system'
  snippet: string
  createdAt: string
}

export interface Attachment {
  id: string
  filename: string
  mimeSniffed: string
  sizeBytes: number
  parseStatus: string
  parseError?: string
  contextTextTokens: number
  chunkCount?: number
  embeddingStatus?: string | null
  previewText?: string | null
  previewDataUrl?: string
  createdAt?: string
}

export interface ConversationAttachment {
  id: string
  conversationId: string
  conversation_id?: string
  attachment: Attachment
  selected: boolean
  displayName?: string | null
  display_name?: string | null
  createdAt: string
  created_at?: string
  updatedAt: string
  updated_at?: string
}

export interface ImageGenerationSettings {
  size: string
  quality: string
  background: string
  outputFormat: string
  outputCompression: number
  moderation: string
}

export interface AttachmentChunkPreview {
  chunkIndex: number
  contentPreview: string
  tokenCount: number
  embeddingStatus: string
  embeddingModel?: string | null
  error?: string | null
}

export interface ApiKeyGroup {
  id: string
  name: string
  description?: string
  purpose: 'none' | 'chat' | 'image'
  isSystem: boolean
}

export interface ApiKeyEntry {
  id: string
  userId?: string | null
  username?: string | null
  name: string
  groupId?: string | null
  groupName?: string | null
  endpointId?: string | null
  endpointName?: string | null
  baseUrl?: string | null
  fingerprint: string
  last4: string
  maskedKey: string
  status: string
  isActive: boolean
  availableModels: string[]
  probedAt?: string | null
}

export interface ModelEndpoint {
  id: string
  name: string
  baseUrl: string
  isActive: boolean
  status: string
  lastProbeError?: string | null
  probedAt?: string | null
}

export interface UserQuota {
  userId: string
  maxStorageBytes: number
  maxImageMb: number
  maxDocumentMb: number
  uploadRateLimitPerHour: number
  dailyDownloadLimit: number
  allowUpload: boolean
  allowCodeUpload: boolean
  modelWhitelistJson?: string[] | null
  defaultModel?: string | null
  autoCompactionEnabled: boolean
}
