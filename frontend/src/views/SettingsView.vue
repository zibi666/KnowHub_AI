<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiFetch } from '../api/client'
import { useAuthStore } from '../stores/auth'
import type { User } from '../types'

const auth = useAuthStore()
const router = useRouter()

const username = ref(auth.user?.username || '')
const usernamePassword = ref('')
const oldPassword = ref('')
const newPassword = ref('')
const notice = ref('')
const error = ref('')

function resetMessages() {
  notice.value = ''
  error.value = ''
}

async function saveProfile() {
  resetMessages()
  try {
    const user = await apiFetch<User>('/settings/profile', {
      method: 'PATCH',
      body: JSON.stringify({ username: username.value, password: usernamePassword.value })
    })
    auth.user = user
    usernamePassword.value = ''
    notice.value = '用户名已更新'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  }
}

async function savePassword() {
  resetMessages()
  try {
    await auth.changePassword(oldPassword.value, newPassword.value)
    oldPassword.value = ''
    newPassword.value = ''
    notice.value = '密码已更新'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  }
}
</script>

<template>
  <main class="settings-page app-page">
    <header class="app-header h-14 flex items-center px-5">
      <button class="app-secondary-button text-sm rounded-md px-3 py-1" @click="router.push('/')">返回</button>
      <h1 class="ml-4 font-semibold">账号设置</h1>
      <span class="ml-auto app-muted text-sm">{{ auth.user?.role === 'admin' ? '管理员' : '用户' }}</span>
    </header>

    <section class="max-w-3xl mx-auto p-5 space-y-5">
      <div v-if="notice" class="app-card rounded-lg p-3 text-sm text-green-700">{{ notice }}</div>
      <div v-if="error" class="app-card rounded-lg p-3 text-sm text-red-600">{{ error }}</div>

      <form class="app-card rounded-lg p-5 space-y-3" @submit.prevent="saveProfile">
        <div>
          <h2 class="font-semibold">修改用户名</h2>
          <p class="app-muted text-sm mt-1">不需要输入旧用户名，只需要用当前密码确认是本人操作。</p>
        </div>
        <input v-model="username" class="app-input w-full rounded-md px-3 py-2" placeholder="新用户名" />
        <input v-model="usernamePassword" class="app-input w-full rounded-md px-3 py-2" type="password" placeholder="当前密码" />
        <button class="app-primary-button rounded-md px-4 py-2" type="submit">保存用户名</button>
      </form>

      <form class="app-card rounded-lg p-5 space-y-3" @submit.prevent="savePassword">
        <div>
          <h2 class="font-semibold">修改密码</h2>
          <p class="app-muted text-sm mt-1">修改后会保留当前登录，其它会话失效。</p>
        </div>
        <input v-model="oldPassword" class="app-input w-full rounded-md px-3 py-2" type="password" placeholder="当前密码" />
        <input v-model="newPassword" class="app-input w-full rounded-md px-3 py-2" type="password" placeholder="新密码" />
        <button class="app-primary-button rounded-md px-4 py-2" type="submit">保存密码</button>
      </form>

      <div class="app-card rounded-lg p-5 space-y-3">
        <div>
          <h2 class="font-semibold">API Key 管理</h2>
          <p class="app-muted text-sm mt-1">密钥已经拆到独立页面，可添加多个密钥、切换当前使用密钥并选择分组。</p>
        </div>
        <button class="app-primary-button rounded-md px-4 py-2" type="button" @click="router.push('/keys')">进入密钥管理</button>
      </div>
    </section>
  </main>
</template>
