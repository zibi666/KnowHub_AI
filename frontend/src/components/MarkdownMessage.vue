<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { renderMarkdownCached } from '../utils/markdown'

const props = defineProps<{ content: string }>()
const rendered = ref(renderMarkdownCached(props.content))

let renderTimer: number | null = null

function renderNow(content: string) {
  rendered.value = renderMarkdownCached(content)
}

watch(
  () => props.content,
  (content) => {
    if (renderTimer !== null) window.clearTimeout(renderTimer)
    renderTimer = window.setTimeout(() => {
      renderTimer = null
      renderNow(content)
    }, 80)
  }
)

onBeforeUnmount(() => {
  if (renderTimer !== null) window.clearTimeout(renderTimer)
})

async function handleClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const button = target?.closest<HTMLButtonElement>('.code-copy-button')
  if (!button) return

  const block = button.closest<HTMLElement>('.code-block')
  const encoded = block?.dataset.code
  if (!encoded) return

  await navigator.clipboard.writeText(decodeURIComponent(encoded))
  const oldText = button.textContent || '复制'
  button.textContent = '已复制'
  window.setTimeout(() => {
    button.textContent = oldText
  }, 1200)
}
</script>

<template>
  <div class="markdown-body max-w-none leading-7" @click="handleClick" v-html="rendered" />
</template>
