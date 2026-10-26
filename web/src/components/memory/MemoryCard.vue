<template>
  <div class="rounded-xl border border-botex-700 bg-botex-900 p-4 transition-all hover:border-slate-600 shadow-sm space-y-3">
    <div class="flex flex-wrap items-start justify-between gap-2">
      <div class="min-w-0 w-full sm:w-auto sm:flex-1">
        <div class="flex flex-wrap items-center gap-2">
          <h4 class="text-sm font-semibold text-slate-100">{{ entry.title }}</h4>
          <Badge :variant="kindBadgeVariant">{{ entry.kind }}</Badge>
          <span
            v-if="entry.compiled_truth"
            class="text-xs px-1.5 py-0.5 rounded font-bold font-mono tracking-wider bg-cyan-950/80 text-cyan-300 border border-cyan-700/80"
          >
            ⭐ Compiled
          </span>
          <span
            :class="[
              'text-xs px-1.5 py-0.5 rounded uppercase font-mono font-bold tracking-wider border',
              entry.scope === 'global'
                ? 'bg-slate-800 text-slate-200 border-slate-600'
                : 'bg-cyan-950/60 text-cyan-200 border-cyan-700/60'
            ]"
          >
            {{ entry.scope || 'local' }}
          </span>
        </div>
        <p class="text-xs text-slate-300 mt-0.5 font-mono break-all">
          ID: {{ entry.memory_id }}
        </p>
      </div>

      <div class="flex items-center space-x-1.5 shrink-0 ml-auto">
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-2 py-1 text-xs text-cyan-400 hover:text-cyan-300 hover:bg-botex-800 rounded font-medium transition border border-cyan-800/60"
          @click="emit('select')"
          title="Pełny podgląd notatki i odnośników"
        >
          {{ locale === 'pl' ? 'Podgląd' : 'Details' }}
        </button>
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-2 py-1 text-xs text-slate-300 hover:text-white hover:bg-botex-800 rounded font-medium transition"
          @click="expanded = !expanded"
        >
          {{ expanded ? (locale === 'pl' ? 'Zwiń' : 'Collapse') : (locale === 'pl' ? 'Rozwiń' : 'Expand') }}
        </button>
        <button
          v-if="!confirmDelete"
          type="button"
          class="min-h-[44px] lg:min-h-[36px] min-w-[44px] lg:min-w-[36px] px-2 py-1 text-xs text-rose-400 hover:text-rose-300 hover:bg-rose-950/50 rounded font-medium transition border border-rose-900/50 flex items-center justify-center"
          @click="confirmDelete = true"
          :title="locale === 'pl' ? 'Usuń notatkę' : 'Delete note'"
        >
          <Trash2Icon class="w-3.5 h-3.5" />
        </button>
        <template v-else>
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-2 py-1 text-xs bg-rose-600 hover:bg-rose-500 text-white rounded font-semibold transition"
            @click="emit('delete'); confirmDelete = false"
          >
            {{ locale === 'pl' ? 'Potwierdź' : 'Confirm' }}
          </button>
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-2 py-1 text-xs bg-botex-800 hover:bg-botex-700 text-slate-300 rounded font-medium transition"
            @click="confirmDelete = false"
          >
            {{ locale === 'pl' ? 'Anuluj' : 'Cancel' }}
          </button>
        </template>
      </div>
    </div>

    <!-- Tags -->
    <div v-if="entry.tags && entry.tags.length > 0" class="flex flex-wrap gap-1.5">
      <span
        v-for="tag in entry.tags"
        :key="tag"
        class="text-xs font-mono px-2 py-0.5 rounded-md bg-botex-950 text-slate-300 border border-botex-800"
      >
        #{{ tag }}
      </span>
    </div>

    <!-- Outbound Wikilinks -->
    <div v-if="entry.links && entry.links.length > 0" class="flex flex-wrap items-center gap-1.5 text-xs font-mono text-cyan-400">
      <span class="text-slate-400 text-xs">Links:</span>
      <span
        v-for="l in entry.links"
        :key="l"
        class="px-1.5 py-0.5 rounded bg-botex-950/80 border border-cyan-900/60 text-cyan-300"
      >
        [[{{ l }}]]
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
import { Trash2 as Trash2Icon } from 'lucide-vue-next'
import type { MemoryEntry } from '../../api/types'
import { useI18n } from '../../i18n'
import Badge from '../common/Badge.vue'
import MarkdownViewer from '../common/MarkdownViewer.vue'

const props = defineProps<{
  entry: MemoryEntry
}>()

const emit = defineEmits<{
  (e: 'select'): void
  (e: 'delete'): void
}>()

const { locale } = useI18n()

const expanded = ref(false)
const confirmDelete = ref(false)

const kindBadgeVariant = computed(() => {
  switch (props.entry.kind) {
    case 'decision':
      return 'info'
    case 'lesson':
      return 'warning'
    case 'bug':
      return 'danger'
    case 'handoff':
      return 'success'
    default:
      return 'default'
  }
})
</script>
