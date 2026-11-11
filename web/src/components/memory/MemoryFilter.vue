<template>
  <div class="bg-botex-900 border border-botex-800 rounded-xl overflow-hidden">
    <!-- Collapsed Summary Bar (always visible; panel stays collapsed until toggled) -->
    <button
      type="button"
      class="w-full min-h-[44px] px-3.5 py-2 flex items-center gap-2 text-left transition hover:bg-botex-800/50"
      :aria-expanded="isOpen"
      @click="toggleOpen"
    >
      <FilterIcon class="w-3.5 h-3.5 text-cyan-400 shrink-0" />
      <span class="text-xs font-semibold text-slate-200 uppercase tracking-wider font-mono shrink-0">
        {{ locale === 'pl' ? 'Filtry' : 'Filters' }}
      </span>

      <!-- Active filter chips (summary, only when collapsed) -->
      <span v-if="!isOpen" class="flex-1 min-w-0 flex items-center gap-1.5 overflow-hidden">
        <span
          v-if="activeCount === 0"
          class="text-xs text-slate-400 italic truncate"
        >
          {{ locale === 'pl' ? 'Wszystkie notatki' : 'All notes' }}
        </span>
        <template v-else>
          <span
            v-for="chip in activeChips"
            :key="chip"
            class="px-1.5 py-0.5 rounded text-xs font-mono bg-cyan-950/70 text-cyan-300 border border-cyan-800/60 whitespace-nowrap truncate max-w-[9rem]"
          >
            {{ chip }}
          </span>
        </template>
      </span>
      <span v-else class="flex-1"></span>

      <span
        v-if="activeCount > 0"
        class="px-1.5 py-0.5 rounded-full text-xs font-mono font-bold bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 shrink-0"
      >
        {{ activeCount }}
      </span>
      <ChevronDownIcon
        class="w-4 h-4 text-slate-300 shrink-0 transition-transform duration-150"
        :class="{ 'rotate-180': isOpen }"
      />
    </button>

    <!-- Expandable Filter Panel -->
    <div v-if="isOpen" class="border-t border-botex-800 p-4 space-y-3">
      <!-- Project Selector Chips -->
      <div v-if="projects && projects.length > 0">
        <label class="block text-xs font-semibold text-slate-200 uppercase tracking-wider mb-2 font-mono">
          {{ locale === 'pl' ? 'Projekt (Project)' : 'Project' }}
        </label>
        <div class="flex flex-wrap gap-1.5" role="group">
          <button
            type="button"
            :aria-pressed="!project"
            :class="[
              'min-h-[36px] px-2.5 py-1 rounded-md text-xs font-mono font-medium transition border',
              !project
                ? 'bg-emerald-950/70 text-emerald-300 border-emerald-500/50'
                : 'bg-botex-950 text-slate-300 border-botex-800 hover:border-slate-700 hover:text-white'
            ]"
            @click="$emit('update:project', '')"
          >
            {{ locale === 'pl' ? 'Wszystkie projekty' : 'All Projects' }}
          </button>
          <button
            v-for="item in projects"
            :key="item.project || '_unassigned'"
            type="button"
            :aria-pressed="project === (item.project || '_unassigned')"
            :class="[
              'min-h-[36px] px-2.5 py-1 rounded-md text-xs font-mono font-medium transition border flex items-center space-x-1.5',
              project === (item.project || '_unassigned')
                ? 'bg-emerald-950/70 text-emerald-300 border-emerald-500/50'
                : 'bg-botex-950 text-slate-300 border-botex-800 hover:border-slate-700 hover:text-white'
            ]"
            @click="$emit('update:project', item.project || '_unassigned')"
          >
            <span>{{ item.project || (locale === 'pl' ? 'Nieprzypisane' : 'Unassigned') }}</span>
            <span class="text-[10px] px-1 rounded bg-botex-900 text-slate-400">{{ item.count }}</span>
          </button>
        </div>
      </div>

      <!-- Scope Selector Tabs -->
      <div>
        <label class="block text-xs font-semibold text-slate-200 uppercase tracking-wider mb-2 font-mono">
          {{ locale === 'pl' ? 'Lokalizacja pamięci (Vault Scope)' : 'Vault Scope' }}
        </label>
        <div class="grid grid-cols-3 gap-1 bg-botex-950 p-1 rounded-lg border border-botex-800" role="group">
          <button
            type="button"
            :aria-pressed="scope === 'all'"
            :class="[
              'min-h-[44px] lg:min-h-[36px] py-1.5 rounded-md text-xs font-medium transition text-center',
              scope === 'all' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="$emit('update:scope', 'all')"
          >
            {{ locale === 'pl' ? 'Wszystkie' : 'All Scopes' }}
          </button>
          <button
            type="button"
            :aria-pressed="scope === 'local'"
            :class="[
              'min-h-[44px] lg:min-h-[36px] py-1.5 rounded-md text-xs font-medium transition text-center',
              scope === 'local' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="$emit('update:scope', 'local')"
          >
            {{ locale === 'pl' ? 'Lokalne (.botex)' : 'Local (.botex)' }}
          </button>
          <button
            type="button"
            :aria-pressed="scope === 'global'"
            :class="[
              'min-h-[44px] lg:min-h-[36px] py-1.5 rounded-md text-xs font-medium transition text-center',
              scope === 'global' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="$emit('update:scope', 'global')"
          >
            {{ locale === 'pl' ? 'Globalne (~/.botex)' : 'Global (~/.botex)' }}
          </button>
        </div>
      </div>

      <!-- Search Query, Kind, and Tag filter -->
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">
            {{ locale === 'pl' ? 'Szukaj w treści' : 'Search Keywords' }}
          </label>
          <input
            :value="query"
            type="text"
            :placeholder="locale === 'pl' ? 'Szukaj po tytule lub treści...' : 'Filter title or content...'"
            class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-1.5 text-base sm:text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:border-cyan-500"
            @input="$emit('update:query', ($event.target as HTMLInputElement).value)"
            @keydown.enter="$emit('search')"
          />
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">
            {{ locale === 'pl' ? 'Filtruj po tagu' : 'Filter by Tag' }}
          </label>
          <input
            :value="tag"
            type="text"
            placeholder="mcp, security, api..."
            class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-1.5 text-base sm:text-xs text-slate-100 placeholder-slate-400 font-mono focus:outline-none focus:border-cyan-500"
            @input="$emit('update:tag', ($event.target as HTMLInputElement).value)"
            @keydown.enter="$emit('search')"
          />
        </div>

        <div>
          <label class="block text-xs font-medium text-slate-300 mb-1">
            {{ locale === 'pl' ? 'Kategoria' : 'Entry Kind' }}
          </label>
          <select
            :value="kind"
            class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-1.5 text-base sm:text-xs text-slate-100 focus:outline-none focus:border-cyan-500"
            @change="$emit('update:kind', ($event.target as HTMLSelectElement).value)"
          >
            <option value="">{{ locale === 'pl' ? 'Wszystkie typy' : 'All Types' }}</option>
            <option value="context">Context{{ kindSuffix('context') }}</option>
            <option value="decision">Architectural Decision{{ kindSuffix('decision') }}</option>
            <option value="lesson">Meta Lesson{{ kindSuffix('lesson') }}</option>
            <option value="handoff">Handoff{{ kindSuffix('handoff') }}</option>
          </select>
        </div>
      </div>

      <!-- Hide auto-generated task receipts -->
      <label class="flex items-center gap-2 pt-1 cursor-pointer select-none w-fit">
        <input
          :checked="hideReceipts"
          type="checkbox"
          class="w-4 h-4 rounded border-botex-700 bg-botex-950 accent-cyan-500 cursor-pointer"
          @change="$emit('update:hideReceipts', ($event.target as HTMLInputElement).checked)"
        />
        <span class="text-xs text-slate-300">
          {{ locale === 'pl' ? 'Ukryj auto-raporty (task-receipt)' : 'Hide auto-reports (task-receipt)' }}
        </span>
      </label>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import type { MemoryProjectItem, MemoryScope } from '../../api/types'
import { useI18n } from '../../i18n'
import { Filter as FilterIcon, ChevronDown as ChevronDownIcon } from 'lucide-vue-next'

const props = defineProps<{
  scope: MemoryScope
  project?: string
  projects?: MemoryProjectItem[]
  query: string
  kind: string
  kindCounts?: Record<string, number>
  tag?: string
  hideReceipts?: boolean
}>()

defineEmits<{
  (e: 'update:scope', val: MemoryScope): void
  (e: 'update:project', val: string): void
  (e: 'update:query', val: string): void
  (e: 'update:kind', val: string): void
  (e: 'update:tag', val: string): void
  (e: 'update:hideReceipts', val: boolean): void
  (e: 'search'): void
}>()

const { locale } = useI18n()

function kindSuffix(k: string): string {
  const n = props.kindCounts?.[k]
  return n ? ` (${n})` : ''
}

const STORAGE_KEY = 'botex_memory_filters_open'
const isOpen = ref<boolean>(
  typeof localStorage !== 'undefined' && localStorage.getItem(STORAGE_KEY) === '1'
)

function toggleOpen() {
  isOpen.value = !isOpen.value
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_KEY, isOpen.value ? '1' : '0')
  }
}

const activeCount = computed(() => {
  let n = 0
  if (props.project) n++
  if (props.scope && props.scope !== 'all') n++
  if (props.query?.trim()) n++
  if (props.kind) n++
  if (props.tag?.trim()) n++
  if (props.hideReceipts) n++
  return n
})

const activeChips = computed(() => {
  const chips: string[] = []
  if (props.project) {
    chips.push('📁 ' + (props.project === '_unassigned' ? 'unassigned' : props.project))
  }
  if (props.scope && props.scope !== 'all') {
    chips.push(props.scope === 'local' ? 'local' : 'global')
  }
  if (props.kind) chips.push(props.kind)
  if (props.tag?.trim()) chips.push('#' + props.tag.trim())
  if (props.query?.trim()) chips.push('"' + props.query.trim() + '"')
  if (props.hideReceipts) chips.push('!task-receipt')
  return chips
})
</script>
