<template>
  <Modal
    :is-open="isOpen"
    :title="t('chat.save_memory.title')"
    :subtitle="t('chat.save_memory.subtitle')"
    max-width="lg"
    @close="handleClose"
  >
    <div class="space-y-4 text-xs">
      <!-- Status Toast / Feedback -->
      <div
        v-if="statusFeedback"
        :class="[
          'p-3 rounded-lg flex items-center space-x-2 border transition',
          statusFeedback.type === 'success'
            ? 'bg-emerald-950/60 border-emerald-500/40 text-emerald-200'
            : 'bg-rose-950/60 border-rose-500/40 text-rose-200'
        ]"
      >
        <CheckIcon v-if="statusFeedback.type === 'success'" class="w-4 h-4 text-emerald-400 shrink-0" />
        <AlertCircleIcon v-else class="w-4 h-4 text-rose-400 shrink-0" />
        <span class="text-xs">{{ statusFeedback.message }}</span>
      </div>

      <!-- Note Title -->
      <div>
        <label class="block text-slate-200 font-medium mb-1 font-mono text-xs uppercase tracking-wide">
          {{ t('chat.save_memory.note_title') }} *
        </label>
        <input
          v-model="formTitle"
          type="text"
          placeholder="e.g. WebSocket Streaming and Error Recovery Flow"
          class="w-full bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-base sm:text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-sky-500 font-sans"
        />
      </div>

      <!-- Category (Kind) & Scope -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label class="block text-slate-200 font-medium mb-1 font-mono text-xs uppercase tracking-wide">
            {{ t('chat.save_memory.kind') }}
          </label>
          <select
            v-model="formKind"
            class="w-full bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-base sm:text-xs text-slate-100 focus:outline-none focus:ring-1 focus:ring-sky-500 font-sans"
          >
            <option value="context">context (General Context)</option>
            <option value="decision">decision (Architectural / Technical Decision)</option>
            <option value="lesson">lesson (Post-mortem / Meta-Lesson)</option>
            <option value="handoff">handoff (Task Handoff & Next Steps)</option>
          </select>
        </div>

        <div>
          <label class="block text-slate-200 font-medium mb-1 font-mono text-xs uppercase tracking-wide">
            {{ t('chat.save_memory.scope') }}
          </label>
          <select
            v-model="formScope"
            class="w-full bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-base sm:text-xs text-slate-100 focus:outline-none focus:ring-1 focus:ring-sky-500 font-sans"
          >
            <option value="local">local (.botex/memory/ in project)</option>
            <option value="global">global (~/.botex/memory/ user home)</option>
          </select>
        </div>
      </div>

      <!-- Tags -->
      <div>
        <label class="block text-slate-200 font-medium mb-1 font-mono text-xs uppercase tracking-wide">
          {{ t('chat.save_memory.tags') }}
        </label>
        <input
          v-model="formTags"
          type="text"
          placeholder="chat, architecture, fastapi"
          class="w-full bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-base sm:text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-sky-500 font-mono"
        />
      </div>

      <!-- Content Markdown Preview / Editor -->
      <div>
        <div class="flex items-center justify-between mb-1">
          <label class="block text-slate-200 font-medium font-mono text-xs uppercase tracking-wide">
            {{ t('chat.save_memory.content') }}
          </label>
          <span class="text-xs font-mono text-slate-400">
            {{ formContent ? formContent.length : 0 }} chars
          </span>
        </div>
        <textarea
          v-model="formContent"
          rows="6"
          placeholder="Conversation transcript or summary notes in Markdown format..."
          class="w-full bg-botex-950 border border-botex-700 rounded-lg p-3 text-base sm:text-xs text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-sky-500 font-mono leading-relaxed"
        ></textarea>
      </div>
    </div>

    <template #footer>
      <div class="flex items-center justify-end space-x-2">
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-3.5 py-2 rounded-lg bg-botex-800 hover:bg-botex-700 text-slate-200 text-xs font-medium transition"
          :disabled="isSubmitting"
          @click="handleClose"
        >
          Cancel
        </button>
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-xs font-medium transition flex items-center space-x-1.5"
          :disabled="!formTitle.trim() || isSubmitting"
          @click="handleSubmit"
        >
          <Loader2Icon v-if="isSubmitting" class="w-3.5 h-3.5 animate-spin" />
          <BookmarkPlusIcon v-else class="w-3.5 h-3.5" />
          <span>{{ t('chat.save_memory.submit') }}</span>
        </button>
      </div>
    </template>
  </Modal>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import Modal from '../common/Modal.vue'
import { useI18n } from '../../i18n'
import {
  BookmarkPlus as BookmarkPlusIcon,
  Check as CheckIcon,
  AlertCircle as AlertCircleIcon,
  Loader2 as Loader2Icon
} from 'lucide-vue-next'

const props = defineProps<{
  isOpen: boolean
  defaultTitle?: string
  defaultContent?: string
  defaultKind?: string
  defaultScope?: string
  defaultTags?: string[]
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (
    e: 'save',
    payload: {
      title: string
      kind: string
      scope: string
      tags: string[]
      content: string
    }
  ): void
}>()

const { t } = useI18n()

const formTitle = ref('')
const formKind = ref('context')
const formScope = ref('local')
const formTags = ref('chat, notes')
const formContent = ref('')
const isSubmitting = ref(false)
const statusFeedback = ref<{ type: 'success' | 'error'; message: string } | null>(null)

watch(
  () => props.isOpen,
  (open) => {
    if (open) {
      statusFeedback.value = null
      formTitle.value = props.defaultTitle || ''
      formKind.value = props.defaultKind || 'context'
      formScope.value = props.defaultScope || 'local'
      formTags.value = props.defaultTags ? props.defaultTags.join(', ') : 'chat, notes'
      formContent.value = props.defaultContent || ''
    }
  },
  { immediate: true }
)

function handleClose() {
  if (isSubmitting.value) return
  emit('close')
}

async function handleSubmit() {
  if (!formTitle.value.trim() || isSubmitting.value) return
  isSubmitting.value = true
  statusFeedback.value = null

  const tagsArray = formTags.value
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean)

  emit('save', {
    title: formTitle.value.trim(),
    kind: formKind.value,
    scope: formScope.value,
    tags: tagsArray,
    content: formContent.value.trim()
  })
}

defineExpose({
  setSubmitting: (val: boolean) => {
    isSubmitting.value = val
  },
  setStatusFeedback: (feedback: { type: 'success' | 'error'; message: string } | null) => {
    statusFeedback.value = feedback
  }
})
</script>
