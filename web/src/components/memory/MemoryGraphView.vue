<template>
  <div
    ref="rootRef"
    class="relative w-full rounded-xl border border-botex-800 bg-botex-950 overflow-hidden flex flex-col select-none"
    :class="isFullscreen ? 'fixed inset-0 z-[9999] rounded-none h-[100dvh] w-screen border-none' : 'h-[clamp(360px,72dvh,720px)] min-h-[360px]'"
  >
    <!-- Top Bar: Health Stats & Navigation Controls -->
    <div class="absolute top-2.5 left-2.5 right-2.5 z-10 flex items-start justify-between gap-2 pointer-events-none max-w-full">
      <!-- Left column: Health Badge (diagnostics popover) + Quick Node Search -->
      <div class="flex flex-col items-start gap-1.5 min-w-0">
      <div ref="healthWrapRef" class="relative pointer-events-auto min-w-0">
        <button
          type="button"
          class="flex items-center space-x-1.5 bg-botex-900/90 backdrop-blur-md border border-botex-700/80 rounded-lg px-2.5 sm:px-3 py-1.5 shadow-xl text-xs min-w-0 truncate hover:border-botex-500 hover:bg-botex-800/90 transition cursor-pointer"
          :aria-expanded="showHealthPopover"
          :title="locale === 'pl' ? 'Diagnostyka grafu' : 'Graph diagnostics'"
          @click="showHealthPopover = !showHealthPopover"
        >
          <span class="flex items-center space-x-1.5 font-medium text-slate-100 shrink-0">
            <span
              class="w-2.5 h-2.5 rounded-full inline-block shadow-sm"
              :class="health?.is_healthy ? 'bg-emerald-400 shadow-emerald-500/50 animate-pulse' : 'bg-amber-400 shadow-amber-500/50'"
              aria-hidden="true"
            ></span>
            <span class="hidden sm:inline font-semibold">{{ health?.is_healthy ? (locale === 'pl' ? 'Graf Spójny' : 'Graph Healthy') : (locale === 'pl' ? 'Wykryto Uwagi' : 'Graph Issues') }}</span>
          </span>
          <span class="text-botex-600">|</span>
          <span class="text-slate-200 font-mono font-medium">{{ nodes.length }} <span class="hidden sm:inline text-slate-400">{{ locale === 'pl' ? 'węzłów' : 'nodes' }}</span></span>
          <span class="text-botex-600">|</span>
          <span class="text-slate-200 font-mono font-medium">{{ edges.length }} <span class="hidden sm:inline text-slate-400">{{ locale === 'pl' ? 'krawędzi' : 'edges' }}</span></span>
          <template v-if="health && health.orphan_count > 0">
            <span class="text-botex-600">|</span>
            <span class="text-amber-300 font-mono font-semibold">{{ health.orphan_count }} <span class="hidden sm:inline">{{ locale === 'pl' ? 'sierot' : 'orphans' }}</span></span>
          </template>
        </button>

        <!-- Diagnostics Popover: legend + orphans + broken links -->
        <div
          v-if="showHealthPopover"
          class="absolute top-full left-0 mt-2 z-30 w-[min(300px,calc(100vw-24px))] max-h-[300px] overflow-y-auto rounded-xl border border-botex-700 bg-botex-900/95 backdrop-blur-md shadow-2xl p-3 space-y-3 text-xs select-text cursor-auto"
        >
          <div class="flex items-center justify-between border-b border-botex-800 pb-1.5">
            <span class="font-semibold text-slate-100">{{ locale === 'pl' ? 'Diagnostyka grafu' : 'Graph Diagnostics' }}</span>
            <button type="button" class="text-slate-400 hover:text-white min-w-[32px] min-h-[32px] px-1 transition" :aria-label="locale === 'pl' ? 'Zamknij diagnostykę' : 'Close diagnostics'" @click="showHealthPopover = false">✕</button>
          </div>

          <div class="space-y-1.5">
            <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{{ locale === 'pl' ? 'Legenda' : 'Legend' }}</div>
            <div class="grid grid-cols-2 gap-1.5">
              <div v-for="item in legendItems" :key="item.kind" class="flex items-center space-x-1.5">
                <span class="w-2.5 h-2.5 rounded-full inline-block shrink-0" :style="{ backgroundColor: item.color }"></span>
                <span class="text-slate-300 font-mono text-[11px]">{{ item.label }}</span>
              </div>
              <div class="flex items-center space-x-1.5 col-span-2">
                <span class="w-2.5 h-2.5 rounded-full inline-block shrink-0 border border-cyan-300"></span>
                <span class="text-cyan-300 font-mono text-[11px]"><span aria-hidden="true">⭐</span> {{ locale === 'pl' ? 'Skompilowana Prawda (obręcz)' : 'Compiled Truth (orbital ring)' }}</span>
              </div>
            </div>
          </div>

          <div class="space-y-1">
            <div class="text-[10px] font-semibold uppercase tracking-wider text-amber-300">
              {{ locale === 'pl' ? 'Sieroty (bez linków)' : 'Orphans (no links)' }} ({{ orphanList.length }})
            </div>
            <div v-if="orphanList.length === 0" class="text-slate-500 italic">{{ locale === 'pl' ? 'Brak sierot.' : 'No orphans.' }}</div>
            <div v-else class="space-y-0.5">
              <button
                v-for="o in orphanList"
                :key="o.id"
                type="button"
                class="w-full text-left px-2 py-1 rounded hover:bg-botex-800 text-slate-200 font-mono text-[11px] truncate transition"
                :title="o.title || o.id"
                @click="focusNodeById(o.id)"
              >
                {{ o.id }}<span v-if="o.title" class="text-slate-500 font-sans"> · {{ o.title }}</span>
              </button>
            </div>
          </div>

          <div v-if="brokenLinks.length > 0" class="space-y-1">
            <div class="text-[10px] font-semibold uppercase tracking-wider text-rose-400">
              {{ locale === 'pl' ? 'Zerwane linki' : 'Broken links' }} ({{ brokenLinks.length }})
            </div>
            <div class="space-y-0.5">
              <button
                v-for="(b, i) in brokenLinks"
                :key="i"
                type="button"
                class="w-full text-left px-2 py-1 rounded hover:bg-botex-800 font-mono text-[11px] truncate transition"
                :title="locale === 'pl' ? 'Kliknij, aby sfokusować źródło' : 'Click to focus the source node'"
                @click="focusNodeById(b.source)"
              >
                <span class="text-slate-200">{{ b.source }}</span>
                <span class="text-slate-500"> → </span>
                <span class="text-rose-400">{{ b.target }}</span>
              </button>
            </div>
          </div>
        </div>
      </div>

      <!-- Quick Node Search: title/id match → dim non-matches, Enter or pick centers it -->
      <div class="relative pointer-events-auto w-[190px] max-w-[52vw]">
        <svg class="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400 pointer-events-none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="7" />
          <line x1="16.5" y1="16.5" x2="21" y2="21" />
        </svg>
        <input
          v-model="searchQuery"
          type="text"
          :placeholder="locale === 'pl' ? 'Szukaj węzła…' : 'Search nodes…'"
          :aria-label="locale === 'pl' ? 'Szukaj węzła na grafie' : 'Search graph nodes'"
          class="w-full bg-botex-900/90 backdrop-blur-md border border-botex-700/80 rounded-lg pl-7 pr-6 py-1.5 text-base sm:text-xs text-slate-100 placeholder-botex-500 shadow-xl focus:outline-none focus:border-cyan-500/60 transition"
          @focus="showHealthPopover = false"
          @keydown.enter.prevent="onSearchEnter"
          @keydown.esc.prevent="clearSearch"
        />
        <button
          v-if="searchQuery"
          type="button"
          class="absolute right-1 top-1/2 -translate-y-1/2 w-6 h-6 flex items-center justify-center text-slate-400 hover:text-white transition"
          :title="locale === 'pl' ? 'Wyczyść' : 'Clear'"
          :aria-label="locale === 'pl' ? 'Wyczyść wyszukiwanie' : 'Clear search'"
          @click="clearSearch"
        >✕</button>
        <div
          v-if="searchQuery.trim()"
          class="absolute top-full left-0 right-0 mt-1 z-30 rounded-lg border border-botex-700 bg-botex-900/95 backdrop-blur-md shadow-2xl max-h-56 overflow-y-auto"
        >
          <div v-if="searchMatches.length === 0" class="px-2.5 py-2 text-[11px] text-slate-500 italic">
            {{ locale === 'pl' ? 'Brak trafień.' : 'No matches.' }}
          </div>
          <button
            v-for="m in searchMatches.slice(0, 7)"
            :key="m.id"
            type="button"
            class="w-full text-left px-2.5 py-1.5 hover:bg-botex-800 flex items-center gap-1.5 text-[11px] min-w-0 transition"
            @click="onSearchPick(m.id)"
          >
            <span class="w-2 h-2 rounded-full shrink-0" :style="{ backgroundColor: getNodeColor(m.kind) }"></span>
            <span class="truncate text-slate-200">{{ m.title || m.id }}</span>
            <span class="ml-auto shrink-0 max-w-[45%] truncate font-mono text-[9.5px] text-slate-500">{{ m.id }}</span>
          </button>
        </div>
      </div>
      </div>

      <!-- Controls: Zoom & Fit & Fullscreen Toggle -->
      <div class="flex items-center space-x-1.5 pointer-events-auto shrink-0">
        <button
          type="button"
          class="w-8 h-8 rounded-lg bg-botex-900/90 hover:bg-botex-800 border border-botex-700/80 text-sm text-slate-200 font-bold transition shadow-lg flex items-center justify-center hover:text-white"
          @click="zoom(1.2)"
          title="Zoom In (+)"
          aria-label="Zoom in"
        >
          +
        </button>
        <button
          type="button"
          class="w-8 h-8 rounded-lg bg-botex-900/90 hover:bg-botex-800 border border-botex-700/80 text-sm text-slate-200 font-bold transition shadow-lg flex items-center justify-center hover:text-white"
          @click="zoom(0.8)"
          title="Zoom Out (-)"
          aria-label="Zoom out"
        >
          -
        </button>
        <button
          type="button"
          class="h-8 px-2.5 rounded-lg bg-botex-900/90 hover:bg-botex-800 border border-botex-700/80 text-xs text-slate-200 font-medium transition shadow-lg flex items-center space-x-1.5 hover:text-white"
          @click="resetView"
          :title="locale === 'pl' ? 'Wycentruj cały graf' : 'Center Graph'"
          :aria-label="locale === 'pl' ? 'Wycentruj cały graf' : 'Center Graph'"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="16" />
            <line x1="8" y1="12" x2="16" y2="12" />
          </svg>
          <span class="hidden sm:inline">{{ locale === 'pl' ? 'Wycentruj' : 'Fit All' }}</span>
        </button>
        <button
          type="button"
          class="w-8 h-8 rounded-lg bg-botex-900/90 hover:bg-botex-800 border border-botex-700/80 text-slate-200 hover:text-white transition shadow-lg flex items-center justify-center"
          @click="toggleFullscreen"
          :title="isFullscreen ? (locale === 'pl' ? 'Zamknij pełny ekran (Esc)' : 'Exit Fullscreen (Esc)') : (locale === 'pl' ? 'Pełny ekran' : 'Fullscreen')"
          :aria-label="isFullscreen ? (locale === 'pl' ? 'Zamknij pełny ekran' : 'Exit Fullscreen') : (locale === 'pl' ? 'Pełny ekran' : 'Fullscreen')"
          :aria-pressed="isFullscreen"
        >
          <svg v-if="!isFullscreen" class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" />
          </svg>
          <svg v-else class="w-4 h-4 text-cyan-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path stroke-linecap="round" stroke-linejoin="round" d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 9h4.5M15 9V4.5M15 9l5.25-5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
          </svg>
        </button>
      </div>
    </div>

    <!-- Canvas Interactive Viewport -->
    <div ref="containerRef" class="w-full h-full relative cursor-grab active:cursor-grabbing touch-none">
      <canvas
        ref="canvasRef"
        class="block w-full h-full"
        role="img"
        :aria-label="(locale === 'pl' ? 'Interaktywny graf powiązań notatek: ' : 'Interactive memory note link graph: ') + nodes.length + ' nodes, ' + edges.length + ' edges'"
      ></canvas>

      <!-- Hover / Focused Spotlight Tooltip (Viewport-Clamped for Mobile & Desktop) -->
      <div
        v-if="tooltipNode"
        class="absolute z-20 bg-botex-900/95 border border-cyan-500/40 backdrop-blur-md rounded-xl p-3 shadow-2xl text-xs space-y-1.5 w-[min(270px,calc(100vw-24px))] transition-opacity duration-150 ring-1 ring-cyan-500/20"
        :class="focusedNode ? 'pointer-events-auto' : 'pointer-events-none'"
        :style="tooltipStyle"
        @mousedown.stop
        @touchstart.stop
        @dblclick.stop
      >
        <div class="flex items-center justify-between gap-2 border-b border-botex-800 pb-1.5">
          <span class="font-bold text-slate-100 truncate text-sm">{{ tooltipNode.title }}</span>
          <span
            class="px-2 py-0.5 rounded text-[10px] uppercase font-mono font-bold tracking-wider shrink-0"
            :style="{ backgroundColor: `${getNodeColor(tooltipNode.kind)}25`, color: getNodeColor(tooltipNode.kind) }"
          >
            {{ tooltipNode.kind }}
          </span>
        </div>
        <div class="text-[11px] font-mono text-cyan-400/90 truncate">ID: {{ tooltipNode.id }}</div>
        <div class="flex items-center space-x-3 text-slate-300 text-[11px] pt-1">
          <span>Relacje wejściowe: <b class="text-white">{{ tooltipNode.in_degree }}</b></span>
          <span>Wyjściowe: <b class="text-white">{{ tooltipNode.out_degree }}</b></span>
        </div>
        <div v-if="tooltipNode.compiled_truth" class="flex items-center space-x-1 text-cyan-300 font-semibold text-[11px]">
          <span><span aria-hidden="true">⭐</span> Zweryfikowana Prawda (Compiled Truth)</span>
        </div>
        <div class="text-[10px] text-slate-400 pt-0.5 italic">
          {{ isTooltipFocused
            ? (locale === 'pl' ? 'Podwójny klik otwiera pełną notatkę' : 'Double-click to open the full note')
            : (locale === 'pl' ? 'Kliknij, aby sfokusować · podwójny klik otwiera notatkę' : 'Click to focus · double-click to open') }}
        </div>
        <button
          v-if="isTooltipFocused"
          type="button"
          class="w-full mt-1 px-2.5 py-1.5 rounded-lg bg-cyan-500/20 border border-cyan-500/50 text-cyan-200 font-semibold text-[11px] hover:bg-cyan-500/35 active:bg-cyan-500/40 transition"
          @click.stop="emitSelectNode(focusedNode)"
        >
          <span aria-hidden="true">📖</span> {{ locale === 'pl' ? 'Otwórz szczegóły' : 'Open details' }}
        </button>
      </div>
    </div>

    <!-- Bottom: Obsidian-Style Cluster Galaxy Navigator Ribbon -->
    <div class="absolute bottom-2.5 left-2.5 right-2.5 z-10 flex items-center justify-between pointer-events-none">
      <!-- Galaxy Selector Pills -->
      <div class="flex items-center gap-1.5 p-1 bg-botex-900/90 backdrop-blur-md border border-botex-700/80 rounded-xl shadow-2xl pointer-events-auto overflow-x-auto max-w-[calc(100%-80px)] no-scrollbar">
        <button
          type="button"
          class="px-2.5 py-1 rounded-lg text-xs font-medium transition flex items-center space-x-1.5 shrink-0"
          :class="activeClusterId === null ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 font-semibold shadow-sm' : 'text-slate-300 hover:text-white hover:bg-botex-800/80'"
          :aria-pressed="activeClusterId === null"
          @click="selectCluster(null)"
        >
          <span aria-hidden="true">✨</span>
          <span>{{ locale === 'pl' ? 'Wszystkie' : 'All' }} ({{ nodes.length }})</span>
        </button>

        <button
          v-for="c in clusterList"
          :key="c.id"
          type="button"
          class="px-2.5 py-1 rounded-lg text-xs font-medium transition flex items-center space-x-1.5 shrink-0"
          :class="activeClusterId === c.id ? 'bg-botex-800 text-white border font-semibold shadow-sm' : 'text-slate-300 hover:text-white hover:bg-botex-800/60'"
          :style="activeClusterId === c.id ? { borderColor: `${c.color}80`, color: c.color } : {}"
          :aria-pressed="activeClusterId === c.id"
          @click="selectCluster(c.id)"
        >
          <span class="w-2 h-2 rounded-full inline-block" :style="{ backgroundColor: c.color }" aria-hidden="true"></span>
          <span>{{ c.shortLabel }}</span>
          <span class="text-[10px] opacity-75 font-mono">({{ c.count }})</span>
        </button>
      </div>

      <!-- Quick Reset Indicator -->
      <div class="pointer-events-auto shrink-0 bg-botex-900/90 backdrop-blur-md border border-botex-700/80 rounded-xl px-2.5 py-1 text-[11px] text-slate-300 font-mono shadow-xl hidden md:flex items-center space-x-1">
        <span class="text-cyan-400 font-bold">LOD</span>
        <span class="text-slate-500">|</span>
        <span>{{ Math.round(transform.k * 100) }}%</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useI18n } from '../../i18n'
import type { GraphNode, GraphEdge, GraphHealth } from '../../api/types'

const { locale } = useI18n()

const prefersReducedMotion =
  typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches

const props = defineProps<{
  nodes: GraphNode[]
  edges: GraphEdge[]
  health: GraphHealth | null
}>()

const emit = defineEmits<{
  (e: 'select-node', node: GraphNode): void
}>()

interface SimNode extends GraphNode {
  x: number
  y: number
  vx: number
  vy: number
  radius: number
}

interface ClusterDef {
  id: string
  label: string
  shortLabel: string
  color: string
  icon: string
  targetX: number
  targetY: number
}

const CLUSTERS: Record<string, ClusterDef> = {
  architecture: {
    id: 'architecture',
    label: 'ARCHITEKTURA & VPS ORACLE',
    shortLabel: 'Architektura',
    color: '#38bdf8',
    icon: '🏛️',
    targetX: -320,
    targetY: -200,
  },
  security: {
    id: 'security',
    label: 'BEZPIECZEŃSTWO & CODEQL',
    shortLabel: 'Security',
    color: '#10b981',
    icon: '🛡️',
    targetX: 320,
    targetY: -200,
  },
  review: {
    id: 'review',
    label: 'CODE REVIEW & DEVIN',
    shortLabel: 'Review',
    color: '#c084fc',
    icon: '🔍',
    targetX: 320,
    targetY: 220,
  },
  modding: {
    id: 'modding',
    label: 'RPG & MODDING WIEDŹMIN',
    shortLabel: 'Modding',
    color: '#fbbf24',
    icon: '⚔️',
    targetX: -320,
    targetY: 220,
  },
  tasks: {
    id: 'tasks',
    label: 'RAPORTY & AUDYTY ZADAŃ',
    shortLabel: 'Zadania',
    color: '#94a3b8',
    icon: '📋',
    targetX: 0,
    targetY: 340,
  },
  general: {
    id: 'general',
    label: 'BAZA WIEDZY PKM',
    shortLabel: 'Wiedza',
    color: '#64748b',
    icon: '📚',
    targetX: 0,
    targetY: -60,
  },
}

function getNodeCluster(node: GraphNode): ClusterDef {
  const tStr = ((node.tags || []).join(' ') + ' ' + (node.id || '') + ' ' + (node.title || '')).toLowerCase()
  if (
    tStr.includes('vps') ||
    tStr.includes('oracle') ||
    tStr.includes('architecture') ||
    tStr.includes('harness') ||
    tStr.includes('infra') ||
    tStr.includes('systemd') ||
    (tStr.includes('bielik') && !tStr.includes('eval'))
  ) {
    return CLUSTERS.architecture
  }
  if (
    tStr.includes('security') ||
    tStr.includes('codeql') ||
    tStr.includes('cwe') ||
    tStr.includes('taint') ||
    tStr.includes('alert')
  ) {
    return CLUSTERS.security
  }
  if (
    tStr.includes('review') ||
    tStr.includes('devin') ||
    tStr.includes('handoff') ||
    tStr.includes('sprint') ||
    tStr.includes('cr')
  ) {
    return CLUSTERS.review
  }
  if (
    tStr.includes('modding') ||
    tStr.includes('skellige') ||
    tStr.includes('witcher') ||
    tStr.includes('wiedźmin') ||
    tStr.includes('script') ||
    tStr.includes('rpg') ||
    tStr.includes('sandbox')
  ) {
    return CLUSTERS.modding
  }
  if (
    node.id.startsWith('mem_task_') ||
    tStr.includes('task_') ||
    tStr.includes('receipt') ||
    tStr.includes('audit')
  ) {
    return CLUSTERS.tasks
  }
  return CLUSTERS.general
}

function adjustHex(hex: string, percent: number): string {
  const num = parseInt(hex.replace('#', ''), 16)
  const amt = Math.round(2.55 * percent)
  const R = Math.min(255, Math.max(0, (num >> 16) + amt))
  const G = Math.min(255, Math.max(0, ((num >> 8) & 0x00ff) + amt))
  const B = Math.min(255, Math.max(0, (num & 0x0000ff) + amt))
  return `#${((1 << 24) + (R << 16) + (G << 8) + B).toString(16).slice(1)}`
}

const COLOR_MAP: Record<string, string> = {
  decision: '#a855f7',
  lesson: '#f59e0b',
  handoff: '#10b981',
  context: '#38bdf8',
  deprecated: '#ef4444',
  default: '#64748b',
}

function getNodeColor(kind: string): string {
  const norm = (kind || '').toLowerCase().trim()
  return COLOR_MAP[norm] || COLOR_MAP.default
}

// Active Galaxy Filter State
const activeClusterId = ref<string | null>(null)
const showHealthPopover = ref(false)

// Quick Node Search — title/id match set drives canvas dim + priority labels;
// Enter / dropdown pick routes through focusNodeById for the center + pulse
const searchQuery = ref('')
const searchMatches = computed<GraphNode[]>(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return []
  return props.nodes.filter((n) => n.title.toLowerCase().includes(q) || n.id.toLowerCase().includes(q))
})
const searchMatchIds = computed(() => new Set(searchMatches.value.map((n) => n.id)))
const searchDimActive = computed(() => searchMatchIds.value.size > 0)

function clearSearch() {
  searchQuery.value = ''
}

function onSearchEnter() {
  const first = searchMatches.value[0]
  if (first) focusNodeById(first.id)
}

function onSearchPick(id: string) {
  focusNodeById(id)
}

const clusterList = computed(() => {
  const counts = new Map<string, number>()
  for (const n of props.nodes) {
    const c = getNodeCluster(n)
    counts.set(c.id, (counts.get(c.id) || 0) + 1)
  }
  return Object.values(CLUSTERS)
    .filter((c) => (counts.get(c.id) || 0) > 0)
    .map((c) => ({
      ...c,
      count: counts.get(c.id) || 0,
    }))
})

// DOM and Simulation References
const rootRef = ref<HTMLDivElement | null>(null)
const containerRef = ref<HTMLDivElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const isFullscreen = ref(false)
let animId = 0
let simNodes: SimNode[] = []
let transform = { x: 0, y: 0, k: 0.95 }

// Idle rAF sleep — the loop parks once every node crawls below ~0.04px/frame,
// no drag/hold/pinch is live and no focus pulse is running; wakeLoop() restarts it
const SLEEP_VELOCITY_SQ = 0.0016
const SLEEP_AFTER_STILL_FRAMES = 45
let loopActive = false
let stillFrames = 0

// Focus pulse — brief expanding ring whenever a node gets centered programmatically
const FOCUS_PULSE_MS = 700
let pulseNodeId: string | null = null
let pulseStartedAt = 0

interface LabelCandidate {
  n: SimNode
  isActive: boolean
  baseColor: string
  priority: number
}

// Canvas-drawn surfaces resolve against the active skin's CSS tokens so pills and
// cluster badges follow every data-skin palette (cached per skin switch)
let skinCacheKey = ''
let canvasTheme = {
  labelBg: 'rgba(15, 23, 42, 0.88)',
  labelBgActive: 'rgba(2, 6, 23, 0.96)',
  labelBorder: 'rgba(51, 65, 85, 0.7)',
  labelText: '#e2e8f0',
  badgeBg: '#090d16',
}

function resolveCanvasTheme() {
  const skin = document.documentElement.getAttribute('data-skin') || 'cyber-slate'
  if (skin === skinCacheKey) return
  skinCacheKey = skin
  const cs = getComputedStyle(document.documentElement)
  const tri = (name: string, fb: string) => (cs.getPropertyValue(name).trim() || fb).split(/\s+/).join(', ')
  canvasTheme = {
    labelBg: `rgba(${tri('--botex-900', '15 23 42')}, 0.88)`,
    labelBgActive: `rgba(${tri('--botex-950', '2 6 23')}, 0.96)`,
    labelBorder: `rgba(${tri('--botex-700', '51 65 85')}, 0.7)`,
    labelText: `rgb(${tri('--botex-200', '226 232 240')})`,
    badgeBg: `rgb(${tri('--botex-950', '2 6 23')})`,
  }
}

type FullscreenCapableDocument = Document & {
  webkitFullscreenElement?: Element | null
  webkitExitFullscreen?: () => void
}
type FullscreenCapableElement = HTMLElement & {
  webkitRequestFullscreen?: () => void | Promise<void>
}

// True when native Fullscreen API is unavailable or denied — UI falls back to CSS-only mode
let cssFullscreenFallback = false

function nativeFullscreenElement(): Element | null {
  const doc = document as FullscreenCapableDocument
  return document.fullscreenElement || doc.webkitFullscreenElement || null
}

function scheduleFullscreenReflow() {
  setTimeout(() => {
    handleResize()
    resetView()
  }, 50)
  setTimeout(() => {
    handleResize()
    resetView()
  }, 250)
}

function enterCssFullscreenFallback() {
  cssFullscreenFallback = true
  isFullscreen.value = true
  document.body.classList.add('overflow-hidden')
  scheduleFullscreenReflow()
}

function toggleFullscreen() {
  const el = rootRef.value as FullscreenCapableElement | null
  if (!el) return
  const doc = document as FullscreenCapableDocument

  if (isFullscreen.value) {
    if (nativeFullscreenElement() === el) {
      if (document.exitFullscreen) {
        document.exitFullscreen().catch(() => {})
      } else {
        doc.webkitExitFullscreen?.()
      }
      // isFullscreen re-syncs via onFullscreenChange
    } else {
      cssFullscreenFallback = false
      isFullscreen.value = false
      document.body.classList.remove('overflow-hidden')
      scheduleFullscreenReflow()
    }
    return
  }

  const requestNative = el.requestFullscreen?.bind(el) ?? el.webkitRequestFullscreen?.bind(el)
  if (!requestNative) {
    enterCssFullscreenFallback()
    return
  }
  try {
    const result = requestNative() as Promise<void> | undefined
    result?.catch?.(() => enterCssFullscreenFallback())
  } catch {
    enterCssFullscreenFallback()
  }
}

function exitFullscreen() {
  const el = rootRef.value
  const doc = document as FullscreenCapableDocument
  if (el && nativeFullscreenElement() === el) {
    if (document.exitFullscreen) {
      document.exitFullscreen().catch(() => {})
    } else {
      doc.webkitExitFullscreen?.()
    }
    return
  }
  if (isFullscreen.value) {
    cssFullscreenFallback = false
    isFullscreen.value = false
    document.body.classList.remove('overflow-hidden')
    scheduleFullscreenReflow()
  }
}

function onFullscreenChange() {
  const el = rootRef.value
  isFullscreen.value = Boolean(el && nativeFullscreenElement() === el) || cssFullscreenFallback
  document.body.classList.toggle('overflow-hidden', isFullscreen.value)
  scheduleFullscreenReflow()
}

function onKeydown(evt: KeyboardEvent) {
  if (evt.key === 'Escape' && isFullscreen.value) {
    exitFullscreen()
  }
}

// Interaction State
const hoveredNode = ref<SimNode | null>(null)
const focusedNode = ref<SimNode | null>(null)
const tooltipNode = computed<SimNode | null>(() => hoveredNode.value || focusedNode.value)
const isTooltipFocused = computed(() => Boolean(focusedNode.value && tooltipNode.value?.id === focusedNode.value.id))
const tooltipPos = ref({ x: 0, y: 0 })

const tooltipStyle = computed(() => {
  const w = containerRef.value?.clientWidth || window.innerWidth || 360
  const h = containerRef.value?.clientHeight || window.innerHeight || 600
  const cardW = Math.min(270, w - 24)
  const cardH = isTooltipFocused.value ? 175 : 135
  const pad = 12

  let left = tooltipPos.value.x + 14
  let top = tooltipPos.value.y + 14

  // If overflowing right edge, flip to left of node or clamp inside viewport
  if (left + cardW > w - pad) {
    left = Math.max(pad, tooltipPos.value.x - cardW - 14)
    if (left + cardW > w - pad) {
      left = Math.max(pad, w - cardW - pad)
    }
  }
  if (left < pad) left = pad

  // If overflowing bottom edge (including bottom galaxy bar), flip above node or clamp
  if (top + cardH > h - 56) {
    top = Math.max(52, tooltipPos.value.y - cardH - 14)
    if (top + cardH > h - 56) {
      top = Math.max(52, h - cardH - 56)
    }
  }
  if (top < 52) top = 52

  return {
    left: `${Math.round(left)}px`,
    top: `${Math.round(top)}px`,
  }
})
let isDragging = false
let draggedNode: SimNode | null = null
let dragStartPos = { x: 0, y: 0 }
let lastMousePos = { x: 0, y: 0 }
let mouseDownClient = { x: 0, y: 0 }

// Touch Gesture State — 1-finger PAN is the default; TAP selects; HOLD-to-drag grabs nodes; 2-finger pinch zooms
type TouchGestureMode = 'idle' | 'pending' | 'pan' | 'pinch' | 'drag'
let touchMode: TouchGestureMode = 'idle'
let touchHoldTimer: ReturnType<typeof setTimeout> | null = null
let touchStartClient = { x: 0, y: 0 }
let lastTouchClient = { x: 0, y: 0 }
let touchCandidate: SimNode | null = null
let pinchPrevDist = 0
let pinchPrevMid = { x: 0, y: 0 }
const TOUCH_TAP_MAX_PX = 10
const TOUCH_HOLD_MS = 250
const TOUCH_HOLD_CANCEL_PX = 8
const DOUBLE_TAP_MAX_MS = 320
let lastTapNodeId: string | null = null
let lastTapTimestamp = 0

// Deep Space 2.5D Starfield Background (Parallax Layers)
interface StarParticle {
  x: number
  y: number
  radius: number
  depth: number // 0.08 (far, slow) to 0.5 (near, faster)
  baseAlpha: number
  twinklePhase: number
  twinkleSpeed: number
  color: string
}

let starfield: StarParticle[] = []

function initStarfield(width: number, height: number) {
  const count = Math.min(180, Math.max(80, Math.round((width * height) / 4500)))
  const palette = ['#93c5fd', '#c4b5fd', '#67e8f9', '#f8fafc', '#38bdf8']
  starfield = []
  for (let i = 0; i < count; i++) {
    const depth = 0.08 + Math.random() * 0.45
    starfield.push({
      x: Math.random() * width,
      y: Math.random() * height,
      radius: 0.6 + depth * 1.8,
      depth,
      baseAlpha: 0.25 + depth * 0.55,
      twinklePhase: Math.random() * Math.PI * 2,
      twinkleSpeed: 0.8 + Math.random() * 1.6,
      color: palette[Math.floor(Math.random() * palette.length)],
    })
  }
}

function initSimulation() {
  if (!containerRef.value) return

  simNodes = props.nodes.map((n) => {
    const existing = simNodes.find((en) => en.id === n.id)
    const radius = Math.min(24, Math.max(12, 11 + (n.in_degree + n.out_degree) * 2))
    const cluster = getNodeCluster(n)
    const offsetAngle = Math.random() * Math.PI * 2
    const offsetDist = 45 + Math.random() * 160

    return {
      ...n,
      x: existing ? existing.x : cluster.targetX + Math.cos(offsetAngle) * offsetDist,
      y: existing ? existing.y : cluster.targetY + Math.sin(offsetAngle) * offsetDist,
      vx: 0,
      vy: 0,
      radius,
    }
  })

  // Re-anchor transient interaction state onto the fresh SimNode objects
  if (focusedNode.value) {
    focusedNode.value = simNodes.find((n) => n.id === focusedNode.value?.id) ?? null
  }
  if (hoveredNode.value) {
    hoveredNode.value = simNodes.find((n) => n.id === hoveredNode.value?.id) ?? null
  }

  for (let i = 0; i < 45; i++) updateSimulation()
  resetView()
}

function resetView() {
  if (!containerRef.value) return
  activeClusterId.value = null
  const w = containerRef.value.clientWidth
  const h = containerRef.value.clientHeight
  if (simNodes.length === 0) {
    transform = { x: w / 2, y: h / 2, k: 1.0 }
    return
  }
  const PAD = 75
  let minX = Infinity,
    minY = Infinity,
    maxX = -Infinity,
    maxY = -Infinity
  for (const n of simNodes) {
    minX = Math.min(minX, n.x - n.radius)
    maxX = Math.max(maxX, n.x + n.radius)
    minY = Math.min(minY, n.y - n.radius)
    maxY = Math.max(maxY, n.y + n.radius + 20)
  }
  const bw = Math.max(1, maxX - minX)
  const bh = Math.max(1, maxY - minY)
  const k = Math.max(0.2, Math.min(1.4, Math.min((w - PAD * 2) / bw, (h - PAD * 2) / bh)))
  transform = {
    x: w / 2 - ((minX + maxX) / 2) * k,
    y: h / 2 - ((minY + maxY) / 2) * k,
    k,
  }
  wakeLoop()
}

function selectCluster(clusterId: string | null) {
  activeClusterId.value = clusterId
  wakeLoop()
  if (!containerRef.value) return
  const w = containerRef.value.clientWidth
  const h = containerRef.value.clientHeight

  if (!clusterId) {
    resetView()
    return
  }

  const def = CLUSTERS[clusterId]
  if (!def) return

  // Smoothly center and zoom into the targeted galaxy
  const targetNodes = simNodes.filter((n) => getNodeCluster(n).id === clusterId)
  if (targetNodes.length === 0) return

  let avgX = 0,
    avgY = 0
  for (const n of targetNodes) {
    avgX += n.x
    avgY += n.y
  }
  avgX /= targetNodes.length
  avgY /= targetNodes.length

  const targetK = 1.35
  transform = {
    x: w / 2 - avgX * targetK,
    y: h / 2 - avgY * targetK,
    k: targetK,
  }
}

function zoom(factor: number) {
  if (!containerRef.value) return
  const w = containerRef.value.clientWidth / 2
  const h = containerRef.value.clientHeight / 2
  const newK = Math.max(0.2, Math.min(4.5, transform.k * factor))
  const curK = transform.k || 1.0
  transform.x = w - ((w - transform.x) * newK) / curK
  transform.y = h - ((h - transform.y) * newK) / curK
  transform.k = newK
  wakeLoop()
}

function updateSimulation(): number {
  const nodeMap = new Map<string, SimNode>()
  for (const n of simNodes) nodeMap.set(n.id, n)

  // 1. Repulsion & Anti-overlap Collision (avoids dense clumps with 300+ nodes)
  for (let i = 0; i < simNodes.length; i++) {
    for (let j = i + 1; j < simNodes.length; j++) {
      const a = simNodes[i]
      const b = simNodes[j]
      const dx = b.x - a.x
      const dy = b.y - a.y
      const distSq = dx * dx + dy * dy || 1
      const dist = Math.sqrt(distSq)

      // A. Hard collision / clearance buffer: prevents overlapping planetary discs
      const minDist = a.radius + b.radius + 18
      if (dist < minDist) {
        const overlap = (minDist - dist) * 0.45
        const ox = (dx / dist) * overlap
        const oy = (dy / dist) * overlap
        a.vx -= ox
        a.vy -= oy
        b.vx += ox
        b.vy += oy
      } else if (dist < 420) {
        // B. Electrostatic field repulsion
        const force = (650 / (distSq + 180)) * 2.4
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        a.vx -= fx
        a.vy -= fy
        b.vx += fx
        b.vy += fy
      }
    }
  }

  // 2. Attraction along edges
  for (const e of props.edges) {
    const src = nodeMap.get(e.source)
    const tgt = nodeMap.get(e.target)
    if (src && tgt) {
      const dx = tgt.x - src.x
      const dy = tgt.y - src.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 1
      const desiredDist = 130
      const force = (dist - desiredDist) * 0.018
      const fx = (dx / dist) * force
      const fy = (dy / dist) * force
      src.vx += fx
      src.vy += fy
      tgt.vx -= fx
      tgt.vy -= fy
    }
  }

  // 3. Dedicated Galaxy Gravity & Damping (softened pull prevents planetary collapse)
  let maxV2 = 0
  for (const n of simNodes) {
    if (n === draggedNode) continue
    const cluster = getNodeCluster(n)

    // Pull towards cluster center - organic planetary orbit rather than singularity collapse
    const cdx = cluster.targetX - n.x
    const cdy = cluster.targetY - n.y
    n.vx += cdx * 0.0022
    n.vy += cdy * 0.0022

    // Weak anchor to canvas origin
    n.vx -= n.x * 0.00025
    n.vy -= n.y * 0.00025

    n.vx *= 0.85
    n.vy *= 0.85
    n.x += n.vx
    n.y += n.vy
    const v2 = n.vx * n.vx + n.vy * n.vy
    if (v2 > maxV2) maxV2 = v2
  }
  return maxV2
}

// Pre-baked comet-trail echo colors (head → tail falloff) — avoids per-particle
// CanvasGradient churn inside the 60 FPS edge loop
const CONDUIT_TRAIL_COLORS = ['rgba(34, 211, 238, 0.34)', 'rgba(34, 211, 238, 0.18)', 'rgba(34, 211, 238, 0.07)']

function render() {
  const canvas = canvasRef.value
  const container = containerRef.value
  if (!canvas || !container) return

  // Auto-sync canvas pixel buffer with actual container CSS dimensions every frame (prevents stretching on mobile fullscreen/orientation change)
  const dpr = window.devicePixelRatio || 1
  const cw = container.clientWidth
  const ch = container.clientHeight
  const targetW = Math.round(cw * dpr)
  const targetH = Math.round(ch * dpr)
  if (cw > 0 && ch > 0 && (canvas.width !== targetW || canvas.height !== targetH)) {
    canvas.width = targetW
    canvas.height = targetH
    const rCtx = canvas.getContext('2d')
    if (rCtx) rCtx.setTransform(dpr, 0, 0, dpr, 0, 0)
    initStarfield(cw, ch)
  }

  const ctx = canvas.getContext('2d')
  if (!ctx) return

  resolveCanvasTheme()
  const searchMatchSet = searchMatchIds.value
  const searchDimNow = searchDimActive.value

  ctx.clearRect(0, 0, cw, ch)

  // Keep focused node tooltip anchored when panning/zooming
  if (focusedNode.value && !hoveredNode.value) {
    positionTooltipAtNode(focusedNode.value)
  }

  // 0. Render Deep Space 2.5D Starfield with Parallax
  const nowSec = prefersReducedMotion ? 0 : performance.now() * 0.001
  const w = cw
  const h = ch

  if (starfield.length > 0) {
    ctx.save()
    for (const star of starfield) {
      // Parallax shift based on camera translation and star depth
      const px = ((star.x + transform.x * star.depth) % w + w) % w
      const py = ((star.y + transform.y * star.depth) % h + h) % h
      const twinkle = 0.7 + 0.3 * Math.sin(nowSec * star.twinkleSpeed + star.twinklePhase)
      const alpha = Math.max(0.08, Math.min(1.0, star.baseAlpha * twinkle))

      ctx.beginPath()
      ctx.arc(px, py, star.radius, 0, Math.PI * 2)
      ctx.fillStyle = star.color
      ctx.globalAlpha = alpha
      ctx.shadowColor = star.color
      ctx.shadowBlur = star.depth > 0.3 ? 4 : 0
      ctx.fill()
    }
    ctx.restore()
  }

  ctx.save()

  // Apply Camera Pan & Zoom
  ctx.translate(transform.x, transform.y)
  ctx.scale(transform.k, transform.k)

  const nodeMap = new Map<string, SimNode>()
  for (const n of simNodes) nodeMap.set(n.id, n)

  // Determine Connected Spotlight Set — hovered node wins; focused node is the fallback anchor
  const spotlightRoot = hoveredNode.value || focusedNode.value
  const spotlightSet = new Set<string>()
  const hasActiveSpotlight = Boolean(spotlightRoot)
  if (spotlightRoot) {
    spotlightSet.add(spotlightRoot.id)
    for (const e of props.edges) {
      if (e.source === spotlightRoot.id) spotlightSet.add(e.target)
      if (e.target === spotlightRoot.id) spotlightSet.add(e.source)
    }
  }

  // 0. Draw High-Contrast Galaxy Enclosures & Badges
  const clusterDataMap = new Map<string, { count: number; minX: number; maxX: number; minY: number; maxY: number; def: ClusterDef }>()
  for (const n of simNodes) {
    const c = getNodeCluster(n)
    const cur = clusterDataMap.get(c.id) || {
      count: 0,
      minX: Infinity,
      maxX: -Infinity,
      minY: Infinity,
      maxY: -Infinity,
      def: c,
    }
    cur.count++
    cur.minX = Math.min(cur.minX, n.x - n.radius)
    cur.maxX = Math.max(cur.maxX, n.x + n.radius)
    cur.minY = Math.min(cur.minY, n.y - n.radius)
    cur.maxY = Math.max(cur.maxY, n.y + n.radius)
    clusterDataMap.set(c.id, cur)
  }

  for (const [cid, data] of clusterDataMap) {
    if (data.count === 0) continue
    const isFilteredCluster = activeClusterId.value === null || activeClusterId.value === cid
    const opacityMult = isFilteredCluster ? 1.0 : 0.25

    const cx = (data.minX + data.maxX) / 2
    const cy = (data.minY + data.maxY) / 2
    const w = Math.max(260, (data.maxX - data.minX) + 90)
    const h = Math.max(200, (data.maxY - data.minY) + 90)

    ctx.save()
    ctx.globalAlpha = opacityMult

    // Glassmorphic Rounded Enclosure
    ctx.beginPath()
    ctx.roundRect(cx - w / 2, cy - h / 2, w, h, 28)
    ctx.fillStyle = `${data.def.color}0a`
    ctx.fill()
    ctx.strokeStyle = `${data.def.color}35`
    ctx.lineWidth = isFilteredCluster && activeClusterId.value === cid ? 2.0 : 1.2
    ctx.setLineDash([8, 6])
    ctx.stroke()
    ctx.setLineDash([])

    // High-Contrast Header Badge
    const badgeText = `${data.def.icon} ${data.def.label} (${data.count})`
    ctx.font = '700 11px Inter, system-ui, sans-serif'
    const textWidth = ctx.measureText(badgeText).width
    const badgeW = textWidth + 24
    const badgeH = 24
    const badgeX = cx - badgeW / 2
    const badgeY = cy - h / 2 - badgeH / 2

    // Badge Container
    ctx.beginPath()
    ctx.roundRect(badgeX, badgeY, badgeW, badgeH, 12)
    ctx.fillStyle = canvasTheme.badgeBg
    ctx.fill()
    ctx.strokeStyle = data.def.color
    ctx.lineWidth = 1.4
    ctx.stroke()

    // Badge Text
    ctx.fillStyle = data.def.color
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'
    ctx.fillText(badgeText, cx, badgeY + badgeH / 2)

    ctx.restore()
  }

  // 1. Draw Edges with Directional Energy Particles
  props.edges.forEach((e, edgeIdx) => {
    const src = nodeMap.get(e.source)
    const tgt = nodeMap.get(e.target)
    if (!src || !tgt) return

    // Degenerate-edge guard: dist < 1 or non-finite geometry would make ux/uy and
    // the arrow math NaN — bail before touching canvas state
    const dx = tgt.x - src.x
    const dy = tgt.y - src.y
    const dist = Math.sqrt(dx * dx + dy * dy)
    if (!Number.isFinite(dist) || dist < 1) return

    if (activeClusterId.value !== null && (getNodeCluster(src).id !== activeClusterId.value || getNodeCluster(tgt).id !== activeClusterId.value)) return
    const isConnectedToSpotlight = hasActiveSpotlight && (src.id === spotlightRoot?.id || tgt.id === spotlightRoot?.id)
    const isSearchEdge = searchDimNow && (searchMatchSet.has(src.id) || searchMatchSet.has(tgt.id))
    const isDimmed = (hasActiveSpotlight && !isConnectedToSpotlight) || (searchDimNow && !isSearchEdge)

    ctx.save()
    ctx.globalAlpha = isDimmed ? 0.08 : isConnectedToSpotlight ? 1.0 : 0.65

    // Synaptic Connection Line — edges touching a compiled_truth node upgrade to a
    // radiant "Canonical Knowledge Conduit" (cyan/gold gradient core + ambient sheath)
    const isConduit = Boolean(src.compiled_truth || tgt.compiled_truth)
    const ux = dx / dist
    const uy = dy / dist

    if (isConduit) {
      ctx.beginPath()
      ctx.moveTo(src.x, src.y)
      ctx.lineTo(tgt.x, tgt.y)
      ctx.lineWidth = isConnectedToSpotlight ? 8.5 : 6.5
      ctx.strokeStyle = 'rgba(34, 211, 238, 0.10)'
      ctx.stroke()
    }

    ctx.beginPath()
    ctx.moveTo(src.x, src.y)
    ctx.lineTo(tgt.x, tgt.y)
    if (isConduit) {
      const conduitGrad = ctx.createLinearGradient(src.x, src.y, tgt.x, tgt.y)
      if (src.compiled_truth && tgt.compiled_truth) {
        conduitGrad.addColorStop(0, '#22d3ee')
        conduitGrad.addColorStop(0.5, '#f59e0b')
        conduitGrad.addColorStop(1, '#22d3ee')
      } else {
        conduitGrad.addColorStop(0, src.compiled_truth ? '#22d3ee' : '#f59e0b')
        conduitGrad.addColorStop(1, tgt.compiled_truth ? '#22d3ee' : '#f59e0b')
      }
      ctx.lineWidth = isConnectedToSpotlight ? 2.8 : 2.4
      ctx.strokeStyle = conduitGrad
      ctx.shadowColor = '#22d3ee'
      ctx.shadowBlur = isConnectedToSpotlight ? 14 : 11
    } else {
      ctx.lineWidth = isConnectedToSpotlight ? 2.4 : 1.2
      ctx.strokeStyle = isConnectedToSpotlight ? '#38bdf8' : 'rgba(56, 189, 248, 0.42)'
      if (isConnectedToSpotlight) {
        ctx.shadowColor = '#38bdf8'
        ctx.shadowBlur = 8
      }
    }
    ctx.stroke()
    ctx.shadowBlur = 0

    // Directional Arrow Head
    const arrowDist = Math.max(14, tgt.radius + 6)
    const arrowX = tgt.x - (dx / dist) * arrowDist
    const arrowY = tgt.y - (dy / dist) * arrowDist
    const angle = Math.atan2(dy, dx)
    const arrowSize = isConnectedToSpotlight ? 7 : 5

    ctx.beginPath()
    ctx.moveTo(arrowX, arrowY)
    ctx.lineTo(
      arrowX - arrowSize * Math.cos(angle - Math.PI / 6),
      arrowY - arrowSize * Math.sin(angle - Math.PI / 6)
    )
    ctx.lineTo(
      arrowX - arrowSize * Math.cos(angle + Math.PI / 6),
      arrowY - arrowSize * Math.sin(angle + Math.PI / 6)
    )
    ctx.closePath()
    ctx.fillStyle = isConduit
      ? tgt.compiled_truth
        ? '#22d3ee'
        : '#f59e0b'
      : isConnectedToSpotlight
        ? '#38bdf8'
        : 'rgba(56, 189, 248, 0.7)'
    if (isConduit) {
      ctx.shadowColor = tgt.compiled_truth ? '#22d3ee' : '#f59e0b'
      ctx.shadowBlur = isConnectedToSpotlight ? 12 : 8
    }
    ctx.fill()
    ctx.shadowBlur = 0

    // Animated Energy Flow Particles (60 FPS Synaptic Flow)
    // Conduits fire 3-4 energetic Knowledge Photon pulses with comet trails
    if (!isDimmed) {
      const particleCount = isConduit ? (isConnectedToSpotlight ? 4 : 3) : isConnectedToSpotlight ? 3 : 2
      const speed = isConduit ? (isConnectedToSpotlight ? 0.52 : 0.36) : isConnectedToSpotlight ? 0.38 : 0.22
      for (let pIdx = 0; pIdx < particleCount; pIdx++) {
        const phase = (nowSec * speed + edgeIdx * 0.28 + pIdx * (1.0 / particleCount)) % 1.0
        const px = src.x + dx * phase
        const py = src.y + dy * phase

        if (isConduit) {
          const flicker = 0.82 + 0.18 * Math.sin(nowSec * 7.0 + edgeIdx * 1.7 + pIdx * 2.4)
          const coreR = (isConnectedToSpotlight ? 4.0 : 3.2) * flicker

          // Comet tail — three fading echo dots trailing the photon head.
          // Fixed rgba() fills only: zero CanvasGradient churn in the 60 FPS loop.
          const trailLen = Math.min(30, dist * 0.22, phase * dist)
          if (trailLen > 0.5) {
            ctx.shadowColor = '#22d3ee'
            ctx.shadowBlur = 7
            for (let t = 0; t < CONDUIT_TRAIL_COLORS.length; t++) {
              const k = (t + 1) / CONDUIT_TRAIL_COLORS.length
              ctx.beginPath()
              ctx.arc(px - ux * trailLen * k, py - uy * trailLen * k, coreR * (1.05 - k * 0.75), 0, Math.PI * 2)
              ctx.fillStyle = CONDUIT_TRAIL_COLORS[t]
              ctx.fill()
            }
            ctx.shadowBlur = 0
          }

          // Photon bloom halo + white-hot core
          ctx.beginPath()
          ctx.arc(px, py, coreR * 2.0, 0, Math.PI * 2)
          ctx.fillStyle = 'rgba(34, 211, 238, 0.20)'
          ctx.fill()

          ctx.beginPath()
          ctx.arc(px, py, coreR, 0, Math.PI * 2)
          ctx.fillStyle = '#ffffff'
          ctx.shadowColor = '#22d3ee'
          ctx.shadowBlur = isConnectedToSpotlight ? 16 : 12
          ctx.fill()
          ctx.shadowBlur = 0
        } else {
          ctx.beginPath()
          ctx.arc(px, py, isConnectedToSpotlight ? 3.5 : 2.5, 0, Math.PI * 2)
          ctx.fillStyle = isConnectedToSpotlight ? '#ffffff' : '#7dd3fc'
          ctx.shadowColor = '#38bdf8'
          ctx.shadowBlur = isConnectedToSpotlight ? 10 : 5
          ctx.fill()
        }
      }
    }

    ctx.restore()
  })

  // 2. Draw Nodes with 3D Volumetric Orbs, Atmospheric Glow & Obsidian Badges
  const labelCandidates: LabelCandidate[] = []
  for (const n of simNodes) {
    const isHovered = hoveredNode.value?.id === n.id
    const isFocused = focusedNode.value?.id === n.id
    const isActive = isHovered || isFocused
    const isSpotlit = hasActiveSpotlight && spotlightSet.has(n.id)
    const isSearchMatch = searchMatchSet.has(n.id)
    const cluster = getNodeCluster(n)
    const baseColor = getNodeColor(n.kind) || cluster.color
    const r = n.radius

    if (activeClusterId.value !== null && cluster.id !== activeClusterId.value) continue
    const isDimmed = (hasActiveSpotlight && !isSpotlit) || (searchDimNow && !isSearchMatch)

    ctx.save()
    ctx.globalAlpha = isDimmed ? 0.14 : 1.0

    // A. Multi-Layer Atmospheric Bloom Glow
    const glowRadius = isActive ? r * 2.5 : r * 1.6
    const glowGrad = ctx.createRadialGradient(n.x, n.y, r * 0.5, n.x, n.y, glowRadius)
    glowGrad.addColorStop(0, `${baseColor}60`)
    glowGrad.addColorStop(0.5, `${baseColor}18`)
    glowGrad.addColorStop(1, 'transparent')
    ctx.fillStyle = glowGrad
    ctx.beginPath()
    ctx.arc(n.x, n.y, glowRadius, 0, Math.PI * 2)
    ctx.fill()

    // B. Orbital Planetary Ring for Compiled Truth
    if (n.compiled_truth) {
      ctx.save()
      ctx.translate(n.x, n.y)
      ctx.rotate(nowSec * 1.4)
      ctx.beginPath()
      ctx.ellipse(0, 0, r * 1.65, r * 0.65, 0, 0, Math.PI * 2)
      ctx.strokeStyle = '#67e8f9'
      ctx.lineWidth = 1.8
      ctx.shadowColor = '#67e8f9'
      ctx.shadowBlur = 10
      ctx.stroke()
      ctx.restore()
    }

    // C. 3D Volumetric Sphere with Specular Gleam
    // Light source positioned upper-left: (n.x - r*0.35, n.y - r*0.35)
    const orbGrad = ctx.createRadialGradient(
      n.x - r * 0.35,
      n.y - r * 0.35,
      r * 0.08,
      n.x,
      n.y,
      r
    )
    orbGrad.addColorStop(0, '#ffffff')                      // Specular core gleam
    orbGrad.addColorStop(0.25, adjustHex(baseColor, 45))    // Inner shine
    orbGrad.addColorStop(0.75, baseColor)                   // Core sphere tone
    orbGrad.addColorStop(1, adjustHex(baseColor, -55))      // 3D rim occlusion shadow

    ctx.beginPath()
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2)
    ctx.fillStyle = orbGrad
    ctx.shadowColor = baseColor
    ctx.shadowBlur = isActive ? 16 : 8
    ctx.fill()

    // D. Outer Glass Ring
    ctx.lineWidth = isActive ? 2.5 : 1.3
    ctx.strokeStyle = isActive ? '#ffffff' : adjustHex(baseColor, 35)
    ctx.stroke()

    // D2. Search-match halo — amber ring keeps query hits readable inside the dimmed field
    if (isSearchMatch) {
      ctx.beginPath()
      ctx.arc(n.x, n.y, r + 5.5, 0, Math.PI * 2)
      ctx.strokeStyle = '#facc15'
      ctx.lineWidth = 2.1
      ctx.shadowColor = '#facc15'
      ctx.shadowBlur = 10
      ctx.stroke()
      ctx.shadowBlur = 0
    }

    // E. Label candidacy — pills are painted in a second pass (below) ordered by
    // priority with screen-space AABB anti-collision
    // Far (k < 1.15): hide regular labels so galaxy orbits & badges remain uncluttered
    // Mid (1.15 <= k < 2.0): show only hovered, focused, spotlight, compiled_truth, search hits, or major hubs (degree >= 3)
    // Deep zoom (k >= 2.0): show regular labels with anti-collision clarity
    const isMajorHub = (n.in_degree + n.out_degree) >= 3
    const isPriorityNode = n.compiled_truth || isActive || isSpotlit || isMajorHub || isSearchMatch
    const shouldShowLabel = isActive || isSearchMatch || transform.k >= 2.0 || (transform.k >= 1.15 && isPriorityNode)

    if (shouldShowLabel && !isDimmed) {
      labelCandidates.push({
        n,
        isActive,
        baseColor,
        priority:
          (isActive ? 1e6 : 0) +
          (isSearchMatch ? 1e5 : 0) +
          (n.compiled_truth ? 1e4 : 0) +
          (isSpotlit ? 1e3 : 0) +
          Math.min(n.in_degree + n.out_degree, 999),
      })
    }

    ctx.restore()
  }

  // 3. Focus Pulse — expanding spotlight ripple when a node is centered programmatically
  const pulseAge = pulseNodeId ? performance.now() - pulseStartedAt : Infinity
  if (pulseNodeId && pulseAge < FOCUS_PULSE_MS) {
    const pn = nodeMap.get(pulseNodeId)
    if (pn) {
      const t = pulseAge / FOCUS_PULSE_MS
      ctx.beginPath()
      ctx.arc(pn.x, pn.y, pn.radius + 6 + t * 40, 0, Math.PI * 2)
      ctx.strokeStyle = `rgba(34, 211, 238, ${(1 - t) * 0.75})`
      ctx.lineWidth = 3 * (1 - t) + 0.6
      ctx.stroke()
    }
  } else {
    pulseNodeId = null
  }

  // 4. Label Pass — priority-sorted, screen-anchored pill size, AABB anti-collision.
  // fontPx is derived in screen space then scaled by 1/k for the world transform,
  // so labels keep a readable on-screen size band at every zoom level instead of
  // going microscopic zoomed out or billboard-sized zoomed in.
  if (labelCandidates.length > 0) {
    const invK = 1 / transform.k
    const fontScreen = Math.min(Math.max(10.5 * Math.sqrt(transform.k), 8), 15)
    const fontPx = fontScreen * invK
    const padX = fontPx * 0.62
    const pillH = fontPx + 9 * invK
    const pillR = Math.min(6 * invK, pillH * 0.5)
    const gapY = 7 * invK
    const M = 6 // screen-px collision margin around each painted pill
    const drawnRects: number[] = []

    labelCandidates.sort((a, b) => b.priority - a.priority)
    ctx.textAlign = 'center'
    ctx.textBaseline = 'middle'

    for (const c of labelCandidates) {
      const n = c.n
      // Cheap viewport cull before paying for measureText
      const nsx = n.x * transform.k + transform.x
      const nsy = n.y * transform.k + transform.y
      if (nsx < -140 || nsx > cw + 140 || nsy < -80 || nsy > ch + 80) continue

      ctx.font = `${c.isActive ? 700 : 500} ${fontPx}px Inter, system-ui, sans-serif`
      const titleText = n.title.length > 22 ? n.title.slice(0, 20) + '…' : n.title
      const labelW = ctx.measureText(titleText).width + padX * 2
      const labelX = n.x - labelW / 2
      const labelY = n.y + n.radius + gapY

      const rx1 = labelX * transform.k + transform.x - M
      const ry1 = labelY * transform.k + transform.y - M
      const rx2 = (labelX + labelW) * transform.k + transform.x + M
      const ry2 = (labelY + pillH) * transform.k + transform.y + M
      let collides = false
      for (let i = 0; i < drawnRects.length; i += 4) {
        if (rx1 < drawnRects[i + 2] && rx2 > drawnRects[i] && ry1 < drawnRects[i + 3] && ry2 > drawnRects[i + 1]) {
          collides = true
          break
        }
      }
      if (collides) continue
      drawnRects.push(rx1, ry1, rx2, ry2)

      ctx.beginPath()
      ctx.roundRect(labelX, labelY, labelW, pillH, pillR)
      ctx.fillStyle = c.isActive ? canvasTheme.labelBgActive : canvasTheme.labelBg
      ctx.fill()
      ctx.strokeStyle = c.isActive ? c.baseColor : canvasTheme.labelBorder
      ctx.lineWidth = c.isActive ? 1.5 : 0.8
      ctx.stroke()

      ctx.fillStyle = c.isActive ? '#ffffff' : canvasTheme.labelText
      ctx.fillText(titleText, n.x, labelY + pillH / 2)
    }
  }

  ctx.restore()
}

function loop() {
  animId = 0
  const maxV2 = updateSimulation()
  render()
  const fxActive = pulseNodeId !== null && performance.now() - pulseStartedAt < FOCUS_PULSE_MS
  const interacting = isDragging || draggedNode !== null || touchMode !== 'idle' || touchHoldTimer !== null
  stillFrames = !interacting && !fxActive && maxV2 < SLEEP_VELOCITY_SQ ? stillFrames + 1 : 0
  if (stillFrames >= SLEEP_AFTER_STILL_FRAMES) {
    loopActive = false
    return
  }
  animId = requestAnimationFrame(loop)
}

function wakeLoop() {
  stillFrames = 0
  if (!loopActive) {
    loopActive = true
    animId = requestAnimationFrame(loop)
  }
}

function startFocusPulse(n: SimNode | null) {
  if (!n || prefersReducedMotion) return
  pulseNodeId = n.id
  pulseStartedAt = performance.now()
  wakeLoop()
}

function handleResize() {
  if (!containerRef.value || !canvasRef.value) return
  const dpr = window.devicePixelRatio || 1
  const w = containerRef.value.clientWidth
  const h = containerRef.value.clientHeight
  canvasRef.value.width = w * dpr
  canvasRef.value.height = h * dpr
  const ctx = canvasRef.value.getContext('2d')
  if (ctx) ctx.scale(dpr, dpr)
  if (starfield.length === 0 || Math.abs(starfield[0]?.x || 0) > w * 2) {
    initStarfield(w, h)
  }
  wakeLoop()
}

function getCanvasPoint(evt: MouseEvent) {
  if (!containerRef.value) return { x: 0, y: 0, screenX: 0, screenY: 0 }
  const rect = containerRef.value.getBoundingClientRect()
  const mouseX = evt.clientX - rect.left
  const mouseY = evt.clientY - rect.top
  return {
    x: (mouseX - transform.x) / transform.k,
    y: (mouseY - transform.y) / transform.k,
    screenX: mouseX,
    screenY: mouseY,
  }
}

function hitTestNode(x: number, y: number): SimNode | null {
  for (let i = simNodes.length - 1; i >= 0; i--) {
    const n = simNodes[i]
    if (activeClusterId.value !== null && getNodeCluster(n).id !== activeClusterId.value) continue
    const dx = n.x - x
    const dy = n.y - y
    if (dx * dx + dy * dy <= (n.radius + 5) * (n.radius + 5)) {
      return n
    }
  }
  return null
}

function onMouseDown(evt: MouseEvent) {
  if (evt.button !== 0) return
  if (evt.target !== canvasRef.value) return
  wakeLoop()
  const pt = getCanvasPoint(evt)
  mouseDownClient = { x: evt.clientX, y: evt.clientY }
  const found = hitTestNode(pt.x, pt.y)
  if (found) {
    draggedNode = found
    isDragging = true
    dragStartPos = { x: pt.x, y: pt.y }
  } else {
    isDragging = true
    lastMousePos = { x: evt.clientX, y: evt.clientY }
  }
}

function onMouseMove(evt: MouseEvent) {
  wakeLoop()
  const pt = getCanvasPoint(evt)
  if (isDragging) {
    if (draggedNode) {
      draggedNode.x = pt.x
      draggedNode.y = pt.y
      draggedNode.vx = 0
      draggedNode.vy = 0
    } else {
      const dx = evt.clientX - lastMousePos.x
      const dy = evt.clientY - lastMousePos.y
      transform.x += dx
      transform.y += dy
      lastMousePos = { x: evt.clientX, y: evt.clientY }
    }
    return
  }

  // Hover detection
  const found = hitTestNode(pt.x, pt.y)
  hoveredNode.value = found
  if (found) {
    tooltipPos.value = { x: pt.screenX, y: pt.screenY }
  }
}

function onMouseUp(evt: MouseEvent) {
  wakeLoop()
  if (draggedNode && dragStartPos) {
    // Single click on a node — focus it (first stage); 'select-node' fires on double-click
    const dx = draggedNode.x - dragStartPos.x
    const dy = draggedNode.y - dragStartPos.y
    if (dx * dx + dy * dy < 25) {
      focusedNode.value = draggedNode
      positionTooltipAtNode(draggedNode)
      startFocusPulse(draggedNode)
    }
  } else if (isDragging) {
    // Click on empty background (not a camera pan) — clear focus
    const dx = evt.clientX - mouseDownClient.x
    const dy = evt.clientY - mouseDownClient.y
    if (dx * dx + dy * dy < 25) {
      focusedNode.value = null
    }
  }
  isDragging = false
  draggedNode = null
}

function onDoubleClick(evt: MouseEvent) {
  wakeLoop()
  const pt = getCanvasPoint(evt)
  const found = hitTestNode(pt.x, pt.y)
  if (found) {
    emitSelectNode(found)
  }
}

// Dedupes rapid duplicate 'select-node' emissions (e.g. double-tap followed by a synthetic dblclick)
let lastSelectEmitId: string | null = null
let lastSelectEmitTime = 0

function emitSelectNode(node: SimNode | null) {
  if (!node) return
  const now = performance.now()
  if (lastSelectEmitId === node.id && now - lastSelectEmitTime < 450) return
  lastSelectEmitId = node.id
  lastSelectEmitTime = now
  // If browser native Fullscreen is active on rootRef, exit native FS so <Teleport to="body"> modal is visible
  const el = rootRef.value
  const doc = document as FullscreenCapableDocument
  if (el && nativeFullscreenElement() === el) {
    if (document.exitFullscreen) {
      document.exitFullscreen().catch(() => {})
    } else {
      doc.webkitExitFullscreen?.()
    }
  }
  emit('select-node', node)
}

function positionTooltipAtNode(n: SimNode) {
  tooltipPos.value = { x: n.x * transform.k + transform.x, y: n.y * transform.k + transform.y }
}

// Graph Health Diagnostics Popover state & helpers
const healthWrapRef = ref<HTMLDivElement | null>(null)

const legendItems = computed(() => [
  { kind: 'decision', label: 'decision', color: COLOR_MAP.decision },
  { kind: 'lesson', label: 'lesson', color: COLOR_MAP.lesson },
  { kind: 'handoff', label: 'handoff', color: COLOR_MAP.handoff },
  { kind: 'context', label: 'context', color: COLOR_MAP.context },
  { kind: 'default', label: locale.value === 'pl' ? 'inne' : 'other', color: COLOR_MAP.default },
])

const orphanList = computed<Array<{ id: string; title: string }>>(() => {
  const list = props.health?.orphans || []
  return list
    .map((o: any) => ({ id: String(o?.id ?? o ?? ''), title: String(o?.title ?? '') }))
    .filter((o) => o.id.length > 0)
})

const brokenLinks = computed(() => props.health?.broken_links || [])

function focusNodeById(id: string) {
  const n = simNodes.find((sn) => sn.id === id)
  showHealthPopover.value = false
  if (!n) return
  focusedNode.value = n
  hoveredNode.value = null
  if (containerRef.value) {
    const w = containerRef.value.clientWidth
    const h = containerRef.value.clientHeight
    const k = Math.max(1.0, transform.k)
    transform = { x: w / 2 - n.x * k, y: h / 2 - n.y * k, k }
  }
  positionTooltipAtNode(n)
  startFocusPulse(n)
}

function onDocumentPointerDown(evt: Event) {
  if (!showHealthPopover.value) return
  const el = healthWrapRef.value
  if (el && evt.target instanceof Node && !el.contains(evt.target)) {
    showHealthPopover.value = false
  }
}

function onWheel(evt: WheelEvent) {
  evt.preventDefault()
  if (!containerRef.value) return
  const rect = containerRef.value.getBoundingClientRect()
  const mouseX = evt.clientX - rect.left
  const mouseY = evt.clientY - rect.top

  const zoomFactor = evt.deltaY < 0 ? 1.15 : 0.88
  const newK = Math.max(0.18, Math.min(4.5, transform.k * zoomFactor))

  transform.x = mouseX - ((mouseX - transform.x) * newK) / transform.k
  transform.y = mouseY - ((mouseY - transform.y) * newK) / transform.k
  transform.k = newK
  wakeLoop()
}

// Touch Handling for Mobile Devices — PAN is the default 1-finger gesture; nodes are never grabbed on touchdown
function clientToCanvas(clientX: number, clientY: number) {
  if (!containerRef.value) return { x: 0, y: 0 }
  const rect = containerRef.value.getBoundingClientRect()
  return {
    x: (clientX - rect.left - transform.x) / transform.k,
    y: (clientY - rect.top - transform.y) / transform.k,
  }
}

function hitTestClientPoint(clientX: number, clientY: number): SimNode | null {
  const pt = clientToCanvas(clientX, clientY)
  return hitTestNode(pt.x, pt.y)
}

function cancelTouchHold() {
  if (touchHoldTimer !== null) {
    clearTimeout(touchHoldTimer)
    touchHoldTimer = null
  }
}

function touchDistance(a: Touch, b: Touch): number {
  const dx = a.clientX - b.clientX
  const dy = a.clientY - b.clientY
  return Math.sqrt(dx * dx + dy * dy)
}

function touchMidpoint(a: Touch, b: Touch): { x: number; y: number } {
  if (!containerRef.value) return { x: 0, y: 0 }
  const rect = containerRef.value.getBoundingClientRect()
  return {
    x: (a.clientX + b.clientX) / 2 - rect.left,
    y: (a.clientY + b.clientY) / 2 - rect.top,
  }
}

function onTouchStart(evt: TouchEvent) {
  if (evt.target !== canvasRef.value) return
  wakeLoop()
  if (evt.touches.length === 1) {
    const t = evt.touches[0]
    touchStartClient = { x: t.clientX, y: t.clientY }
    lastTouchClient = { x: t.clientX, y: t.clientY }
    touchCandidate = hitTestClientPoint(t.clientX, t.clientY)
    // PAN by default; on a node we first wait to distinguish TAP vs HOLD-to-drag
    touchMode = touchCandidate ? 'pending' : 'pan'
    if (touchCandidate) {
      const candidate = touchCandidate
      touchHoldTimer = setTimeout(() => {
        touchHoldTimer = null
        if (touchMode !== 'pending' || touchCandidate !== candidate) return
        touchMode = 'drag'
        draggedNode = candidate
        isDragging = true
        dragStartPos = { x: candidate.x, y: candidate.y }
        hoveredNode.value = candidate
        if (containerRef.value) {
          const rect = containerRef.value.getBoundingClientRect()
          tooltipPos.value = { x: lastTouchClient.x - rect.left, y: lastTouchClient.y - rect.top }
        }
        navigator.vibrate?.(20)
      }, TOUCH_HOLD_MS)
    }
  } else if (evt.touches.length >= 2) {
    cancelTouchHold()
    isDragging = false
    draggedNode = null
    touchCandidate = null
    touchMode = 'pinch'
    pinchPrevDist = touchDistance(evt.touches[0], evt.touches[1])
    pinchPrevMid = touchMidpoint(evt.touches[0], evt.touches[1])
  }
}

function onTouchMove(evt: TouchEvent) {
  wakeLoop()
  if (touchMode === 'pinch' && evt.touches.length >= 2) {
    const dist = touchDistance(evt.touches[0], evt.touches[1])
    const mid = touchMidpoint(evt.touches[0], evt.touches[1])
    if (pinchPrevDist > 0) {
      const newK = Math.max(0.18, Math.min(4.5, (transform.k * dist) / pinchPrevDist))
      const curK = transform.k || 1.0
      transform.x = mid.x - ((mid.x - transform.x) * newK) / curK
      transform.y = mid.y - ((mid.y - transform.y) * newK) / curK
      transform.k = newK
      transform.x += mid.x - pinchPrevMid.x
      transform.y += mid.y - pinchPrevMid.y
    }
    pinchPrevDist = dist
    pinchPrevMid = mid
    return
  }

  if (evt.touches.length !== 1) return
  const t = evt.touches[0]

  if (touchMode === 'pending') {
    const totalDx = t.clientX - touchStartClient.x
    const totalDy = t.clientY - touchStartClient.y
    if (totalDx * totalDx + totalDy * totalDy > TOUCH_HOLD_CANCEL_PX * TOUCH_HOLD_CANCEL_PX) {
      // Finger moved before the hold elapsed — abort drag arming and switch to camera PAN
      cancelTouchHold()
      touchCandidate = null
      touchMode = 'pan'
    } else {
      lastTouchClient = { x: t.clientX, y: t.clientY }
      return
    }
  }

  if (touchMode === 'pan') {
    transform.x += t.clientX - lastTouchClient.x
    transform.y += t.clientY - lastTouchClient.y
  } else if (touchMode === 'drag' && draggedNode) {
    const pt = clientToCanvas(t.clientX, t.clientY)
    draggedNode.x = pt.x
    draggedNode.y = pt.y
    draggedNode.vx = 0
    draggedNode.vy = 0
    if (containerRef.value) {
      const rect = containerRef.value.getBoundingClientRect()
      tooltipPos.value = { x: t.clientX - rect.left, y: t.clientY - rect.top }
    }
  }

  lastTouchClient = { x: t.clientX, y: t.clientY }
}

function onTouchEnd(evt: TouchEvent) {
  wakeLoop()
  cancelTouchHold()

  if (evt.touches.length >= 2) {
    // Still multi-touch — re-baseline the pinch to avoid a zoom jump
    pinchPrevDist = touchDistance(evt.touches[0], evt.touches[1])
    pinchPrevMid = touchMidpoint(evt.touches[0], evt.touches[1])
    return
  }

  if (evt.touches.length === 1) {
    // One finger remains after a pinch — continue as PAN without a jump
    const t = evt.touches[0]
    touchMode = 'pan'
    touchCandidate = null
    lastTouchClient = { x: t.clientX, y: t.clientY }
    return
  }

  // Last finger lifted — single TAP focuses the node; a second TAP on the same node
  // within DOUBLE_TAP_MAX_MS opens it ('select-node'); a TAP on empty space clears focus
  const endTouch = evt.changedTouches[0]
  const endDx = endTouch.clientX - touchStartClient.x
  const endDy = endTouch.clientY - touchStartClient.y
  const isTap = endDx * endDx + endDy * endDy < TOUCH_TAP_MAX_PX * TOUCH_TAP_MAX_PX

  if (touchCandidate && (touchMode === 'pending' || touchMode === 'drag') && isTap) {
    const now = performance.now()
    if (lastTapNodeId === touchCandidate.id && now - lastTapTimestamp < DOUBLE_TAP_MAX_MS) {
      lastTapNodeId = null
      lastTapTimestamp = 0
      emitSelectNode(touchCandidate)
    } else {
      focusedNode.value = touchCandidate
      positionTooltipAtNode(touchCandidate)
      startFocusPulse(touchCandidate)
      lastTapNodeId = touchCandidate.id
      lastTapTimestamp = now
    }
  } else if (!touchCandidate && touchMode === 'pan' && isTap) {
    focusedNode.value = null
    lastTapNodeId = null
  }

  isDragging = false
  draggedNode = null
  touchCandidate = null
  hoveredNode.value = null
  touchMode = 'idle'
  pinchPrevDist = 0
}

watch(
  () => [props.nodes, props.edges],
  () => {
    initSimulation()
    wakeLoop()
  },
  { deep: true }
)

watch(searchQuery, () => wakeLoop())

onMounted(() => {
  handleResize()
  window.addEventListener('resize', handleResize)
  window.addEventListener('keydown', onKeydown)
  document.addEventListener('fullscreenchange', onFullscreenChange)
  document.addEventListener('webkitfullscreenchange', onFullscreenChange)
  document.addEventListener('mousedown', onDocumentPointerDown)
  document.addEventListener('touchstart', onDocumentPointerDown, { passive: true })
  if (containerRef.value) {
    containerRef.value.addEventListener('mousedown', onMouseDown)
    containerRef.value.addEventListener('dblclick', onDoubleClick)
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    containerRef.value.addEventListener('wheel', onWheel, { passive: false })
    containerRef.value.addEventListener('touchstart', onTouchStart, { passive: true })
    window.addEventListener('touchmove', onTouchMove, { passive: true })
    window.addEventListener('touchend', onTouchEnd)
    window.addEventListener('touchcancel', onTouchEnd)
  }
  initSimulation()
  wakeLoop()
})

onUnmounted(() => {
  cancelAnimationFrame(animId)
  loopActive = false
  cancelTouchHold()
  window.removeEventListener('resize', handleResize)
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
  window.removeEventListener('touchmove', onTouchMove)
  window.removeEventListener('touchend', onTouchEnd)
  window.removeEventListener('touchcancel', onTouchEnd)
  document.removeEventListener('fullscreenchange', onFullscreenChange)
  document.removeEventListener('webkitfullscreenchange', onFullscreenChange)
  document.removeEventListener('mousedown', onDocumentPointerDown)
  document.removeEventListener('touchstart', onDocumentPointerDown)
  document.body.classList.remove('overflow-hidden')
  if (containerRef.value) {
    containerRef.value.removeEventListener('mousedown', onMouseDown)
    containerRef.value.removeEventListener('dblclick', onDoubleClick)
    containerRef.value.removeEventListener('wheel', onWheel)
    containerRef.value.removeEventListener('touchstart', onTouchStart)
  }
})
</script>

<style scoped>
.no-scrollbar::-webkit-scrollbar {
  display: none;
}
.no-scrollbar {
  -ms-overflow-style: none;
  scrollbar-width: none;
}
</style>
