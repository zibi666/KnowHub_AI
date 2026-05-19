<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'
import MarkdownMessage from './MarkdownMessage.vue'
import type { Message } from '../types'

const props = defineProps<{ message: Message }>()

const USER_COLLAPSE_CHARACTER_LIMIT = 120
const USER_COLLAPSE_LINE_LIMIT = 3

const isExpanded = ref(false)
const lineCount = computed(() => props.message.content.split(/\r\n|\n|\r/).length)
const isStreaming = computed(() => props.message.status === 'streaming')
const isUserMessage = computed(() => props.message.role === 'user')
const isLiveDraft = computed(() => isStreaming.value && props.message.id.startsWith('stream-'))
const emptyAssistantFailureText = computed(() => {
  if (props.message.content.trim()) return ''
  if (props.message.status === 'failed_no_output') return '回复生成失败，请重试'
  if (props.message.status === 'failed_partial') return '回复生成中断，请重试'
  if (props.message.status === 'interrupted') return '回复已中断'
  if (props.message.status === 'streaming' && !isLiveDraft.value) return '回复未完成，请重试'
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
const collapsibleClasses = computed(() => ({
  'has-collapse': canCollapse.value,
  'is-collapsed': isCollapsed.value,
  'is-expanded': isExpanded.value,
  'is-user': isUserMessage.value,
  'is-assistant': !isUserMessage.value
}))

watch(
  () => props.message.id,
  () => {
    isExpanded.value = false
  }
)
</script>

<template>
  <article class="chat-message py-3">
    <div class="message-row chat-message-row mx-auto flex w-full" :class="message.role === 'user' ? 'message-row-user justify-end' : 'justify-start'">
      <div :class="message.role === 'user' ? 'message-bubble message-user' : 'message-assistant'">
        <template v-if="message.role === 'user'">
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
        </template>
        <div v-else-if="isLiveDraft && !message.content" class="thinking-panel">
          <div class="thinking-label">正在思考</div>
          <div class="thinking-bars" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
        </div>
        <div v-else-if="emptyAssistantFailureText" class="message-status-text">
          {{ emptyAssistantFailureText }}
        </div>
        <template v-else>
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
              <MarkdownMessage :content="message.content" />
              <span v-if="message.status === 'streaming'" class="typing-cursor" />
            </div>
          </div>
        </template>
      </div>
    </div>
  </article>
</template>
