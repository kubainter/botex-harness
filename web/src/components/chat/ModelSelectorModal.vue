<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-100 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="isOpen"
        class="fixed inset-0 z-50 bg-botex-950/85 backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4"
        role="dialog"
        aria-modal="true"
        aria-label="Model Selector"
        @click.self="close"
      >
        <div
          class="w-full sm:max-w-2xl bg-botex-900 border-t sm:border border-botex-700 rounded-t-2xl sm:rounded-xl shadow-2xl text-slate-100 flex flex-col max-h-[88dvh] sm:max-h-[82dvh] px-safe pb-safe overflow-hidden"
          @keydown="handleKeydown"
        >
          <!-- Top Header & Search Input -->
          <div class="p-3.5 sm:p-4 border-b border-botex-800 space-y-3 bg-botex-900">
            <div class="flex items-center justify-between gap-2">
              <div>
                <h3 class="text-sm font-semibold text-slate-100 tracking-tight">
                  {{ t('models.title') }}
                </h3>
                <p class="text-xs text-slate-300 mt-0.5">
                  {{ t('models.subtitle') }} ({{ filteredModels.length }} / {{ availableModels.length }})
                </p>
              </div>
              <div class="flex items-center space-x-1.5">
                <button
                  type="button"
                  title="Refresh catalog"
                  class="min-w-[44px] min-h-[44px] lg:min-w-[36px] lg:min-h-[36px] px-2.5 rounded-lg bg-botex-950 hover:bg-botex-800 border border-botex-700 text-slate-300 hover:text-white transition flex items-center justify-center"
                  @click="onRefresh"
                >
                  <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': isLoading }" />
                </button>
                <button
                  type="button"
                  aria-label="Close model selector"
                  class="min-w-[44px] min-h-[44px] lg:min-w-[36px] lg:min-h-[36px] px-2.5 rounded-lg bg-botex-950 hover:bg-botex-800 border border-botex-700 text-slate-300 hover:text-white transition flex items-center justify-center"
                  @click="close"
                >
                  <XIcon class="w-4 h-4" />
                </button>
              </div>
            </div>

            <!-- Search Input -->
            <div class="relative">
              <SearchIcon class="w-4 h-4 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" />
              <input
                ref="searchInputRef"
                v-model="searchQuery"
                type="text"
                :placeholder="t('models.search.placeholder')"
                class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg pl-10 pr-20 py-2 text-base sm:text-sm text-slate-100 placeholder-slate-400 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 font-sans"
              />
              <button
                v-if="searchQuery"
                type="button"
                class="absolute right-2.5 top-1/2 -translate-y-1/2 min-h-[32px] px-2.5 py-1 text-xs font-mono text-slate-300 hover:text-white bg-botex-800 rounded flex items-center"
                @click="searchQuery = ''"
              >
                Clear
              </button>
              <span
                v-else
                class="hidden sm:inline-block absolute right-3 top-1/2 -translate-y-1/2 px-1.5 py-0.5 text-xs font-mono text-slate-400 bg-botex-900 border border-botex-700 rounded"
              >
                ESC
              </span>
            </div>

            <!-- Provider Filter Pills / Tabs -->
            <div
              class="flex items-center gap-1.5 overflow-x-auto pb-0.5 no-scrollbar"
              role="tablist"
              @wheel="onWheelScrollX"
            >
              <button
                v-for="pTab in providerTabs"
                :key="pTab.id"
                type="button"
                role="tab"
                :aria-selected="activeProvider === pTab.id"
                :class="[
                  'min-h-[40px] lg:min-h-[36px] px-2.5 py-1 rounded-md text-xs font-mono font-medium whitespace-nowrap transition border flex items-center space-x-1.5 shrink-0',
                  activeProvider === pTab.id
                    ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/50 font-semibold'
                    : 'bg-botex-950 text-slate-400 border-botex-800 hover:border-botex-700 hover:text-white'
                ]"
                @click="activeProvider = pTab.id"
              >
                <span>{{ pTab.label }}</span>
                <span
                  class="text-xs font-mono px-1 py-px rounded"
                  :class="activeProvider === pTab.id ? 'bg-emerald-500/20 text-emerald-200' : 'bg-botex-900 text-slate-500'"
                >
                  {{ pTab.count }}
                </span>
              </button>
            </div>

            <!-- Category Filter Pills / Tabs -->
            <div
              class="flex items-center gap-1.5 overflow-x-auto pb-0.5 no-scrollbar"
              role="tablist"
              @wheel="onWheelScrollX"
            >
              <button
                v-for="tab in categoryTabs"
                :key="tab.id"
                type="button"
                role="tab"
                :aria-selected="activeCategory === tab.id"
                :class="[
                  'min-h-[40px] lg:min-h-[36px] px-3 py-1.5 rounded-lg text-xs font-medium whitespace-nowrap transition border flex items-center space-x-1.5 shrink-0',
                  activeCategory === tab.id
                    ? 'bg-sky-500/15 text-sky-300 border-sky-500/50 font-semibold'
                    : 'bg-botex-950 text-slate-300 border-botex-800 hover:border-botex-700 hover:text-white'
                ]"
                @click="setCategory(tab.id)"
              >
                <span>{{ tab.label }}</span>
                <span
                  class="text-xs font-mono px-1.5 py-px rounded"
                  :class="activeCategory === tab.id ? 'bg-sky-500/20 text-sky-200' : 'bg-botex-900 text-slate-400'"
                >
                  {{ tab.count }}
                </span>
              </button>
            </div>
          </div>

          <!-- Skeleton Loader when refreshing empty list -->
          <div v-if="isLoading && models.length === 0" class="p-4 space-y-2.5 overflow-y-auto flex-1">
            <div
              v-for="n in 5"
              :key="n"
              class="h-16 rounded-lg bg-botex-950 border border-botex-800 p-3 animate-pulse flex items-center justify-between"
            >
              <div class="space-y-2 w-2/3">
                <div class="h-3.5 bg-botex-800 rounded w-1/2"></div>
                <div class="h-2.5 bg-botex-800 rounded w-3/4"></div>
              </div>
              <div class="h-6 bg-botex-800 rounded w-20"></div>
            </div>
          </div>

          <!-- Models List -->
          <div
            v-else
            ref="listContainerRef"
            class="flex-1 overflow-y-auto overscroll-contain divide-y divide-botex-800/70 bg-botex-950/60"
            role="listbox"
          >
            <div
              v-if="filteredModels.length === 0"
              class="p-10 text-center space-y-3"
            >
              <p class="text-sm font-medium text-slate-200">{{ t('models.empty') }}</p>
              <button
                type="button"
                class="min-h-[44px] lg:min-h-[36px] px-3.5 py-1.5 rounded-lg bg-botex-800 hover:bg-botex-700 text-xs text-slate-200 border border-botex-700 transition"
                @click="resetFilters"
              >
                Reset filters
              </button>
            </div>

            <button
              v-for="(item, idx) in visibleModels"
              :key="`${item.provider}:${item.id}`"
              :ref="(el) => setItemRef(el, idx)"
              type="button"
              role="option"
              :aria-selected="isSelected(item)"
              :class="[
                'w-full text-left px-4 py-3 transition flex flex-col sm:flex-row sm:items-center justify-between gap-2.5 focus:outline-none',
                idx === highlightedIndex
                  ? 'bg-botex-800/90 border-l-2 border-l-sky-400'
                  : isSelected(item)
                  ? 'bg-sky-950/25 border-l-2 border-l-emerald-400'
                  : 'hover:bg-botex-900/80'
              ]"
              @mouseenter="highlightedIndex = idx"
              @click="chooseModel(item)"
            >
              <!-- Left Column: Readable Name + Mono Slug -->
              <div class="min-w-0 flex-1">
                <div class="flex items-center space-x-2">
                  <span class="text-sm font-semibold text-slate-100 truncate">
                    {{ item.name }}
                  </span>
                  <CheckIcon
                    v-if="isSelected(item)"
                    class="w-4 h-4 text-emerald-400 shrink-0"
                  />
                </div>
                <div class="font-mono text-xs text-slate-400 truncate mt-0.5">
                  {{ item.id }}
                </div>
              </div>

              <!-- Right Column: Badges (Provider, Cost, Context Window) -->
              <div class="flex flex-wrap items-center gap-1.5 shrink-0">
                <!-- Provider Badge -->
                <span
                  :class="[
                    'px-2 py-0.5 rounded text-xs font-mono font-medium border',
                    providerBadgeClass(item.provider)
                  ]"
                >
                  {{ formatProviderLabel(item.provider) }}
                </span>

                <!-- Cost Badge -->
                <span
                  v-if="item.is_free"
                  class="px-2 py-0.5 rounded text-xs font-mono font-semibold bg-emerald-950/80 text-emerald-300 border border-emerald-500/40"
                >
                  FREE (0.00$)
                </span>
                <span
                  v-else
                  class="px-2 py-0.5 rounded text-xs font-mono font-medium bg-botex-900 text-slate-200 border border-botex-700"
                >
                  {{ formatPriceLabel(item) }}
                </span>

                <!-- Context Window Pill -->
                <span
                  class="px-2 py-0.5 rounded text-xs font-mono text-slate-300 bg-botex-900 border border-botex-800"
                  title="Maximum context window"
                >
                  {{ formatContextWindow(item.context_length) }}
                </span>
              </div>
            </button>
          </div>

          <!-- Footer Keyboard Hint -->
          <div class="px-4 py-2.5 bg-botex-900 border-t border-botex-800 flex items-center justify-between text-xs text-slate-300 font-mono">
            <span class="hidden sm:inline">{{ t('models.select_hint') }}</span>
            <span class="sm:hidden">Tap a model to select</span>
            <span>{{ availableModels.length }} catalog models</span>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import type { CatalogModel } from '../../api/types'
import { useI18n } from '../../i18n'
import {
  Search as SearchIcon,
  X as XIcon,
  RefreshCw as RefreshCwIcon,
  Check as CheckIcon
} from 'lucide-vue-next'

export type ModelCategory = 'all' | 'free' | 'cheap' | 'reasoning' | 'coding'

const props = withDefaults(
  defineProps<{
    isOpen: boolean
    models: CatalogModel[]
    selectedModel: string
    selectedProvider?: string
    isLoading?: boolean
    allowedProviders?: string[]
    excludeProviders?: string[]
  }>(),
  {
    selectedProvider: '',
    isLoading: false,
    allowedProviders: () => [],
    excludeProviders: () => []
  }
)

const availableModels = computed<CatalogModel[]>(() => {
  return props.models.filter((m) => {
    if (props.excludeProviders && props.excludeProviders.length > 0 && props.excludeProviders.includes(m.provider)) {
      return false
    }
    if (props.allowedProviders && props.allowedProviders.length > 0 && !props.allowedProviders.includes(m.provider)) {
      return false
    }
    return true
  })
})

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'select', model: CatalogModel): void
  (e: 'refresh'): void
}>()

const { t } = useI18n()

const searchQuery = ref('')
const activeCategory = ref<ModelCategory>('all')
const activeProvider = ref<string>('all')
const highlightedIndex = ref(0)
const searchInputRef = ref<HTMLInputElement | null>(null)
const listContainerRef = ref<HTMLElement | null>(null)
const itemRefs = ref<Record<number, HTMLElement | null>>({})

function onWheelScrollX(e: WheelEvent) {
  const el = e.currentTarget as HTMLElement | null
  if (el) el.scrollLeft += e.deltaY || e.deltaX
}

function setItemRef(el: any, idx: number) {
  if (el) {
    itemRefs.value[idx] = el as HTMLElement
  }
}

function isReasoningModel(m: CatalogModel): boolean {
  const s = `${m.id} ${m.name}`.toLowerCase()
  return (
    s.includes('r1') ||
    s.includes('thinking') ||
    s.includes('reason') ||
    s.includes('/o1') ||
    s.includes('/o3') ||
    s.includes('/o4') ||
    s.includes('qwq') ||
    s.includes('opus') ||
    s.includes('ultra')
  )
}

function isCodingModel(m: CatalogModel): boolean {
  const s = `${m.id} ${m.name}`.toLowerCase()
  return (
    s.includes('coder') ||
    s.includes('code') ||
    s.includes('codestral') ||
    s.includes('devstral') ||
    s.includes('starcoder') ||
    s.includes('laguna') ||
    s.includes('sonnet') ||
    s.includes('deepseek-v3') ||
    s.includes('deepseek-v4') ||
    s.includes('gpt-4.1') ||
    s.includes('gpt-4o')
  )
}

function isUltraCheapModel(m: CatalogModel): boolean {
  if (m.is_free) return true
  const promptRate = m.pricing?.prompt_per_m ?? 0
  return promptRate > 0 && promptRate <= 0.1
}

function matchesCategory(m: CatalogModel, cat: ModelCategory): boolean {
  switch (cat) {
    case 'free':
      return m.is_free || m.provider === 'openrouter-free' || m.provider === 'ollama'
    case 'cheap':
      return isUltraCheapModel(m)
    case 'reasoning':
      return isReasoningModel(m)
    case 'coding':
      return isCodingModel(m)
    default:
      return true
  }
}

function normalizeSearchText(str: string): string {
  return str.toLowerCase().replace(/[-_/:()]/g, ' ').replace(/\s+/g, ' ').trim()
}

const providerTabs = computed(() => {
  const all = availableModels.value
  const providers = Array.from(new Set(all.map((m) => m.provider))).filter(Boolean)
  const tabs = [
    { id: 'all', label: 'Wszyscy Dostawcy', count: all.length }
  ]
  for (const p of providers) {
    tabs.push({
      id: p,
      label: formatProviderLabel(p),
      count: all.filter((m) => m.provider === p).length
    })
  }
  return tabs
})

const categoryTabs = computed(() => {
  const all = availableModels.value
  return [
    { id: 'all' as ModelCategory, label: 'Wszystkie (All)', count: all.length },
    {
      id: 'free' as ModelCategory,
      label: '100% Free Tier',
      count: all.filter((m) => matchesCategory(m, 'free')).length
    },
    {
      id: 'cheap' as ModelCategory,
      label: 'Ultra-Cheap / Fast',
      count: all.filter((m) => matchesCategory(m, 'cheap')).length
    },
    {
      id: 'reasoning' as ModelCategory,
      label: 'Reasoning / Planners',
      count: all.filter((m) => matchesCategory(m, 'reasoning')).length
    },
    {
      id: 'coding' as ModelCategory,
      label: 'Coding / Agents',
      count: all.filter((m) => matchesCategory(m, 'coding')).length
    }
  ]
})

const filteredModels = computed<CatalogModel[]>(() => {
  const qRaw = searchQuery.value.trim().toLowerCase()
  const qNorm = normalizeSearchText(qRaw)
  const qTokens = qNorm ? qNorm.split(' ') : []

  return availableModels.value.filter((m) => {
    if (activeProvider.value !== 'all' && m.provider !== activeProvider.value) return false
    if (!matchesCategory(m, activeCategory.value)) return false
    if (!qRaw) return true

    const haystackRaw = `${m.name} ${m.id} ${m.provider} ${formatProviderLabel(m.provider)}`.toLowerCase()
    if (haystackRaw.includes(qRaw)) return true

    const haystackNorm = normalizeSearchText(haystackRaw)
    return qTokens.every((tok) => haystackNorm.includes(tok))
  })
})

// Render up to 150 items at once for instant DOM responsiveness across 500+ models
const visibleModels = computed<CatalogModel[]>(() => {
  return filteredModels.value.slice(0, 150)
})

watch(
  () => props.isOpen,
  async (open) => {
    if (open) {
      highlightedIndex.value = 0
      await nextTick()
      searchInputRef.value?.focus()
    }
  }
)

watch([searchQuery, activeCategory, activeProvider], () => {
  highlightedIndex.value = 0
})

function setCategory(cat: ModelCategory) {
  activeCategory.value = cat
  searchInputRef.value?.focus()
}

function resetFilters() {
  searchQuery.value = ''
  activeCategory.value = 'all'
  activeProvider.value = 'all'
  searchInputRef.value?.focus()
}

function isSelected(item: CatalogModel): boolean {
  if (props.selectedProvider) {
    return item.id === props.selectedModel && item.provider === props.selectedProvider
  }
  return item.id === props.selectedModel
}

function chooseModel(item: CatalogModel) {
  emit('select', item)
  emit('close')
}

function close() {
  emit('close')
}

function onRefresh() {
  emit('refresh')
}

function scrollHighlightedIntoView() {
  const el = itemRefs.value[highlightedIndex.value]
  if (el && typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ block: 'nearest' })
  }
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    close()
    return
  }
  const len = visibleModels.value.length
  if (len === 0) return

  if (e.key === 'ArrowDown') {
    e.preventDefault()
    highlightedIndex.value = (highlightedIndex.value + 1) % len
    nextTick(scrollHighlightedIntoView)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    highlightedIndex.value = (highlightedIndex.value - 1 + len) % len
    nextTick(scrollHighlightedIntoView)
  } else if (e.key === 'Enter') {
    e.preventDefault()
    const picked = visibleModels.value[highlightedIndex.value]
    if (picked) {
      chooseModel(picked)
    }
  }
}

function formatProviderLabel(provider: string): string {
  switch (provider) {
    case 'openrouter-free':
      return 'OpenRouter Free'
    case 'openrouter':
      return 'OpenRouter'
    case 'nvidia':
      return 'NVIDIA NIM'
    case 'ollama':
      return 'Ollama (Lokalny Bielik)'
    default:
      return provider
  }
}

function providerBadgeClass(provider: string): string {
  switch (provider) {
    case 'openrouter-free':
      return 'bg-emerald-950/60 text-emerald-300 border-emerald-600/40'
    case 'nvidia':
      return 'bg-lime-950/60 text-lime-300 border-lime-600/40'
    case 'ollama':
      return 'bg-purple-950/60 text-purple-300 border-purple-600/40'
    default:
      return 'bg-sky-950/60 text-sky-300 border-sky-600/40'
  }
}

function formatPriceLabel(item: CatalogModel): string {
  if (item.provider === 'nvidia' && item.pricing.prompt_per_m === 0) {
    return 'NIM Quota'
  }
  const p = item.pricing?.prompt_per_m ?? 0
  if (p === 0) return '$0.00 / 1M'
  if (p < 0.01) return `$${p.toFixed(4)} / 1M`
  return `$${p.toFixed(2)} / 1M`
}

function formatContextWindow(ctx: number): string {
  if (!ctx || ctx <= 0) return '32k'
  if (ctx >= 1000000) {
    const m = ctx / 1000000
    return Number.isInteger(m) ? `${m}M` : `${m.toFixed(1)}M`
  }
  return `${Math.round(ctx / 1024)}k`
}
</script>
