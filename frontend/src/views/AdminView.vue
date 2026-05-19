<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiFetch } from '../api/client'
import AppSelect from '../components/AppSelect.vue'
import type { ApiKeyEntry, ApiKeyGroup, User } from '../types'

const router = useRouter()
const users = ref<User[]>([])
const analytics = ref<any>(null)
const deadLetters = ref<any[]>([])
const username = ref('')
const loginPassword = ref('')
const cleanupKind = ref('pending_cos')
const cleanupPreview = ref<any>(null)
const cleanupConfirming = ref(false)
const reasoningModels = ref('')
const notice = ref('')
const userDrafts = ref<Record<string, { username: string; role: string; status: string; password: string }>>({})
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
  totalTokens: '总 Token',
  estimatedCosBytes: 'COS 估算流量'
}

const cleanupKindLabels: Record<string, string> = {
  pending_cos: '未提交上传',
  soft_deleted_attachments: '软删除附件',
  expired_attachments: '过期附件',
  orphan_attachments: '孤儿附件'
}

const cleanupKindOptions = [
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
  { value: 'disabled', label: '禁用' },
  { value: 'purging', label: '删除中' }
]

const groupOptions = computed(() => [
  { value: '', label: '不分组' },
  ...groups.value.map((group) => ({ value: group.id, label: group.name, hint: group.description || undefined }))
])

function draftFor(user: User) {
  if (!userDrafts.value[user.id]) {
    userDrafts.value[user.id] = {
      username: user.username,
      role: user.role,
      status: user.status,
      password: ''
    }
  }
  return userDrafts.value[user.id]
}

function keyDraftFor(key: ApiKeyEntry) {
  if (!keyDrafts.value[key.id]) {
    keyDrafts.value[key.id] = { name: key.name, groupId: key.groupId || '' }
  }
  return keyDrafts.value[key.id]
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
        status: user.status,
        password: ''
      }
    ])
  )
  analytics.value = await apiFetch('/admin/analytics')
  deadLetters.value = await apiFetch<any[]>('/admin/dead-letters')
  const reasoning = await apiFetch<{ models: string[] }>('/admin/settings/reasoning-models')
  reasoningModels.value = reasoning.models.join(', ')
  groups.value = await apiFetch<ApiKeyGroup[]>('/api-key-groups')
  if (selectedKeyUser.value) {
    const refreshed = users.value.find((user) => user.id === selectedKeyUser.value?.id)
    selectedKeyUser.value = refreshed || null
    if (selectedKeyUser.value) await loadSelectedUserKeys(selectedKeyUser.value)
  }
}

async function createUser() {
  await apiFetch('/admin/users', {
    method: 'POST',
    body: JSON.stringify({ username: username.value, password: loginPassword.value, role: 'user' })
  })
  username.value = ''
  loginPassword.value = ''
  await load()
}

async function updateUser(user: User, status: string) {
  await apiFetch(`/admin/users/${user.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ status })
  })
  await load()
}

async function saveUser(user: User) {
  const draft = userDrafts.value[user.id]
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
  notice.value = '用户信息已保存'
  await load()
}

async function loadSelectedUserKeys(user: User) {
  selectedKeyUser.value = user
  selectedUserKeys.value = await apiFetch<ApiKeyEntry[]>(`/admin/users/${user.id}/api-keys`)
  keyDrafts.value = Object.fromEntries(selectedUserKeys.value.map((key) => [key.id, { name: key.name, groupId: key.groupId || '' }]))
}

async function createAdminKey() {
  if (!selectedKeyUser.value) return
  await apiFetch<ApiKeyEntry>(`/admin/users/${selectedKeyUser.value.id}/api-keys`, {
    method: 'POST',
    body: JSON.stringify({
      name: adminKeyDraft.value.name,
      apiKey: adminKeyDraft.value.apiKey,
      groupId: adminKeyDraft.value.groupId || null,
      makeActive: adminKeyDraft.value.makeActive
    })
  })
  adminKeyDraft.value = { name: '默认密钥', apiKey: '', groupId: '', makeActive: true }
  notice.value = '用户密钥已添加'
  await load()
}

async function saveAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  const draft = keyDraftFor(key)
  await apiFetch<ApiKeyEntry>(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}`, {
    method: 'PATCH',
    body: JSON.stringify({ name: draft.name, groupId: draft.groupId || null })
  })
  notice.value = '用户密钥已保存'
  await loadSelectedUserKeys(selectedKeyUser.value)
}

async function activateAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  await apiFetch<ApiKeyEntry>(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}/activate`, { method: 'POST' })
  notice.value = '已切换该用户当前密钥'
  await loadSelectedUserKeys(selectedKeyUser.value)
}

async function deleteAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  await apiFetch(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}`, { method: 'DELETE' })
  notice.value = '用户密钥已删除'
  await load()
}

async function copyAdminKey(key: ApiKeyEntry) {
  if (!selectedKeyUser.value) return
  const result = await apiFetch<{ apiKey: string }>(`/admin/users/${selectedKeyUser.value.id}/api-keys/${key.id}/secret`)
  await navigator.clipboard.writeText(result.apiKey)
  notice.value = '密钥已复制'
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
    notice.value = `清理完成：${JSON.stringify(result.result)}`
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
  notice.value = 'Reasoning 模型列表已保存'
}

onMounted(load)
</script>

<template>
  <main class="admin-page app-page">
    <header class="app-header h-14 flex items-center px-5">
      <button class="app-secondary-button text-sm rounded-md px-3 py-1" @click="router.push('/')">返回</button>
      <h1 class="ml-4 font-semibold">管理后台</h1>
    </header>
    <section class="max-w-6xl mx-auto p-5 space-y-5">
      <p v-if="notice" class="app-card rounded-lg p-3 text-sm text-green-700">{{ notice }}</p>
      <div class="grid grid-cols-5 gap-3">
        <div v-for="(value, key) in analytics" :key="key" class="app-card rounded-lg p-4">
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
            <tr><th class="p-3">用户名</th><th class="p-3">角色</th><th class="p-3">状态</th><th class="p-3">需改密</th><th class="p-3">密钥</th><th class="p-3">操作</th></tr>
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
                  <button v-if="user.status === 'active'" class="app-secondary-button rounded px-2 py-1" @click="updateUser(user, 'disabled')">禁用</button>
                  <button v-else class="app-secondary-button rounded px-2 py-1" @click="updateUser(user, 'active')">启用</button>
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
            设为当前
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
              <td class="p-3">{{ key.isActive ? '当前使用' : '备用' }}</td>
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
  </main>
</template>
