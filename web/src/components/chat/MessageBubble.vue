<template>
  <div :class="['flex w-full mb-3', isUser ? 'justify-end' : 'justify-start']">
    <div
      :class="[
        'max-w-[92%] sm:max-w-[82%] min-w-0 rounded-xl px-4 py-3 border transition-colors select-text',
        isUser
          ? 'bg-sky-950/35 border-sky-700/50 text-slate-100 rounded-tr-sm'
          : 'bg-botex-900 border-botex-700 text-slate-100 rounded-tl-sm'
      ]"
    >
      <!-- Sender & metadata header -->
      <div class="flex items-center justify-between gap-3 mb-1.5 text-xs text-slate-300 border-b border-botex-800 pb-1 select-none">
        <div class="flex items-center space-x-2">
          <span class="font-semibold" :class="isUser ? 'text-sky-300' : 'text-slate-100'">
            {{ isUser ? 'Operator' : 'BoteX' }}
          </span>
          <span
            v-if="!isUser && message.model"
            class="text-xs px-1.5 py-0.5 rounded bg-botex-950 border border-botex-800 text-slate-300 font-mono"
          >
            {{ message.model }}
          </span>
        </div>
        <div class="flex items-center space-x-2">
          <span class="text-xs text-slate-400 font-mono">
            {{ formattedTime }}
          </span>
          <!-- One-click Copy Button for both user prompts and model responses -->
          <button
            v-if="message.content"
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-2 py-0.5 rounded text-xs font-mono transition flex items-center space-x-1"
            :class="
              copied
                ? 'bg-emerald-950 text-emerald-300 border border-emerald-500/50'
                : 'bg-botex-950 hover:bg-botex-800 text-slate-300 hover:text-white border border-botex-800 hover:border-botex-700'
            "
            :title="copied ? t('chat.copied') : t('chat.copy_message')"
            @click="copyContent"
          >
            <CheckIcon v-if="copied" class="w-3 h-3 text-emerald-400" />
            <CopyIcon v-else class="w-3 h-3" />
            <span class="text-xs">{{ copied ? t('chat.copied') : t('chat.copy') }}</span>
          </button>
        </div>
      </div>

      <!-- Reasoning Box (Thinking Process) -->
      <div v-if="message.reasoning || (message.streaming && message.is_thinking)" class="mb-2">
        <details :open="Boolean(message.is_thinking || (!message.content && message.reasoning))" class="bg-botex-950/90 border border-botex-800 rounded-lg p-2 text-xs">
          <summary class="cursor-pointer text-amber-400/90 hover:text-amber-300 font-mono flex items-center justify-between select-none">
            <span class="flex items-center space-x-1.5">
              <BrainIcon class="w-3.5 h-3.5" :class="{'animate-pulse text-amber-400': message.is_thinking}" />
              <span class="font-medium">{{ message.is_thinking ? 'Generowanie toku myślenia (Reasoning)...' : 'Tok rozumowania (Reasoning)' }}</span>
            </span>
            <span v-if="message.is_thinking" class="w-2 h-2 rounded-full bg-amber-400 animate-ping"></span>
          </summary>
          <div class="mt-2 text-slate-300 font-mono text-xs whitespace-pre-wrap max-h-48 overflow-y-auto bg-botex-950 p-2 rounded border border-botex-900 leading-normal select-text">
            {{ message.reasoning }}
          </div>
        </details>
      </div>

      <!-- Message Content -->
      <div class="text-sm leading-relaxed select-text cursor-text">
        <MarkdownViewer v-if="message.content" :content="message.content" />
        <div v-else-if="message.streaming && !message.reasoning" class="flex items-center space-x-2 text-slate-300 py-1">
          <span class="inline-block w-2 h-2 rounded-full bg-sky-400 animate-pulse"></span>
          <span class="text-xs font-mono">{{ t('chat.streaming') }}</span>
        </div>
      </div>

      <!-- Streaming cursor indicator -->
      <div v-if="message.streaming && message.content" class="mt-1 flex items-center space-x-1 text-sky-400 text-xs">
        <span class="w-1.5 h-3.5 bg-sky-400 animate-pulse inline-block"></span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ChatMessage } from '../../api/types'
import { useI18n } from '../../i18n'
import MarkdownViewer from '../common/MarkdownViewer.vue'
import { Copy as CopyIcon, Check as CheckIcon, Brain as BrainIcon } from 'lucide-vue-next'

const props = defineProps<{
  message: ChatMessage
}>()

const { t } = useI18n()
const copied = ref(false)

const isUser = computed(() => props.message.role === 'user')

const formattedTime = computed(() => {
  const d = new Date(props.message.timestamp)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
})

async function copyContent() {
  if (!props.message.content) return
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(props.message.content)
    } else {
      // Fallback for non-https contexts
      const ta = document.createElement('textarea')
      ta.value = props.message.content
      ta.style.position = 'fixed'
      ta.style.left = '-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 2000)
  } catch (err) {
    console.warn('Failed to copy message content:', err)
  }
}
</script>

