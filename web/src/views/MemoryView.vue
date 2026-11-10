<template>
  <div class="h-full overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6">
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

      <div class="flex flex-wrap items-center gap-2 w-full sm:w-auto">
        <!-- View Toggle (Cards vs Graph) -->
        <div class="flex items-center bg-botex-900 border border-botex-700 rounded-lg p-0.5 shrink-0">
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-2.5 sm:px-3 py-1 rounded-md text-xs font-medium transition flex items-center space-x-1"
            :class="viewMode === 'cards' ? 'bg-botex-800 text-white shadow' : 'text-slate-400 hover:text-slate-200'"
            @click="viewMode = 'cards'"
          >
            <LayoutGridIcon class="w-3.5 h-3.5" />
            <span>{{ locale === 'pl' ? 'Karty' : 'Cards' }}</span>
          </button>
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-2.5 sm:px-3 py-1 rounded-md text-xs font-medium transition flex items-center space-x-1"
            :class="viewMode === 'graph' ? 'bg-botex-800 text-cyan-400 shadow' : 'text-slate-400 hover:text-slate-200'"
            @click="switchToGraph"
          >
            <NetworkIcon class="w-3.5 h-3.5" />
            <span>{{ locale === 'pl' ? 'Graf' : 'Graph' }}</span>
          </button>
        </div>

        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] min-w-[44px] justify-center px-2.5 sm:px-3 py-1 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1 shrink-0"
          @click="refreshCurrentView"
        >
          <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': memoryStore.isLoading || memoryStore.isGraphLoading }" />
          <span class="hidden sm:inline">{{ t('refresh') }}</span>
        </button>

        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-3 py-1 rounded-lg bg-brand-blue hover:bg-blue-600 text-xs font-semibold text-white transition flex items-center space-x-1 shrink-0"
          @click="openCreateModal"
        >
          <PlusIcon class="w-3.5 h-3.5" />
          <span>{{ t('addMemory') }}</span>
        </button>
      </div>
    </div>

    <!-- Error notice -->
    <div
      v-if="memoryStore.errorMessage"
      class="p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ memoryStore.errorMessage }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="refreshCurrentView">{{ t('refresh') }}</button>
    </div>

    <!-- Curator Status Banner / Card -->
    <div
      v-if="curatorStatus"
      class="rounded-xl border border-botex-800 bg-botex-900/60 p-3.5 sm:p-4 text-xs transition space-y-2.5"
    >
      <div class="flex flex-wrap items-center justify-between gap-2.5">
        <div class="flex items-center space-x-2.5">
          <div
            class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0"
            :class="curatorStatus.is_healthy ? 'bg-emerald-950/60 text-emerald-400 border border-emerald-800/60' : 'bg-amber-950/60 text-amber-400 border border-amber-800/60'"
          >
            <ShieldCheckIcon v-if="curatorStatus.is_healthy" class="w-4 h-4" />
            <SparklesIcon v-else class="w-4 h-4" />
          </div>
          <div>
            <div class="flex items-center space-x-2">
              <span class="font-bold text-slate-100">
                {{ locale === 'pl' ? 'Nocny Kurator Grafu Wiedzy' : 'Knowledge Graph Curator' }}
              </span>
              <span
                class="px-2 py-0.5 rounded text-[10px] font-mono uppercase font-bold"
                :class="curatorStatus.is_healthy ? 'bg-emerald-900/50 text-emerald-300 border border-emerald-700/50' : 'bg-amber-900/50 text-amber-300 border border-amber-700/50'"
              >
                {{ curatorStatus.is_healthy ? (locale === 'pl' ? 'Zdrowy' : 'Healthy') : (locale === 'pl' ? 'Wymaga uwagi' : 'Attention') }}
              </span>
              <span v-if="curatorStatus.tier_used && curatorStatus.tier_used !== 'none'" class="text-[10px] font-mono text-slate-400 bg-botex-950 px-1.5 py-0.5 rounded border border-botex-800">
                {{ curatorStatus.tier_used }}
              </span>
            </div>
            <p v-if="curatorStatus.summary" class="text-slate-300 text-[11px] mt-0.5 line-clamp-1">
              {{ curatorStatus.summary }}
            </p>
          </div>
        </div>

        <div class="flex items-center space-x-2 shrink-0">
          <button
            type="button"
            :disabled="isCuratorRunning"
            class="min-h-[44px] lg:min-h-[32px] px-3 py-1 rounded-lg bg-botex-800 hover:bg-botex-700 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5 disabled:opacity-50"
            @click="handleRunCurator"
          >
            <RefreshCwIcon class="w-3.5 h-3.5 text-cyan-400" :class="{ 'animate-spin': isCuratorRunning }" />
            <span>{{ isCuratorRunning ? (locale === 'pl' ? 'Analiza w toku...' : 'Curating...') : (locale === 'pl' ? 'Uruchom Kuratora' : 'Run Curator') }}</span>
          </button>
        </div>
      </div>

      <!-- Quick Metrics Strip -->
      <div class="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] font-mono text-slate-400 pt-1 border-t border-botex-800/60">
        <span>{{ locale === 'pl' ? 'Węzły' : 'Nodes' }}: <strong class="text-slate-200">{{ curatorStatus.total_nodes }}</strong></span>
        <span>{{ locale === 'pl' ? 'Relacje' : 'Edges' }}: <strong class="text-slate-200">{{ curatorStatus.total_edges }}</strong></span>
        <span>{{ locale === 'pl' ? 'Osierocone' : 'Orphans' }}: <strong :class="curatorStatus.orphan_count > 0 ? 'text-amber-400 font-bold' : 'text-slate-200'">{{ curatorStatus.orphan_count }}</strong></span>
        <span>{{ locale === 'pl' ? 'Martwe linki' : 'Broken' }}: <strong :class="curatorStatus.broken_count > 0 ? 'text-rose-400 font-bold' : 'text-slate-200'">{{ curatorStatus.broken_count }}</strong></span>
        <span v-if="curatorStatus.last_run_timestamp" class="text-slate-400">
          {{ locale === 'pl' ? 'Ostatni audyt' : 'Last run' }}: {{ new Date(curatorStatus.last_run_timestamp).toLocaleTimeString() }}
        </span>
      </div>
    </div>

    <!-- Filter Component -->
    <MemoryFilter
      v-model:scope="activeScope"
      v-model:project="filterProject"
      :projects="projects"
      v-model:query="searchQuery"
      v-model:kind="filterKind"
      v-model:tag="filterTag"
      v-model:hide-receipts="hideReceipts"
      @search="refreshCurrentView"
    />

    <!-- GRAPH VIEW MODE -->
    <div v-if="viewMode === 'graph'" class="space-y-4">
      <div v-if="memoryStore.isGraphLoading" class="w-full h-[clamp(320px,65dvh,600px)] flex items-center justify-center rounded-xl border border-botex-800 bg-botex-900/40">
        <div class="flex flex-col items-center space-y-3 text-slate-400 text-xs">
          <RefreshCwIcon class="w-6 h-6 animate-spin text-cyan-400" />
          <span>{{ locale === 'pl' ? 'Ładowanie topologii grafu...' : 'Loading knowledge graph topology...' }}</span>
        </div>
      </div>
      <MemoryGraphView
        v-else
        :nodes="filteredGraphNodes"
        :edges="filteredGraphEdges"
        :health="graphHealth"
        @select-node="handleSelectGraphNode"
      />
    </div>

    <!-- CARDS LIST MODE -->
    <div v-else class="space-y-4">
      <div class="flex items-center justify-between text-xs text-slate-300 px-1">
        <span class="font-semibold uppercase tracking-wider font-mono">{{ t('vaultEntries') }} ({{ visibleEntries.length }})</span>
      </div>

      <!-- Skeleton Loader -->
      <div v-if="memoryStore.isLoading && entries.length === 0" class="grid grid-cols-1 lg:grid-cols-2 gap-4">
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
        v-else-if="visibleEntries.length === 0"
        class="rounded-xl border border-botex-800 bg-botex-900/50 p-10 text-center text-slate-300 text-xs space-y-3"
      >
        <p>{{ t('noMemoryFound') }}</p>
        <button
          type="button"
          class="inline-flex items-center space-x-1.5 min-h-[44px] px-3.5 py-2 rounded-lg bg-botex-800 hover:bg-botex-700 border border-botex-700 text-xs font-medium text-slate-100 transition"
          @click="openCreateModal"
        >
          <PlusIcon class="w-3.5 h-3.5 text-cyan-400" />
          <span>{{ t('addMemory') }}</span>
        </button>
      </div>

      <div v-else class="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MemoryCard
          v-for="entry in visibleEntries"
          :key="entry.memory_id"
          :entry="entry"
          @select="handleSelectCard(entry)"
          @delete="handleDeleteCard(entry)"
          @select-link="loadOtherNode"
          @select-tag="(t) => { filterTag = t }"
          @select-project="(p) => { filterProject = p }"
        />
      </div>
    </div>

    <!-- Node Detail Modal from Graph Selection -->
    <Modal
      :is-open="showNodeModal"
      :title="selectedNode?.title || (locale === 'pl' ? 'Szczegóły Notatki' : 'Memory Note Details')"
      :subtitle="selectedNode ? `ID: ${selectedNode.id} | Kind: ${selectedNode.kind} | Scope: ${selectedNode.scope}` : ''"
      max-width="2xl"
      @close="showNodeModal = false"
    >
      <div v-if="selectedNode" class="space-y-4 text-xs">
        <div class="flex flex-wrap items-center gap-2">
          <span class="px-2 py-0.5 rounded text-xs font-bold uppercase font-mono bg-botex-800 text-cyan-300 border border-botex-700">
            {{ selectedNode.kind }}
          </span>
          <span v-if="selectedNode.compiled_truth" class="px-2 py-0.5 rounded text-xs font-bold bg-cyan-950 text-cyan-400 border border-cyan-800 flex items-center space-x-1">
            <span>⭐ Compiled Truth</span>
          </span>
          <span v-for="tag in selectedNode.tags" :key="tag" class="px-2 py-0.5 rounded text-xs bg-botex-900 text-slate-300 border border-botex-800 font-mono">
            #{{ tag }}
          </span>
        </div>

        <!-- Backlinks section -->
        <div class="rounded-lg bg-botex-900 border border-botex-800 p-3 space-y-2">
          <h4 class="font-semibold text-slate-200 text-xs flex items-center space-x-1.5">
            <Link2Icon class="w-3.5 h-3.5 text-cyan-400" />
            <span>{{ locale === 'pl' ? 'Przychodzące odnośniki (Backlinks)' : 'Incoming Backlinks' }} ({{ nodeBacklinks.length }})</span>
          </h4>
          <div v-if="nodeBacklinks.length === 0" class="text-slate-400 italic text-xs">
            {{ locale === 'pl' ? 'Brak przychodzących wikilinków do tej notatki.' : 'No incoming wikilinks point to this note.' }}
          </div>
          <div v-else class="flex flex-wrap gap-2">
            <button
              v-for="b in nodeBacklinks"
              :key="b.id"
              type="button"
              class="min-h-[44px] lg:min-h-[36px] px-2 py-1 rounded bg-botex-950 hover:bg-botex-800 border border-botex-700 text-slate-200 font-mono text-xs transition flex items-center space-x-1"
              @click="loadOtherNode(b.id)"
            >
              <span>[[{{ b.id }}]]</span>
              <span class="text-slate-400 font-sans text-xs">({{ b.title }})</span>
            </button>
          </div>
        </div>

        <!-- Full Content View -->
        <div class="rounded-lg bg-botex-900/60 border border-botex-800 p-4 space-y-2 max-h-[55dvh] overflow-y-auto">
          <div class="flex items-center justify-between pb-2 border-b border-botex-800">
            <h4 class="font-semibold text-slate-200 text-xs">{{ locale === 'pl' ? 'Treść Notatki' : 'Note Content' }}</h4>
            <span class="text-xs font-mono text-slate-400 uppercase">{{ selectedNode.scope }}</span>
          </div>
          <MarkdownViewer v-if="nodeContent" :content="nodeContent" @navigate-wikilink="loadOtherNode" />
          <div v-else class="text-slate-400 italic text-xs py-4 text-center">
            {{ locale === 'pl' ? 'Ładowanie zawartości...' : 'Loading content...' }}
          </div>
        </div>
      </div>
      <template #footer>
        <div class="flex items-center justify-between w-full">
          <div v-if="selectedNode" class="flex items-center space-x-2">
            <Button
              v-if="!confirmingDeleteNode"
              variant="danger"
              size="sm"
              @click="confirmingDeleteNode = true"
            >
              {{ locale === 'pl' ? 'Usuń notatkę' : 'Delete Note' }}
            </Button>
            <template v-else>
              <Button
                variant="danger"
                size="sm"
                @click="handleDeleteSelectedNode"
              >
                {{ locale === 'pl' ? 'Potwierdź usunięcie' : 'Confirm Delete' }}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                @click="confirmingDeleteNode = false"
              >
                {{ t('cancel') }}
              </Button>
            </template>
          </div>
          <div class="flex items-center space-x-2">
            <Button
              v-if="selectedNode"
              variant="secondary"
              size="sm"
              @click="handleEditSelectedNode"
            >
              {{ locale === 'pl' ? 'Edytuj' : 'Edit' }}
            </Button>
            <Button variant="secondary" size="sm" @click="showNodeModal = false; confirmingDeleteNode = false">{{ t('close') }}</Button>
          </div>
        </div>
      </template>
    </Modal>

    <!-- Create Memory Modal -->
    <Modal
      :is-open="showCreateModal"
      :title="isEditingEntry ? (locale === 'pl' ? 'Edytuj notatkę w pamięci' : 'Edit Memory Note') : (locale === 'pl' ? 'Zapisz notatkę w pamięci' : 'Save Note to Memory Vault')"
      :subtitle="isEditingEntry ? `ID: ${newEntry.memory_id}` : (locale === 'pl' ? 'Notatki są automatycznie indeksowane z wikilinkami [[target]] w Obsidianie.' : 'Notes are indexed with [[target]] wikilinks in Obsidian.')"
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
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100"
            >
              <option value="context">Context (Background)</option>
              <option value="decision">Architectural Decision</option>
              <option value="lesson">Meta Lesson</option>
              <option value="handoff">Handoff Notes</option>
            </select>
          </div>

          <div>
            <label class="block font-medium text-slate-200 mb-1">
              {{ locale === 'pl' ? 'Lokalizacja zapisu' : 'Storage Scope' }}
            </label>
            <select
              v-model="newEntry.scope"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100 font-mono"
            >
              <option value="local">Local (.botex/memory/)</option>
              <option value="global">Global (~/.botex/memory/)</option>
            </select>
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block font-medium text-slate-200 mb-1">
              {{ locale === 'pl' ? 'Projekt (slug)' : 'Project (slug)' }}
            </label>
            <input
              v-model="newEntry.project"
              type="text"
              list="botex-project-suggestions"
              placeholder="botex, witcher3, ue4ss..."
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100 font-mono"
            />
            <datalist id="botex-project-suggestions">
              <option v-for="p in projects.filter((i) => i.project)" :key="p.project" :value="p.project" />
            </datalist>
          </div>

          <div>
            <label class="block font-medium text-slate-200 mb-1">
              {{ locale === 'pl' ? 'Tagi (oddzielone przecinkiem)' : 'Tags (comma separated)' }}
            </label>
            <input
              v-model="rawTags"
              type="text"
              placeholder="api, architecture, security"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100 font-mono"
            />
          </div>
        </div>

        <div>
          <label class="block font-medium text-slate-200 mb-1">
            {{ locale === 'pl' ? 'Treść (Markdown z [[wikilinkami]]) *' : 'Markdown Content (with [[wikilinks]]) *' }}
          </label>
          <textarea
            v-model="newEntry.content"
            rows="5"
            required
            :placeholder="locale === 'pl' ? 'Opisz ustalenia... możesz linkować inne notatki np. [[mem_xyz]]' : 'Describe decisions... link other notes like [[mem_xyz]]'"
            class="w-full rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 p-2.5 text-base sm:text-xs font-mono focus:outline-none focus:border-cyan-500"
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
import { ref, computed, watch, onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useMemoryStore } from '../stores/memory'
import { useI18n } from '../i18n'
import type { MemoryEntry, MemorySaveRequest, GraphNode, BacklinkItem } from '../api/types'
import MemoryFilter from '../components/memory/MemoryFilter.vue'
import MemoryCard from '../components/memory/MemoryCard.vue'
import MemoryGraphView from '../components/memory/MemoryGraphView.vue'
import Modal from '../components/common/Modal.vue'
import MarkdownViewer from '../components/common/MarkdownViewer.vue'
import Button from '../components/common/Button.vue'
import Input from '../components/common/Input.vue'
import {
  Database as DatabaseIcon,
  Plus as PlusIcon,
  RefreshCw as RefreshCwIcon,
  LayoutGrid as LayoutGridIcon,
  Network as NetworkIcon,
  Link2 as Link2Icon,
  ShieldCheck as ShieldCheckIcon,
  Sparkles as SparklesIcon
} from 'lucide-vue-next'

const memoryStore = useMemoryStore()
const {
  entries,
  projects,
  activeScope,
  filterProject,
  searchQuery,
  filterKind,
  filterTag,
  viewMode,
  graphNodes,
  graphEdges,
  graphHealth,
  curatorStatus,
  isCuratorRunning
} = storeToRefs(memoryStore)

const { locale, t } = useI18n()

async function handleRunCurator() {
  try {
    await memoryStore.triggerCuratorRun()
  } catch (err) {
    console.warn('Manual curator run failed:', err)
  }
}

const showCreateModal = ref(false)
const showNodeModal = ref(false)
const selectedNode = ref<GraphNode | null>(null)
const confirmingDeleteNode = ref(false)

const nodeContent = ref<string>('')
const nodeBacklinks = ref<BacklinkItem[]>([])
const rawTags = ref('architecture, pkm')
const hideReceipts = ref(false)

const newEntry = ref<MemorySaveRequest>({
  title: '',
  content: '',
  kind: 'context',
  tags: [],
  scope: 'local',
  workspace_dir: '.'
})

const isEditingEntry = computed(() => Boolean(newEntry.value.memory_id))

function isTaskReceipt(tags: string[] | undefined, id: string): boolean {
  if (id.startsWith('mem_task_')) return true
  return (tags || []).some((tag) => tag === 'task-receipt')
}

const visibleEntries = computed(() => {
  if (!hideReceipts.value) return entries.value
  return entries.value.filter((e) => !isTaskReceipt(e.tags, e.memory_id))
})

const filteredGraphNodes = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  const kind = filterKind.value
  const tag = filterTag.value.trim().toLowerCase().replace(/^#/, '')
  return graphNodes.value.filter((n) => {
    if (hideReceipts.value && isTaskReceipt(n.tags, n.id)) return false
    if (kind && n.kind !== kind) return false
    if (tag && !(n.tags || []).some((t) => t.toLowerCase() === tag)) return false
    if (q && !`${n.title} ${n.id} ${(n.tags || []).join(' ')}`.toLowerCase().includes(q)) return false
    return true
  })
})

const filteredGraphEdges = computed(() => {
  const ids = new Set(filteredGraphNodes.value.map((n) => n.id))
  return graphEdges.value.filter((e) => ids.has(e.source) && ids.has(e.target))
})

onMounted(() => {
  memoryStore.fetchProjects()
  memoryStore.fetchMemory()
  memoryStore.fetchCuratorStatus()
})


watch([activeScope, filterProject, filterKind, filterTag, hideReceipts], () => {
  memoryStore.fetchProjects()
  if (viewMode.value === 'graph') {
    memoryStore.fetchGraph()
  } else {
    memoryStore.fetchMemory()
  }
})

function switchToGraph() {
  viewMode.value = 'graph'
  memoryStore.fetchGraph()
}

function refreshCurrentView() {
  memoryStore.fetchProjects()
  if (viewMode.value === 'graph') {
    memoryStore.fetchGraph()
  } else {
    memoryStore.fetchMemory()
  }
}

async function handleSelectCard(entry: MemoryEntry) {
  confirmingDeleteNode.value = false
  selectedNode.value = {
    id: entry.memory_id,
    title: entry.title,
    kind: entry.kind || 'context',
    tags: entry.tags || [],
    status: entry.status || 'active',
    compiled_truth: Boolean(entry.compiled_truth),
    timestamp: entry.timestamp || '',
    scope: entry.scope || 'local',
    in_degree: 0,
    out_degree: 0
  }
  nodeContent.value = entry.content || ''
  nodeBacklinks.value = []
  showNodeModal.value = true

  const [freshEntry, backlinks] = await Promise.all([
    memoryStore.loadEntry(entry.memory_id, entry.scope),
    memoryStore.fetchBacklinks(entry.memory_id, entry.scope)
  ])
  if (freshEntry?.content) {
    nodeContent.value = freshEntry.content
  }
  nodeBacklinks.value = backlinks
}

async function handleSelectGraphNode(node: GraphNode) {
  selectedNode.value = node
  confirmingDeleteNode.value = false
  nodeContent.value = ''
  nodeBacklinks.value = []
  showNodeModal.value = true

  // Fetch full content and backlinks concurrently
  const [entry, backlinks] = await Promise.all([
    memoryStore.loadEntry(node.id, node.scope),
    memoryStore.fetchBacklinks(node.id, node.scope)
  ])

  nodeContent.value = entry?.content || (locale.value === 'pl' ? 'Brak dodatkowej treści.' : 'No additional content.')
  nodeBacklinks.value = backlinks
}

async function loadOtherNode(nodeId: string) {
  const targetNode = graphNodes.value.find((n) => n.id === nodeId)
  if (targetNode) {
    handleSelectGraphNode(targetNode)
  } else {
    // If not in local list, load entry directly
    const entry = await memoryStore.loadEntry(nodeId, activeScope.value)
    if (entry) {
      selectedNode.value = {
        id: entry.memory_id,
        title: entry.title,
        kind: entry.kind || 'context',
        tags: entry.tags || [],
        status: entry.status || 'active',
        compiled_truth: Boolean(entry.compiled_truth),
        timestamp: entry.timestamp || '',
        scope: entry.scope || 'local',
        in_degree: 0,
        out_degree: 0
      }
      nodeContent.value = entry.content || ''
      nodeBacklinks.value = await memoryStore.fetchBacklinks(nodeId, activeScope.value)
    }
  }
}

function openCreateModal() {
  const defaultProject = filterProject.value && filterProject.value !== '_unassigned' ? filterProject.value : 'botex'
  newEntry.value = {
    title: '',
    content: '',
    kind: 'context',
    tags: [],
    scope: 'local',
    project: defaultProject,
    workspace_dir: '.',
    memory_id: undefined
  }
  rawTags.value = 'architecture, pkm'
  showCreateModal.value = true
}

function handleEditSelectedNode() {
  if (!selectedNode.value) return
  const existingProj = memoryStore.activeEntry?.memory_id === selectedNode.value.id
    ? (memoryStore.activeEntry?.project || '')
    : ''
  newEntry.value = {
    memory_id: selectedNode.value.id,
    title: selectedNode.value.title,
    kind: selectedNode.value.kind || 'context',
    scope: selectedNode.value.scope === 'global' ? 'global' : 'local',
    project: existingProj,
    content: nodeContent.value,
    tags: [...(selectedNode.value.tags || [])],
    workspace_dir: '.'
  }
  rawTags.value = (selectedNode.value.tags || []).join(', ')
  confirmingDeleteNode.value = false
  showNodeModal.value = false
  showCreateModal.value = true
}

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
    newEntry.value.memory_id = undefined
    if (viewMode.value === 'graph') {
      memoryStore.fetchGraph()
    }
  }
}

async function handleDeleteCard(entry: MemoryEntry) {
  await memoryStore.deleteEntry(entry.memory_id, entry.scope || 'local')
  if (viewMode.value === 'graph') {
    memoryStore.fetchGraph()
  }
}


async function handleDeleteSelectedNode() {
  if (!selectedNode.value) return
  const ok = await memoryStore.deleteEntry(selectedNode.value.id, selectedNode.value.scope || 'local')
  if (ok) {
    confirmingDeleteNode.value = false
    showNodeModal.value = false
    selectedNode.value = null
    if (viewMode.value === 'graph') {
      memoryStore.fetchGraph()
    } else {
      memoryStore.fetchMemory()
    }
  }
}
</script>
