<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { apiFetch } from '../api/client'
import AppSelect from '../components/AppSelect.vue'
import { useAuthStore } from '../stores/auth'
import type { ApiKeyEntry, ApiKeyGroup } from '../types'

const router = useRouter()
const auth = useAuthStore()

const keys = ref<ApiKeyEntry[]>([])
const groups = ref<ApiKeyGroup[]>([])
const notice = ref('')
const error = ref('')
const newKey = ref({ name: '默认密钥', apiKey: '', groupId: '', makeActive: true })
const keyDrafts = ref<Record<string, { name: string; groupId: string }>>({})

const groupOptions = computed(() => [
  { value: '', label: '不分组' },
  ...groups.value.map((group) => ({ value: group.id, label: group.name, hint: group.description || undefined }))
])

function resetMessage() {
  notice.value = ''
  error.value = ''
}

function draftFor(key: ApiKeyEntry) {
  if (!keyDrafts.value[key.id]) {
    keyDrafts.value[key.id] = { name: key.name, groupId: key.groupId || '' }
  }
  return keyDrafts.value[key.id]
}

function setNewKeyGroup(value: string | number) {
  newKey.value.groupId = String(value)
}

function setKeyDraftGroup(key: ApiKeyEntry, value: string | number) {
  draftFor(key).groupId = String(value)
}

async function load() {
  keys.value = await apiFetch<ApiKeyEntry[]>('/api-keys')
  groups.value = await apiFetch<ApiKeyGroup[]>('/api-key-groups')
  keyDrafts.value = Object.fromEntries(keys.value.map((key) => [key.id, { name: key.name, groupId: key.groupId || '' }]))
}

async function createKey() {
  resetMessage()
  try {
    await apiFetch<ApiKeyEntry>('/api-keys', {
      method: 'POST',
      body: JSON.stringify({
        name: newKey.value.name,
        apiKey: newKey.value.apiKey,
        groupId: newKey.value.groupId || null,
        makeActive: newKey.value.makeActive
      })
    })
    newKey.value = { name: '默认密钥', apiKey: '', groupId: '', makeActive: true }
    notice.value = '密钥已添加'
    await load()
    await auth.loadMe()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '添加失败'
  }
}

async function saveKey(key: ApiKeyEntry) {
  resetMessage()
  const draft = draftFor(key)
  try {
    await apiFetch<ApiKeyEntry>(`/api-keys/${key.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name: draft.name, groupId: draft.groupId || null })
    })
    notice.value = '密钥信息已保存'
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '保存失败'
  }
}

async function activateKey(key: ApiKeyEntry) {
  resetMessage()
  try {
    await apiFetch<ApiKeyEntry>(`/api-keys/${key.id}/activate`, { method: 'POST' })
    notice.value = '已切换当前使用密钥'
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '切换失败'
  }
}

async function deleteKey(key: ApiKeyEntry) {
  resetMessage()
  try {
    await apiFetch(`/api-keys/${key.id}`, { method: 'DELETE' })
    notice.value = '密钥已删除'
    await load()
    await auth.loadMe()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '删除失败'
  }
}

async function copyKey(key: ApiKeyEntry) {
  resetMessage()
  try {
    const result = await apiFetch<{ apiKey: string }>(`/api-keys/${key.id}/secret`)
    await navigator.clipboard.writeText(result.apiKey)
    notice.value = '密钥已复制'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '复制失败'
  }
}

onMounted(load)
</script>

<template>
  <main class="settings-page app-page">
    <header class="app-header h-14 flex items-center px-5">
      <button class="app-secondary-button text-sm rounded-md px-3 py-1" @click="router.push('/')">返回</button>
      <h1 class="ml-4 font-semibold">密钥管理</h1>
      <span class="ml-auto app-muted text-sm">当前账号：{{ auth.user?.username }}</span>
    </header>

    <section class="max-w-5xl mx-auto p-5 space-y-5">
      <div v-if="notice" class="app-card rounded-lg p-3 text-sm text-green-700">{{ notice }}</div>
      <div v-if="error" class="app-card rounded-lg p-3 text-sm text-red-600">{{ error }}</div>

      <form class="app-card rounded-lg p-5 space-y-3" @submit.prevent="createKey">
        <h2 class="font-semibold">添加新密钥</h2>
        <div class="grid gap-3 md:grid-cols-2">
          <input v-model="newKey.name" class="app-input rounded-md px-3 py-2" placeholder="密钥名称，例如：工作 / 备用" />
          <AppSelect
            v-model="newKey.groupId"
            class="app-select-compact"
            :options="groupOptions"
            @change="setNewKeyGroup"
          />
          <input v-model="newKey.apiKey" class="app-input rounded-md px-3 py-2" type="password" placeholder="API Key 明文只提交一次" />
        </div>
        <label class="inline-flex items-center gap-2 text-sm app-muted">
          <input v-model="newKey.makeActive" type="checkbox" />
          添加后立即设为当前使用密钥
        </label>
        <div>
          <button class="app-primary-button rounded-md px-4 py-2" type="submit">添加密钥</button>
        </div>
      </form>

      <div class="app-card rounded-lg overflow-visible">
        <div class="p-4 font-semibold">我的密钥</div>
        <table class="w-full text-sm">
          <thead class="app-table-head text-left">
            <tr>
              <th class="p-3">名称</th>
              <th class="p-3">分组</th>
              <th class="p-3">标识</th>
              <th class="p-3">状态</th>
              <th class="p-3">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!keys.length"><td class="app-muted p-3" colspan="5">暂无密钥</td></tr>
            <tr v-for="key in keys" :key="key.id" class="app-table-row">
              <td class="p-3">
                <input v-model="draftFor(key).name" class="app-input w-full rounded-md px-2 py-1" />
              </td>
              <td class="p-3">
                <AppSelect
                  :model-value="draftFor(key).groupId"
                  class="app-select-compact min-w-[150px]"
                  :options="groupOptions"
                  @update:model-value="setKeyDraftGroup(key, $event)"
                />
              </td>
              <td class="p-3">
                <div class="key-mask">{{ key.maskedKey }}</div>
              </td>
              <td class="p-3">
                <span v-if="key.isActive" class="text-green-500 font-semibold">当前使用</span>
                <span v-else class="app-muted">备用</span>
              </td>
              <td class="p-3">
                <div class="flex flex-wrap gap-2">
                  <button class="app-secondary-button rounded px-2 py-1" @click="saveKey(key)">保存</button>
                  <button class="app-secondary-button rounded px-2 py-1" @click="copyKey(key)">复制</button>
                  <button class="app-primary-button rounded px-2 py-1" :disabled="key.isActive" @click="activateKey(key)">切换使用</button>
                  <button class="app-secondary-button rounded px-2 py-1" @click="deleteKey(key)">删除</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

    </section>
  </main>
</template>
