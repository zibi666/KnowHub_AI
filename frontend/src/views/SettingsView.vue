<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiFetch, readCookie } from '../api/client'
import AppSelect from '../components/AppSelect.vue'
import { useAuthStore } from '../stores/auth'
import type { ImageGenerationSettings, User } from '../types'

const auth = useAuthStore()
const router = useRouter()

const username = ref(auth.user?.username || '')
const usernamePassword = ref('')
const oldPassword = ref('')
const newPassword = ref('')
const notice = ref('')
const error = ref('')
const avatarUploading = ref(false)
const imageSettingsLoading = ref(false)
const imageSettingsSaving = ref(false)
const imageSettings = ref<ImageGenerationSettings>({
  size: 'auto',
  quality: 'auto',
  background: 'auto',
  outputFormat: 'png',
  outputCompression: 100,
  moderation: 'auto'
})
const imageSizeOptions = [
  { value: '1024x1024', label: '1024 x 1024' },
  { value: '1024x1536', label: '1024 x 1536' },
  { value: '1536x1024', label: '1536 x 1024' },
  { value: 'auto', label: '自动' }
]
const imageQualityOptions = [
  { value: 'high', label: '高' },
  { value: 'medium', label: '中' },
  { value: 'low', label: '低' },
  { value: 'auto', label: '自动' }
]
const imageBackgroundOptions = [
  { value: 'auto', label: '自动' },
  { value: 'opaque', label: '不透明' },
  { value: 'transparent', label: '透明' }
]
const imageFormatOptions = [
  { value: 'png', label: 'PNG' },
  { value: 'jpeg', label: 'JPEG' },
  { value: 'webp', label: 'WebP' }
]
const imageModerationOptions = [
  { value: 'auto', label: '自动' },
  { value: 'low', label: '低' }
]

function resetMessages() {
  notice.value = ''
  error.value = ''
}

function normalizeImageSettings(data: any): ImageGenerationSettings {
  return {
    size: data?.size || 'auto',
    quality: data?.quality || 'auto',
    background: data?.background || 'auto',
    outputFormat: data?.outputFormat || data?.output_format || 'png',
    outputCompression: Number(data?.outputCompression ?? data?.output_compression ?? 100),
    moderation: data?.moderation || 'auto'
  }
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

async function uploadAvatar(event: Event) {
  resetMessages()
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  avatarUploading.value = true
  try {
    const form = new FormData()
    form.append('file', file)
    const csrf = readCookie('csrf_token')
    const response = await fetch('/api/settings/avatar', {
      method: 'POST',
      body: form,
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : undefined
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new Error(payload?.detail?.message || '头像上传失败')
    }
    auth.user = await response.json()
    notice.value = '头像已更新'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '头像上传失败'
  } finally {
    avatarUploading.value = false
    input.value = ''
  }
}

async function deleteAvatar() {
  resetMessages()
  avatarUploading.value = true
  try {
    auth.user = await apiFetch<User>('/settings/avatar', { method: 'DELETE' })
    notice.value = '头像已删除'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '头像删除失败'
  } finally {
    avatarUploading.value = false
  }
}

async function loadImageSettings() {
  imageSettingsLoading.value = true
  try {
    imageSettings.value = normalizeImageSettings(await apiFetch('/settings/image-generation'))
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载图像生成设置失败'
  } finally {
    imageSettingsLoading.value = false
  }
}

async function saveImageSettings() {
  resetMessages()
  imageSettingsSaving.value = true
  try {
    imageSettings.value = normalizeImageSettings(await apiFetch('/settings/image-generation', {
      method: 'PATCH',
      body: JSON.stringify(imageSettings.value)
    }))
    notice.value = '图像生成设置已保存'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存图像生成设置失败'
  } finally {
    imageSettingsSaving.value = false
  }
}

onMounted(loadImageSettings)
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

      <form class="app-card rounded-lg p-5 space-y-3" @submit.prevent="saveImageSettings">
        <div>
          <h2 class="font-semibold">图像生成</h2>
          <p class="app-muted text-sm mt-1">配置 image-2、image-1.5、image-1 的默认生成参数。</p>
        </div>
        <div v-if="imageSettingsLoading" class="app-muted text-sm">正在加载图像设置...</div>
        <div v-else class="grid gap-3 md:grid-cols-2">
          <label class="space-y-1">
            <span class="app-muted text-sm">尺寸</span>
            <AppSelect v-model="imageSettings.size" class="app-select-compact" :options="imageSizeOptions" />
          </label>
          <label class="space-y-1">
            <span class="app-muted text-sm">质量</span>
            <AppSelect v-model="imageSettings.quality" class="app-select-compact" :options="imageQualityOptions" />
          </label>
          <label class="space-y-1">
            <span class="app-muted text-sm">背景</span>
            <AppSelect v-model="imageSettings.background" class="app-select-compact" :options="imageBackgroundOptions" />
          </label>
          <label class="space-y-1">
            <span class="app-muted text-sm">格式</span>
            <AppSelect v-model="imageSettings.outputFormat" class="app-select-compact" :options="imageFormatOptions" />
          </label>
          <label class="space-y-1">
            <span class="app-muted text-sm">压缩质量</span>
            <input
              v-model.number="imageSettings.outputCompression"
              class="app-input w-full rounded-md px-3 py-2"
              type="number"
              min="0"
              max="100"
              :disabled="imageSettings.outputFormat === 'png'"
            />
          </label>
          <label class="space-y-1">
            <span class="app-muted text-sm">审核强度</span>
            <AppSelect v-model="imageSettings.moderation" class="app-select-compact" :options="imageModerationOptions" />
          </label>
        </div>
        <button class="app-primary-button rounded-md px-4 py-2" type="submit" :disabled="imageSettingsLoading || imageSettingsSaving">
          {{ imageSettingsSaving ? '保存中...' : '保存图像设置' }}
        </button>
      </form>

      <div class="app-card rounded-lg p-5 space-y-3">
        <div>
          <h2 class="font-semibold">更换头像</h2>
          <p class="app-muted text-sm mt-1">支持 PNG、JPG、WebP，最大 2MB，会自动裁切为方形头像。</p>
        </div>
        <div class="settings-avatar-editor">
          <div class="settings-avatar-preview" aria-hidden="true">
            <img v-if="auth.user?.avatarUrl" :src="auth.user.avatarUrl" :alt="auth.user.username" />
            <span v-else>{{ auth.user?.username?.slice(0, 1).toUpperCase() || 'U' }}</span>
          </div>
          <div class="settings-avatar-actions">
            <label class="app-primary-button rounded-md px-4 py-2">
              {{ avatarUploading ? '上传中...' : '上传头像' }}
              <input class="hidden" type="file" accept="image/png,image/jpeg,image/webp" :disabled="avatarUploading" @change="uploadAvatar" />
            </label>
            <button
              class="app-secondary-button rounded-md px-4 py-2"
              type="button"
              :disabled="avatarUploading || !auth.user?.avatarUrl"
              @click="deleteAvatar"
            >
              删除头像
            </button>
          </div>
        </div>
      </div>

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
          <p class="app-muted text-sm mt-1">密钥已经拆到独立页面，可添加多个密钥、按分组切换当前使用密钥并选择分组。</p>
        </div>
        <button class="app-primary-button rounded-md px-4 py-2" type="button" @click="router.push('/keys')">进入密钥管理</button>
      </div>
    </section>
  </main>
</template>
