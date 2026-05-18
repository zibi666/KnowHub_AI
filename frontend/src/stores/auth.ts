import { defineStore } from 'pinia'
import { apiFetch, ApiError } from '../api/client'
import type { User } from '../types'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as User | null,
    keyRequiredFor: null as string | null
  }),
  actions: {
    async loadMe() {
      try {
        this.user = await apiFetch<User>('/auth/me')
      } catch {
        this.user = null
      }
    },
    async login(username: string, password: string) {
      try {
        const result = await apiFetch<{ user: User; csrfToken: string }>('/auth/login', {
          method: 'POST',
          body: JSON.stringify({ username, password })
        })
        this.user = result.user
        return result.user
      } catch (error) {
        if (error instanceof ApiError && error.code === 'KEY_REQUIRED') {
          this.keyRequiredFor = username
        }
        throw error
      }
    },
    async firstLogin(username: string, password: string, apiKey: string) {
      const result = await apiFetch<{ user: User; csrfToken: string }>('/auth/first-login', {
        method: 'POST',
        body: JSON.stringify({ username, password, apiKey })
      })
      this.user = result.user
      return result.user
    },
    async changePassword(oldPassword: string, newPassword: string) {
      const result = await apiFetch<{ user: User; csrfToken: string }>('/auth/change-password', {
        method: 'POST',
        body: JSON.stringify({ oldPassword, newPassword })
      })
      this.user = result.user
    },
    async logout() {
      await apiFetch('/auth/logout', { method: 'POST' })
      this.user = null
    }
  }
})
