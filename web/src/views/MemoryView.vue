<template>
  <div class="h-full flex flex-col overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6">
    <!-- View Header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4">
      <div>
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <DatabaseIcon class="w-5 h-5 text-cyan-400" />
          <span>{{ t('memoryTitle') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">
          {{ t('memorySubtitle') }}
        </p>
      </div>

      <div class="flex items-center space-x-2">
        <button
          type="button"
          class="min-h-[40px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5"
          @click="memoryStore.fetchMemory"
        >
          <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': memoryStore.isLoading }" />
          <span>{{ t('refresh') }}</span>
        </button>
        <button
          type="button"
          class="min-h-[40px] px-4 py-1.5 rounded-lg bg-brand-blue hover:bg-blue-600 text-xs font-semibold text-white transition flex items-center space-x-1.5"
          @click="showCreateModal = true"
        >
          <PlusIcon class="w-4 h-4" />
          <span>{{ t('addMemory') }}</span>
        </button>
      </div>
    </div>

    <!-- Filter Component -->
    <MemoryFilter
      v-model:scope="activeScope"
      v-model:query="searchQuery"
      v-model:kind="filterKind"
      v-model:tag="filterTag"
      @search="memoryStore.fetchMemory"
    />

    <!-- Results List -->
    <div class="space-y-4">
      <div class="flex items-center justify-between text-xs text-slate-300 px-1">
        <span class="font-semibold uppercase tracking-wider font-mono">{{ t('vaultEntries') }} ({{ entries.length }})</span>
      </div>

      <!-- Skeleton Loader -->
      <div v-if="memoryStore.isLoading && entries.length === 0" class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div
          v-for="idx in 4"
          :key="idx"
          class="rounded-xl border border-botex-800 bg-botex-900/60 p-4 animate-pulse space-y-3"
        >
          <div class="flex items-center justify-between">
            <div class="h-4 w-44 bg-botex-800 rounded"></div>
            <div class="h-4 w-16 bg-botex-800 rounded"></div>
          </div>
          <div class="h-3 w-28 bg-botex-800/70 rounded"></div>
          <div class="h-12 w-full bg-botex-800/50 rounded"></div>
        </div>
      </div>

      <div
        v-else-if="entries.length === 0"
        class="rounded-xl border border-botex-800 bg-botex-900/50 p-10 text-center text-slate-300 text-xs space-y-3"
      >
        <p>{{ t('noMemoryFound') }}</p>
        <button
          type="button"
          class="inline-flex items-center space-x-1.5 px-3.5 py-2 rounded-lg bg-botex-800 hover:bg-botex-700 border border-botex-700 text-xs font-medium text-slate-100 transition"
          @click="showCreateModal = true"
        >
          <PlusIcon class="w-3.5 h-3.5 text-cyan-400" />
          <span>{{ t('addMemory') }}</span>
        </button>
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <MemoryCard
          v-for="entry in entries"
          :key="entry.memory_id"
          :entry="entry"
        />
      </div>
    </div>

    <!-- Create Memory Modal -->
    <Modal
      :is-open="showCreateModal"
      :title="locale === 'pl' ? 'Zapisz notatkę w pamięci' : 'Save Note to Memory Vault'"
      :subtitle="locale === 'pl' ? 'Notatki są automatycznie wyszukiwane przez agentów w kolejnych zadaniach.' : 'Notes are indexed and retrieved across future agent runs.'"
      max-width="xl"
      @close="showCreateModal = false"
    >
      <form @submit.prevent="handleSaveMemory" class="space-y-4 text-xs">
        <Input
          v-model="newEntry.title"
          :label="locale === 'pl' ? 'Tytuł notatki *' : 'Title / Summary *'"
          required
          placeholder="e.g. FastAPI Cloud Gateway auth header format"
        />

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block font-medium text-slate-200 mb-1">
              {{ locale === 'pl' ? 'Kategoria' : 'Entry Kind' }}
            </label>
            <select
              v-model="newEntry.kind"
              class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-slate-100"
            >
              <option value="context">Context (Background)</option>
              <option value="decision">Architectural Decision</option>
              <option value="handoff">Handoff Notes</option>
              <option value="bug">Known Bug / Edge Case</option>
            </select>
          </div>

          <div>
            <label class="block font-medium text-slate-200 mb-1">
              {{ locale === 'pl' ? 'Zakres zapisu' : 'Storage Scope' }}
            </label>
            <select
              v-model="newEntry.scope"
              class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-slate-100 font-mono"
            >
              <option value="local">Local (.botex/memory/)</option>
              <option value="global">Global (~/.botex/memory/)</option>
            </select>
          </div>
        </div>

        <div>
          <label class="block font-medium text-slate-200 mb-1">
            {{ locale === 'pl' ? 'Tagi (oddzielone przecinkiem)' : 'Tags (comma separated)' }}
          </label>
          <input
            v-model="rawTags"
            type="text"
            placeholder="api, architecture, security"
            class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-slate-100 font-mono"
          />
        </div>

        <div>
          <label class="block font-medium text-slate-200 mb-1">
            {{ locale === 'pl' ? 'Treść (Markdown) *' : 'Markdown Content *' }}
          </label>
          <textarea
            v-model="newEntry.content"
            rows="5"
            required
            :placeholder="locale === 'pl' ? 'Opisz ustalenia, ograniczenia lub kontekst techniczny...' : 'Write technical context, decisions, or constraints...'"
            class="w-full rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 p-2.5 font-mono focus:outline-none focus:border-cyan-500"
          ></textarea>
        </div>
      </form>

      <template #footer>
        <Button variant="secondary" size="sm" @click="showCreateModal = false">{{ t('cancel') }}</Button>
        <Button
          variant="primary"
          size="sm"
          :loading="memoryStore.isSaving"
          @click="handleSaveMemory"
        >
          {{ t('saveToVault') }}
        </Button>
      </template>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useMemoryStore } from '../stores/memory'
import { useI18n } from '../i18n'
import type { MemorySaveRequest } from '../api/types'
import MemoryFilter from '../components/memory/MemoryFilter.vue'
import MemoryCard from '../components/memory/MemoryCard.vue'
import Modal from '../components/common/Modal.vue'
import Button from '../components/common/Button.vue'
import Input from '../components/common/Input.vue'
import {
  Database as DatabaseIcon,
  Plus as PlusIcon,
  RefreshCw as RefreshCwIcon
} from 'lucide-vue-next'

const memoryStore = useMemoryStore()
const { entries, activeScope, searchQuery, filterKind, filterTag } = storeToRefs(memoryStore)
const { locale, t } = useI18n()

const showCreateModal = ref(false)
const rawTags = ref('architecture, pwa')

const newEntry = ref<MemorySaveRequest>({
  title: '',
  content: '',
  kind: 'context',
  tags: [],
  scope: 'local',
  workspace_dir: '.'
})

onMounted(() => {
  memoryStore.fetchMemory()
})

watch([activeScope, filterKind, filterTag], () => {
  memoryStore.fetchMemory()
})

async function handleSaveMemory() {
  if (!newEntry.value.title.trim() || !newEntry.value.content.trim()) return

  newEntry.value.tags = rawTags.value
    .split(',')
    .map((tag) => tag.trim())
    .filter(Boolean)

  const ok = await memoryStore.saveEntry(newEntry.value)
  if (ok) {
    showCreateModal.value = false
    newEntry.value.title = ''
    newEntry.value.content = ''
  }
}
</script>
