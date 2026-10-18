<template>
  <div class="h-full flex flex-col overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6 max-w-4xl">
    <!-- View Header -->
    <div class="border-b border-botex-800 pb-4">
      <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
        <SettingsIcon class="w-5 h-5 text-cyan-400" />
        <span>{{ t('settingsTitle') }}</span>
      </h2>
      <p class="text-xs text-slate-300 mt-1">
        {{ t('settingsSubtitle') }}
      </p>
    </div>

    <!-- Connection & Auth Card -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-5 shadow-sm">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-botex-800 pb-3">
        <div class="flex items-center space-x-2">
          <ServerIcon class="w-4 h-4 text-cyan-400" />
          <h3 class="text-sm font-semibold text-slate-100">{{ t('backendConnection') }}</h3>
        </div>
        <Badge :variant="authStore.isServerHealthy ? 'success' : 'danger'">
          {{ authStore.isServerHealthy ? (locale === 'pl' ? 'Połączono' : 'Connected') : (locale === 'pl' ? 'Brak połączenia' : 'Unreachable') }}
        </Badge>
      </div>

      <!-- Backend URL Input -->
      <div>
        <label class="block text-xs font-medium text-slate-200 mb-1 flex items-center justify-between">
          <span>Backend URL</span>
          <button
            type="button"
            class="text-[11px] text-cyan-300 hover:text-cyan-200"
            @click="urlInput = 'http://127.0.0.1:8000'"
          >
            {{ locale === 'pl' ? 'Przywróć lokalny adres' : 'Reset to Local Default' }}
          </button>
        </label>
        <input
          v-model="urlInput"
          type="text"
          placeholder="http://127.0.0.1:8000"
          class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-100 focus:outline-none focus:border-cyan-500"
        />
        <p class="text-[11px] text-slate-300 mt-1">
          {{ locale === 'pl' ? 'Adres bramki FastAPI obsługującej REST, Remote MCP i WebSockets.' : 'FastAPI gateway address serving REST endpoints, Remote MCP, and WebSockets.' }}
        </p>
      </div>

      <!-- Master Bearer Token Input -->
      <div>
        <label class="block text-xs font-medium text-slate-200 mb-1 flex items-center justify-between">
          <span>Bearer Token (BOTEX_ACCESS_TOKEN)</span>
          <span class="text-slate-300 font-normal">
            {{ locale === 'pl' ? 'Opcjonalny w trybie lokalnym' : 'Optional on localhost' }}
          </span>
        </label>
        <div class="relative">
          <input
            v-model="tokenInput"
            :type="showToken ? 'text' : 'password'"
            :placeholder="locale === 'pl' ? 'Zostaw puste lokalnie lub wpisz token dostępowy' : 'Leave blank on localhost or enter bearer token'"
            class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 pr-10 text-xs font-mono text-slate-100 placeholder-slate-400 focus:outline-none focus:border-cyan-500"
          />
          <button
            type="button"
            class="absolute inset-y-0 right-0 pr-3 flex items-center text-slate-300 hover:text-white"
            @click="showToken = !showToken"
          >
            <EyeOffIcon v-if="showToken" class="w-4 h-4" />
            <EyeIcon v-else class="w-4 h-4" />
          </button>
        </div>
        <p class="text-[11px] text-slate-300 mt-1">
          {{ locale === 'pl' ? 'Zapisywany w localStorage przeglądarki i wysyłany w nagłówkach HTTP/WS.' : 'Saved in browser localStorage and sent with HTTP and WebSocket requests.' }}
        </p>
      </div>

      <!-- Action Buttons -->
      <div class="pt-2 flex flex-wrap items-center justify-between gap-3 border-t border-botex-800">
        <div class="flex items-center space-x-2">
          <Button
            variant="primary"
            size="sm"
            @click="saveSettings"
          >
            {{ t('saveConfig') }}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            :loading="testingConnection"
            @click="testConnection"
          >
            {{ t('testConnection') }}
          </Button>
        </div>

        <span v-if="saveMessage" class="text-xs text-emerald-300 animate-fadeIn">
          {{ saveMessage }}
        </span>
      </div>

      <!-- Probe Result Banner -->
      <div v-if="testResult" :class="['p-3 rounded-lg border text-xs', testResult.ok ? 'bg-emerald-950/40 border-emerald-700 text-emerald-200' : 'bg-rose-950/40 border-rose-700 text-rose-200']">
        <div class="font-semibold">{{ testResult.title }}</div>
        <p class="mt-0.5 font-mono text-[11px]">{{ testResult.detail }}</p>
      </div>
    </div>

    <!-- Interface & Language -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-4 shadow-sm">
      <div class="flex items-center space-x-2 border-b border-botex-800 pb-3">
        <PaletteIcon class="w-4 h-4 text-cyan-400" />
        <h3 class="text-sm font-semibold text-slate-100">{{ t('interfaceLanguage') }}</h3>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <span class="text-xs font-medium text-slate-100">{{ locale === 'pl' ? 'Język interfejsu' : 'UI Language' }}</span>
          <p class="text-[11px] text-slate-300">
            {{ locale === 'pl' ? 'Polski (miodkuj) lub angielski (humanify).' : 'Switch between English (technical) and Polish.' }}
          </p>
        </div>
        <div class="flex items-center bg-botex-950 p-1 rounded-lg border border-botex-800 space-x-1">
          <button
            type="button"
            :class="[
              'min-h-[34px] px-3 py-1 rounded text-xs font-mono font-semibold transition',
              locale === 'en' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="setLocale('en')"
          >
            English (EN)
          </button>
          <button
            type="button"
            :class="[
              'min-h-[34px] px-3 py-1 rounded text-xs font-mono font-semibold transition',
              locale === 'pl' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="setLocale('pl')"
          >
            Polski (PL)
          </button>
        </div>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3 pt-3 border-t border-botex-800">
        <div>
          <span class="text-xs font-medium text-slate-100">{{ locale === 'pl' ? 'Motyw kolorystyczny' : 'Color Theme' }}</span>
          <p class="text-[11px] text-slate-300">
            {{ locale === 'pl' ? 'Wysokokontrastowy motyw ciemny lub jasny.' : 'High-contrast dark or light workspace palette.' }}
          </p>
        </div>
        <div class="flex items-center bg-botex-950 p-1 rounded-lg border border-botex-800 space-x-1">
          <button
            type="button"
            :class="[
              'min-h-[34px] px-3 py-1 rounded text-xs font-medium transition',
              theme === 'dark' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="setTheme('dark')"
          >
            Dark
          </button>
          <button
            type="button"
            :class="[
              'min-h-[34px] px-3 py-1 rounded text-xs font-medium transition',
              theme === 'light' ? 'bg-botex-800 text-amber-200 border border-amber-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="setTheme('light')"
          >
            Light
          </button>
        </div>
      </div>
    </div>

    <!-- Progressive Web App Info -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-4 shadow-sm">
      <div class="flex items-center space-x-2 border-b border-botex-800 pb-3">
        <SmartphoneIcon class="w-4 h-4 text-cyan-400" />
        <h3 class="text-sm font-semibold text-slate-100">PWA Runtime</h3>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs font-mono">
        <div class="p-3 bg-botex-950 rounded-lg border border-botex-800">
          <span class="block text-[10px] text-slate-300 uppercase">Service Worker</span>
          <span class="text-emerald-300 font-semibold">Active & Precached</span>
        </div>
        <div class="p-3 bg-botex-950 rounded-lg border border-botex-800">
          <span class="block text-[10px] text-slate-300 uppercase">Display Mode</span>
          <span class="text-cyan-300 font-semibold">Standalone Shell</span>
        </div>
        <div class="p-3 bg-botex-950 rounded-lg border border-botex-800">
          <span class="block text-[10px] text-slate-300 uppercase">Network Mode</span>
          <span :class="authStore.isOnline ? 'text-emerald-300' : 'text-amber-300'" class="font-semibold">
            {{ authStore.isOnline ? 'Online' : 'Offline Shell' }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useI18n } from '../i18n'
import { api } from '../api/client'
import Badge from '../components/common/Badge.vue'
import Button from '../components/common/Button.vue'
import {
  Settings as SettingsIcon,
  Server as ServerIcon,
  Eye as EyeIcon,
  EyeOff as EyeOffIcon,
  Palette as PaletteIcon,
  Smartphone as SmartphoneIcon
} from 'lucide-vue-next'

const authStore = useAuthStore()
const { locale, theme, setLocale, setTheme, t } = useI18n()

const urlInput = ref(authStore.backendUrl)
const tokenInput = ref(authStore.accessToken)
const showToken = ref(false)
const saveMessage = ref('')
const testingConnection = ref(false)
const testResult = ref<{ ok: boolean; title: string; detail: string } | null>(null)

function saveSettings() {
  authStore.updateBackendUrl(urlInput.value)
  authStore.updateAccessToken(tokenInput.value)
  saveMessage.value = locale.value === 'pl' ? 'Zapisano w localStorage.' : 'Settings saved to localStorage.'
  setTimeout(() => {
    saveMessage.value = ''
  }, 2500)
}

async function testConnection() {
  saveSettings()
  testingConnection.value = true
  testResult.value = null
  try {
    const health = await api.getHealth()
    let walletMsg = 'Wallet probe passed'
    try {
      const wallet = await api.getWallet()
      walletMsg = `Wallet: $${wallet.balance_usd ?? '0.00'} (${wallet.label})`
    } catch (e: any) {
      walletMsg = `Wallet notice: ${e.message}`
    }

    testResult.value = {
      ok: true,
      title: locale.value === 'pl' ? 'Połączenie aktywne' : 'Connection Verified',
      detail: `Health: ${health.status} v${health.version} (uptime ${health.uptime_s}s) | ${walletMsg}`
    }
  } catch (err: any) {
    testResult.value = {
      ok: false,
      title: locale.value === 'pl' ? 'Błąd połączenia' : 'Connection Failed',
      detail: err?.message || 'Server did not respond to /api/health.'
    }
  } finally {
    testingConnection.value = false
  }
}
</script>
