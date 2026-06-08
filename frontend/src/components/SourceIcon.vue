<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { WebSearchSource } from '../types'
import { sourceIconUrls, sourceLabel, sourceOpenUrl, sourceSiteName } from '../utils/sources'

const props = defineProps<{ source: WebSearchSource }>()

const successfulIconBySource = new Map<string, string>()
const failedIconUrls = new Set<string>()

const attempt = ref(0)
const sourceKey = computed(() => sourceOpenUrl(props.source) || props.source.url || String(props.source.index))
const iconUrls = computed(() => {
  const cached = successfulIconBySource.get(sourceKey.value)
  const candidates = sourceIconUrls(props.source).filter((url) => !failedIconUrls.has(url))
  return cached ? [cached, ...candidates.filter((url) => url !== cached)] : candidates
})
const iconUrl = computed(() => iconUrls.value[attempt.value] || '')
const label = computed(() => sourceLabel(props.source))
const siteName = computed(() => sourceSiteName(props.source))

watch(
  () => props.source.url,
  () => {
    attempt.value = 0
  }
)

function handleLoad() {
  if (iconUrl.value) successfulIconBySource.set(sourceKey.value, iconUrl.value)
}

function handleError() {
  if (iconUrl.value) failedIconUrls.add(iconUrl.value)
  attempt.value += 1
}
</script>

<template>
  <span class="source-icon" :title="siteName">
    <img v-if="iconUrl" :src="iconUrl" alt="" @load="handleLoad" @error="handleError" />
    <span v-else>{{ label }}</span>
  </span>
</template>
