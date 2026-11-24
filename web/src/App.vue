<template>
  <div class="h-screen supports-[height:100dvh]:h-dvh w-full flex flex-col md:flex-row bg-botex-950 text-slate-100 font-sans overflow-hidden pt-safe pl-safe pr-safe">
    <a
      href="#main-content"
      class="skip-link px-4 py-2.5 rounded-lg bg-sky-600 text-white text-sm font-semibold shadow-lg"
    >{{ lang === 'pl' ? 'Przejdź do treści' : 'Skip to main content' }}</a>
    <!-- Tablet / Desktop Sidebar -->
    <aside
      id="app-sidebar"
      :class="[
        'hidden md:flex flex-col bg-botex-900 border-r border-botex-800 shrink-0 transition-all duration-150 select-none',
        sidebarCollapsed ? 'w-16' : 'w-56 lg:w-64'
      ]"
    >
      <!-- App Brand -->
      <div class="px-3.5 py-3.5 border-b border-botex-800 flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center space-x-2.5 min-w-0">
          <button
            type="button"
            class="w-11 h-11 lg:w-9 lg:h-9 rounded-lg bg-botex-950 border border-botex-700 hover:border-sky-500/50 flex items-center justify-center shrink-0 transition"
            title="Toggle sidebar width"
            :aria-label="sidebarCollapsed ? (lang === 'pl' ? 'Rozwiń panel nawigacji' : 'Expand sidebar') : (lang === 'pl' ? 'Zwiń panel nawigacji' : 'Collapse sidebar')"
            :aria-expanded="!sidebarCollapsed"
            aria-controls="app-sidebar"
            @click="sidebarCollapsed = !sidebarCollapsed"
          >
            <span class="font-mono font-bold text-sky-400 text-sm">BX</span>
          </button>
          <button
            v-if="!sidebarCollapsed"
            type="button"
            class="text-left truncate select-none rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
            title="Go to Chat"
            @click="currentView = 'chat'"
          >
            <h1 class="text-base font-bold text-slate-100 tracking-tight truncate hover:text-sky-300 transition">BoteX Control</h1>
            <p class="text-xs text-slate-300 font-mono truncate">{{ t('app.subtitle') }}</p>
          </button>
        </div>

        <!-- Connection Status, Theme & Language Switcher -->
        <div v-if="!sidebarCollapsed" class="flex items-center space-x-1.5 shrink-0 ml-auto">
          <button
            type="button"
            class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] p-1.5 rounded bg-botex-950 border border-botex-700 text-slate-200 hover:border-sky-500/50 transition flex items-center justify-center"
            :title="themeStore.isDark ? 'Switch to Light Mode' : 'Switch to Dark Mode'"
            :aria-label="themeStore.isDark ? 'Switch to Light Mode' : 'Switch to Dark Mode'"
            :aria-pressed="themeStore.isDark"
            @click="toggleTheme"
          >
            <SunIcon v-if="themeStore.isDark" class="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
            <MoonIcon v-else class="w-3.5 h-3.5 text-sky-400" aria-hidden="true" />
          </button>
          <button
            type="button"
            class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] px-2 py-1 rounded bg-botex-950 border border-botex-700 text-xs font-mono font-semibold text-slate-200 hover:border-sky-500/50 transition uppercase"
            title="Switch interface language (EN / PL)"
            aria-label="Switch interface language (EN / PL)"
            @click="toggleLang"
          >
            {{ lang }}
          </button>
          <div
            class="w-2.5 h-2.5 rounded-full"
            :class="authStore.isServerHealthy ? 'bg-emerald-400' : 'bg-rose-500 animate-pulse'"
            :title="authStore.isServerHealthy ? t('app.online') : t('app.offline')"
            role="status"
          >
            <span class="sr-only">{{ authStore.isServerHealthy ? t('app.online') : t('app.offline') }}</span>
          </div>
        </div>
      </div>

      <!-- Navigation Links -->
      <nav class="flex-1 px-2.5 py-3 space-y-1 overflow-y-auto" aria-label="Primary">
        <button
          v-for="item in navItems"
          :key="item.id"
          type="button"
          :title="t(item.labelKey)"
          :aria-label="navAriaLabel(item)"
          :aria-current="currentView === item.id ? 'page' : undefined"
          :class="[
            'w-full min-h-[44px] flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition-colors',
            currentView === item.id
              ? 'bg-botex-800 text-slate-100 border border-botex-700'
              : 'text-slate-300 hover:text-white hover:bg-botex-800/60 border border-transparent'
          ]"
          @click="currentView = item.id"
        >
          <div class="flex items-center space-x-3 truncate">
            <component
              :is="item.icon"
              class="w-4 h-4 shrink-0"
              :class="currentView === item.id ? 'text-sky-400' : 'text-slate-300'"
              aria-hidden="true"
            />
            <span v-if="!sidebarCollapsed" class="truncate">{{ t(item.labelKey) }}</span>
          </div>

          <!-- Pending count badge -->
          <span
            v-if="!sidebarCollapsed && item.id === 'approvals' && approvalsCount > 0"
            class="px-2 py-0.5 rounded text-xs font-mono font-bold bg-amber-950 text-amber-300 border border-amber-500/40"
            aria-hidden="true"
          >
            {{ approvalsCount }}
          </span>
          <!-- Low budget warning badge -->
          <span
            v-if="!sidebarCollapsed && item.id === 'wallet' && isBudgetLow"
            class="px-1.5 py-0.5 rounded text-xs font-mono font-bold bg-rose-950 text-rose-300 border border-rose-500/40"
            :title="t('app.budget.low')"
            aria-hidden="true"
          >
            !$
          </span>
        </button>
      </nav>

      <!-- Sidebar Footer -->
      <div v-if="!sidebarCollapsed" class="p-3 border-t border-botex-800 bg-botex-950/70 text-xs">
        <button
          v-if="isBudgetLow"
          type="button"
          class="mb-2 w-full px-2 py-1 rounded bg-rose-950/90 border border-rose-500/40 text-rose-200 text-xs font-mono flex items-center justify-between text-left hover:bg-rose-900/70 transition"
          @click="currentView = 'wallet'"
        >
          <span class="truncate">{{ t('app.budget.low') }}</span>
        </button>
        <div class="flex items-center justify-between text-xs text-slate-300 font-mono mb-1">
          <span class="truncate max-w-[140px]">{{ backendHost }}</span>
          <span :class="wsColorClass">{{ wsStatus }}</span>
        </div>
        <div class="flex items-center justify-between text-xs text-slate-300 font-mono">
          <button
            type="button"
            class="min-h-[36px] hover:text-sky-300 transition cursor-pointer flex items-center space-x-1 select-none"
            :title="lang === 'pl' ? 'Wersja PWA (kliknij aby sprawdzić aktualizacje)' : 'PWA Version (click to check for updates)'"
            @click="checkForAppUpdate"
          >
            <span class="font-bold">{{ appVersion }}</span>
            <span v-if="updateStatusMessage" role="status" class="text-[10px] text-amber-300 font-sans">({{ updateStatusMessage }})</span>
          </button>
          <span v-if="authStore.accessToken" class="text-sky-300">{{ t('app.auth.bearer') }}</span>
          <span v-else class="text-slate-300">{{ t('app.auth.local') }}</span>
        </div>
      </div>
    </aside>

    <!-- Main Content Area -->
    <main id="main-content" tabindex="-1" class="flex-1 flex flex-col min-w-0 h-full relative overflow-hidden focus:outline-none">
      <!-- Mobile Top Bar (Strict RWD & Safe Bounds) -->
      <header class="md:hidden px-2.5 sm:px-3.5 py-2 bg-botex-900 border-b border-botex-800 flex items-center justify-between gap-1.5 shrink-0 w-full max-w-full overflow-hidden">
        <button
          type="button"
          class="flex items-center space-x-2 select-none min-w-0 shrink rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-400"
          title="Go to Chat"
          @click="currentView = 'chat'"
        >
          <div class="w-7 h-7 shrink-0 rounded-lg bg-botex-950 border border-botex-700 hover:border-sky-500/50 flex items-center justify-center text-sky-400 font-bold font-mono text-xs transition">
            BX
          </div>
          <span class="text-base font-bold text-slate-100 hover:text-sky-300 transition truncate">BoteX Control</span>
        </button>

        <div class="flex items-center space-x-1 shrink-0">
          <button
            v-if="isBudgetLow"
            type="button"
            class="min-h-[44px] px-1.5 py-0.5 rounded text-xs font-mono font-bold bg-rose-950 text-rose-300 border border-rose-500/40"
            :title="t('app.budget.low')"
            @click="currentView = 'wallet'"
          >
            !$
          </button>
          <button
            type="button"
            class="min-h-[44px] min-w-[44px] p-1.5 rounded bg-botex-950 border border-botex-700 text-slate-200 flex items-center justify-center transition hover:border-slate-500"
            :title="themeStore.isDark ? 'Switch to Light Mode' : 'Switch to Dark Mode'"
            :aria-label="themeStore.isDark ? 'Switch to Light Mode' : 'Switch to Dark Mode'"
            :aria-pressed="themeStore.isDark"
            @click="toggleTheme"
          >
            <SunIcon v-if="themeStore.isDark" class="w-4 h-4 text-amber-400" aria-hidden="true" />
            <MoonIcon v-else class="w-4 h-4 text-sky-400" aria-hidden="true" />
          </button>
          <button
            type="button"
            class="min-h-[44px] min-w-[44px] px-2 py-1 rounded bg-botex-950 border border-botex-700 text-xs font-mono font-semibold text-slate-200 uppercase transition hover:border-slate-500 flex items-center justify-center"
            title="Switch Language"
            aria-label="Switch interface language (EN / PL)"
            @click="toggleLang"
          >
            {{ lang }}
          </button>
          <button
            v-if="approvalsCount > 0"
            type="button"
            class="min-h-[44px] px-2 py-0.5 rounded text-xs font-mono font-bold bg-amber-950 text-amber-300 border border-amber-500/40 flex items-center space-x-1"
            title="Pending Approvals"
            :aria-label="`${t('nav.approvals')} (${approvalsCount})`"
            @click="currentView = 'approvals'"
          >
            <ShieldAlertIcon class="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
            <span aria-hidden="true">{{ approvalsCount }}</span>
          </button>
          <div
            class="w-2.5 h-2.5 rounded-full shrink-0 ml-0.5"
            :class="authStore.isServerHealthy ? 'bg-emerald-400' : 'bg-rose-500 animate-pulse'"
            :title="authStore.isServerHealthy ? t('app.online') : t('app.offline')"
            role="status"
          >
            <span class="sr-only">{{ authStore.isServerHealthy ? t('app.online') : t('app.offline') }}</span>
          </div>
        </div>
      </header>

      <!-- Connection Warning Banner if offline -->
      <div
        v-if="!authStore.isOnline || !authStore.isServerHealthy"
        role="alert"
        class="bg-amber-950/90 border-b border-amber-500/40 text-amber-200 text-xs px-4 py-1.5 text-center flex items-center justify-center space-x-2 shrink-0"
      >
        <AlertTriangleIcon class="w-3.5 h-3.5 text-amber-400 shrink-0" aria-hidden="true" />
        <span class="truncate">
          {{ !authStore.isOnline ? t('app.offline.banner') : t('app.unreachable.banner') + authStore.backendUrl }}
        </span>
      </div>

      <!-- Auth Failure Banner -->
      <div
        v-if="authError"
        role="alert"
        class="bg-rose-950/90 border-b border-rose-500/40 text-rose-200 text-xs px-4 py-1.5 flex items-center justify-between shrink-0"
      >
        <div class="flex items-center space-x-2 truncate">
          <AlertTriangleIcon class="w-3.5 h-3.5 text-rose-400 shrink-0" aria-hidden="true" />
          <span class="truncate">{{ t('app.auth.banner') }}</span>
        </div>
        <button
          type="button"
          class="underline text-xs text-rose-300 hover:text-white shrink-0 ml-2 font-mono min-h-[44px] px-2"
          @click="currentView = 'settings'"
        >
          {{ lang === 'pl' ? 'Ustawienia' : 'Settings' }} &rarr;
        </button>
      </div>

      <!-- Low Balance Warning Banner -->
      <div
        v-if="isBudgetLow"
        role="alert"
        class="bg-rose-950/90 border-b border-rose-500/40 text-rose-200 text-xs px-4 py-1.5 flex items-center justify-between shrink-0"
      >
        <div class="flex items-center space-x-2 truncate">
          <AlertTriangleIcon class="w-3.5 h-3.5 text-rose-400 shrink-0" aria-hidden="true" />
          <span class="truncate">
            {{ lang === 'pl' ? 'Niski stan portfela OpenRouter' : 'Low OpenRouter Balance' }}:
            <strong class="font-mono text-white">${{ (walletStore.walletInfo?.balance_usd ?? walletStore.keyStatus?.balance_usd ?? 0).toFixed(2) }}</strong>
            ({{ lang === 'pl' ? 'wymagane zasilenie lub przełączenie na darmowy model :free' : 'top-up required or switch to free model' }})
          </span>
        </div>
        <button
          type="button"
          class="underline text-xs text-rose-300 hover:text-white shrink-0 ml-2 font-mono"
          @click="currentView = 'wallet'"
        >
          {{ lang === 'pl' ? 'Szczegóły' : 'Details' }} &rarr;
        </button>
      </div>

      <!-- Dynamic Views -->
      <div class="flex-1 overflow-hidden relative">
        <Transition name="view-fade" mode="out-in">
          <div :key="currentView" class="h-full min-h-0">
            <ChatView
              v-if="currentView === 'chat'"
              @navigate="onNavigate"
            />
            <TasksView
              v-else-if="currentView === 'tasks'"
            />
            <BotsView
              v-else-if="currentView === 'bots'"
            />
            <HostsView
              v-else-if="currentView === 'hosts'"
            />
            <ChannelsView
              v-else-if="currentView === 'channels'"
            />
            <ApprovalsView
              v-else-if="currentView === 'approvals'"
            />
            <MemoryView
              v-else-if="currentView === 'memory'"
            />
            <WalletView
              v-else-if="currentView === 'wallet'"
            />
            <SettingsView
              v-else-if="currentView === 'settings'"
            />
          </div>
        </Transition>
      </div>

      <!-- Mobile Bottom Navigation Bar (Safe Bounds; horizontally scrollable for 9 tabs) -->
      <nav class="md:hidden bg-botex-900 border-t border-botex-800 flex items-center overflow-x-auto gap-0.5 px-1 py-1 pb-safe w-full max-w-full shrink-0 z-30 select-none [scrollbar-width:none] [&::-webkit-scrollbar]:hidden" aria-label="Primary">
        <button
          v-for="item in navItems"
          :key="item.id"
          type="button"
          :aria-label="navAriaLabel(item)"
          :aria-current="currentView === item.id ? 'page' : undefined"
          :class="[
            'min-h-[48px] shrink-0 min-w-[3.75rem] overflow-hidden flex flex-col items-center justify-center py-1 px-0.5 rounded-lg text-xs font-medium transition relative',
            currentView === item.id ? 'text-sky-300 bg-botex-800/80 shadow-sm' : 'text-slate-300 hover:text-white'
          ]"
          @click="currentView = item.id"
        >
          <div class="relative shrink-0 flex items-center justify-center">
            <component :is="item.icon" class="w-4 h-4 shrink-0" aria-hidden="true" />
            <span
              v-if="item.id === 'approvals' && approvalsCount > 0"
              class="absolute -top-1 -right-1.5 w-3.5 h-3.5 bg-amber-500 text-botex-950 text-[8px] font-mono font-bold rounded-full flex items-center justify-center"
              aria-hidden="true"
            >
              {{ approvalsCount }}
            </span>
          </div>
          <span class="mt-0.5 truncate w-full text-center text-xs tracking-tight leading-none">{{ t(item.shortKey) }}</span>
        </button>
      </nav>
    </main>

    <!-- Global Operator Approval Modal -->
    <ApprovalModal
      :is-open="Boolean(activeModalAction)"
      :action="activeModalAction"
      @close="approvalsStore.closeModal"
      @decide="onModalDecide"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { authError } from './api/client'
import { useAuthStore } from './stores/auth'
import { useApprovalsStore } from './stores/approvals'
import { useChatStore } from './stores/chat'
import { useWalletStore } from './stores/wallet'
import { useThemeStore } from './stores/theme'
import { useSwarmStore } from './stores/swarm'
import { useI18n } from './i18n'
import ChatView from './views/ChatView.vue'
import TasksView from './views/TasksView.vue'
import BotsView from './views/BotsView.vue'
import HostsView from './views/HostsView.vue'
import ChannelsView from './views/ChannelsView.vue'
import ApprovalsView from './views/ApprovalsView.vue'
import MemoryView from './views/MemoryView.vue'
import WalletView from './views/WalletView.vue'
import SettingsView from './views/SettingsView.vue'
import ApprovalModal from './components/approvals/ApprovalModal.vue'
import {
  MessageSquare as MessageSquareIcon,
  Cpu as CpuIcon,
  Bot as BotIcon,
  Server as ServerIcon,
  Hash as HashIcon,
  ShieldAlert as ShieldAlertIcon,
  Database as DatabaseIcon,
  Wallet as WalletIcon,
  Settings as SettingsIcon,
  AlertTriangle as AlertTriangleIcon,
  Sun as SunIcon,
  Moon as MoonIcon
} from 'lucide-vue-next'

const authStore = useAuthStore()
const approvalsStore = useApprovalsStore()
const chatStore = useChatStore()
const walletStore = useWalletStore()
const themeStore = useThemeStore()
const swarm = useSwarmStore()
const { lang, setUiLang, t } = useI18n()

const { count: approvalsCount, activeModalAction } = storeToRefs(approvalsStore)
const { wsStatus } = storeToRefs(authStore)
const { isBudgetLow } = storeToRefs(walletStore)

const currentView = ref<string>('chat')
const sidebarCollapsed = ref<boolean>(false)
let stopSwarmRealtime: (() => void) | null = null

const navItems = [
  { id: 'chat', labelKey: 'nav.chat', shortKey: 'nav.chat.short', icon: MessageSquareIcon },
  { id: 'tasks', labelKey: 'nav.tasks', shortKey: 'nav.tasks.short', icon: CpuIcon },
  { id: 'bots', labelKey: 'nav.bots', shortKey: 'nav.bots.short', icon: BotIcon },
  { id: 'hosts', labelKey: 'nav.hosts', shortKey: 'nav.hosts.short', icon: ServerIcon },
  { id: 'channels', labelKey: 'nav.channels', shortKey: 'nav.channels.short', icon: HashIcon },
  { id: 'approvals', labelKey: 'nav.approvals', shortKey: 'nav.approvals.short', icon: ShieldAlertIcon },
  { id: 'memory', labelKey: 'nav.memory', shortKey: 'nav.memory.short', icon: DatabaseIcon },
  { id: 'wallet', labelKey: 'nav.wallet', shortKey: 'nav.wallet.short', icon: WalletIcon },
  { id: 'settings', labelKey: 'nav.settings', shortKey: 'nav.settings.short', icon: SettingsIcon }
]

const viewIds = new Set(navItems.map((item) => item.id))

function viewFromHash(): string {
  const hash = window.location.hash.replace(/^#/, '')
  return viewIds.has(hash) ? hash : 'chat'
}

function onHashChange() {
  const view = viewFromHash()
  if (currentView.value !== view) currentView.value = view
}

watch(currentView, (view) => {
  const expected = `#${view}`
  if (window.location.hash !== expected) {
    // Guard against the hashchange echo setting the same view back.
    window.location.hash = expected
  }
})

const backendHost = computed(() => {
  return authStore.backendUrl.replace(/^https?:\/\//, '')
})

const appVersion = computed(() => {
  return authStore.healthData?.version ? `v${authStore.healthData.version}` : 'v2.2.0'
})

const isCheckingUpdate = ref(false)
const updateStatusMessage = ref('')

async function checkForAppUpdate() {
  if (isCheckingUpdate.value) return
  isCheckingUpdate.value = true
  updateStatusMessage.value = lang.value === 'pl' ? 'Sprawdzam...' : 'Checking...'
  try {
    await authStore.checkHealth()
    if (typeof window !== 'undefined' && window.__checkPwaUpdate) {
      await window.__checkPwaUpdate()
    }
    updateStatusMessage.value = lang.value === 'pl' ? 'Aktualna' : 'Up to date'
  } catch {
    updateStatusMessage.value = lang.value === 'pl' ? 'Błąd' : 'Error'
  } finally {
    setTimeout(() => {
      isCheckingUpdate.value = false
      updateStatusMessage.value = ''
    }, 2500)
  }
}

function navAriaLabel(item: (typeof navItems)[number]): string {
  let label = t(item.labelKey)
  if (item.id === 'approvals' && approvalsCount.value > 0) label += ` (${approvalsCount.value})`
  else if (item.id === 'wallet' && isBudgetLow.value) label += ` — ${t('app.budget.low')}`
  return label
}

const wsColorClass = computed(() => {
  switch (wsStatus.value) {
    case 'CONNECTED':
      return 'text-emerald-400'
    case 'CONNECTING':
    case 'RECONNECTING':
      return 'text-amber-300'
    default:
      return 'text-slate-300'
  }
})

function toggleLang() {
  setUiLang(lang.value === 'en' ? 'pl' : 'en')
}

function toggleTheme() {
  themeStore.toggleMode()
}

onMounted(() => {
  currentView.value = viewFromHash()
  window.addEventListener('hashchange', onHashChange)
  authStore.init()
  approvalsStore.init()
  chatStore.init()
  walletStore.fetchWallet()
  stopSwarmRealtime = swarm.initRealtime()
})

onUnmounted(() => {
  window.removeEventListener('hashchange', onHashChange)
  stopSwarmRealtime?.()
  stopSwarmRealtime = null
})

function onNavigate(viewName: string) {
  currentView.value = viewName
}

async function onModalDecide(actionId: string, approved: boolean, reason: string, remember: boolean) {
  await approvalsStore.decide(actionId, {
    approved,
    reason,
    remember
  })
}
</script>
