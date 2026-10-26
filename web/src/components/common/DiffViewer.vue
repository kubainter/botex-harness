<template>
  <div class="rounded-lg overflow-hidden border border-botex-700 bg-botex-950 font-mono text-xs">
    <!-- Toolbar -->
    <div class="flex items-center justify-between px-3 py-1.5 bg-botex-900 border-b border-botex-800 text-slate-400">
      <div class="flex items-center space-x-2">
        <span class="w-2.5 h-2.5 rounded-full bg-rose-500/80"></span>
        <span class="w-2.5 h-2.5 rounded-full bg-amber-500/80"></span>
        <span class="w-2.5 h-2.5 rounded-full bg-emerald-500/80"></span>
        <span class="text-xs font-semibold text-slate-300 ml-1.5">{{ title || 'Unified Diff Preview' }}</span>
      </div>
      <div class="flex items-center space-x-2">
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-2 py-0.5 rounded text-xs bg-botex-800 hover:bg-botex-700 text-slate-300 transition flex items-center space-x-1"
          @click="copyContent"
        >
          <CopyIcon class="w-3 h-3" />
          <span>{{ copied ? 'Copied!' : 'Copy' }}</span>
        </button>
      </div>
    </div>

    <!-- Unified Diff Rendering -->
    <div v-if="parsedLines.length > 0" class="overflow-x-auto max-h-96 divide-y divide-slate-800/40">
      <div
        v-for="(line, idx) in parsedLines"
        :key="idx"
        :class="[
          'flex items-start px-3 py-0.5 leading-5 font-mono select-text',
          line.type === 'add' ? 'bg-emerald-950/40 text-emerald-300' :
          line.type === 'del' ? 'bg-rose-950/40 text-rose-300' :
          line.type === 'hunk' ? 'bg-indigo-950/40 text-indigo-300 font-semibold' :
          'text-slate-300 hover:bg-botex-900/40'
        ]"
      >
        <span class="w-6 shrink-0 text-right pr-3 select-none text-slate-600 text-xs">
          {{ idx + 1 }}
        </span>
        <span class="w-4 shrink-0 select-none font-bold text-center">
          {{ line.prefix }}
        </span>
        <pre class="flex-1 overflow-x-auto whitespace-pre font-mono text-xs m-0">{{ line.text }}</pre>
      </div>
    </div>

    <!-- Fallback / Plain Text / Shell command output -->
    <div v-else class="p-3 text-slate-400 overflow-x-auto max-h-96">
      <pre class="m-0 font-mono text-xs text-slate-300 whitespace-pre">{{ rawContent || 'No diff preview available.' }}</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Copy as CopyIcon } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    diff?: string
    title?: string
    rawContent?: string
  }>(),
  {
    diff: '',
    title: '',
    rawContent: ''
  }
)

interface ParsedLine {
  prefix: string
  text: string
  type: 'add' | 'del' | 'hunk' | 'normal'
}

const copied = ref(false)

const parsedLines = computed<ParsedLine[]>(() => {
  const content = props.diff || props.rawContent || ''
  if (!content) return []

  const lines = content.split('\n')
  return lines.map((line) => {
    if (line.startsWith('+++') || line.startsWith('---')) {
      return { prefix: ' ', text: line, type: 'hunk' }
    } else if (line.startsWith('@@')) {
      return { prefix: '@', text: line, type: 'hunk' }
    } else if (line.startsWith('+')) {
      return { prefix: '+', text: line.substring(1), type: 'add' }
    } else if (line.startsWith('-')) {
      return { prefix: '-', text: line.substring(1), type: 'del' }
    } else {
      return { prefix: ' ', text: line.startsWith(' ') ? line.substring(1) : line, type: 'normal' }
    }
  })
})

function copyContent() {
  const text = props.diff || props.rawContent || ''
  if (!text) return
  navigator.clipboard.writeText(text).then(() => {
    copied.value = true
    setTimeout(() => {
      copied.value = false
    }, 2000)
  })
}
</script>
