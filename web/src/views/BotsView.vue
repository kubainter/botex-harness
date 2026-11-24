<template>
  <div class="h-full overflow-y-auto bg-botex-950 p-4 sm:p-6">
    <!-- View header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4 mb-6">
      <div>
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <BotIcon class="w-5 h-5 text-cyan-400" aria-hidden="true" />
          <span>{{ tr('Team', 'Drużyna') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">
          {{ tr('Teammate roster and their domain briefs.', 'Lista teammate\'ów i ich krótkich opisów roli.') }}
        </p>
      </div>
      <button
        type="button"
        class="min-h-[44px] lg:min-h-[32px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60"
        @click="swarm.fetchBots()"
      >
        <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': swarm.loadingBots }" aria-hidden="true" />
        <span>{{ tr('Refresh', 'Odśwież') }}</span>
      </button>
    </div>

    <!-- Global error -->
    <div
      v-if="swarm.error"
      role="alert"
      class="mb-4 p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ swarm.error }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="swarm.fetchBots()">
        {{ tr('Retry', 'Ponów') }}
      </button>
    </div>

    <div class="lg:grid lg:grid-cols-[minmax(300px,340px)_1fr] gap-6 items-start">
      <!-- Roster -->
      <aside class="rounded-xl border border-botex-800 bg-botex-900 shadow-sm overflow-hidden mb-6 lg:mb-0 lg:sticky lg:top-0">
        <div class="p-3 border-b border-botex-800 space-y-2.5">
          <div class="flex items-center justify-between gap-2">
            <span class="text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
              {{ tr('Roster', 'Skład') }}
            </span>
            <span class="text-[11px] text-slate-400 font-mono">{{ filteredBots.length }} / {{ swarm.bots.length }}</span>
          </div>
          <div class="flex items-center bg-botex-950 p-0.5 rounded-lg border border-botex-800 space-x-1" role="group" :aria-label="tr('Status filter', 'Filtr statusu')">
            <button
              v-for="opt in statusFilters"
              :key="opt.value"
              type="button"
              :aria-pressed="statusFilter === opt.value"
              class="flex-1 min-h-[44px] lg:min-h-[32px] px-2 py-1 rounded text-xs font-medium transition"
              :class="statusFilter === opt.value
                ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30'
                : 'text-slate-300 hover:text-white'"
              @click="statusFilter = opt.value"
            >
              {{ opt.label }}
            </button>
          </div>
          <div class="relative">
            <SearchIcon class="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none" aria-hidden="true" />
            <input
              v-model="search"
              type="search"
              :placeholder="tr('Search slug or role…', 'Szukaj slugu lub roli…')"
              :aria-label="tr('Search teammates', 'Szukaj teammate\'ów')"
              class="block w-full min-h-[44px] lg:min-h-[36px] rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 text-sm px-3 py-2 pl-8 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-blue focus:border-brand-blue"
            />
          </div>
        </div>

        <ul v-if="filteredBots.length" class="divide-y divide-botex-800 max-h-72 lg:max-h-[calc(100dvh-19rem)] overflow-y-auto">
          <li v-for="bot in filteredBots" :key="bot.slug">
            <button
              type="button"
              :aria-current="swarm.selectedBotSlug === bot.slug ? 'true' : undefined"
              class="w-full text-left px-3 py-2.5 min-h-[44px] flex items-start gap-2.5 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-500/60"
              :class="swarm.selectedBotSlug === bot.slug ? 'bg-cyan-950/30' : 'hover:bg-botex-800/40'"
              @click="swarm.selectBot(bot.slug)"
            >
              <span
                class="mt-1.5 h-2 w-2 shrink-0 rounded-full"
                :class="statusDotClass(bot.status)"
                :title="bot.status"
              />
              <span class="min-w-0 flex-1">
                <span class="flex items-center justify-between gap-2">
                  <span
                    class="font-mono text-xs font-semibold truncate"
                    :class="swarm.selectedBotSlug === bot.slug ? 'text-cyan-200' : 'text-slate-100'"
                  >{{ bot.slug }}</span>
                  <span class="text-[10px] font-mono text-slate-400 bg-botex-950 border border-botex-800 rounded px-1 py-0.5 shrink-0 max-w-[96px] truncate">
                    {{ bot.namespace }}
                  </span>
                </span>
                <span v-if="bot.role_desc" class="block text-[11px] text-slate-400 truncate mt-0.5">{{ bot.role_desc }}</span>
              </span>
            </button>
          </li>
        </ul>
        <div v-else-if="swarm.loadingBots" class="p-4 space-y-2" aria-busy="true">
          <div v-for="i in 3" :key="i" class="h-9 rounded-lg bg-botex-950 border border-botex-800 animate-pulse" />
        </div>
        <div v-else class="p-6 text-center text-xs text-slate-400">
          {{ search || statusFilter !== 'all'
            ? tr('No teammates match this filter.', 'Brak teammate\'ów dla tych filtrów.')
            : tr('No teammates registered yet.', 'Nie zarejestrowano jeszcze żadnych teammate\'ów.') }}
        </div>
      </aside>

      <!-- Detail -->
      <section class="min-w-0 space-y-4">
        <div v-if="!swarm.selectedBot" class="rounded-xl border border-botex-800 bg-botex-900/40 p-10 text-center">
          <div class="mx-auto w-11 h-11 rounded-full bg-botex-950 border border-botex-800 flex items-center justify-center">
            <UsersIcon class="w-5 h-5 text-slate-400" aria-hidden="true" />
          </div>
          <h3 class="mt-4 text-sm font-semibold text-slate-200">
            {{ tr('Select a teammate', 'Wybierz teammate\'a') }}
          </h3>
          <p class="mt-1.5 text-xs text-slate-400 max-w-sm mx-auto">
            {{ tr('Pick someone from the roster to read their brief, pause them, or edit what they know.', 'Wybierz kogoś z listy, aby zobaczyć jego brief, wstrzymać go lub edytować jego wiedzę.') }}
          </p>
        </div>

        <template v-else>
          <!-- Bot header -->
          <div class="rounded-xl border border-botex-800 bg-botex-900 p-4 shadow-sm">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <h3 class="text-base font-bold font-mono text-slate-100 break-all">{{ swarm.selectedBot.slug }}</h3>
                  <Badge :variant="botStatusVariant(swarm.selectedBot.status)">{{ swarm.selectedBot.status }}</Badge>
                  <Badge v-if="swarm.selectedBrief?.pinned" variant="purple">
                    <PinIcon class="w-3 h-3 mr-1 -mt-0.5 inline" aria-hidden="true" />
                    {{ tr('pinned', 'przypięty') }}
                  </Badge>
                </div>
                <p v-if="swarm.selectedBot.role_desc" class="text-xs text-slate-300 mt-1.5">
                  {{ swarm.selectedBot.role_desc }}
                </p>
              </div>
              <Button
                :variant="swarm.selectedBot.status === 'active' ? 'secondary' : 'success'"
                size="sm"
                :loading="swarm.togglingBotSlug === swarm.selectedBot.slug"
                :disabled="swarm.togglingBotSlug !== null && swarm.togglingBotSlug !== swarm.selectedBot.slug"
                @click="togglePause()"
              >
                <PauseIcon v-if="swarm.selectedBot.status === 'active'" class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                <PlayIcon v-else class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                {{ swarm.selectedBot.status === 'active' ? tr('Pause', 'Wstrzymaj') : tr('Resume', 'Wznów') }}
              </Button>
            </div>

            <dl class="mt-3 pt-3 border-t border-botex-800 flex flex-wrap gap-x-6 gap-y-1.5 text-[11px] font-mono text-slate-400">
              <div class="flex items-center gap-1.5">
                <dt class="uppercase tracking-wide">ns</dt>
                <dd class="text-slate-200">{{ swarm.selectedBot.namespace }}</dd>
              </div>
              <div v-if="swarm.selectedBrief" class="flex items-center gap-1.5">
                <dt class="uppercase tracking-wide">epoch</dt>
                <dd class="text-slate-200">{{ swarm.selectedBrief.fencing_epoch }}</dd>
              </div>
              <div v-if="swarm.selectedBrief" class="flex items-center gap-1.5">
                <dt class="uppercase tracking-wide">{{ tr('author', 'autor') }}</dt>
                <dd class="text-slate-200">{{ swarm.selectedBrief.author }}</dd>
              </div>
              <div v-if="swarm.selectedBrief" class="flex items-center gap-1.5">
                <dt class="uppercase tracking-wide">{{ tr('updated', 'zmieniono') }}</dt>
                <dd class="text-slate-200">{{ formatRelative(swarm.selectedBrief.updated_at) }}</dd>
              </div>
            </dl>
          </div>

          <!-- Pause / resume failure -->
          <div
            v-if="toggleError"
            role="alert"
            class="p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
          >
            <span class="font-mono truncate">{{ toggleError }}</span>
            <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="toggleError = null">
              {{ tr('Dismiss', 'Zamknij') }}
            </button>
          </div>

          <!-- Active leases -->
          <div v-if="swarm.selectedLeases.length" class="rounded-xl border border-botex-800 bg-botex-900 shadow-sm overflow-hidden">
            <div class="px-4 py-2.5 border-b border-botex-800 text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
              {{ tr('Active leases', 'Aktywne dzierżawy') }}
            </div>
            <ul class="divide-y divide-botex-800">
              <li v-for="lease in swarm.selectedLeases" :key="lease.lease_key" class="px-4 py-2.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs font-mono">
                <span class="text-slate-100">{{ lease.host_id }}</span>
                <span class="text-slate-400 truncate min-w-0 max-w-[280px]" :title="lease.scope_path">{{ lease.scope_path }}</span>
                <span class="ml-auto flex items-center gap-3">
                  <span class="text-slate-400">#{{ lease.fencing_token }}</span>
                  <span :class="leaseCountdownClass(lease.expires_at)">{{ formatCountdown(lease.expires_at) }}</span>
                </span>
              </li>
            </ul>
          </div>

          <!-- Brief -->
          <div class="rounded-xl border border-botex-800 bg-botex-900 shadow-sm overflow-hidden">
            <div class="px-4 py-2.5 border-b border-botex-800 flex flex-wrap items-center justify-between gap-2">
              <span class="text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
                {{ tr('Domain brief', 'Brief roli') }}
              </span>
              <div class="flex items-center gap-2">
                <span
                  v-if="savedFlash"
                  class="flex items-center gap-1 text-[11px] text-emerald-300 font-mono"
                  aria-live="polite"
                >
                  <CheckIcon class="w-3.5 h-3.5" aria-hidden="true" />
                  {{ tr('saved', 'zapisano') }}
                </span>
                <div
                  v-if="swarm.selectedBrief || swarm.briefMissing"
                  class="flex items-center bg-botex-950 p-0.5 rounded-lg border border-botex-800 space-x-1"
                  role="group"
                  :aria-label="tr('Brief mode', 'Tryb briefu')"
                >
                  <button
                    type="button"
                    :aria-pressed="!editing"
                    class="min-h-[44px] lg:min-h-[32px] min-w-[44px] px-2.5 py-1 rounded text-xs font-medium transition flex items-center gap-1.5"
                    :class="!editing ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'"
                    @click="cancelEdit()"
                  >
                    <EyeIcon class="w-3.5 h-3.5" aria-hidden="true" />
                    {{ tr('Preview', 'Podgląd') }}
                  </button>
                  <button
                    type="button"
                    :aria-pressed="editing"
                    class="min-h-[44px] lg:min-h-[32px] min-w-[44px] px-2.5 py-1 rounded text-xs font-medium transition flex items-center gap-1.5"
                    :class="editing ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'"
                    @click="startEdit()"
                  >
                    <PencilIcon class="w-3.5 h-3.5" aria-hidden="true" />
                    {{ tr('Edit', 'Edytuj') }}
                  </button>
                </div>
              </div>
            </div>

            <!-- Brief loading -->
            <div v-if="swarm.loadingBrief && !swarm.selectedBrief" class="p-4 space-y-2" aria-busy="true">
              <div class="h-3 w-1/3 rounded bg-botex-950 animate-pulse" />
              <div class="h-3 w-2/3 rounded bg-botex-950 animate-pulse" />
              <div class="h-3 w-1/2 rounded bg-botex-950 animate-pulse" />
            </div>

            <!-- Conflict banner (kept above the editor so both versions stay visible) -->
            <div
              v-if="conflict"
              role="alert"
              class="m-3 p-3 rounded-lg bg-amber-950/50 border border-amber-700/60 text-xs text-amber-200 space-y-2"
            >
              <div class="flex items-start gap-2">
                <AlertTriangleIcon class="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
                <div class="min-w-0">
                  <p class="font-semibold">{{ tr('Brief changed on the server', 'Brief zmienił się na serwerze') }}</p>
                  <p class="mt-0.5 text-amber-200/80">
                    {{ conflict.message }}
                    {{ tr('Your version is still in the editor below.', 'Twoja wersja wciąż jest w edytorze poniżej.') }}
                  </p>
                </div>
              </div>
              <div class="flex flex-wrap gap-2 pl-6">
                <button
                  type="button"
                  class="min-h-[44px] lg:min-h-[32px] px-3 py-1 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition"
                  @click="loadServerVersion()"
                >
                  {{ tr('Load server version', 'Wczytaj wersję serwera') }}
                </button>
                <button
                  type="button"
                  class="min-h-[44px] lg:min-h-[32px] px-3 py-1 rounded-lg bg-amber-600 hover:bg-amber-500 text-white text-xs font-medium transition"
                  @click="save(true)"
                >
                  {{ tr('Keep my version', 'Zachowaj moją wersję') }}
                </button>
              </div>
            </div>

            <!-- Brief errors -->
            <div
              v-if="saveError || swarm.briefError"
              role="alert"
              class="m-3 p-3 rounded-lg bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
            >
              <span class="font-mono truncate">{{ saveError || swarm.briefError }}</span>
              <button
                v-if="saveError"
                type="button"
                class="shrink-0 underline hover:text-white min-h-[44px] px-2"
                @click="saveError = null"
              >{{ tr('Dismiss', 'Zamknij') }}</button>
            </div>

            <!-- Refresh notice (e.g. synthesis already running) -->
            <div
              v-else-if="refreshNotice"
              class="m-3 p-3 rounded-lg bg-amber-950/50 border border-amber-700/60 text-xs text-amber-200 flex items-center justify-between gap-3"
            >
              <span class="truncate">{{ refreshNotice }}</span>
              <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="refreshNotice = null">
                {{ tr('Dismiss', 'Zamknij') }}
              </button>
            </div>

            <!-- No brief yet -->
            <div v-else-if="swarm.briefMissing" class="p-8 text-center">
              <FileTextIcon class="w-6 h-6 text-slate-400 mx-auto" aria-hidden="true" />
              <h4 class="mt-3 text-sm font-semibold text-slate-200">{{ tr('No brief yet', 'Jeszcze nie ma briefu') }}</h4>
              <p class="mt-1 text-xs text-slate-400 max-w-sm mx-auto">
                {{ tr('Write the first version by hand, or ask for an automatic synthesis.', 'Napisz pierwszą wersję ręcznie lub poproś o automatyczną syntezę.') }}
              </p>
              <div class="mt-4 flex flex-wrap items-center justify-center gap-2">
                <Button variant="secondary" size="sm" @click="startEdit()">
                  <PencilIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                  {{ tr('Write brief', 'Napisz brief') }}
                </Button>
                <Button variant="secondary" size="sm" :loading="swarm.refreshingBrief" @click="refresh()">
                  <RefreshCwIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                  {{ tr('Synthesize', 'Zsyntetyzuj') }}
                </Button>
              </div>
            </div>

            <!-- Editor -->
            <div v-else-if="editing" class="p-4 space-y-3">
              <label class="sr-only" for="brief-editor">{{ tr('Brief content', 'Treść briefu') }}</label>
              <textarea
                id="brief-editor"
                v-model="draft"
                rows="14"
                spellcheck="false"
                :placeholder="tr('Describe what this teammate should know and how it should behave…', 'Opisz, co ten teammate powinien wiedzieć i jak powinien działać…')"
                class="block w-full min-h-[280px] rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 text-xs font-mono leading-5 px-3 py-2.5 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-blue focus:border-brand-blue resize-y"
              />
              <div class="flex flex-wrap items-center justify-between gap-3">
                <label class="flex items-center gap-2 min-h-[44px] lg:min-h-[32px] cursor-pointer text-xs text-slate-300 select-none">
                  <input
                    v-model="draftPinned"
                    type="checkbox"
                    class="h-4 w-4 rounded border-botex-600 bg-botex-950 accent-cyan-500"
                  />
                  {{ tr('Pinned (blocks automatic synthesis)', 'Przypięty (blokuje automatyczną syntezę)') }}
                </label>
                <div
                  class="text-[11px] font-mono"
                  :class="overBudget ? 'text-amber-300' : 'text-slate-400'"
                >
                  ≈ {{ draftTokens }} / {{ swarm.selectedBot?.brief_budget_tokens ?? '?' }}
                  {{ tr('tokens', 'tokenów') }}
                </div>
              </div>
              <div class="flex flex-wrap items-center justify-end gap-2 pt-1 border-t border-botex-800">
                <Button variant="ghost" size="sm" @click="cancelEdit()">{{ tr('Cancel', 'Anuluj') }}</Button>
                <Button variant="secondary" size="sm" :loading="swarm.refreshingBrief" @click="refresh()">
                  <RefreshCwIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                  {{ tr('Synthesize', 'Zsyntetyzuj') }}
                </Button>
                <Button variant="primary" size="sm" :disabled="!dirty || conflict !== null" :loading="swarm.savingBrief" @click="save(false)">
                  <SaveIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                  {{ tr('Save brief', 'Zapisz brief') }}
                </Button>
              </div>
            </div>

            <!-- Preview -->
            <div v-else-if="swarm.selectedBrief" class="p-4 relative">
              <div v-if="!swarm.selectedBrief.content" class="py-6 text-center text-xs text-slate-400">
                {{ tr('This brief is empty — switch to Edit to write it.', 'Ten brief jest pusty — przejdź do edycji, aby go napisać.') }}
              </div>
              <MarkdownViewer v-else :content="swarm.selectedBrief.content" />
              <div class="mt-4 pt-3 border-t border-botex-800 flex flex-wrap items-center justify-end gap-2">
                <Button variant="secondary" size="sm" :loading="swarm.refreshingBrief" @click="refresh()">
                  <RefreshCwIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                  {{ tr('Synthesize', 'Zsyntetyzuj') }}
                </Button>
              </div>
            </div>
          </div>
        </template>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useSwarmStore } from '../stores/swarm'
import { useI18n } from '../i18n'
import Badge from '../components/common/Badge.vue'
import Button from '../components/common/Button.vue'
import MarkdownViewer from '../components/common/MarkdownViewer.vue'
import {
  AlertTriangle as AlertTriangleIcon,
  Bot as BotIcon,
  Check as CheckIcon,
  Eye as EyeIcon,
  FileText as FileTextIcon,
  Pause as PauseIcon,
  Pencil as PencilIcon,
  Pin as PinIcon,
  Play as PlayIcon,
  RefreshCw as RefreshCwIcon,
  Save as SaveIcon,
  Search as SearchIcon,
  Users as UsersIcon
} from 'lucide-vue-next'

const swarm = useSwarmStore()
const { selectedBotSlug, selectedBrief, briefMissing } = storeToRefs(swarm)
const { locale } = useI18n()

function tr(en: string, pl: string): string {
  return locale.value === 'pl' ? pl : en
}

// --- Roster filtering -------------------------------------------------------
const statusFilter = ref<'all' | 'active' | 'paused'>('all')
const search = ref('')

const statusFilters = computed(() => [
  { value: 'all' as const, label: tr('All', 'Wszyscy') },
  { value: 'active' as const, label: tr('Active', 'Aktywni') },
  { value: 'paused' as const, label: tr('Paused', 'Wstrzymani') }
])

const filteredBots = computed(() => {
  const q = search.value.trim().toLowerCase()
  return swarm.bots.filter((b) => {
    if (statusFilter.value !== 'all' && b.status !== statusFilter.value) return false
    if (q && !`${b.slug} ${b.role_desc} ${b.namespace}`.toLowerCase().includes(q)) return false
    return true
  })
})

function statusDotClass(status: string): string {
  if (status === 'active') return 'bg-emerald-400 animate-pulse'
  if (status === 'paused') return 'bg-amber-400'
  return 'bg-slate-500'
}

function botStatusVariant(status: string): 'success' | 'warning' | 'default' {
  if (status === 'active') return 'success'
  if (status === 'paused') return 'warning'
  return 'default'
}

// --- Brief editor state -----------------------------------------------------
const editing = ref(false)
const draft = ref('')
const draftPinned = ref(false)
const conflict = ref<{ message: string; currentEtag?: string } | null>(null)
const saveError = ref<string | null>(null)
const refreshNotice = ref<string | null>(null)
const toggleError = ref<string | null>(null)
const savedFlash = ref(false)
let savedFlashTimer: ReturnType<typeof setTimeout> | null = null

watch(selectedBotSlug, () => {
  editing.value = false
  conflict.value = null
  saveError.value = null
  refreshNotice.value = null
  toggleError.value = null
})

watch([selectedBrief, briefMissing], () => {
  if (!editing.value) resetDraft()
})

function resetDraft(): void {
  draft.value = selectedBrief.value?.content ?? ''
  draftPinned.value = selectedBrief.value?.pinned ?? false
  conflict.value = null
  saveError.value = null
}

const dirty = computed(
  () =>
    draft.value !== (selectedBrief.value?.content ?? '') ||
    draftPinned.value !== (selectedBrief.value?.pinned ?? false)
)

const draftTokens = computed(() => Math.ceil(draft.value.length / 4))

const overBudget = computed(() => {
  const budget = swarm.selectedBot?.brief_budget_tokens
  return typeof budget === 'number' && budget > 0 && draftTokens.value > budget
})

function startEdit(): void {
  if (!editing.value) resetDraft()
  editing.value = true
}

function cancelEdit(): void {
  editing.value = false
  resetDraft()
}

function flashSaved(): void {
  savedFlash.value = true
  if (savedFlashTimer) clearTimeout(savedFlashTimer)
  savedFlashTimer = setTimeout(() => {
    savedFlash.value = false
  }, 1800)
}

async function save(force: boolean): Promise<void> {
  const res = await swarm.saveBrief(draft.value, draftPinned.value, { force })
  if (res.ok) {
    conflict.value = null
    saveError.value = null
    flashSaved()
    return
  }
  if (res.conflict) {
    conflict.value = { message: res.message, currentEtag: res.currentEtag }
  } else {
    conflict.value = null
    saveError.value = res.message
  }
}

async function loadServerVersion(): Promise<void> {
  editing.value = false
  conflict.value = null
  await swarm.selectBot(selectedBotSlug.value, { force: true })
}

async function refresh(): Promise<void> {
  const res = await swarm.refreshBrief()
  if (res.ok) {
    refreshNotice.value = null
    flashSaved()
  } else if (res.message) {
    refreshNotice.value = res.message
  }
}

async function togglePause(): Promise<void> {
  const bot = swarm.selectedBot
  if (!bot) return
  toggleError.value = null
  const res = await swarm.toggleBotPause(bot)
  if (!res.ok && res.message) toggleError.value = res.message
}

// --- Time formatting --------------------------------------------------------
const now = ref(Date.now())
const tickTimer = setInterval(() => {
  now.value = Date.now()
}, 30000)
onUnmounted(() => clearInterval(tickTimer))

function formatRelative(iso: string): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return iso
  const diffS = Math.max(0, (now.value - ts) / 1000)
  if (diffS < 60) return tr('just now', 'właśnie teraz')
  if (diffS < 3600) return tr(`${Math.floor(diffS / 60)}m ago`, `${Math.floor(diffS / 60)} min temu`)
  if (diffS < 86400) return tr(`${Math.floor(diffS / 3600)}h ago`, `${Math.floor(diffS / 3600)} godz. temu`)
  return tr(`${Math.floor(diffS / 86400)}d ago`, `${Math.floor(diffS / 86400)} dni temu`)
}

function formatCountdown(expiresAt: number): string {
  const diffS = expiresAt - now.value / 1000
  if (diffS <= 0) return tr('expired', 'wygasła')
  if (diffS < 60) return `${Math.ceil(diffS)}s`
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m`
  if (diffS < 86400) return `${Math.floor(diffS / 3600)}h`
  return `${Math.floor(diffS / 86400)}d`
}

function leaseCountdownClass(expiresAt: number): string {
  const diffS = expiresAt - now.value / 1000
  if (diffS <= 0) return 'text-rose-300'
  if (diffS < 300) return 'text-amber-300'
  return 'text-emerald-300'
}

// Initial load
void swarm.fetchBots()
</script>
