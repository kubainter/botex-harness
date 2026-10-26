<template>
  <Teleport to="body">
    <!-- Backdrop -->
    <Transition
      enter-active-class="transition-opacity duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition-opacity duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="isOpen"
        class="fixed inset-0 z-50 bg-botex-950/80 backdrop-blur-sm"
        @click="$emit('close')"
      />
    </Transition>

    <!-- Slide-over panel -->
    <Transition
      enter-active-class="transform transition duration-200 ease-out"
      enter-from-class="translate-x-full"
      enter-to-class="translate-x-0"
      leave-active-class="transform transition duration-150 ease-in"
      leave-from-class="translate-x-0"
      leave-to-class="translate-x-full"
    >
      <div
        v-if="isOpen"
        class="fixed inset-y-0 right-0 z-50 w-full max-w-md bg-botex-900 border-l border-botex-800 shadow-2xl flex flex-col h-full text-slate-100 pt-safe pr-safe pb-safe"
      >
        <!-- Header -->
        <div class="px-4 py-3.5 border-b border-botex-800 flex items-center justify-between gap-2">
          <div class="flex items-center space-x-2">
            <HistoryIcon class="w-4 h-4 text-sky-400" />
            <h3 class="text-sm font-semibold text-slate-100 font-mono">
              {{ t('chat.history.title') }}
            </h3>
            <span
              v-if="sessions.length > 0"
              class="px-2 py-0.5 rounded-full text-xs font-mono bg-botex-800 text-sky-300 font-semibold"
            >
              {{ sessions.length }}
            </span>
          </div>

          <div class="flex items-center space-x-1">
            <button
              type="button"
              class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] rounded-lg text-slate-300 hover:text-white hover:bg-botex-800 transition flex items-center justify-center"
              title="New Chat Session"
              @click="handleNewSession"
            >
              <PlusIcon class="w-4 h-4" />
            </button>
            <button
              type="button"
              class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] rounded-lg text-slate-300 hover:text-white hover:bg-botex-800 transition flex items-center justify-center"
              title="Refresh Sessions"
              :disabled="isLoading"
              @click="$emit('refresh')"
            >
              <RefreshCwIcon class="w-4 h-4" :class="{ 'animate-spin': isLoading }" />
            </button>
            <button
              type="button"
              class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] rounded-lg text-slate-300 hover:text-white hover:bg-botex-800 transition flex items-center justify-center"
              title="Close panel"
              @click="$emit('close')"
            >
              <XIcon class="w-4 h-4" />
            </button>
          </div>
        </div>

        <!-- Filter Search Bar -->
        <div class="p-3 border-b border-botex-800 bg-botex-950/50">
          <div class="relative">
            <SearchIcon class="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
            <input
              v-model="searchQuery"
              type="text"
              :placeholder="t('chat.history.search')"
              class="w-full bg-botex-950 border border-botex-700 rounded-lg pl-9 pr-3 py-2 text-base sm:text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-sky-500 font-sans"
            />
          </div>
        </div>

        <!-- Sessions List -->
        <div class="flex-1 overflow-y-auto overscroll-contain p-3 space-y-2">
          <div
            v-if="filteredSessions.length === 0"
            class="p-6 text-center text-xs text-slate-300 space-y-2"
          >
            <HistoryIcon class="w-8 h-8 text-slate-400 mx-auto" />
            <p>{{ t('chat.history.empty') }}</p>
            <p class="text-xs text-slate-400">
              New chat sessions are automatically persisted to <code class="font-mono text-sky-300">.botex/chat_sessions/</code>.
            </p>
          </div>

          <div
            v-for="s in filteredSessions"
            :key="s.session_id"
            :class="[
              'rounded-xl border p-3 cursor-pointer transition text-xs space-y-2 group',
              s.session_id === currentSessionId
                ? 'bg-sky-950/30 border-sky-500/50 ring-1 ring-sky-500/20'
                : 'bg-botex-950/70 border-botex-800 hover:border-botex-700 hover:bg-botex-950'
            ]"
            @click="handleSelect(s.session_id)"
          >
            <div class="flex items-start justify-between gap-2">
              <span class="font-semibold text-slate-100 group-hover:text-white line-clamp-1">
                {{ s.title }}
              </span>
              <div class="flex items-center space-x-1 shrink-0">
                <button
                  type="button"
                  title="Save conversation to Memory Vault"
                  class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] p-0 rounded text-slate-300 hover:text-sky-300 hover:bg-botex-800 transition flex items-center justify-center"
                  @click.stop="$emit('save-memory', s)"
                >
                  <BookmarkPlusIcon class="w-3.5 h-3.5" />
                </button>
                <button
                  type="button"
                  title="Delete conversation"
                  class="min-h-[44px] min-w-[44px] lg:min-h-[36px] lg:min-w-[36px] p-0 rounded text-slate-300 hover:text-rose-400 hover:bg-botex-800 transition flex items-center justify-center"
                  @click.stop="handleDelete(s.session_id)"
                >
                  <Trash2Icon class="w-3.5 h-3.5" />
                </button>
              </div>
            </div>

            <div class="flex flex-wrap items-center justify-between gap-1 text-xs font-mono text-slate-300">
              <div class="flex items-center space-x-1.5 truncate">
                <span class="truncate text-slate-300">{{ formatModel(s.model) }}</span>
                <span class="px-1.5 py-px rounded bg-botex-800 text-slate-200 text-xs">
                  {{ s.message_count }} msg
                </span>
              </div>
              <span class="text-slate-400 text-xs shrink-0">
                {{ formatTime(s.updated_at) }}
              </span>
            </div>
          </div>
        </div>

        <!-- Footer -->
        <div class="p-3 border-t border-botex-800 bg-botex-950/60 flex items-center justify-between text-xs font-mono text-slate-300">
          <span>Active: {{ currentSessionId.slice(0, 8) }}...</span>
          <button
            type="button"
            class="text-sky-400 hover:text-sky-300 transition"
            @click="handleNewSession"
          >
            + Start New Conversation
          </button>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ChatSessionSummary } from '../../api/types'
import { useI18n } from '../../i18n'
import {
  History as HistoryIcon,
  Search as SearchIcon,
  Plus as PlusIcon,
  RefreshCw as RefreshCwIcon,
  X as XIcon,
  Trash2 as Trash2Icon,
  BookmarkPlus as BookmarkPlusIcon
} from 'lucide-vue-next'

const props = defineProps<{
  isOpen: boolean
  sessions: ChatSessionSummary[]
  currentSessionId: string
  isLoading?: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'select', sessionId: string): void
  (e: 'new-session'): void
  (e: 'delete', sessionId: string): void
  (e: 'save-memory', session: ChatSessionSummary): void
  (e: 'refresh'): void
}>()

const { t } = useI18n()
const searchQuery = ref('')

const filteredSessions = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return props.sessions
  return props.sessions.filter(
    (s) =>
      s.title.toLowerCase().includes(q) ||
      s.model.toLowerCase().includes(q) ||
      s.session_id.toLowerCase().includes(q)
  )
})

function formatModel(slug: string): string {
  if (!slug) return 'Default'
  const parts = slug.split('/')
  return parts[parts.length - 1]
}

function formatTime(timestamp: number): string {
  if (!timestamp) return ''
  const date = new Date(timestamp * 1000)
  const now = new Date()
  const diffHours = (now.getTime() - date.getTime()) / (1000 * 60 * 60)
  if (diffHours < 24) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  }
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

function handleSelect(sessionId: string) {
  emit('select', sessionId)
  emit('close')
}

function handleNewSession() {
  emit('new-session')
  emit('close')
}

function handleDelete(sessionId: string) {
  if (window.confirm(t('chat.history.delete_confirm'))) {
    emit('delete', sessionId)
  }
}
</script>
