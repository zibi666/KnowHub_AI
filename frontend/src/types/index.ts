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
  createdAt: string
  updatedAt: string
}

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
  createdAt?: string
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
}

export interface ApiKeyEntry {
  id: string
  userId?: string | null
  username?: string | null
  name: string
  groupId?: string | null
  groupName?: string | null
  fingerprint: string
  last4: string
  maskedKey: string
  status: string
  isActive: boolean
  availableModels: string[]
  probedAt?: string | null
}
