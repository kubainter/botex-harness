<template>
  <div class="flex flex-col h-full bg-botex-950">
    <!-- Header / Control Bar -->
    <div class="px-3 sm:px-4 py-2.5 bg-botex-900 border-b border-botex-800 flex flex-wrap items-center justify-between gap-2">
      <!-- Mode Switcher -->
      <div class="flex items-center bg-botex-950 p-1 rounded-lg border border-botex-800">
        <button
          type="button"
          :class="[
            'min-h-[38px] sm:min-h-[32px] px-3 py-1 rounded-md text-xs font-medium transition-colors flex items-center space-x-1.5',
            activeMode === 'chat'
              ? 'bg-botex-800 text-slate-100 border border-botex-700'
              : 'text-slate-300 hover:text-white'
          ]"
          @click="setMode('chat')"
        >
          <TerminalIcon class="w-3.5 h-3.5 text-sky-400" />
          <span>{{ t('chat.mode.chat') }}</span>
        </button>
        <button
          type="button"
          :class="[
            'min-h-[38px] sm:min-h-[32px] px-3 py-1 rounded-md text-xs font-medium transition-colors flex items-center space-x-1.5',
            activeMode === 'agent'
              ? 'bg-botex-800 text-slate-100 border border-botex-700'
              : 'text-slate-300 hover:text-white'
          ]"
          @click="setMode('agent')"
        >
          <CpuIcon class="w-3.5 h-3.5 text-emerald-400" />
          <span>{{ t('chat.mode.agent') }}</span>
        </button>
      </div>

      <!-- Searchable Model Trigger & Session Controls -->
      <div class="flex items-center gap-2 flex-1 sm:flex-initial justify-end">
        <button
          type="button"
          data-testid="model-selector-trigger"
          class="min-h-[44px] sm:min-h-[36px] px-3 py-1.5 bg-botex-950 hover:bg-botex-800 border border-botex-700 hover:border-slate-500 rounded-lg text-xs text-slate-100 transition flex items-center justify-between gap-2 max-w-full sm:max-w-xs"
          title="Select execution model (Ctrl+K)"
          @click="showModelModal = true"
        >
          <div class="flex items-center gap-2 truncate">
            <span class="font-medium text-slate-100 truncate">{{ currentModelDisplayName }}</span>
            <span
              v-if="isCurrentModelFree"
              class="px-1.5 py-0.2 rounded text-[10px] font-mono font-semibold bg-emerald-950 text-emerald-300 border border-emerald-500/40 shrink-0"
            >
              FREE
            </span>
            <span
              v-else
              class="hidden md:inline-block px-1.5 py-0.2 rounded text-[10px] font-mono text-slate-300 bg-botex-900 border border-botex-700 shrink-0"
            >
              {{ selectedProviderLabel }}
            </span>
          </div>
          <ChevronDownIcon class="w-3.5 h-3.5 text-slate-300 shrink-0" />
        </button>

        <button
          type="button"
          :title="t('chat.new_session')"
          class="min-w-[44px] min-h-[44px] sm:min-w-[36px] sm:min-h-[36px] p-2 bg-botex-950 hover:bg-botex-800 border border-botex-700 rounded-lg text-slate-200 hover:text-white transition flex items-center justify-center"
          @click="chatStore.newSession"
        >
          <PlusIcon class="w-4 h-4" />
        </button>

        <!-- History Drawer Button -->
        <button
          type="button"
          :title="t('chat.history.button')"
          class="min-h-[44px] sm:min-h-[36px] px-2.5 py-1.5 bg-botex-950 hover:bg-botex-800 border border-botex-700 rounded-lg text-xs text-slate-200 hover:text-white transition flex items-center space-x-1.5"
          @click="openHistoryDrawer"
        >
          <HistoryIcon class="w-3.5 h-3.5 text-sky-400" />
          <span class="hidden md:inline">{{ t('chat.history.button') }}</span>
          <span
            v-if="sessions.length > 0"
            class="px-1.5 py-0.2 rounded-full text-[10px] font-mono bg-botex-800 text-sky-300 font-semibold"
          >
            {{ sessions.length }}
          </span>
        </button>

        <!-- Save to Memory Button -->
        <button
          v-if="messages.length > 0"
          type="button"
          :title="t('chat.save_memory.button')"
          class="min-h-[44px] sm:min-h-[36px] px-2.5 py-1.5 bg-botex-950 hover:bg-botex-800 border border-botex-700 rounded-lg text-xs text-slate-200 hover:text-sky-300 transition flex items-center space-x-1.5"
          @click="openSaveMemoryModal"
        >
          <BookmarkPlusIcon class="w-3.5 h-3.5 text-amber-400" />
          <span class="hidden md:inline">{{ t('chat.save_memory.button') }}</span>
        </button>

        <button
          v-if="messages.length > 0"
          type="button"
          class="min-h-[44px] sm:min-h-[36px] px-3 py-1.5 bg-sky-600/20 hover:bg-sky-600/30 border border-sky-500/40 rounded-lg text-xs font-medium text-sky-200 transition flex items-center space-x-1.5"
          @click="onConvertClick"
        >
          <ArrowRightCircleIcon class="w-3.5 h-3.5" />
          <span class="hidden sm:inline">{{ t('chat.convert_task') }}</span>
        </button>
      </div>
    </div>

    <!-- Messages List / Structured Empty State -->
    <div ref="scrollContainer" class="flex-1 overflow-y-auto p-3 sm:p-5 space-y-3">
      <div
        v-if="messages.length === 0"
        class="min-h-full flex flex-col items-center justify-center p-4 sm:p-8"
      >
        <div class="w-full max-w-xl rounded-xl border border-botex-800 bg-botex-900/70 p-5 sm:p-6 space-y-5">
          <div class="flex items-start justify-between gap-4 border-b border-botex-800 pb-4">
            <div class="space-y-1">
              <div class="flex items-center space-x-2">
                <TerminalIcon class="w-4 h-4 text-sky-400" />
                <h4 class="text-sm font-semibold text-slate-100">{{ t('chat.empty.title') }}</h4>
              </div>
              <p class="text-xs text-slate-300 leading-relaxed">
                {{ t('chat.empty.subtitle') }}
              </p>
            </div>
            <button
              type="button"
              class="shrink-0 px-2.5 py-1 rounded bg-botex-950 border border-botex-700 text-[11px] font-mono text-slate-200 hover:border-sky-500/50 transition"
              @click="showModelModal = true"
            >
              Ctrl+K · Models ({{ catalogModels.length }})
            </button>
          </div>

          <!-- Actionable Starter Prompts -->
          <div class="space-y-2">
            <span class="block text-[11px] font-mono uppercase tracking-wider text-slate-300">
              Quick Workspace Actions
            </span>
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-2 text-left">
              <button
                v-for="(item, idx) in quickPrompts"
                :key="idx"
                type="button"
                class="min-h-[44px] p-3 rounded-lg bg-botex-950 hover:bg-botex-800 border border-botex-800 hover:border-botex-600 transition text-left flex flex-col justify-between gap-1 group"
                @click="inputText = item.prompt"
              >
                <div class="flex items-center justify-between w-full">
                  <span class="font-mono text-[11px] font-semibold text-sky-300 group-hover:text-sky-200">
                    {{ item.tag }}
                  </span>
                  <span class="text-[10px] font-mono text-slate-400">{{ item.shortcut }}</span>
                </div>
                <span class="text-xs text-slate-200 leading-snug">{{ item.prompt }}</span>
              </button>
            </div>
          </div>

          <!-- Keyboard Shortcuts Bar -->
          <div class="pt-3 border-t border-botex-800 flex flex-wrap items-center justify-between gap-2 text-[11px] font-mono text-slate-300">
            <span><kbd class="px-1.5 py-0.5 rounded bg-botex-950 border border-botex-700">Enter</kbd> send</span>
            <span><kbd class="px-1.5 py-0.5 rounded bg-botex-950 border border-botex-700">Shift+Enter</kbd> newline</span>
            <span><kbd class="px-1.5 py-0.5 rounded bg-botex-950 border border-botex-700">Ctrl+K</kbd> switch model</span>
          </div>
        </div>
      </div>

      <MessageBubble
        v-for="msg in messages"
        :key="msg.id"
        :message="msg"
      />
    </div>

    <!-- Input Bar -->
    <div class="p-3 bg-botex-900 border-t border-botex-800">
      <form class="flex items-end space-x-2" @submit.prevent="handleSend">
        <div class="relative flex-1">
          <textarea
            v-model="inputText"
            rows="2"
            :placeholder="t('chat.input.placeholder')"
            class="w-full bg-botex-950 border border-botex-700 rounded-xl px-3.5 py-2.5 text-sm text-slate-100 placeholder-slate-400 resize-none focus:outline-none focus:ring-1 focus:ring-sky-500 focus:border-sky-500 font-sans"
            @keydown.enter.exact.prevent="handleSend"
          ></textarea>
        </div>
        <button
          type="submit"
          :disabled="!inputText.trim() || isStreaming"
          :title="t('chat.send')"
          class="min-h-[44px] min-w-[44px] h-11 px-4 rounded-xl bg-brand-blue hover:bg-blue-600 disabled:opacity-40 disabled:pointer-events-none text-white text-xs font-medium transition flex items-center justify-center space-x-1.5"
        >
          <SendIcon v-if="!isStreaming" class="w-4 h-4" />
          <Loader2Icon v-else class="w-4 h-4 animate-spin" />
          <span class="hidden sm:inline">{{ t('chat.send') }}</span>
        </button>
      </form>
    </div>

    <!-- Dynamic Searchable Model Selector Modal -->
    <ModelSelectorModal
      :is-open="showModelModal"
      :models="catalogModels"
      :selected-model="selectedModel"
      :selected-provider="selectedProvider"
      :is-loading="isLoadingModels"
      @close="showModelModal = false"
      @select="onSelectModel"
      @refresh="chatStore.fetchCatalogModels(true)"
    />

    <!-- Slide-over Conversation History Drawer -->
    <ChatHistoryDrawer
      :is-open="showHistoryDrawer"
      :sessions="sessions"
      :current-session-id="sessionId"
      :is-loading="isLoadingSessions"
      @close="showHistoryDrawer = false"
      @select="handleSelectSession"
      @new-session="chatStore.newSession"
      @delete="handleDeleteSession"
      @save-memory="handleOpenSaveMemoryForSession"
      @refresh="chatStore.fetchSessions"
    />

    <!-- Save to Memory Vault Modal -->
    <SaveToMemoryModal
      ref="saveModalRef"
      :is-open="showSaveMemoryModal"
      :default-title="memoryInitialTitle"
      :default-content="memoryInitialContent"
      default-kind="context"
      default-scope="local"
      @close="showSaveMemoryModal = false"
      @save="handleSaveToMemory"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useChatStore } from '../../stores/chat'
import { useI18n } from '../../i18n'
import type { CatalogModel, ChatSessionSummary } from '../../api/types'
import MessageBubble from './MessageBubble.vue'
import ModelSelectorModal from './ModelSelectorModal.vue'
import ChatHistoryDrawer from './ChatHistoryDrawer.vue'
import SaveToMemoryModal from './SaveToMemoryModal.vue'
import {
  Terminal as TerminalIcon,
  Cpu as CpuIcon,
  Plus as PlusIcon,
  Send as SendIcon,
  ArrowRightCircle as ArrowRightCircleIcon,
  Loader2 as Loader2Icon,
  ChevronDown as ChevronDownIcon,
  History as HistoryIcon,
  BookmarkPlus as BookmarkPlusIcon
} from 'lucide-vue-next'

const emit = defineEmits<{
  (e: 'convert-to-task', draft: string): void
  (e: 'switch-mode', mode: 'chat' | 'agent'): void
}>()

const chatStore = useChatStore()
const { t } = useI18n()
const {
  sessionId,
  messages,
  isStreaming,
  activeMode,
  selectedModel,
  selectedProvider,
  catalogModels,
  isLoadingModels,
  activeModelMeta,
  sessions,
  isLoadingSessions
} = storeToRefs(chatStore)

const inputText = ref('')
const showModelModal = ref(false)
const showHistoryDrawer = ref(false)
const showSaveMemoryModal = ref(false)
const saveModalRef = ref<InstanceType<typeof SaveToMemoryModal> | null>(null)
const memoryInitialTitle = ref('')
const memoryInitialContent = ref('')
const scrollContainer = ref<HTMLElement | null>(null)

const currentModelDisplayName = computed(() => {
  if (activeModelMeta.value?.name) return activeModelMeta.value.name
  return selectedModel.value
})

const isCurrentModelFree = computed(() => {
  if (activeModelMeta.value) return activeModelMeta.value.is_free
  return selectedModel.value.toLowerCase().includes(':free') || selectedProvider.value === 'openrouter-free'
})

const selectedProviderLabel = computed(() => {
  if (selectedProvider.value === 'openrouter-free') return 'OR Free'
  if (selectedProvider.value === 'nvidia') return 'NIM'
  return 'OpenRouter'
})

const quickPrompts = [
  {
    tag: 'ARCHITECTURE',
    shortcut: 'read-only',
    prompt: 'Inspect botex/server_cloud.py and list all REST and WebSocket routes.'
  },
  {
    tag: 'SECURITY AUDIT',
    shortcut: 'CodeQL',
    prompt: 'Check botex/security.py and botex/memory.py for CWE-312 taint barriers.'
  },
  {
    tag: 'MODEL CATALOG',
    shortcut: 'pricing',
    prompt: 'Compare free-tier vs paid coding models configured in botex.config.json.'
  },
  {
    tag: 'MEMORY VAULT',
    shortcut: 'context',
    prompt: 'Search local project memory for recent architecture decisions and handoffs.'
  }
]

function onSelectModel(model: CatalogModel) {
  chatStore.selectModel(model.id, model.provider)
}

function setMode(mode: 'chat' | 'agent') {
  activeMode.value = mode
  emit('switch-mode', mode)
}

function onConvertClick() {
  const draft = chatStore.convertChatToTask()
  emit('convert-to-task', draft)
}

async function handleSend() {
  const text = inputText.value.trim()
  if (!text || isStreaming.value) return
  inputText.value = ''
  await chatStore.sendMessage(text)
}

function openHistoryDrawer() {
  chatStore.fetchSessions()
  showHistoryDrawer.value = true
}

function handleSelectSession(sid: string) {
  chatStore.switchSession(sid)
  showHistoryDrawer.value = false
}

async function handleDeleteSession(sid: string) {
  await chatStore.deleteSession(sid)
}

function generateTranscriptText(): string {
  const parts: string[] = []
  messages.value.forEach((m) => {
    if (m.content) {
      const role = m.role === 'user' ? 'Operator' : 'BoteX'
      parts.push(`### ${role}\n${m.content}`)
    }
  })
  return parts.join('\n\n')
}

function openSaveMemoryModal() {
  const firstUser = messages.value.find((m) => m.role === 'user')?.content || 'Conversation Summary'
  memoryInitialTitle.value = firstUser.slice(0, 60).replace(/\n/g, ' ')
  memoryInitialContent.value = generateTranscriptText()
  showSaveMemoryModal.value = true
}

function handleOpenSaveMemoryForSession(sess: ChatSessionSummary) {
  memoryInitialTitle.value = sess.title || 'Chat Summary'
  memoryInitialContent.value = `Chat session ${sess.session_id} (${sess.model})\nSaved messages: ${sess.message_count}`
  showSaveMemoryModal.value = true
}

async function handleSaveToMemory(payload: {
  title: string
  kind: string
  scope: string
  tags: string[]
  content: string
}) {
  const res = await chatStore.saveCurrentToMemory(
    payload.title,
    payload.kind,
    payload.scope,
    payload.content,
    payload.tags
  )
  if (res.ok) {
    saveModalRef.value?.setStatusFeedback({
      type: 'success',
      message: t('chat.save_memory.success')
    })
    setTimeout(() => {
      showSaveMemoryModal.value = false
      saveModalRef.value?.setStatusFeedback(null)
      saveModalRef.value?.setSubmitting(false)
    }, 1200)
  } else {
    saveModalRef.value?.setStatusFeedback({
      type: 'error',
      message: res.error || t('chat.save_memory.error')
    })
    saveModalRef.value?.setSubmitting(false)
  }
}

function handleGlobalKeydown(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    showModelModal.value = !showModelModal.value
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleGlobalKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleGlobalKeydown)
})

// Auto scroll on new messages or chunk updates
watch(
  () => messages.value.length,
  async () => {
    await nextTick()
    if (scrollContainer.value) {
      scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight
    }
  }
)

watch(
  () => messages.value[messages.value.length - 1]?.content,
  async () => {
    await nextTick()
    if (scrollContainer.value) {
      scrollContainer.value.scrollTop = scrollContainer.value.scrollHeight
    }
  }
)
</script>

