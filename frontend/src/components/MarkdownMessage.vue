<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { copyText } from '../utils/clipboard'
import { renderMarkdown, renderMarkdownCached } from '../utils/markdown'

const props = defineProps<{ content: string; live?: boolean }>()
const rendered = ref(renderSource(props.content))

let renderTimer: number | null = null
let renderFrame: number | null = null

function renderSource(content: string) {
  return props.live ? renderMarkdown(content) : renderMarkdownCached(content)
}

function renderNow(content: string) {
  rendered.value = renderSource(content)
}

function clearScheduledRender() {
  if (renderTimer !== null) {
    window.clearTimeout(renderTimer)
    renderTimer = null
  }
  if (renderFrame !== null) {
    window.cancelAnimationFrame(renderFrame)
    renderFrame = null
  }
}

function scheduleRender(content: string, live?: boolean) {
  clearScheduledRender()
  if (live) {
    renderFrame = window.requestAnimationFrame(() => {
      renderFrame = null
      renderNow(content)
    })
    return
  }
  renderTimer = window.setTimeout(() => {
    renderTimer = null
    renderNow(content)
  }, 80)
}

watch(
  () => [props.content, props.live] as const,
  ([content]) => {
    scheduleRender(content, props.live)
  }
)

onBeforeUnmount(() => {
  clearScheduledRender()
})

async function handleClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const button = target?.closest<HTMLButtonElement>('.code-copy-button')
  if (!button) return

  const block = button.closest<HTMLElement>('.code-block')
  const encoded = block?.dataset.code
  if (!encoded) return

  const oldText = button.textContent || '复制'
  try {
    await copyText(decodeURIComponent(encoded))
    button.textContent = '已复制'
  } catch {
    button.textContent = '复制失败'
  }

  window.setTimeout(() => {
    button.textContent = oldText
  }, 1200)
}
</script>

<template>
  <div class="markdown-body max-w-none leading-7" @click="handleClick" v-html="rendered" />
</template>
