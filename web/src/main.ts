import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { registerSW } from 'virtual:pwa-register'
import App from './App.vue'
import './style.css'

declare global {
  interface Window {
    __checkPwaUpdate?: () => Promise<boolean>
  }
}

// Register PWA Service Worker with immediate auto-reload and lifecycle re-checks
const updateSW = registerSW({
  immediate: true,
  onNeedRefresh() {
    console.info('[PWA] New version detected, auto-activating...')
    updateSW(true)
  },
  onOfflineReady() {
    console.info('[PWA] Application ready to work offline.')
  },
  onRegisteredSW(_swUrl: string, registration?: ServiceWorkerRegistration) {
    if (!registration) return

    // 1. Check for updates on visibility change (resuming PWA on Android / switching back to tab)
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible' && navigator.onLine) {
          console.debug('[PWA] App resumed into foreground, checking for updates...')
          registration.update().catch((err: unknown) => console.warn('[PWA] Failed to check SW update on resume:', err))
        }
      })
    }

    // 2. Periodic background check every 15 minutes while app is running online
    setInterval(() => {
      if (navigator.onLine) {
        registration.update().catch((err: unknown) => console.warn('[PWA] Periodic SW update check failed:', err))
      }
    }, 15 * 60 * 1000)

    // 3. Expose manual trigger for UI version badge / settings
    if (typeof window !== 'undefined') {
      window.__checkPwaUpdate = async () => {
        if (!navigator.onLine) return false
        console.info('[PWA] Manual update check triggered...')
        try {
          await registration.update()
          return true
        } catch (err: unknown) {
          console.warn('[PWA] Manual update check failed:', err)
          return false
        }
      }
    }
  }
})

// Initialize Dark/Light theme mode from localStorage (Dark mode default)
if (typeof document !== 'undefined') {
  const savedTheme = localStorage.getItem('botex_theme') || 'dark'
  if (savedTheme === 'light') {
    document.documentElement.classList.add('light')
    document.documentElement.classList.remove('dark')
  } else {
    document.documentElement.classList.add('dark')
    document.documentElement.classList.remove('light')
  }
}

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.mount('#app')
