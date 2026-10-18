<template>
  <div class="rounded-xl border border-botex-700 bg-botex-900 p-4 transition-all hover:border-slate-600 shadow-sm">
    <div class="flex items-start justify-between gap-2 mb-2">
      <div>
        <div class="flex flex-wrap items-center gap-2">
          <h4 class="text-sm font-semibold text-slate-100">{{ entry.title }}</h4>
          <Badge :variant="kindBadgeVariant">{{ entry.kind }}</Badge>
          <span
            :class="[
              'text-[10px] px-1.5 py-0.5 rounded uppercase font-mono font-bold tracking-wider border',
              entry.scope === 'global'
                ? 'bg-slate-800 text-slate-200 border-slate-600'
                : 'bg-cyan-950/60 text-cyan-200 border-cyan-700/60'
            ]"
          >
            {{ entry.scope || 'local' }}
          </span>
        </div>
        <p class="text-xs text-slate-300 mt-0.5 font-mono">
          ID: {{ entry.memory_id }}
        </p>
      </div>

      <button
        type="button"
        class="text-xs text-cyan-300 hover:text-cyan-200 font-medium transition shrink-0"
        @click="expanded = !expanded"
      >
        {{ expanded ? 'Collapse' : 'Expand' }}
      </button>
    </div>

    <!-- Tags -->
    <div v-if="entry.tags && entry.tags.length > 0" class="flex flex-wrap gap-1.5 mb-3">
      <span
        v-for="tag in entry.tags"
        :key="tag"
        class="text-[11px] font-mono px-2 py-0.5 rounded-md bg-botex-950 text-slate-300 border border-botex-800"
      >
        #{{ tag }}
      </span>
    </div>

    <!-- Content Preview / Full -->
    <div :class="['overflow-hidden transition-all text-xs text-slate-200', expanded ? 'max-h-none' : 'max-h-24 relative']">
      <MarkdownViewer :content="entry.content" />
      <div v-if="!expanded" class="absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t from-botex-900 to-transparent pointer-events-none"></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { MemoryEntry } from '../../api/types'
import Badge from '../common/Badge.vue'
import MarkdownViewer from '../common/MarkdownViewer.vue'

const props = defineProps<{
  entry: MemoryEntry
}>()

const expanded = ref(false)

const kindBadgeVariant = computed(() => {
  switch (props.entry.kind) {
    case 'decision':
      return 'info'
    case 'bug':
      return 'danger'
    case 'handoff':
      return 'warning'
    default:
      return 'default'
  }
})
</script>
