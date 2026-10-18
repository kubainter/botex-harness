import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { registerSW } from 'virtual:pwa-register'
import App from './App.vue'
import './style.css'

// Register PWA Service Worker with auto-update capability
const updateSW = registerSW({
  onNeedRefresh() {
    if (confirm('A new version of BoteX Control is available. Reload to update?')) {
      updateSW(true)
    }
  },
  onOfflineReady() {
    console.info('[PWA] Application ready to work offline.')
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
