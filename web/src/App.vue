<template>
  <div class="h-screen w-screen flex flex-col md:flex-row bg-botex-950 text-slate-100 font-sans overflow-hidden">
    <!-- Tablet / Desktop Sidebar -->
    <aside
      :class="[
        'hidden md:flex flex-col bg-botex-900 border-r border-botex-800 shrink-0 transition-all duration-150 select-none',
        sidebarCollapsed ? 'w-16' : 'w-56 lg:w-64'
      ]"
    >
      <!-- App Brand -->
      <div class="px-3.5 py-3.5 border-b border-botex-800 flex items-center justify-between gap-2">
        <div class="flex items-center space-x-2.5 min-w-0">
          <button
            type="button"
            class="w-8 h-8 rounded-lg bg-botex-950 border border-botex-700 hover:border-sky-500/50 flex items-center justify-center shrink-0 transition"
            title="Toggle sidebar width"
            @click="sidebarCollapsed = !sidebarCollapsed"
          >
            <span class="font-mono font-bold text-sky-400 text-sm">BX</span>
          </button>
          <div v-if="!sidebarCollapsed" class="truncate">
            <h1 class="text-sm font-bold text-slate-100 tracking-tight truncate">BoteX Control</h1>
            <p class="text-[11px] text-slate-300 font-mono truncate">{{ t('app.subtitle') }}</p>
          </div>
        </div>

        <!-- Connection Status, Theme & Language Switcher -->
        <div v-if="!sidebarCollapsed" class="flex items-center space-x-1.5 shrink-0">
          <button
            type="button"
            class="p-1.5 rounded bg-botex-950 border border-botex-700 text-slate-200 hover:border-sky-500/50 transition"
            :title="theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'"
            @click="toggleTheme"
          >
            <SunIcon v-if="theme === 'dark'" class="w-3.5 h-3.5 text-amber-400" />
            <MoonIcon v-else class="w-3.5 h-3.5 text-sky-400" />
          </button>
          <button
            type="button"
            class="px-1.5 py-0.5 rounded bg-botex-950 border border-botex-700 text-[10px] font-mono font-semibold text-slate-200 hover:border-sky-500/50 transition uppercase"
            title="Switch interface language (EN / PL)"
            @click="toggleLang"
          >
            {{ lang }}
          </button>
          <div
            class="w-2.5 h-2.5 rounded-full"
            :class="authStore.isServerHealthy ? 'bg-emerald-400' : 'bg-rose-500 animate-pulse'"
            :title="authStore.isServerHealthy ? t('app.online') : t('app.offline')"
          ></div>
        </div>
      </div>

      <!-- Navigation Links -->
      <nav class="flex-1 px-2.5 py-3 space-y-1 overflow-y-auto">
        <button
          v-for="item in navItems"
          :key="item.id"
          type="button"
          :title="t(item.labelKey)"
          :class="[
            'w-full min-h-[40px] flex items-center justify-between px-3 py-2 rounded-lg text-xs font-medium transition-colors',
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
            />
            <span v-if="!sidebarCollapsed" class="truncate">{{ t(item.labelKey) }}</span>
          </div>

          <!-- Pending count badge -->
          <span
            v-if="!sidebarCollapsed && item.id === 'approvals' && approvalsCount > 0"
            class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-950 text-amber-300 border border-amber-500/40"
          >
            {{ approvalsCount }}
          </span>
          <!-- Low budget warning badge -->
          <span
            v-if="!sidebarCollapsed && item.id === 'wallet' && isBudgetLow"
            class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-rose-950 text-rose-300 border border-rose-500/40"
            :title="t('app.budget.low')"
          >
            !$
          </span>
        </button>
      </nav>

      <!-- Sidebar Footer -->
      <div v-if="!sidebarCollapsed" class="p-3 border-t border-botex-800 bg-botex-950/70 text-xs">
        <div
          v-if="isBudgetLow"
          class="mb-2 px-2 py-1 rounded bg-rose-950/90 border border-rose-500/40 text-rose-200 text-[11px] font-mono flex items-center justify-between cursor-pointer"
          @click="currentView = 'wallet'"
        >
          <span class="truncate">{{ t('app.budget.low') }}</span>
        </div>
        <div class="flex items-center justify-between text-[11px] text-slate-300 font-mono mb-1">
          <span class="truncate max-w-[140px]">{{ backendHost }}</span>
          <span :class="wsColorClass">{{ wsStatus }}</span>
        </div>
        <div class="flex items-center justify-between text-[11px] text-slate-300 font-mono">
          <span>v1.0.0</span>
          <span v-if="authStore.accessToken" class="text-sky-300">{{ t('app.auth.bearer') }}</span>
          <span v-else class="text-slate-300">{{ t('app.auth.local') }}</span>
        </div>
      </div>
    </aside>

    <!-- Main Content Area -->
    <main class="flex-1 flex flex-col min-w-0 h-full relative overflow-hidden">
      <!-- Mobile Top Bar -->
      <header class="md:hidden px-3.5 py-2.5 bg-botex-900 border-b border-botex-800 flex items-center justify-between shrink-0">
        <div class="flex items-center space-x-2.5">
          <div class="w-7 h-7 rounded-lg bg-botex-950 border border-botex-700 flex items-center justify-center text-sky-400 font-bold font-mono text-xs">
            BX
          </div>
          <span class="text-sm font-bold text-slate-100">BoteX Control</span>
        </div>

        <div class="flex items-center space-x-2">
          <button
            v-if="isBudgetLow"
            type="button"
            class="min-h-[36px] px-2 py-1 rounded text-xs font-mono font-bold bg-rose-950 text-rose-300 border border-rose-500/40"
            :title="t('app.budget.low')"
            @click="currentView = 'wallet'"
          >
            !$
          </button>
          <button
            type="button"
            class="min-h-[36px] min-w-[36px] p-1.5 rounded bg-botex-950 border border-botex-700 text-slate-200 flex items-center justify-center"
            :title="theme === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode'"
            @click="toggleTheme"
          >
            <SunIcon v-if="theme === 'dark'" class="w-4 h-4 text-amber-400" />
            <MoonIcon v-else class="w-4 h-4 text-sky-400" />
          </button>
          <button
            type="button"
            class="min-h-[36px] px-2.5 py-1 rounded bg-botex-950 border border-botex-700 text-xs font-mono font-semibold text-slate-200 uppercase"
            @click="toggleLang"
          >
            {{ lang }}
          </button>
          <button
            v-if="approvalsCount > 0"
            type="button"
            class="min-h-[36px] px-2.5 py-1 rounded text-xs font-mono font-bold bg-amber-950 text-amber-300 border border-amber-500/40"
            @click="currentView = 'approvals'"
          >
            {{ approvalsCount }} Gate
          </button>
          <div
            class="w-2.5 h-2.5 rounded-full"
            :class="authStore.isServerHealthy ? 'bg-emerald-400' : 'bg-rose-500'"
          ></div>
        </div>
      </header>

      <!-- Connection Warning Banner if offline -->
      <div
        v-if="!authStore.isOnline || !authStore.isServerHealthy"
        class="bg-amber-950/90 border-b border-amber-500/40 text-amber-200 text-xs px-4 py-1.5 text-center flex items-center justify-center space-x-2 shrink-0"
      >
        <AlertTriangleIcon class="w-3.5 h-3.5 text-amber-400 shrink-0" />
        <span class="truncate">
          {{ !authStore.isOnline ? t('app.offline.banner') : t('app.unreachable.banner') + authStore.backendUrl }}
        </span>
      </div>

      <!-- Dynamic Views -->
      <div class="flex-1 overflow-hidden relative">
        <ChatView
          v-if="currentView === 'chat'"
          @navigate="onNavigate"
        />
        <TasksView
          v-else-if="currentView === 'tasks'"
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

      <!-- Mobile Bottom Navigation Bar (44x44px minimum touch target) -->
      <nav class="md:hidden bg-botex-900 border-t border-botex-800 grid grid-cols-6 py-1 px-1 shrink-0 z-30">
        <button
          v-for="item in navItems"
          :key="item.id"
          type="button"
          :class="[
            'min-h-[48px] min-w-[44px] flex flex-col items-center justify-center py-1 rounded-lg text-[10px] font-medium transition relative',
            currentView === item.id ? 'text-sky-300 bg-botex-800/70' : 'text-slate-300 hover:text-white'
          ]"
          @click="currentView = item.id"
        >
          <div class="relative">
            <component :is="item.icon" class="w-4 h-4" />
            <span
              v-if="item.id === 'approvals' && approvalsCount > 0"
              class="absolute -top-1 -right-2.5 w-4 h-4 bg-amber-500 text-botex-950 text-[9px] font-mono font-bold rounded-full flex items-center justify-center"
            >
              {{ approvalsCount }}
            </span>
          </div>
          <span class="mt-1 truncate max-w-[54px]">{{ t(item.shortKey) }}</span>
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
import { ref, computed, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useAuthStore } from './stores/auth'
import { useApprovalsStore } from './stores/approvals'
import { useChatStore } from './stores/chat'
import { useWalletStore } from './stores/wallet'
import { useI18n } from './i18n'
import ChatView from './views/ChatView.vue'
import TasksView from './views/TasksView.vue'
import ApprovalsView from './views/ApprovalsView.vue'
import MemoryView from './views/MemoryView.vue'
import WalletView from './views/WalletView.vue'
import SettingsView from './views/SettingsView.vue'
import ApprovalModal from './components/approvals/ApprovalModal.vue'
import {
  MessageSquare as MessageSquareIcon,
  Cpu as CpuIcon,
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
const { lang, theme, setUiLang, toggleTheme, t } = useI18n()

const { count: approvalsCount, activeModalAction } = storeToRefs(approvalsStore)
const { wsStatus } = storeToRefs(authStore)
const { isBudgetLow } = storeToRefs(walletStore)

const currentView = ref<string>('chat')
const sidebarCollapsed = ref<boolean>(false)

const navItems = [
  { id: 'chat', labelKey: 'nav.chat', shortKey: 'nav.chat.short', icon: MessageSquareIcon },
  { id: 'tasks', labelKey: 'nav.tasks', shortKey: 'nav.tasks.short', icon: CpuIcon },
  { id: 'approvals', labelKey: 'nav.approvals', shortKey: 'nav.approvals.short', icon: ShieldAlertIcon },
  { id: 'memory', labelKey: 'nav.memory', shortKey: 'nav.memory.short', icon: DatabaseIcon },
  { id: 'wallet', labelKey: 'nav.wallet', shortKey: 'nav.wallet.short', icon: WalletIcon },
  { id: 'settings', labelKey: 'nav.settings', shortKey: 'nav.settings.short', icon: SettingsIcon }
]

const backendHost = computed(() => {
  return authStore.backendUrl.replace(/^https?:\/\//, '')
})

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

onMounted(() => {
  authStore.init()
  approvalsStore.init()
  chatStore.init()
  walletStore.fetchKeyStatus()
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
