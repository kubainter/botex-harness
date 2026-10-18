<template>
  <div class="space-y-3 bg-botex-900 border border-botex-800 rounded-xl p-4">
    <!-- Scope Selector Tabs -->
    <div>
      <label class="block text-xs font-semibold text-slate-200 uppercase tracking-wider mb-2 font-mono">
        {{ locale === 'pl' ? 'Zakres pamięci' : 'Vault Scope' }}
      </label>
      <div class="grid grid-cols-3 gap-1 bg-botex-950 p-1 rounded-lg border border-botex-800">
        <button
          type="button"
          :class="[
            'min-h-[36px] py-1.5 rounded-md text-xs font-medium transition text-center',
            scope === 'all' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
          ]"
          @click="$emit('update:scope', 'all')"
        >
          {{ locale === 'pl' ? 'Wszystkie' : 'All Scopes' }}
        </button>
        <button
          type="button"
          :class="[
            'min-h-[36px] py-1.5 rounded-md text-xs font-medium transition text-center',
            scope === 'local' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
          ]"
          @click="$emit('update:scope', 'local')"
        >
          {{ locale === 'pl' ? 'Projekt (.botex)' : 'Workspace (.botex)' }}
        </button>
        <button
          type="button"
          :class="[
            'min-h-[36px] py-1.5 rounded-md text-xs font-medium transition text-center',
            scope === 'global' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
          ]"
          @click="$emit('update:scope', 'global')"
        >
          {{ locale === 'pl' ? 'Globalne (~/.botex)' : 'Global (~/.botex)' }}
        </button>
      </div>
    </div>

    <!-- Search Query, Kind, and Tag filter -->
    <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
      <div>
        <label class="block text-xs font-medium text-slate-300 mb-1">
          {{ locale === 'pl' ? 'Szukaj w treści' : 'Search Keywords' }}
        </label>
        <input
          :value="query"
          type="text"
          :placeholder="locale === 'pl' ? 'Szukaj po tytule lub treści...' : 'Filter title or content...'"
          class="w-full min-h-[38px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-1.5 text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:border-cyan-500"
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
          class="w-full min-h-[38px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-1.5 text-xs text-slate-100 placeholder-slate-400 font-mono focus:outline-none focus:border-cyan-500"
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
          class="w-full min-h-[38px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-1.5 text-xs text-slate-100 focus:outline-none focus:border-cyan-500"
          @change="$emit('update:kind', ($event.target as HTMLSelectElement).value)"
        >
          <option value="">{{ locale === 'pl' ? 'Wszystkie typy' : 'All Types' }}</option>
          <option value="context">Context</option>
          <option value="decision">Architectural Decision</option>
          <option value="handoff">Handoff</option>
          <option value="bug">Known Bug / Issue</option>
        </select>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { MemoryScope } from '../../api/types'
import { useI18n } from '../../i18n'

defineProps<{
  scope: MemoryScope
  query: string
  kind: string
  tag?: string
}>()

defineEmits<{
  (e: 'update:scope', val: MemoryScope): void
  (e: 'update:query', val: string): void
  (e: 'update:kind', val: string): void
  (e: 'update:tag', val: string): void
  (e: 'search'): void
}>()

const { locale } = useI18n()
</script>
