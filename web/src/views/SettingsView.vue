<template>
  <div class="h-full overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6">
    <!-- View Header -->
    <div class="border-b border-botex-800 pb-4">
      <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
        <SettingsIcon class="w-5 h-5 text-cyan-400" />
        <span>{{ t('settingsTitle') }}</span>
      </h2>
      <p class="text-xs text-slate-300 mt-1">
        {{ t('settingsSubtitle') }}
      </p>
    </div>

    <!-- Connection & Auth Card -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-5 shadow-sm">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-botex-800 pb-3">
        <div class="flex items-center space-x-2">
          <ServerIcon class="w-4 h-4 text-cyan-400" />
          <h3 class="text-sm font-semibold text-slate-100">{{ t('backendConnection') }}</h3>
        </div>
        <Badge :variant="authStore.isServerHealthy ? 'success' : 'danger'">
          {{ authStore.isServerHealthy ? (locale === 'pl' ? 'Połączono' : 'Connected') : (locale === 'pl' ? 'Brak połączenia' : 'Unreachable') }}
        </Badge>
      </div>

      <!-- Backend URL Input -->
      <div>
        <label class="block text-xs font-medium text-slate-200 mb-1 flex items-center justify-between">
          <span>Backend URL</span>
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] inline-flex items-center text-xs text-cyan-300 hover:text-cyan-200"
            @click="urlInput = 'http://127.0.0.1:8000'"
          >
            {{ locale === 'pl' ? 'Przywróć lokalny adres' : 'Reset to Local Default' }}
          </button>
        </label>
        <input
          v-model="urlInput"
          type="text"
          placeholder="http://127.0.0.1:8000"
          class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-base sm:text-xs font-mono text-slate-100 focus:outline-none focus:border-cyan-500"
        />
        <p class="text-xs text-slate-300 mt-1">
          {{ locale === 'pl' ? 'Adres bramki FastAPI obsługującej REST, Remote MCP i WebSockets.' : 'FastAPI gateway address serving REST endpoints, Remote MCP, and WebSockets.' }}
        </p>
      </div>

      <!-- Master Bearer Token Input -->
      <div>
        <label class="block text-xs font-medium text-slate-200 mb-1 flex items-center justify-between">
          <span>Bearer Token (BOTEX_ACCESS_TOKEN)</span>
          <span class="text-slate-300 font-normal">
            {{ locale === 'pl' ? 'Opcjonalny w trybie lokalnym' : 'Optional on localhost' }}
          </span>
        </label>
        <div class="relative">
          <input
            v-model="tokenInput"
            :type="showToken ? 'text' : 'password'"
            :placeholder="locale === 'pl' ? 'Zostaw puste lokalnie lub wpisz token dostępowy' : 'Leave blank on localhost or enter bearer token'"
            class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 pr-10 text-base sm:text-xs font-mono text-slate-100 placeholder-slate-400 focus:outline-none focus:border-cyan-500"
          />
          <button
            type="button"
            class="absolute inset-y-0 right-0 w-11 flex items-center justify-center text-slate-300 hover:text-white"
            @click="showToken = !showToken"
          >
            <EyeOffIcon v-if="showToken" class="w-4 h-4" />
            <EyeIcon v-else class="w-4 h-4" />
          </button>
        </div>
        <p class="text-xs text-slate-300 mt-1">
          {{ locale === 'pl' ? 'Zapisywany w localStorage przeglądarki i wysyłany w nagłówkach HTTP/WS.' : 'Saved in browser localStorage and sent with HTTP and WebSocket requests.' }}
        </p>
      </div>

      <!-- Action Buttons -->
      <div class="pt-2 flex flex-wrap items-center justify-between gap-3 border-t border-botex-800">
        <div class="flex items-center space-x-2">
          <Button
            variant="primary"
            size="sm"
            @click="saveSettings"
          >
            {{ t('saveConfig') }}
          </Button>
          <Button
            variant="secondary"
            size="sm"
            :loading="testingConnection"
            @click="testConnection"
          >
            {{ t('testConnection') }}
          </Button>
        </div>

        <span v-if="saveMessage" class="text-xs text-emerald-300 animate-fadeIn">
          {{ saveMessage }}
        </span>
      </div>

      <!-- Probe Result Banner -->
      <div v-if="testResult" :class="['p-3 rounded-lg border text-xs', testResult.ok ? 'bg-emerald-950/40 border-emerald-700 text-emerald-200' : 'bg-rose-950/40 border-rose-700 text-rose-200']">
        <div class="font-semibold">{{ testResult.title }}</div>
        <p class="mt-0.5 font-mono text-xs">{{ testResult.detail }}</p>
      </div>
    </div>

    <!-- Interface & Language -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-4 shadow-sm">
      <div class="flex items-center space-x-2 border-b border-botex-800 pb-3">
        <PaletteIcon class="w-4 h-4 text-cyan-400" />
        <h3 class="text-sm font-semibold text-slate-100">{{ t('interfaceLanguage') }}</h3>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <span class="text-xs font-medium text-slate-100">{{ locale === 'pl' ? 'Język interfejsu' : 'UI Language' }}</span>
          <p class="text-xs text-slate-300">
            {{ locale === 'pl' ? 'Polski (miodkuj) lub angielski (humanify).' : 'Switch between English (technical) and Polish.' }}
          </p>
        </div>
        <div class="flex items-center bg-botex-950 p-1 rounded-lg border border-botex-800 space-x-1">
          <button
            type="button"
            :class="[
              'min-h-[44px] lg:min-h-[36px] px-3 py-1 rounded text-xs font-mono font-semibold transition',
              locale === 'en' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="setLocale('en')"
          >
            English (EN)
          </button>
          <button
            type="button"
            :class="[
              'min-h-[44px] lg:min-h-[36px] px-3 py-1 rounded text-xs font-mono font-semibold transition',
              locale === 'pl' ? 'bg-botex-800 text-cyan-200 border border-cyan-500/30' : 'text-slate-300 hover:text-white'
            ]"
            @click="setLocale('pl')"
          >
            Polski (PL)
          </button>
        </div>
      </div>

      <!-- Skin Selector (Operate Modes) -->
      <div class="pt-3 border-t border-botex-800 space-y-3">
        <div>
          <span class="text-xs font-medium text-slate-100">{{ locale === 'pl' ? 'Motyw i Skin Operacyjny' : 'Operational Skin & Theme' }}</span>
          <p class="text-xs text-slate-300">
            {{ locale === 'pl' ? 'Wybierz jeden z 5 zoptymalizowanych skinów środowiska BoteX.' : 'Select one of 5 tailored operating skins for BoteX.' }}
          </p>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
          <button
            v-for="s in themeStore.skins"
            :key="s.id"
            type="button"
            :class="[
              'p-3 rounded-lg border text-left transition relative flex flex-col justify-between space-y-2',
              themeStore.currentSkinId === s.id
                ? 'bg-botex-800 border-cyan-400/80 shadow-sm ring-1 ring-cyan-500/20'
                : 'bg-botex-950 border-botex-800 hover:border-botex-700 hover:bg-botex-900/50'
            ]"
            @click="themeStore.setSkin(s.id)"
          >
            <div class="flex items-center justify-between">
              <div class="flex items-center space-x-2">
                <span
                  class="w-3.5 h-3.5 rounded-full border border-white/20 shrink-0"
                  :style="{ backgroundColor: s.accentColor }"
                ></span>
                <span class="text-xs font-semibold text-slate-100">
                  {{ locale === 'pl' ? s.namePl : s.name }}
                </span>
              </div>
              <span
                v-if="themeStore.currentSkinId === s.id"
                class="text-xs font-mono font-bold text-cyan-300"
              >
                ACTIVE
              </span>
            </div>
            <p class="text-xs text-slate-300 leading-relaxed">
              {{ locale === 'pl' ? s.descriptionPl : s.description }}
            </p>
          </button>
        </div>
      </div>
    </div>

    <!-- Progressive Web App Info -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-4 shadow-sm">
      <div class="flex items-center space-x-2 border-b border-botex-800 pb-3">
        <SmartphoneIcon class="w-4 h-4 text-cyan-400" />
        <h3 class="text-sm font-semibold text-slate-100">PWA Runtime</h3>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs font-mono">
        <div class="p-3 bg-botex-950 rounded-lg border border-botex-800">
          <span class="block text-xs text-slate-300 uppercase">Service Worker</span>
          <span class="text-emerald-300 font-semibold">Active & Precached</span>
        </div>
        <div class="p-3 bg-botex-950 rounded-lg border border-botex-800">
          <span class="block text-xs text-slate-300 uppercase">Display Mode</span>
          <span class="text-cyan-300 font-semibold">Standalone Shell</span>
        </div>
        <div class="p-3 bg-botex-950 rounded-lg border border-botex-800">
          <span class="block text-xs text-slate-300 uppercase">Network Mode</span>
          <span :class="authStore.isOnline ? 'text-emerald-300' : 'text-amber-300'" class="font-semibold">
            {{ authStore.isOnline ? 'Online' : 'Offline Shell' }}
          </span>
        </div>
      </div>
    </div>

    <!-- Server-Side Skills Catalog (Skill Vault) -->
    <div class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-4 shadow-sm">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-botex-800 pb-3">
        <div>
          <div class="flex items-center space-x-2">
            <BookOpenIcon class="w-4 h-4 text-cyan-400" />
            <h3 class="text-sm font-semibold text-slate-100">
              {{ locale === 'pl' ? 'Katalog Skilli BoteX (Skill Vault & L0 Triage)' : 'Server-Side Skills Catalog (Skill Vault)' }}
            </h3>
            <Badge variant="info">{{ skillsList.length }}</Badge>
          </div>
          <p class="text-xs text-slate-300 mt-0.5">
            {{ locale === 'pl'
              ? 'Skille zainstalowane po stronie serwera / VPS, automatycznie dobierane podczas L0 Triage i zarządzane przez Bielika.'
              : 'Server-side skills automatically injected during L0 Triage and administered via Chat or REST.' }}
          </p>
        </div>

        <div class="flex items-center space-x-2">
          <Button variant="secondary" size="sm" :loading="skillsLoading" @click="fetchSkills">
            <RefreshCwIcon class="w-3.5 h-3.5 mr-1" />
            <span>{{ locale === 'pl' ? 'Odśwież' : 'Refresh' }}</span>
          </Button>
          <Button variant="primary" size="sm" @click="showInstallSkillModal = true">
            <PlusIcon class="w-3.5 h-3.5 mr-1" />
            <span>{{ locale === 'pl' ? 'Zainstaluj Skill' : 'Install Skill' }}</span>
          </Button>
        </div>
      </div>

      <div v-if="skillStatusBanner" :class="[
        'p-3 rounded-lg border text-xs',
        skillStatusBanner.ok
          ? 'bg-emerald-950/40 border-emerald-700 text-emerald-200'
          : 'bg-rose-950/40 border-rose-700 text-rose-200'
      ]">
        {{ skillStatusBanner.message }}
      </div>

      <div v-if="skillsLoading && skillsList.length === 0" class="text-xs text-slate-400 py-4 text-center font-mono">
        {{ locale === 'pl' ? 'Ładowanie katalogu skilli...' : 'Loading installed skills...' }}
      </div>

      <div v-else-if="skillsList.length === 0" class="p-6 rounded-lg bg-botex-950 border border-botex-800 text-center text-xs text-slate-400">
        {{ locale === 'pl' ? 'Brak zainstalowanych skilli w katalogu serwera.' : 'No server-side skills installed yet.' }}
      </div>

      <div v-else class="grid grid-cols-1 gap-3">
        <div
          v-for="sk in skillsList"
          :key="sk.name"
          class="p-4 rounded-lg bg-botex-950 border border-botex-800 hover:border-botex-700 transition space-y-2.5"
        >
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div class="space-y-1">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-xs font-mono font-bold text-cyan-300">{{ sk.name }}</span>
                <span class="px-1.5 py-0.5 rounded text-[11px] font-mono bg-botex-800 text-slate-200 border border-botex-700">
                  v{{ sk.version || '1.0.0' }}
                </span>
                <span
                  v-for="tax in sk.taxonomy"
                  :key="tax"
                  class="px-1.5 py-0.5 rounded text-[10px] uppercase font-mono bg-cyan-950/60 text-cyan-300 border border-cyan-800/60"
                >
                  {{ tax }}
                </span>
              </div>
              <p class="text-xs text-slate-200">{{ sk.description }}</p>
            </div>

            <div class="flex items-center space-x-1.5 shrink-0">
              <button
                type="button"
                class="min-h-[44px] lg:min-h-[36px] px-2.5 py-1 rounded text-xs font-medium text-cyan-300 hover:text-cyan-200 bg-botex-900 hover:bg-botex-800 border border-botex-700 transition"
                @click="openSkillPreview(sk.name)"
              >
                {{ locale === 'pl' ? 'Podgląd SKILL.md' : 'Inspect' }}
              </button>
              <button
                v-if="confirmDeleteSkill !== sk.name"
                type="button"
                class="min-h-[44px] lg:min-h-[36px] min-w-[44px] lg:min-w-[36px] px-2 py-1 rounded text-xs font-medium text-rose-400 hover:text-rose-300 bg-botex-900 hover:bg-rose-950/50 border border-rose-900/50 transition flex items-center justify-center"
                @click="confirmDeleteSkill = sk.name"
                :title="locale === 'pl' ? 'Usuń skill' : 'Delete skill'"
              >
                <Trash2Icon class="w-3.5 h-3.5" />
              </button>
              <template v-else>
                <button
                  type="button"
                  class="min-h-[44px] lg:min-h-[36px] px-2.5 py-1 rounded text-xs font-semibold bg-rose-600 hover:bg-rose-500 text-white transition"
                  @click="handleDeleteSkill(sk.name)"
                >
                  {{ locale === 'pl' ? 'Usuń' : 'Confirm' }}
                </button>
                <button
                  type="button"
                  class="min-h-[44px] lg:min-h-[36px] px-2.5 py-1 rounded text-xs bg-botex-800 text-slate-300 hover:bg-botex-700 transition"
                  @click="confirmDeleteSkill = null"
                >
                  {{ locale === 'pl' ? 'Anuluj' : 'Cancel' }}
                </button>
              </template>
            </div>
          </div>

          <div class="flex flex-wrap items-center justify-between gap-2 pt-1 border-t border-botex-900 text-[11px] font-mono text-slate-400">
            <div class="flex items-center space-x-2 truncate">
              <span>Origin:</span>
              <a
                v-if="sk.origin_repo && sk.origin_repo.startsWith('http')"
                :href="sk.origin_repo"
                target="_blank"
                rel="noopener noreferrer"
                class="text-cyan-400 hover:underline truncate max-w-xs sm:max-w-md inline-block py-1.5 -my-1.5"
              >
                {{ sk.origin_repo }}
              </a>
              <span v-else class="text-slate-300">{{ sk.origin_repo || 'builtin' }}</span>
            </div>
            <div class="flex items-center space-x-3 shrink-0">
              <span v-if="sk.commit_sha">SHA: {{ sk.commit_sha.slice(0, 7) }}</span>
              <span>{{ (sk.size_bytes / 1024).toFixed(1) }} KB</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Skill Preview Modal -->
    <Modal
      :is-open="showSkillPreviewModal"
      :title="activeSkillDetail ? `SKILL: ${activeSkillDetail.name} (v${activeSkillDetail.version})` : 'Skill Detail'"
      :subtitle="activeSkillDetail?.origin_repo || ''"
      max-width="2xl"
      @close="showSkillPreviewModal = false"
    >
      <div v-if="activeSkillDetail" class="space-y-3 text-xs">
        <p class="text-slate-300">{{ activeSkillDetail.description }}</p>
        <div class="rounded-lg bg-botex-950 border border-botex-800 p-4 max-h-[55dvh] overflow-y-auto">
          <MarkdownViewer :content="activeSkillDetail.content" />
        </div>
      </div>
      <template #footer>
        <Button variant="secondary" size="sm" @click="showSkillPreviewModal = false">
          {{ locale === 'pl' ? 'Zamknij' : 'Close' }}
        </Button>
      </template>
    </Modal>

    <!-- Install / Update Skill Modal -->
    <Modal
      :is-open="showInstallSkillModal"
      :title="locale === 'pl' ? 'Zainstaluj lub zaktualizuj Skill (VPS / BoteX)' : 'Install or Update Server-Side Skill'"
      :subtitle="locale === 'pl' ? 'Podaj link do repozytorium GitHub / pliku SKILL.md albo wklej treść Markdown.' : 'Provide a public GitHub/raw URL or paste SKILL.md Markdown content.'"
      max-width="xl"
      @close="showInstallSkillModal = false"
    >
      <form @submit.prevent="handleInstallSkill" class="space-y-3.5 text-xs">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-slate-200 font-medium mb-1 font-mono uppercase">
              {{ locale === 'pl' ? 'Nazwa skilla (opcjonalnie z URL)' : 'Skill Name' }}
            </label>
            <input
              v-model="installForm.name"
              type="text"
              placeholder="e.g. release-readiness"
              class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-100"
            />
          </div>
          <div>
            <label class="block text-slate-200 font-medium mb-1 font-mono uppercase">
              {{ locale === 'pl' ? 'Wersja' : 'Version' }}
            </label>
            <input
              v-model="installForm.version"
              type="text"
              placeholder="1.0.0"
              class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-100"
            />
          </div>
        </div>

        <div>
          <label class="block text-slate-200 font-medium mb-1 font-mono uppercase">
            {{ locale === 'pl' ? 'Adres URL repozytorium lub pliku SKILL.md' : 'Source Repository or SKILL.md URL' }}
          </label>
          <input
            v-model="installForm.source_url"
            type="text"
            placeholder="https://github.com/owner/repo/blob/main/skills/my-skill/SKILL.md"
            class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-100"
          />
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-slate-200 font-medium mb-1 font-mono uppercase">
              {{ locale === 'pl' ? 'Pochodzenie (Origin Repo)' : 'Origin Repo Label/URL' }}
            </label>
            <input
              v-model="installForm.origin_repo"
              type="text"
              placeholder="https://github.com/owner/repo"
              class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-100"
            />
          </div>
          <div>
            <label class="block text-slate-200 font-medium mb-1 font-mono uppercase">
              {{ locale === 'pl' ? 'Taksonomia L0 (po przecinku)' : 'L0 Taxonomy Tags (comma-separated)' }}
            </label>
            <input
              v-model="installForm.taxonomyRaw"
              type="text"
              placeholder="analysis, bugfix, test, general"
              class="w-full min-h-[40px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs font-mono text-slate-100"
            />
          </div>
        </div>

        <div>
          <label class="block text-slate-200 font-medium mb-1 font-mono uppercase">
            {{ locale === 'pl' ? 'Treść SKILL.md (opcjonalnie, jeśli nie podano URL)' : 'SKILL.md Content (optional if URL provided)' }}
          </label>
          <textarea
            v-model="installForm.content"
            rows="6"
            placeholder="---\nname: my-skill\ndescription: ...\n---\n# Instructions..."
            class="w-full bg-botex-950 border border-botex-700 rounded-lg p-3 text-xs font-mono text-slate-100"
          ></textarea>
        </div>
      </form>
      <template #footer>
        <Button variant="secondary" size="sm" @click="showInstallSkillModal = false">
          {{ locale === 'pl' ? 'Anuluj' : 'Cancel' }}
        </Button>
        <Button
          variant="primary"
          size="sm"
          :loading="installingSkill"
          @click="handleInstallSkill"
        >
          {{ locale === 'pl' ? 'Zainstaluj / Zaktualizuj' : 'Install / Update' }}
        </Button>
      </template>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useThemeStore } from '../stores/theme'
import { useI18n } from '../i18n'
import { api } from '../api/client'
import type { SkillCatalogItem, SkillDetail } from '../api/types'
import Badge from '../components/common/Badge.vue'
import Button from '../components/common/Button.vue'
import Modal from '../components/common/Modal.vue'
import MarkdownViewer from '../components/common/MarkdownViewer.vue'
import {
  Settings as SettingsIcon,
  Server as ServerIcon,
  Eye as EyeIcon,
  EyeOff as EyeOffIcon,
  Palette as PaletteIcon,
  Smartphone as SmartphoneIcon,
  BookOpen as BookOpenIcon,
  RefreshCw as RefreshCwIcon,
  Plus as PlusIcon,
  Trash2 as Trash2Icon
} from 'lucide-vue-next'

const authStore = useAuthStore()
const themeStore = useThemeStore()
const { locale, setLocale, t } = useI18n()

const urlInput = ref(authStore.backendUrl)
const tokenInput = ref(authStore.accessToken)
const showToken = ref(false)
const saveMessage = ref('')
const testingConnection = ref(false)
const testResult = ref<{ ok: boolean; title: string; detail: string } | null>(null)

function saveSettings() {
  authStore.updateBackendUrl(urlInput.value)
  authStore.updateAccessToken(tokenInput.value)
  saveMessage.value = locale.value === 'pl' ? 'Zapisano w localStorage.' : 'Settings saved to localStorage.'
  setTimeout(() => {
    saveMessage.value = ''
  }, 2500)
}

async function testConnection() {
  saveSettings()
  testingConnection.value = true
  testResult.value = null
  try {
    const health = await api.getHealth()
    let walletMsg = 'Wallet probe passed'
    try {
      const wallet = await api.getWallet()
      walletMsg = `Wallet: $${wallet.balance_usd ?? '0.00'} (${wallet.label})`
    } catch (e: any) {
      walletMsg = `Wallet notice: ${e.message}`
    }

    testResult.value = {
      ok: true,
      title: locale.value === 'pl' ? 'Połączenie aktywne' : 'Connection Verified',
      detail: `Health: ${health.status} v${health.version} (uptime ${health.uptime_s}s) | ${walletMsg}`
    }
  } catch (err: any) {
    testResult.value = {
      ok: false,
      title: locale.value === 'pl' ? 'Błąd połączenia' : 'Connection Failed',
      detail: err?.message || 'Server did not respond to /api/health.'
    }
  } finally {
    testingConnection.value = false
  }
}

// Skills Catalog State & Actions
const skillsList = ref<SkillCatalogItem[]>([])
const skillsLoading = ref(false)
const showSkillPreviewModal = ref(false)
const activeSkillDetail = ref<SkillDetail | null>(null)
const showInstallSkillModal = ref(false)
const installingSkill = ref(false)
const confirmDeleteSkill = ref<string | null>(null)
const skillStatusBanner = ref<{ ok: boolean; message: string } | null>(null)

const installForm = ref({
  name: '',
  version: '1.0.0',
  source_url: '',
  origin_repo: '',
  taxonomyRaw: 'general',
  content: ''
})

async function fetchSkills() {
  skillsLoading.value = true
  try {
    const res = await api.getSkills()
    if (res.ok) {
      skillsList.value = res.skills || []
    }
  } catch (err: any) {
    console.warn('Failed to fetch skills catalog:', err)
  } finally {
    skillsLoading.value = false
  }
}

async function openSkillPreview(skillName: string) {
  try {
    const res = await api.getSkill(skillName)
    if (res.ok && res.skill) {
      activeSkillDetail.value = res.skill
      showSkillPreviewModal.value = true
    }
  } catch (err: any) {
    skillStatusBanner.value = {
      ok: false,
      message: err?.message || 'Failed to load skill details'
    }
  }
}

async function handleInstallSkill() {
  if (!installForm.value.source_url.trim() && !installForm.value.content.trim()) {
    skillStatusBanner.value = {
      ok: false,
      message: locale.value === 'pl' ? 'Podaj URL lub treść SKILL.md' : 'Provide either a URL or SKILL.md content'
    }
    return
  }
  installingSkill.value = true
  skillStatusBanner.value = null
  try {
    const taxonomy = installForm.value.taxonomyRaw
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean)
    const res = await api.installSkill({
      name: installForm.value.name.trim() || undefined,
      version: installForm.value.version.trim() || '1.0.0',
      source_url: installForm.value.source_url.trim() || undefined,
      origin_repo: installForm.value.origin_repo.trim() || undefined,
      content: installForm.value.content.trim() || undefined,
      taxonomy
    })
    if (res.ok) {
      showInstallSkillModal.value = false
      installForm.value = {
        name: '',
        version: '1.0.0',
        source_url: '',
        origin_repo: '',
        taxonomyRaw: 'general',
        content: ''
      }
      skillStatusBanner.value = {
        ok: true,
        message: locale.value === 'pl'
          ? `Zainstalowano skill '${res.name}' (v${res.version}).`
          : `Installed skill '${res.name}' (v${res.version}).`
      }
      await fetchSkills()
    }
  } catch (err: any) {
    skillStatusBanner.value = {
      ok: false,
      message: err?.message || 'Failed to install skill'
    }
  } finally {
    installingSkill.value = false
  }
}

async function handleDeleteSkill(skillName: string) {
  confirmDeleteSkill.value = null
  try {
    const res = await api.deleteSkill(skillName)
    if (res.ok) {
      skillsList.value = skillsList.value.filter((s) => s.name !== skillName)
      skillStatusBanner.value = {
        ok: true,
        message: locale.value === 'pl' ? `Usunięto skill '${skillName}'.` : `Deleted skill '${skillName}'.`
      }
    }
  } catch (err: any) {
    skillStatusBanner.value = {
      ok: false,
      message: err?.message || 'Failed to delete skill'
    }
  }
}

onMounted(() => {
  fetchSkills()
})
</script>
