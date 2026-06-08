<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { X } from 'lucide-vue-next'
import { apiFetch } from '../api/client'
import AppSelect from '../components/AppSelect.vue'
import { useAuthStore } from '../stores/auth'
import type { ApiKeyEntry, ApiKeyGroup, User, UserQuota } from '../types'
import { copyText } from '../utils/clipboard'

const router = useRouter()
const auth = useAuthStore()
const users = ref<User[]>([])
const analytics = ref<any>(null)
const deadLetters = ref<any[]>([])
const username = ref('')
const loginPassword = ref('')
const cleanupKind = ref('unused_image_attachments_7d')
const cleanupPreview = ref<any>(null)
const cleanupConfirming = ref(false)
const deleteConfirmUser = ref<User | null>(null)
const deleteConfirming = ref(false)
const reasoningModels = ref('')
const notice = ref('')
const error = ref('')
let noticeTimer: ReturnType<typeof window.setTimeout> | null = null
const userDrafts = ref<Record<string, { username: string; role: string; status: string; password: string }>>({})
const quotaDrafts = ref<Record<string, { uploadRateLimitPerHour: number }>>({})
const groups = ref<ApiKeyGroup[]>([])
const selectedKeyUser = ref<User | null>(null)
const selectedUserKeys = ref<ApiKeyEntry[]>([])
const adminKeyDraft = ref({ name: '默认密钥', apiKey: '', groupId: '', makeActive: true })
const keyDrafts = ref<Record<string, { name: string; groupId: string }>>({})

const metricLabels: Record<string, string> = {
  users: '用户数',
  conversations: '会话数',
  messages: '消息数',
  attachments: '附件数',
  totalTokens: '总 Token'
}

const cleanupKindLabels: Record<string, string> = {
  unused_image_attachments_7d: '7天未使用图片',
  pending_cos: '未提交上传',
  soft_deleted_attachments: '软删除附件',
  expired_attachments: '过期附件',
  orphan_attachments: '孤儿附件'
}

const cleanupKindOptions = [
  {
    value: 'unused_image_attachments_7d',
    label: '7天未使用图片',
    hint: '删除超过 7 天且没有挂到任何聊天消息里的图片'
  },
  { value: 'pending_cos', label: '未提交上传' },
  { value: 'soft_deleted_attachments', label: '软删除附件' },
  { value: 'expired_attachments', label: '过期附件' },
  { value: 'orphan_attachments', label: '孤儿附件' }
]

const userRoleOptions = [
  { value: 'user', label: '用户' },
  { value: 'admin', label: '管理员' }
]

const userStatusOptions = [
  { value: 'active', label: '启用' },
  { value: 'disabled', label: '禁用' }
]

const defaultChatGroup = computed(() => groups.value.find((group) => group.purpose === 'chat') || groups.value[0] || null)
const groupOptions = computed(() =>
  groups.value.map((group) => ({ value: group.id, label: group.name, hint: group.description || undefined }))
)

const visibleAnalytics = computed(() => {
  const source = analytics.value || {}
  return Object.fromEntries(Object.entries(source).filter(([key]) => key !== 'estimatedCosBytes'))
})

function editableUserStatus(status: string) {
  return status === 'disabled' ? 'disabled' : 'active'
}

function showNotice(message: string) {
  error.value = ''
  notice.value = message
  if (noticeTimer) window.clearTimeout(noticeTimer)
  noticeTimer = window.setTimeout(() => {
    notice.value = ''
    noticeTimer = null
  }, 2600)
}

function showError(message: string) {
  notice.value = ''
  error.value = message
}

function draftFor(user: User) {
  if (!userDrafts.value[user.id]) {
    userDrafts.value[user.id] = {
      username: user.username,
      role: user.role,
      status: editableUserStatus(user.status),
      password: ''
    }
  }
  return userDrafts.value[user.id]
}

function keyDraftFor(key: ApiKeyEntry) {
  if (!keyDrafts.value[key.id]) {
    keyDrafts.value[key.id] = { name: key.name, groupId: key.groupId || defaultChatGroup.value?.id || '' }
  }
  return keyDrafts.value[key.id]
}

function quotaDraftFor(user: User) {
  if (!quotaDrafts.value[user.id]) {
    quotaDrafts.value[user.id] = { uploadRateLimitPerHour: 0 }
  }
  return quotaDrafts.value[user.id]
}

function setCleanupKind(value: string | number) {
  cleanupKind.value = String(value)
}

function setUserRole(user: User, value: string | number) {
  draftFor(user).role = String(value)
}

function setUserStatus(user: User, value: string | number) {
  draftFor(user).status = String(value)
}

function setAdminKeyGroup(value: string | number) {
  adminKeyDraft.value.groupId = String(value)
}

function setKeyDraftGroup(key: ApiKeyEntry, value: string | number) {
  keyDraftFor(key).groupId = String(value)
}

async function load() {
  users.value = await apiFetch<User[]>('/admin/users')
  userDrafts.value = Object.fromEntries(
    users.value.map((user) => [
      user.id,
      {
        username: user.username,
        role: user.role,
        status: editableUserStatus(user.status),
        password: ''
      }
    ])
  )
  await loadQuotaDrafts()
  analytics.value = await apiFetch('/admin/analytics')
  deadLetters.value = await apiFetch<any[]>('/admin/dead-letters')
  const reasoning = await apiFetch<{ models: string[] }>('/admin/settings/reasoning-models')
  reasoningModels.value = reasoning.models.join(', ')
  groups.value = await apiFetch<ApiKeyGroup[]>('/api-key-groups')
  if (!adminKeyDraft.value.groupId || !groups.value.some((group) => group.id === adminKeyDraft.value.groupId)) {
    adminKeyDraft.value.groupId = defaultChatGroup.value?.id || ''
  }
  if (selectedKeyUser.value) {
    const refreshed = users.value.find((user) => user.id === selectedKeyUser.value?.id)
    selectedKeyUser.value = refreshed || null
    if (selectedKeyUser.value) await loadSelectedUserKeys(selectedKeyUser.value)
  }
}

async function loadQuotaDrafts() {
  const entries = await Promise.all(
    users.value.map(async (user) => {
      const quota = await apiFetch<UserQuota>(`/admin/users/${user.id}/quotas`)
      return [user.id, { uploadRateLimitPerHour: quota.uploadRateLimitPerHour }] as const
    })
  )
  quotaDrafts.value = Object.fromEntries(entries)
}

async function createUser() {
  await apiFetch('/admin/users', {
    method: 'POST',
    body: JSON.stringify({ username: username.value, password: loginPassword.value, role: 'user' })
  })
  username.value = ''
  loginPassword.value = ''
  showNotice('用户已创建')
  await load()
}

async function saveUser(user: User) {
  const draft = userDrafts.value[user.id]
  const quotaDraft = quotaDraftFor(user)
  if (!draft) return
  await apiFetch(`/admin/users/${user.id}`, {
    method: 'PATCH',
    body: JSON.stringify({
      username: draft.username,
      role: draft.role,
      status: draft.status,
      password: draft.password || undefined
    })
  })
  await apiFetch(`/admin/users/${user.id}/quotas`, {
    method: 'PATCH',
    body: JSON.stringify({
      uploadRateLimitPerHour: Math.max(0, Number(quotaDraft.uploadRateLimitPerHour) || 0)
    })
  })
  showNotice('用户信息已保存')
  await load()
}

function openDeleteUserConfirm(user: User) {
  if (user.id === auth.user?.id) return
  deleteConfirmUser.value = user
}

function closeDeleteUserConfirm() {
  if (!deleteConfirming.value) deleteConfirmUser.value = null
}

async function confirmDeleteUser() {
  if (!deleteConfirmUser.value || deleteConfirming.value) return
  deleteConfirming.value = true
  try {
    await apiFetch(`/admin/users/${deleteConfirmUser.value.id}`, { method: 'DELETE' })
    showNotice(`用户 ${deleteConfirmUser.value.username} 已删除`)
    if (selectedKeyUser.value?.id === deleteConfirmUser.value.id) {
      selectedKeyUser.value = null
      selectedUserKeys.value = []
    }
    deleteConfirmUser.value = null
    await load()
  } finally {
    deleteConfirming.value = false
  }
}

async function loadSelectedUserKeys(user: User) {
  selectedKeyUser.value = user
  selectedUserKeys.value = await apiFetch<ApiKeyEntry[]>(`/admin/users/${user.id}/api-keys`)
  keyDrafts.value = Object.fromEntries(
    selectedUserKeys.value.map((key) => [key.id, { name: key.name, groupId: key.groupId || defaultChatGroup.value?.id || '' }])
  )
}

async function createAdminKey() {
  if (!selectedKeyUser.value) return
  try {
    await apiFetch<ApiKeyEntry>(`/admin/users/${selectedKeyUser.value.id}/api-keys`, {
      method: 'POST',
      body: JSON.stringify({
        name: adminKeyDraft.value.name,
        apiKey: adminKeyDraft.value.apiKey,
        groupId: adminKeyDraft.value.groupId || defaultChatGroup.value?.id || null,
        makeActive: adminKeyDraft.value.makeActive
      })
    })
    adminKeyDraft.value = { name: '默认密钥', apiKey: '', groupId: defaultChatGroup.value?.id || '', makeActive: true }
    showNotice('用户密钥已添加')
    await load()
  } catch (err) {
    showError(err instanceof Error ? err.message : '添加用户密钥失败')
  }
}

async function saveAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  const draft = keyDraftFor(key)
  await apiFetch<ApiKeyEntry>(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ name: draft.name, groupId: draft.groupId || defaultChatGroup.value?.id || null })
  })
  showNotice('用户密钥已保存')
  await loadSelectedUserKeys(selectedKeyUser.value)
}

async function activateAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  await apiFetch<ApiKeyEntry>(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}/activate`, { method: 'POST' })
  showNotice('已切换该用户当前密钥')
  await loadSelectedUserKeys(selectedKeyUser.value)
}

async function deleteAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  await apiFetch(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}`, { method: 'DELETE' })
  showNotice('用户密钥已删除')
  await load()
}

async function copyAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  const result = await apiFetch<{ apiKey: string }>(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}/secret`)
  await copyText(result.apiKey)
  showNotice('密钥已复制')
}

async function previewCleanup() {
  cleanupPreview.value = await apiFetch('/admin/storage/cleanup/preview', {
    method: 'POST',
    body: JSON.stringify({ kind: cleanupKind.value })
  })
}

async function confirmCleanup() {
  if (!cleanupPreview.value || cleanupConfirming.value) return
  cleanupConfirming.value = true
  try {
    const result = await apiFetch<{ result: Record<string, unknown> }>('/admin/storage/cleanup/confirm', {
      method: 'POST',
      body: JSON.stringify({
        jobId: cleanupPreview.value.jobId,
        confirmToken: cleanupPreview.value.confirmToken
      })
    })
    showNotice(`清理完成：${JSON.stringify(result.result)}`)
    cleanupPreview.value = null
    await load()
  } finally {
    cleanupConfirming.value = false
  }
}

async function saveReasoningModels() {
  const models = reasoningModels.value.split(',').map((item) => item.trim()).filter(Boolean)
  await apiFetch('/admin/settings/reasoning-models', {
    method: 'PATCH',
    body: JSON.stringify({ models })
  })
  showNotice('Reasoning 模型列表已保存')
}

onMounted(load)
onBeforeUnmount(() => {
  if (noticeTimer) window.clearTimeout(noticeTimer)
})
</script>

<template>
  <main class="admin-page app-page">
    <header class="app-header h-14 flex items-center px-5">
      <button class="app-secondary-button text-sm rounded-md px-3 py-1" @click="router.push('/')">返回</button>
      <h1 class="ml-4 font-semibold">管理后台</h1>
    </header>
    <Transition name="admin-toast">
      <div v-if="notice" class="admin-toast" role="status" aria-live="polite">{{ notice }}</div>
    </Transition>
    <Transition name="admin-toast">
      <div v-if="error" class="admin-toast error" role="alert" aria-live="assertive">{{ error }}</div>
    </Transition>
    <section class="max-w-6xl mx-auto p-5 space-y-5">
      <div class="grid grid-cols-5 gap-3">
        <div v-for="(value, key) in visibleAnalytics" :key="key" class="app-card rounded-lg p-4">
          <div class="app-muted text-xs">{{ metricLabels[String(key)] || key }}</div>
          <div class="text-xl font-semibold">{{ value }}</div>
        </div>
      </div>
      <div class="app-card rounded-lg p-4">
        <h2 class="font-semibold mb-3">创建用户</h2>
        <form class="flex gap-2" @submit.prevent="createUser">
          <input v-model="username" class="app-input rounded-md px-3 py-2" placeholder="用户名" />
          <input v-model="loginPassword" class="app-input rounded-md px-3 py-2" type="password" placeholder="登录密码" />
          <button class="app-primary-button rounded-md px-4">创建</button>
        </form>
      </div>
      <div class="grid grid-cols-2 gap-4">
        <div class="app-card rounded-lg p-4">
          <h2 class="font-semibold mb-3">存储清理</h2>
          <div class="flex gap-2">
            <AppSelect
              v-model="cleanupKind"
              class="app-select-compact min-w-[180px]"
              :options="cleanupKindOptions"
              @change="setCleanupKind"
            />
            <button class="app-secondary-button rounded-md px-3 text-sm" @click="previewCleanup">预览</button>
          </div>
          <div v-if="cleanupPreview" class="app-subtle-panel mt-3 text-sm rounded-md p-3">
            <div>数量：{{ cleanupPreview.preview.count }}</div>
            <div>字节数：{{ cleanupPreview.preview.bytes }}</div>
            <button class="app-primary-button mt-3 rounded-md px-3 py-2 disabled:opacity-50" :disabled="cleanupConfirming" @click="confirmCleanup">
              确认清理
            </button>
          </div>
        </div>
        <div class="app-card rounded-lg p-4">
          <h2 class="font-semibold mb-3">Reasoning 模型</h2>
          <textarea v-model="reasoningModels" class="app-input w-full rounded-md p-3 min-h-24 text-sm" placeholder="model-a, model-b" />
          <button class="app-primary-button mt-2 rounded-md px-3 py-2 text-sm" @click="saveReasoningModels">保存</button>
        </div>
      </div>
      <div class="app-card rounded-lg overflow-visible">
        <table class="w-full text-sm">
          <thead class="app-table-head text-left">
            <tr><th class="p-3">用户名</th><th class="p-3">角色</th><th class="p-3">状态</th><th class="p-3">上传限流/小时</th><th class="p-3">需改密</th><th class="p-3">密钥</th><th class="p-3">操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id" class="app-table-row">
              <td class="p-3">
                <input v-model="draftFor(user).username" class="app-input w-full rounded-md px-2 py-1" />
              </td>
              <td class="p-3">
                <AppSelect
                  :model-value="draftFor(user).role"
                  class="app-select-compact min-w-[96px]"
                  :options="userRoleOptions"
                  @update:model-value="setUserRole(user, $event)"
                />
              </td>
              <td class="p-3">
                <AppSelect
                  :model-value="draftFor(user).status"
                  class="app-select-compact min-w-[104px]"
                  :options="userStatusOptions"
                  @update:model-value="setUserStatus(user, $event)"
                />
              </td>
              <td class="p-3">
                <input
                  v-model.number="quotaDraftFor(user).uploadRateLimitPerHour"
                  class="app-input w-28 rounded-md px-2 py-1"
                  min="0"
                  type="number"
                  title="0 表示不限流"
                />
                <div class="app-muted mt-1 text-[11px]">0 为不限</div>
              </td>
              <td class="p-3">{{ user.mustChangePassword ? '是' : '否' }}</td>
              <td class="p-3">
                <button class="app-secondary-button rounded px-2 py-1" @click="loadSelectedUserKeys(user)">
                  {{ user.hasApiKey ? '管理密钥' : '添加密钥' }}
                </button>
              </td>
              <td class="p-3 min-w-[300px]">
                <div class="flex flex-wrap gap-2">
                  <input v-model="draftFor(user).password" class="app-input rounded-md px-2 py-1 text-xs" type="password" placeholder="新登录密码" />
                  <button class="app-primary-button rounded px-2 py-1" @click="saveUser(user)">保存</button>
                  <button
                    class="admin-danger-button rounded px-2 py-1"
                    :disabled="user.id === auth.user?.id"
                    :title="user.id === auth.user?.id ? '不能删除当前登录账号' : '删除用户'"
                    @click="openDeleteUserConfirm(user)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="selectedKeyUser" class="app-card rounded-lg p-4 space-y-4">
        <div class="flex items-center gap-3">
          <h2 class="font-semibold">{{ selectedKeyUser.username }} 的密钥</h2>
          <button class="app-secondary-button ml-auto rounded-md px-3 py-1 text-sm" @click="selectedKeyUser = null">关闭</button>
        </div>
        <form class="grid gap-2 lg:grid-cols-[1fr_1fr_1fr_1fr_auto]" @submit.prevent="createAdminKey">
          <input v-model="adminKeyDraft.name" class="app-input rounded-md px-3 py-2" placeholder="密钥名称" />
          <AppSelect
            v-model="adminKeyDraft.groupId"
            class="app-select-compact"
            :options="groupOptions"
            @change="setAdminKeyGroup"
          />
          <input v-model="adminKeyDraft.apiKey" class="app-input rounded-md px-3 py-2" type="password" placeholder="API Key" />
          <label class="inline-flex items-center gap-2 text-sm app-muted">
            <input v-model="adminKeyDraft.makeActive" type="checkbox" />
            设为该分组当前
          </label>
          <button class="app-primary-button rounded-md px-4 py-2" type="submit">添加</button>
        </form>
        <table class="w-full text-sm">
          <thead class="app-table-head text-left">
            <tr><th class="p-3">名称</th><th class="p-3">分组</th><th class="p-3">标识</th><th class="p-3">状态</th><th class="p-3">操作</th></tr>
          </thead>
          <tbody>
            <tr v-if="!selectedUserKeys.length"><td class="app-muted p-3" colspan="5">暂无密钥</td></tr>
            <tr v-for="key in selectedUserKeys" :key="key.id" class="app-table-row">
              <td class="p-3"><input v-model="keyDraftFor(key).name" class="app-input w-full rounded-md px-2 py-1" /></td>
              <td class="p-3">
                <AppSelect
                  :model-value="keyDraftFor(key).groupId"
                  class="app-select-compact min-w-[150px]"
                  :options="groupOptions"
                  @update:model-value="setKeyDraftGroup(key, $event)"
                />
              </td>
              <td class="p-3">
                <div class="key-mask">{{ key.maskedKey }}</div>
              </td>
              <td class="p-3">{{ key.isActive ? '当前分组使用' : '备用' }}</td>
              <td class="p-3">
                <div class="flex flex-wrap gap-2">
                  <button class="app-secondary-button rounded px-2 py-1" @click="saveAdminKey(key)">保存</button>
                  <button class="app-secondary-button rounded px-2 py-1" @click="copyAdminKey(key)">复制</button>
                  <button class="app-primary-button rounded px-2 py-1" :disabled="key.isActive" @click="activateAdminKey(key)">切换</button>
                  <button class="app-secondary-button rounded px-2 py-1" @click="deleteAdminKey(key)">删除</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="app-card rounded-lg overflow-visible">
        <div class="p-4 font-semibold">死信消息</div>
        <table class="w-full text-sm">
          <thead class="app-table-head text-left">
            <tr><th class="p-3">类型</th><th class="p-3">用户</th><th class="p-3">消息</th><th class="p-3">错误</th><th class="p-3">创建时间</th></tr>
          </thead>
          <tbody>
            <tr v-if="!deadLetters.length"><td class="app-muted p-3" colspan="5">暂无死信消息</td></tr>
            <tr v-for="item in deadLetters" :key="item.id" class="app-table-row">
              <td class="p-3">{{ cleanupKindLabels[item.kind] || item.kind }}</td>
              <td class="p-3">{{ item.userId }}</td>
              <td class="p-3">{{ item.messageId }}</td>
              <td class="p-3">{{ item.errorSummary }}</td>
              <td class="p-3">{{ item.createdAt }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
    <Transition name="dialog-pop">
      <div v-if="deleteConfirmUser" class="confirm-modal-backdrop" role="presentation" @click.self="closeDeleteUserConfirm">
        <div class="confirm-modal" role="dialog" aria-modal="true" aria-labelledby="delete-user-title">
          <div class="confirm-modal-header">
            <h2 id="delete-user-title">删除用户</h2>
            <button class="confirm-modal-close" type="button" title="关闭" aria-label="关闭" :disabled="deleteConfirming" @click="closeDeleteUserConfirm">
              <X :size="17" />
            </button>
          </div>
          <p>确定要删除用户「{{ deleteConfirmUser.username }}」吗？该用户的会话、附件、密钥和用量记录会一起删除。</p>
          <div class="confirm-modal-actions">
            <button class="confirm-secondary-button" type="button" :disabled="deleteConfirming" @click="closeDeleteUserConfirm">取消</button>
            <button class="confirm-danger-button" type="button" :disabled="deleteConfirming" @click="confirmDeleteUser">
              {{ deleteConfirming ? '删除中...' : '确认删除' }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </main>
</template>
