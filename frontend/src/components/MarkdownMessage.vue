<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import type { WebSearchSource } from '../types'
import { copyText } from '../utils/clipboard'
import { renderMarkdown, renderMarkdownCached } from '../utils/markdown'

const props = defineProps<{ content: string; live?: boolean; citationSources?: WebSearchSource[] }>()
const emit = defineEmits<{ citationClick: [index: number] }>()
const rendered = ref(renderSource(props.content))

let renderTimer: number | null = null
let renderFrame: number | null = null

function renderSource(content: string) {
  const html = props.live ? renderMarkdown(content) : renderMarkdownCached(content)
  return injectCitationButtons(html)
}

function injectCitationButtons(html: string) {
  const allowed = new Set((props.citationSources || []).map((source) => Number(source.index)).filter((index) => Number.isInteger(index) && index > 0))
  if (!allowed.size || !/\[\[\d{1,2}\]\]/.test(html) || typeof document === 'undefined') return html
  const container = document.createElement('div')
  container.innerHTML = html
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement
      if (!parent || !node.textContent || !/\[\[\d{1,2}\]\]/.test(node.textContent)) return NodeFilter.FILTER_REJECT
      if (parent.closest('pre, code, a, button')) return NodeFilter.FILTER_REJECT
      return NodeFilter.FILTER_ACCEPT
    }
  })
  const nodes: Text[] = []
  while (walker.nextNode()) nodes.push(walker.currentNode as Text)
  for (const node of nodes) {
    const text = node.textContent || ''
    const fragment = document.createDocumentFragment()
    let lastIndex = 0
    for (const match of text.matchAll(/\[\[(\d{1,2})\]\]/g)) {
      const start = match.index || 0
      const index = Number(match[1])
      if (!allowed.has(index)) continue
      if (start > lastIndex) fragment.appendChild(document.createTextNode(text.slice(lastIndex, start)))
      const button = document.createElement('button')
      button.type = 'button'
      button.className = 'citation-button'
      button.dataset.citationIndex = String(index)
      button.title = `打开来源 ${index}`
      button.setAttribute('aria-label', `打开来源 ${index}`)
      button.textContent = String(index)
      fragment.appendChild(button)
      lastIndex = start + match[0].length
    }
    if (lastIndex === 0) continue
    if (lastIndex < text.length) fragment.appendChild(document.createTextNode(text.slice(lastIndex)))
    node.parentNode?.replaceChild(fragment, node)
  }
  return container.innerHTML
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
  () => [props.content, props.live, props.citationSources?.map((source) => source.index).join(',') || ''] as const,
  ([content]) => {
    scheduleRender(content, props.live)
  }
)

onBeforeUnmount(() => {
  clearScheduledRender()
})

async function handleClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const citationButton = target?.closest<HTMLButtonElement>('.citation-button')
  if (citationButton) {
    const index = Number(citationButton.dataset.citationIndex)
    if (Number.isInteger(index) && index > 0) emit('citationClick', index)
    return
  }
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
