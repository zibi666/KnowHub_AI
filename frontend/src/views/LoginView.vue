<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const username = ref('')
const password = ref('')
const apiKey = ref('')
const newPassword = ref('')
const mode = ref<'login' | 'first' | 'password'>('login')
const error = ref('')

async function submit() {
  error.value = ''
  try {
    if (mode.value === 'login') {
      const user = await auth.login(username.value, password.value)
      if (user.mustChangePassword) {
        mode.value = 'password'
        return
      }
      await router.push('/')
    } else if (mode.value === 'first') {
      await auth.firstLogin(username.value, password.value, apiKey.value)
      await router.push('/')
    } else {
      await auth.changePassword(password.value, newPassword.value)
      mode.value = 'login'
      error.value = '密码已修改，请重新登录；如未绑定 API Key，登录后会进入绑定流程。'
    }
  } catch (err) {
    if (err instanceof ApiError && err.code === 'KEY_REQUIRED') {
      mode.value = 'first'
      return
    }
    error.value = err instanceof Error ? err.message : '请求失败'
  }
}
</script>

<template>
  <main class="app-page min-h-screen flex items-center justify-center px-4">
    <section class="app-card w-full max-w-md rounded-lg p-6">
      <h1 class="text-2xl font-semibold mb-1">私有 GPT</h1>
      <p class="app-muted text-sm mb-6">
        {{ mode === 'first' ? '绑定模型 API Key' : mode === 'password' ? '修改临时密码' : '登录' }}
      </p>
      <form class="space-y-4" @submit.prevent="submit">
        <input v-model="username" class="app-input w-full rounded-md px-3 py-2" placeholder="用户名" />
        <input v-model="password" class="app-input w-full rounded-md px-3 py-2" type="password" placeholder="密码" />
        <input
          v-if="mode === 'first'"
          v-model="apiKey"
          class="app-input w-full rounded-md px-3 py-2"
          type="password"
          placeholder="API Key"
        />
        <input
          v-if="mode === 'password'"
          v-model="newPassword"
          class="app-input w-full rounded-md px-3 py-2"
          type="password"
          placeholder="新密码"
        />
        <p v-if="error" class="text-sm text-red-600">{{ error }}</p>
        <button class="app-primary-button w-full rounded-md px-4 py-2" type="submit">
          {{ mode === 'first' ? '绑定并登录' : mode === 'password' ? '修改密码' : '登录' }}
        </button>
      </form>
    </section>
  </main>
</template>
