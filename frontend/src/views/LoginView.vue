<script setup lang="ts">
import { computed, ref } from 'vue'
import { KeyRound, Lock, User } from 'lucide-vue-next'
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

const subtitle = computed(() => {
  if (mode.value === 'first') return '绑定模型 API Key'
  if (mode.value === 'password') return '修改临时密码'
  return '登录你的账户'
})

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
  <main class="login-page">
    <section class="login-card">
      <div class="login-brand">
        <img class="login-brand-icon" src="/brand/knowhub-icon.png" alt="KnowHub" />
        <h1 class="login-brand-name">KnowHub</h1>
      </div>
      <p class="login-subtitle">{{ subtitle }}</p>
      <form @submit.prevent="submit">
        <div class="login-field">
          <span class="login-field-icon"><User :size="18" /></span>
          <input v-model="username" class="app-input" placeholder="用户名" autocomplete="username" />
        </div>
        <div class="login-field">
          <span class="login-field-icon"><Lock :size="18" /></span>
          <input v-model="password" class="app-input" type="password" placeholder="密码" autocomplete="current-password" />
        </div>
        <div v-if="mode === 'first'" class="login-field">
          <span class="login-field-icon"><KeyRound :size="18" /></span>
          <input v-model="apiKey" class="app-input" type="password" placeholder="API Key" autocomplete="off" />
        </div>
        <div v-if="mode === 'password'" class="login-field">
          <span class="login-field-icon"><Lock :size="18" /></span>
          <input v-model="newPassword" class="app-input" type="password" placeholder="新密码" autocomplete="new-password" />
        </div>
        <p v-if="error" class="login-error">{{ error }}</p>
        <button class="login-submit" type="submit">
          {{ mode === 'first' ? '绑定并登录' : mode === 'password' ? '修改密码' : '登录' }}
        </button>
      </form>
    </section>
  </main>
</template>
