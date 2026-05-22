<script setup lang="ts">
import { computed, onUnmounted, ref, watch, type CSSProperties } from 'vue'
import { Check, ChevronDown, ChevronUp, Copy, Download, FileText } from 'lucide-vue-next'
import MarkdownMessage from './MarkdownMessage.vue'
import type { Attachment, Message } from '../types'
import { copyText } from '../utils/clipboard'

const props = defineProps<{ message: Message }>()
const emit = defineEmits<{ previewAttachment: [attachment: Attachment] }>()

const USER_COLLAPSE_CHARACTER_LIMIT = 120
const USER_COLLAPSE_LINE_LIMIT = 3

const isExpanded = ref(false)
const nowMs = ref(Date.now())
const copyState = ref<'idle' | 'copied' | 'failed'>('idle')
const imageLayouts = ref<Record<string, { className: string; style: CSSProperties }>>({})
let progressTimer: number | null = null
let copyStateTimer: number | null = null
const lineCount = computed(() => props.message.content.split(/\r\n|\n|\r/).length)
const isStreaming = computed(() => props.message.status === 'streaming')
const isUserMessage = computed(() => props.message.role === 'user')
const isLiveDraft = computed(() => isStreaming.value && props.message.id.startsWith('stream-'))
const shouldRenderStreamingPlainText = computed(() => props.message.role === 'assistant' && isStreaming.value && props.message.content.trim())
const showThinkingPanel = computed(
  () => props.message.role === 'assistant' && isStreaming.value && !props.message.content.trim() && !props.message.imageProgress
)
const emptyAssistantFailureText = computed(() => {
  if (props.message.content.trim()) return ''
  if (props.message.status === 'failed_no_output') return '回复生成失败，请重试'
  if (props.message.status === 'failed_partial') return '回复生成中断，请重试'
  if (props.message.status === 'interrupted') return '回复已中断'
  return ''
})
const canCollapse = computed(
  () => {
    if (!isUserMessage.value || isStreaming.value) return false
    return props.message.content.length > USER_COLLAPSE_CHARACTER_LIMIT || lineCount.value > USER_COLLAPSE_LINE_LIMIT
  }
)
const isCollapsed = computed(() => canCollapse.value && !isExpanded.value)
const collapseButtonLabel = computed(() => (isCollapsed.value ? '展开全文' : '收起'))
const copyButtonLabel = computed(() => (copyState.value === 'copied' ? '已复制' : copyState.value === 'failed' ? '复制失败' : '复制'))
const collapsibleClasses = computed(() => ({
  'has-collapse': canCollapse.value,
  'is-collapsed': isCollapsed.value,
  'is-expanded': isExpanded.value,
  'is-user': isUserMessage.value,
  'is-assistant': !isUserMessage.value
}))
const imageAttachments = computed(() => (props.message.attachments || []).filter(isImageAttachment))
const fileAttachments = computed(() => (props.message.attachments || []).filter((attachment) => !isImageAttachment(attachment)))
const showGeneratedImageProgress = computed(() => props.message.role === 'assistant' && props.message.status === 'streaming' && props.message.imageProgress)
const generatedImageFrameStyle = computed(() => generatedImageFrameFromSize(props.message.imageProgress?.size || props.message.generatedImageSize))
const generatedImageProgressSrc = computed(() => {
  const progress = props.message.imageProgress
  if (!progress) return ''
  const b64Json = progress.b64Json || progress.b64_json
  if (!b64Json) return ''
  const format = progress.outputFormat === 'jpeg' || progress.output_format === 'jpeg' ? 'jpeg' : progress.outputFormat || progress.output_format || 'png'
  return `data:image/${format};base64,${b64Json}`
})
const generatedImageElapsed = computed(() => {
  const progress = props.message.imageProgress
  const seconds =
    progress?.startedAt && props.message.status === 'streaming'
      ? Math.max(progress.elapsedSeconds || 0, Math.floor((nowMs.value - progress.startedAt) / 1000))
      : progress?.elapsedSeconds
  return formatElapsedSeconds(seconds)
})
function formatElapsedSeconds(seconds: number | undefined) {
  if (!Number.isFinite(seconds) || seconds === undefined) return ''
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.floor(seconds % 60)
  return `${minutes} 分 ${String(rest).padStart(2, '0')} 秒`
}
const streamingElapsed = computed(() => {
  if (props.message.status !== 'streaming' || props.message.imageProgress || props.message.content.trim()) return ''
  const startedAt = props.message.startedAt || props.message.started_at
  const baseElapsed = props.message.elapsedSeconds ?? props.message.elapsed_seconds ?? 0
  const seconds = startedAt ? Math.max(baseElapsed || 0, Math.floor((nowMs.value - startedAt) / 1000)) : baseElapsed
  return formatElapsedSeconds(seconds)
})
const finalElapsed = computed(() => {
  if (props.message.imageProgress) return ''
  const elapsed = props.message.firstTokenSeconds ?? props.message.first_token_seconds ?? props.message.elapsedSeconds ?? props.message.elapsed_seconds
  return formatElapsedSeconds(elapsed)
})
const generatedImageProgressLabel = computed(() => {
  const progress = props.message.imageProgress
  if (!progress) return '等待模型响应'
  if (progress.phase === 'returned' || progress.phase === 'saving') return '正在保存图片'
  if (progress.phase === 'rendering_long') return '高质量生成中'
  if (progress.phase === 'rendering') return '正在绘制'
  if (progress.phase === 'queued') return '排队与构图'
  return '已提交请求'
})

function isImageAttachment(item: Attachment) {
  return item.mimeSniffed?.startsWith('image/')
}

function attachmentPreviewUrl(id: string) {
  return `/api/attachments/${id}/preview`
}

function attachmentImageSrc(attachment: Attachment) {
  return attachment.previewDataUrl || attachmentPreviewUrl(attachment.id)
}

function attachmentDownloadUrl(id: string) {
  return `/api/attachments/${id}/download`
}

function generatedImageFrameFromSize(size?: string): CSSProperties {
  const parsed = parseImageSize(size)
  return imageCardLayout(parsed.width, parsed.height).style
}

function parseImageSize(size?: string) {
  if (!size || size === 'auto') return { width: 1024, height: 1024 }
  const match = size.match(/^(\d+)x(\d+)$/)
  if (!match) return { width: 1024, height: 1024 }
  return {
    width: Number(match[1]) || 1024,
    height: Number(match[2]) || 1024
  }
}

function imageCardLayout(width: number, height: number) {
  const naturalWidth = Math.max(1, width)
  const naturalHeight = Math.max(1, height)
  const aspectRatio = naturalWidth / naturalHeight
  const area = naturalWidth * naturalHeight
  const longEdge = Math.max(naturalWidth, naturalHeight)

  const tier =
    longEdge <= 260 || area <= 45_000
      ? { className: 'is-small', maxWidth: 156, maxHeight: 126 }
      : aspectRatio >= 1.55 || longEdge >= 700 || area >= 240_000
        ? { className: 'is-large', maxWidth: 330, maxHeight: 230 }
        : { className: 'is-medium', maxWidth: 230, maxHeight: 178 }

  let scale = Math.min(tier.maxWidth / naturalWidth, tier.maxHeight / naturalHeight, 1)
  let displayWidth = Math.round(naturalWidth * scale)
  let displayHeight = Math.round(naturalHeight * scale)

  if (displayWidth < 96 && displayHeight * (96 / displayWidth) <= tier.maxHeight) {
    scale *= 96 / displayWidth
    displayWidth = Math.round(naturalWidth * scale)
    displayHeight = Math.round(naturalHeight * scale)
  }

  if (displayHeight < 72 && displayWidth * (72 / displayHeight) <= tier.maxWidth) {
    scale *= 72 / displayHeight
    displayWidth = Math.round(naturalWidth * scale)
    displayHeight = Math.round(naturalHeight * scale)
  }

  return {
    className: tier.className,
    style: {
      width: `${displayWidth}px`,
      height: `${displayHeight}px`
    } as CSSProperties
  }
}

function handleImageLoad(attachment: Attachment, event: Event) {
  if (props.message.role === 'assistant' && props.message.generatedImageSize) return
  const image = event.target as HTMLImageElement | null
  if (!image?.naturalWidth || !image?.naturalHeight) return
  imageLayouts.value = {
    ...imageLayouts.value,
    [attachment.id]: imageCardLayout(image.naturalWidth, image.naturalHeight)
  }
}

function imageCardClass(attachment: Attachment) {
  if (props.message.role === 'assistant' && props.message.generatedImageSize) return 'is-generated-sized'
  return imageLayouts.value[attachment.id]?.className || 'is-loading'
}

function imageCardStyle(attachment: Attachment): CSSProperties | undefined {
  return imageLayouts.value[attachment.id]?.style || generatedImageFrameFromSize(props.message.generatedImageSize)
}

function attachmentKindLabel(item: Attachment) {
  const filename = item.filename || ''
  const mime = item.mimeSniffed || ''
  const extension = filename.split('.').pop()?.toLowerCase() || ''
  if (mime.includes('pdf') || extension === 'pdf') return 'PDF'
  if (mime.includes('wordprocessingml') || ['doc', 'docx'].includes(extension)) return '文档'
  if (['txt', 'md', 'csv'].includes(extension)) return '文本'
  return '文件'
}

function attachmentKindClass(item: Attachment) {
  const filename = item.filename || ''
  const mime = item.mimeSniffed || ''
  const extension = filename.split('.').pop()?.toLowerCase() || ''
  if (mime.includes('pdf') || extension === 'pdf') return 'kind-pdf'
  if (mime.includes('wordprocessingml') || ['doc', 'docx'].includes(extension)) return 'kind-doc'
  return 'kind-file'
}

async function copyUserMessage() {
  const content = props.message.content
  if (!content.trim()) return

  try {
    await copyText(content)
    copyState.value = 'copied'
  } catch {
    copyState.value = 'failed'
  }

  if (copyStateTimer !== null) window.clearTimeout(copyStateTimer)
  copyStateTimer = window.setTimeout(() => {
    copyState.value = 'idle'
    copyStateTimer = null
  }, 1200)
}

watch(
  () => props.message.id,
  () => {
    isExpanded.value = false
  }
)

watch(
  () => Boolean(props.message.role === 'assistant' && props.message.status === 'streaming'),
  (active) => {
    if (progressTimer !== null) {
      window.clearInterval(progressTimer)
      progressTimer = null
    }
    if (active) {
      nowMs.value = Date.now()
      progressTimer = window.setInterval(() => {
        nowMs.value = Date.now()
      }, 1000)
    }
  },
  { immediate: true }
)

onUnmounted(() => {
  if (progressTimer !== null) window.clearInterval(progressTimer)
  if (copyStateTimer !== null) window.clearTimeout(copyStateTimer)
})
</script>

<template>
  <article class="chat-message py-3" :data-message-id="message.id">
    <div class="message-row chat-message-row mx-auto flex w-full" :class="message.role === 'user' ? 'message-row-user justify-end' : 'justify-start'">
      <div :class="message.role === 'user' ? 'message-user-stack' : 'message-assistant'">
        <template v-if="message.role === 'user'">
          <div v-if="imageAttachments.length" class="message-image-attachments">
            <button
              v-for="attachment in imageAttachments"
              :key="attachment.id"
              class="message-image-card"
              :class="imageCardClass(attachment)"
              :style="imageCardStyle(attachment)"
              type="button"
              :title="`查看图片：${attachment.filename}`"
              :aria-label="`查看图片：${attachment.filename}`"
              @click="emit('previewAttachment', attachment)"
            >
              <img :src="attachmentImageSrc(attachment)" :alt="attachment.filename" @load="handleImageLoad(attachment, $event)" />
            </button>
          </div>
          <div v-if="message.content.trim()" class="message-user-content">
            <div class="message-bubble message-user">
              <div class="message-collapsible" :class="collapsibleClasses">
                <button
                  v-if="canCollapse"
                  class="message-collapse-button"
                  type="button"
                  :aria-expanded="isExpanded"
                  :title="collapseButtonLabel"
                  @click="isExpanded = !isExpanded"
                >
                  <ChevronDown v-if="isCollapsed" :size="16" />
                  <ChevronUp v-else :size="16" />
                </button>
                <div class="message-collapsible-content">
                  <div class="plain-user-message">{{ message.content }}</div>
                </div>
              </div>
            </div>
            <div class="message-user-actions" aria-live="polite">
              <button
                class="message-copy-button"
                type="button"
                :class="{ 'is-copied': copyState === 'copied', 'is-failed': copyState === 'failed' }"
                :title="copyButtonLabel"
                :aria-label="copyButtonLabel"
                @click="copyUserMessage"
              >
                <Check v-if="copyState === 'copied'" :size="16" />
                <Copy v-else :size="16" />
              </button>
            </div>
          </div>
          <div v-if="fileAttachments.length" class="message-attachments">
            <button
              v-for="attachment in fileAttachments"
              :key="attachment.id"
              class="message-file-card"
              type="button"
              @click="emit('previewAttachment', attachment)"
            >
              <span class="message-file-icon" :class="attachmentKindClass(attachment)"><FileText :size="21" /></span>
              <span class="message-file-meta">
                <strong>{{ attachment.filename }}</strong>
                <em>{{ attachmentKindLabel(attachment) }}</em>
              </span>
            </button>
          </div>
        </template>
        <div v-else-if="showGeneratedImageProgress" class="generated-image-progress">
          <button
            class="generated-image-preview"
            :class="{ 'is-saving': message.imageProgress?.phase === 'saving' || message.imageProgress?.phase === 'returned' }"
            :style="generatedImageFrameStyle"
            type="button"
            disabled
          >
            <img v-if="generatedImageProgressSrc" :src="generatedImageProgressSrc" alt="生成中的图片" />
            <span v-else class="generated-image-placeholder">
              <span class="generated-image-loader" aria-hidden="true"></span>
              正在生成图片
            </span>
          </button>
          <div class="generated-image-meta">
            <strong>{{ generatedImageProgressLabel }}</strong>
            <span>{{ message.imageProgress?.detail || '正在等待图像模型返回结果。' }}</span>
            <em v-if="generatedImageElapsed">已等待 {{ generatedImageElapsed }}</em>
          </div>
        </div>
        <div v-else-if="imageAttachments.length" class="message-generated-images">
          <div v-for="attachment in imageAttachments" :key="attachment.id" class="message-generated-image-stack">
            <button
              class="generated-image-final-card"
              type="button"
              :title="`查看图片：${attachment.filename}`"
              :aria-label="`查看图片：${attachment.filename}`"
              :class="imageCardClass(attachment)"
              :style="imageCardStyle(attachment)"
              @click="emit('previewAttachment', attachment)"
            >
              <img :src="attachmentImageSrc(attachment)" :alt="attachment.filename" @load="handleImageLoad(attachment, $event)" />
            </button>
            <a class="generated-image-download" :href="attachmentDownloadUrl(attachment.id)" target="_blank" rel="noreferrer">
              <Download :size="15" />
              下载
            </a>
          </div>
        </div>
        <div v-else-if="showThinkingPanel" class="thinking-panel">
          <div class="thinking-label">正在思考</div>
          <div class="thinking-bars" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div v-if="streamingElapsed" class="thinking-elapsed">思考中 {{ streamingElapsed }}</div>
        </div>
        <div v-else-if="emptyAssistantFailureText" class="message-status-text">
          {{ emptyAssistantFailureText }}
        </div>
        <template v-else>
          <div v-if="finalElapsed" class="thinking-final-elapsed">思考用时 {{ finalElapsed }}</div>
          <div class="message-collapsible" :class="collapsibleClasses">
            <button
              v-if="canCollapse"
              class="message-collapse-button"
              type="button"
              :aria-expanded="isExpanded"
              :title="collapseButtonLabel"
              @click="isExpanded = !isExpanded"
            >
              <ChevronDown v-if="isCollapsed" :size="16" />
              <ChevronUp v-else :size="16" />
            </button>
            <div class="message-collapsible-content">
              <div v-if="shouldRenderStreamingPlainText" class="streaming-plain-message">{{ message.content }}</div>
              <MarkdownMessage v-else :content="message.content" />
              <span v-if="message.status === 'streaming'" class="typing-cursor" />
            </div>
          </div>
        </template>
      </div>
    </div>
  </article>
</template>
