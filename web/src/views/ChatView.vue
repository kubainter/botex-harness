<template>
  <div class="h-full flex flex-row overflow-hidden bg-botex-950">
    <!-- Primary Chat Column -->
    <div class="flex-1 min-w-0 h-full flex flex-col">
      <ChatWindow
        @convert-to-task="onConvertToTask"
        @switch-mode="onSwitchMode"
      />
    </div>

    <!-- Collapsible Right-Hand Inspector (Desktop 1024px+ open by default, toggleable on Tablet) -->
    <aside
      v-if="showInspector"
      class="hidden lg:flex flex-col w-80 xl:w-96 border-l border-botex-800 bg-botex-900/70 shrink-0 h-full overflow-y-auto"
    >
      <!-- Inspector Header with Tabs -->
      <div class="px-3 py-2.5 border-b border-botex-800 flex items-center justify-between gap-1">
        <div class="flex items-center space-x-1">
          <button
            type="button"
            :class="[
              'min-h-[32px] px-2.5 py-1 rounded text-xs font-medium font-mono transition',
              activeTab === 'inspector'
                ? 'bg-botex-800 text-sky-300 border border-botex-700'
                : 'text-slate-400 hover:text-slate-200'
            ]"
            @click="activeTab = 'inspector'"
          >
            {{ t('chat.inspector.title') }}
          </button>
          <button
            type="button"
            :class="[
              'min-h-[32px] px-2.5 py-1 rounded text-xs font-medium font-mono transition flex items-center space-x-1',
              activeTab === 'history'
                ? 'bg-botex-800 text-sky-300 border border-botex-700'
                : 'text-slate-400 hover:text-slate-200'
            ]"
            @click="activeTab = 'history'"
          >
            <span>{{ t('chat.history.button') }}</span>
            <span
              v-if="chatStore.sessions.length > 0"
              class="px-1.5 py-px rounded-full text-xs font-mono bg-botex-950 text-sky-300 font-semibold"
            >
              {{ chatStore.sessions.length }}
            </span>
          </button>
        </div>
        <button
          type="button"
          class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] p-0 rounded hover:bg-botex-800 text-slate-300 hover:text-white transition flex items-center justify-center"
          title="Collapse inspector"
          @click="showInspector = false"
        >
          <XIcon class="w-3.5 h-3.5" />
        </button>
      </div>

      <!-- Tab: History -->
      <div v-if="activeTab === 'history'" class="p-3 space-y-3 flex-1 overflow-y-auto text-xs">
        <div class="flex items-center justify-between gap-2">
          <div class="relative flex-1">
            <SearchIcon class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              v-model="historyFilter"
              type="text"
              :placeholder="t('chat.history.search')"
              class="w-full min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg pl-8 pr-2.5 py-1.5 text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-sky-500 font-sans"
            />
          </div>
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-2.5 py-1.5 rounded-lg bg-botex-800 hover:bg-botex-700 text-slate-200 text-xs font-medium transition flex items-center space-x-1"
            title="Start new conversation"
            @click="chatStore.newSession"
          >
            <PlusIcon class="w-3.5 h-3.5" />
            <span>Nowy</span>
          </button>
        </div>

        <div v-if="filteredSessions.length === 0" class="p-6 text-center text-slate-400 space-y-1">
          <HistoryIcon class="w-6 h-6 mx-auto text-slate-500 mb-2" />
          <p>{{ t('chat.history.empty') }}</p>
        </div>

        <div
          v-for="s in filteredSessions"
          :key="s.session_id"
          :class="[
            'rounded-lg border p-2.5 cursor-pointer transition space-y-1.5',
            s.session_id === chatStore.sessionId
              ? 'bg-sky-950/30 border-sky-500/50'
              : 'bg-botex-950 border-botex-800 hover:border-botex-700'
          ]"
          @click="chatStore.switchSession(s.session_id)"
        >
          <div class="flex items-start justify-between gap-2">
            <span class="font-medium text-slate-100 line-clamp-1 text-xs">
              {{ s.title }}
            </span>
            <button
              type="button"
              class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] p-0 rounded text-slate-400 hover:text-rose-400 transition flex items-center justify-center"
              title="Delete session"
              @click.stop="onDeleteSession(s.session_id)"
            >
              <Trash2Icon class="w-3.5 h-3.5" />
            </button>
          </div>
          <div class="flex items-center justify-between text-xs font-mono text-slate-400">
            <span class="truncate">{{ formatModelSlug(s.model) }}</span>
            <span>{{ s.message_count }} msgs</span>
          </div>
        </div>
      </div>

      <!-- Tab: Workspace Inspector -->
      <div v-else class="p-4 space-y-5 text-xs flex-1 overflow-y-auto">
        <!-- Active Model & Provider Card -->
        <div class="rounded-lg border border-botex-800 bg-botex-950 p-3.5 space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-mono uppercase text-slate-300">Active Runtime</span>
            <span
              class="px-1.5 py-0.5 rounded text-xs font-mono font-semibold"
              :class="
                chatStore.activeModelMeta?.is_free
                  ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/40'
                  : 'bg-sky-950 text-sky-300 border border-sky-500/40'
              "
            >
              {{ chatStore.selectedProvider }}
            </span>
          </div>
          <div class="font-semibold text-slate-100 text-xs truncate">
            {{ chatStore.activeModelMeta?.name || chatStore.selectedModel }}
          </div>
          <div class="font-mono text-xs text-slate-400 truncate">
            {{ chatStore.selectedModel }}
          </div>
          <div class="pt-2 border-t border-botex-800 flex items-center justify-between font-mono text-xs text-slate-300">
            <span>Context: {{ formatCtx(chatStore.activeModelMeta?.context_length || 131072) }}</span>
            <span>
              {{ chatStore.activeModelMeta?.is_zdr_compliant ? 'ZDR Enforced' : 'Non-ZDR / Free' }}
            </span>
          </div>
        </div>

        <!-- Pending Operator Approvals / Diff Inspector -->
        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-mono uppercase text-slate-300 font-semibold">
              {{ t('chat.inspector.approvals') }} ({{ approvalsStore.pendingActions.length }})
            </span>
            <button
              type="button"
              class="text-xs text-sky-400 hover:text-sky-300 font-mono"
              @click="emit('navigate', 'approvals')"
            >
              Open view →
            </button>
          </div>

          <div
            v-if="approvalsStore.pendingActions.length === 0"
            class="rounded-lg border border-botex-800 bg-botex-950/60 p-3 text-slate-300 text-xs"
          >
            No tool mutations waiting for operator approval.
          </div>
          <div
            v-for="act in approvalsStore.pendingActions.slice(0, 2)"
            :key="act.action_id"
            class="rounded-lg border border-amber-500/40 bg-amber-950/20 p-3 space-y-1.5"
          >
            <div class="flex items-center justify-between">
              <span class="font-mono font-semibold text-amber-300">{{ act.tool_name }}</span>
              <span class="text-xs font-mono text-slate-300">{{ act.risk_level }}</span>
            </div>
            <p v-if="act.description" class="text-xs text-slate-200 line-clamp-2">
              {{ act.description }}
            </p>
            <button
              type="button"
              class="w-full mt-1 min-h-[40px] py-1.5 rounded bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/40 text-amber-200 font-medium text-xs transition"
              @click="approvalsStore.openModal(act)"
            >
              Inspect Diff & Decide
            </button>
          </div>
        </div>

        <!-- Background Tasks Summary -->
        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-mono uppercase text-slate-300 font-semibold">
              {{ t('chat.inspector.tasks') }} ({{ tasksStore.tasks.length }})
            </span>
            <button
              type="button"
              class="text-xs text-sky-400 hover:text-sky-300 font-mono"
              @click="emit('navigate', 'tasks')"
            >
              All tasks →
            </button>
          </div>

          <div
            v-if="tasksStore.tasks.length === 0"
            class="rounded-lg border border-botex-800 bg-botex-950/60 p-3 text-slate-300 text-xs"
          >
            No background tasks queued in this session.
          </div>
          <div
            v-for="task in tasksStore.tasks.slice(0, 3)"
            :key="task.task_id"
            class="rounded-lg border border-botex-800 bg-botex-950 p-2.5 flex items-center justify-between cursor-pointer hover:border-botex-700 transition"
            @click="emit('navigate', 'tasks')"
          >
            <span class="font-mono text-slate-200">{{ task.task_id }}</span>
            <span
              class="px-2 py-0.5 rounded text-xs font-mono font-semibold"
              :class="
                task.status === 'DONE'
                  ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/30'
                  : task.status === 'RUNNING'
                  ? 'bg-sky-950 text-sky-300 border border-sky-500/30'
                  : 'bg-botex-800 text-slate-300'
              "
            >
              {{ task.status }}
            </span>
          </div>
        </div>

        <!-- Memory Vault Quick Notes -->
        <div class="space-y-2">
          <div class="flex items-center justify-between">
            <span class="text-xs font-mono uppercase text-slate-300 font-semibold">
              {{ t('chat.inspector.memory') }} ({{ memoryStore.entries.length }})
            </span>
            <button
              type="button"
              class="text-xs text-sky-400 hover:text-sky-300 font-mono"
              @click="emit('navigate', 'memory')"
            >
              Vault →
            </button>
          </div>

          <div
            v-if="memoryStore.entries.length === 0"
            class="rounded-lg border border-botex-800 bg-botex-950/60 p-3 text-slate-300 text-xs"
          >
            No saved memory notes loaded yet.
          </div>
          <div
            v-for="note in memoryStore.entries.slice(0, 3)"
            :key="note.memory_id"
            class="rounded-lg border border-botex-800 bg-botex-950 p-2.5 space-y-1 cursor-pointer hover:border-botex-700 transition"
            @click="emit('navigate', 'memory')"
          >
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium text-slate-100 truncate">{{ note.title }}</span>
              <span class="text-xs font-mono text-sky-300 uppercase shrink-0">{{ note.kind }}</span>
            </div>
            <p class="text-xs text-slate-300 line-clamp-2 font-mono">
              {{ note.content }}
            </p>
          </div>
        </div>
      </div>
    </aside>

    <!-- Floating Re-open Button when Inspector is collapsed on Desktop -->
    <button
      v-else
      type="button"
      title="Expand Workspace Inspector"
      class="hidden lg:flex items-center justify-center w-8 border-l border-botex-800 bg-botex-900 hover:bg-botex-800 text-slate-300 hover:text-white transition"
      @click="showInspector = true"
    >
      <PanelRightIcon class="w-4 h-4" />
    </button>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import ChatWindow from '../components/chat/ChatWindow.vue'
import { useChatStore } from '../stores/chat'
import { useApprovalsStore } from '../stores/approvals'
import { useTasksStore } from '../stores/tasks'
import { useMemoryStore } from '../stores/memory'
import { useI18n } from '../i18n'
import {
  PanelRight as PanelRightIcon,
  X as XIcon,
  Search as SearchIcon,
  Trash2 as Trash2Icon,
  Plus as PlusIcon,
  History as HistoryIcon
} from 'lucide-vue-next'

const emit = defineEmits<{
  (e: 'navigate', viewName: string): void
}>()

const chatStore = useChatStore()
const approvalsStore = useApprovalsStore()
const tasksStore = useTasksStore()
const memoryStore = useMemoryStore()
const { t } = useI18n()

const showInspector = ref(true)
const activeTab = ref<'inspector' | 'history'>('inspector')
const historyFilter = ref('')

const filteredSessions = computed(() => {
  const q = historyFilter.value.trim().toLowerCase()
  if (!q) return chatStore.sessions
  return chatStore.sessions.filter(
    (s) =>
      s.title.toLowerCase().includes(q) ||
      s.model.toLowerCase().includes(q) ||
      s.session_id.toLowerCase().includes(q)
  )
})

function formatModelSlug(slug: string): string {
  if (!slug) return 'Default'
  const parts = slug.split('/')
  return parts[parts.length - 1]
}

async function onDeleteSession(sessionId: string) {
  if (window.confirm(t('chat.history.delete_confirm'))) {
    await chatStore.deleteSession(sessionId)
  }
}

onMounted(() => {
  tasksStore.fetchTasks()
  memoryStore.fetchMemory()
  chatStore.fetchSessions()
})

function formatCtx(ctx: number): string {
  if (ctx >= 1000000) return `${Math.round(ctx / 1000000)}M`
  return `${Math.round(ctx / 1024)}k`
}

function onConvertToTask(_draft: string) {
  emit('navigate', 'tasks')
}

function onSwitchMode(mode: 'chat' | 'agent') {
  if (mode === 'agent') {
    emit('navigate', 'tasks')
  }
}
</script>

