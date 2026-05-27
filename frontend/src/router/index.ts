import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import LoginView from '../views/LoginView.vue'
import ChatView from '../views/ChatView.vue'
import AdminView from '../views/AdminView.vue'
import SettingsView from '../views/SettingsView.vue'
import KeyManagementView from '../views/KeyManagementView.vue'
import VersionControlView from '../views/VersionControlView.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView },
    { path: '/', component: ChatView },
    { path: '/settings', component: SettingsView },
    { path: '/keys', component: KeyManagementView },
    { path: '/admin', component: AdminView },
    { path: '/versions', component: VersionControlView }
  ]
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.user) await auth.loadMe()
  if (to.path !== '/login' && !auth.user) return '/login'
  if (to.path === '/admin' && auth.user?.role !== 'admin') return '/'
})
