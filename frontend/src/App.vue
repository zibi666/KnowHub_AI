<script setup lang="ts">
import { onMounted, ref } from 'vue'

const theme = ref<'dark' | 'light'>('dark')

function syncTheme() {
  const stored = window.localStorage.getItem('private-gpt-theme')
  theme.value = stored === 'light' ? 'light' : 'dark'
}

onMounted(() => {
  syncTheme()
  window.addEventListener('storage', syncTheme)
})
</script>

<template>
  <div class="app-root" :class="theme === 'dark' ? 'theme-dark' : 'theme-light'">
    <RouterView />
  </div>
</template>
