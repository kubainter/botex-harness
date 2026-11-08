<template>
  <Modal
    :is-open="isOpen"
    :title="locale === 'pl' ? 'Analiza obrazu (dHash i modele wizyjne)' : 'Image inspection (dHash and vision)'"
    :subtitle="
      locale === 'pl'
        ? 'Skrót 64-bit dHash odsiewa identyczne klatki bez kosztu tokenów. Gdy wykryje różnicę, przekazuje obraz do darmowego modelu wizyjnego.'
        : 'A 64-bit dHash check skips identical frames at zero token cost. Changed frames go to a free vision model.'
    "
    max-width="lg"
    @close="handleClose"
  >
    <div class="space-y-4 text-xs">
      <!-- Error Banner -->
      <div
        v-if="errorMsg"
        class="p-3 rounded-lg bg-rose-950/60 border border-rose-500/40 text-rose-200 flex items-center space-x-2"
      >
        <AlertCircleIcon class="w-4 h-4 text-rose-400 shrink-0" />
        <span class="break-words">{{ errorMsg }}</span>
      </div>

      <!-- Dropzone / Image Picker -->
      <div
        class="border-2 border-dashed rounded-xl p-4 text-center transition-colors cursor-pointer"
        :class="
          isDragging
            ? 'border-sky-400 bg-sky-950/30'
            : 'border-botex-700 bg-botex-950/70 hover:border-slate-500'
        "
        @dragover.prevent="isDragging = true"
        @dragleave.prevent="isDragging = false"
        @drop.prevent="handleDrop"
        @click="triggerFileInput"
      >
        <input
          ref="fileInputRef"
          type="file"
          accept="image/*"
          class="hidden"
          @change="handleFileSelect"
        />

        <div v-if="previewDataUrl" class="space-y-2">
          <img
            :src="previewDataUrl"
            alt="Screenshot preview"
            class="max-h-48 mx-auto rounded-lg border border-botex-700 object-contain"
          />
          <div class="flex items-center justify-center gap-2 pt-1">
            <code class="text-slate-300 truncate max-w-xs">{{ selectedFileName }}</code>
            <button
              type="button"
              class="min-h-[44px] lg:min-h-[32px] px-2.5 py-1 rounded bg-botex-800 hover:bg-rose-900/60 text-rose-300 border border-botex-700 text-xs transition-colors"
              @click.stop="clearImage"
            >
              {{ locale === 'pl' ? 'Usuń obraz' : 'Clear' }}
            </button>
          </div>
        </div>

        <div v-else class="py-4 space-y-1.5 text-slate-300">
          <EyeIcon class="w-6 h-6 mx-auto text-cyan-400" />
          <div class="font-medium text-slate-100">
            {{
              locale === 'pl'
                ? 'Upuść zrzut ekranu, kliknij lub wklej ze schowka (Ctrl+V)'
                : 'Drop a screenshot, click to browse, or paste from clipboard (Ctrl+V)'
            }}
          </div>
          <p class="text-xs text-slate-400">
            {{ locale === 'pl' ? 'PNG, JPEG lub WEBP do 20 MB (maks. 10 MP)' : 'PNG, JPEG, or WEBP up to 20 MB (10 MP max)' }}
          </p>
        </div>
      </div>

      <!-- Prompt Hint & Baseline dHash -->
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label class="block text-slate-200 font-medium mb-1.5 text-xs">
            {{ locale === 'pl' ? 'Na co zwrócić uwagę' : 'What to look for' }}
          </label>
          <input
            v-model="promptHint"
            type="text"
            :placeholder="
              locale === 'pl'
                ? 'np. Sprawdź, czy modal zasłania pasek nawigacji'
                : 'e.g. Check if the modal covers the navigation bar'
            "
            class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-sky-500"
          />
        </div>

        <div>
          <label class="block text-slate-200 font-medium mb-1.5 text-xs">
            {{ locale === 'pl' ? 'Poprzedni skrót dHash (opcjonalnie)' : 'Baseline dHash (optional)' }}
          </label>
          <div class="flex items-center space-x-1.5">
            <input
              v-model="referenceHashText"
              type="text"
              :placeholder="locale === 'pl' ? 'Brak (pełna analiza)' : 'None (full check)'"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg px-3 py-2 text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-sky-500 tabular-nums"
            />
            <button
              v-if="lastDhash !== null"
              type="button"
              class="shrink-0 min-h-[44px] lg:min-h-[36px] px-2.5 py-1.5 rounded-lg bg-botex-800 hover:bg-botex-700 border border-botex-600 text-xs text-sky-300 transition-colors"
              :title="locale === 'pl' ? 'Wstaw dHash z poprzedniego sprawdzenia' : 'Use dHash from previous check'"
              @click="referenceHashText = String(lastDhash)"
            >
              {{ locale === 'pl' ? 'Użyj ostatniego' : 'Use previous' }}
            </button>
          </div>
        </div>
      </div>

      <!-- Run Inspection Button -->
      <div class="flex justify-end">
        <Button
          variant="primary"
          size="sm"
          :loading="isInspecting"
          :disabled="!imageBase64 || isInspecting"
          @click="runInspect"
        >
          {{ locale === 'pl' ? 'Sprawdź zrzut ekranu' : 'Inspect screenshot' }}
        </Button>
      </div>

      <!-- Inspection Result Card -->
      <div
        v-if="result"
        class="p-3.5 rounded-xl bg-botex-950 border border-botex-800 space-y-2.5"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span
            class="px-2 py-0.5 rounded text-xs font-semibold border"
            :class="statusBadgeClass"
          >
            {{ result.status }}
            <template v-if="result.skipped_llm">
              · dHash {{ result.hamming_distance }} &le; 4 · $0.00
            </template>
          </span>

          <span class="text-xs text-slate-400">
            <code v-if="result.provider && result.model">{{ result.provider }}/{{ result.model }}</code>
            <template v-else>
              {{ locale === 'pl' ? 'Lokalny filtr dHash (0 tokenów)' : 'Local dHash check (0 tokens)' }}
            </template>
          </span>
        </div>

        <div class="flex flex-wrap items-center gap-3 text-xs text-slate-300 tabular-nums">
          <span>dHash: <code class="text-sky-300">{{ result.dhash }}</code></span>
          <span>{{ locale === 'pl' ? 'Odległość Hamminga:' : 'Hamming distance:' }} <code class="text-amber-300">{{ result.hamming_distance }}</code></span>
        </div>

        <div class="p-2.5 rounded-lg bg-botex-900/90 border border-botex-800 text-xs text-slate-200 whitespace-pre-wrap break-words max-h-44 overflow-y-auto">
          {{ result.finding }}
        </div>

        <div class="flex justify-end pt-1">
          <Button variant="secondary" size="sm" @click="emitInsert">
            {{ locale === 'pl' ? 'Wstaw wynik do czatu' : 'Insert finding into chat' }}
          </Button>
        </div>
      </div>
    </div>
  </Modal>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '../../api/client'
import type { VisionInspectResponse } from '../../api/types'
import { useI18n } from '../../i18n'
import Modal from '../common/Modal.vue'
import Button from '../common/Button.vue'
import { AlertCircle as AlertCircleIcon, Eye as EyeIcon } from 'lucide-vue-next'

const props = defineProps<{
  isOpen: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'insert-diagnosis', text: string): void
}>()

const { locale } = useI18n()

const fileInputRef = ref<HTMLInputElement | null>(null)
const isDragging = ref(false)
const previewDataUrl = ref<string | null>(null)
const imageBase64 = ref<string>('')
const selectedFileName = ref<string>('')
const promptHint = ref<string>('')
const referenceHashText = ref<string>('')
const lastDhash = ref<number | null>(null)
const isInspecting = ref(false)
const errorMsg = ref<string | null>(null)
const result = ref<VisionInspectResponse | null>(null)

const statusBadgeClass = computed(() => {
  if (!result.value) return ''
  if (result.value.status === 'NO_VISUAL_DIFF') {
    return 'bg-emerald-950/70 text-emerald-300 border-emerald-500/40'
  }
  if (result.value.status === 'DEFECT_DETECTED') {
    return 'bg-amber-950/70 text-amber-300 border-amber-500/40'
  }
  return 'bg-sky-950/70 text-sky-300 border-sky-500/40'
})

function triggerFileInput() {
  fileInputRef.value?.click()
}

function clearImage() {
  previewDataUrl.value = null
  imageBase64.value = ''
  selectedFileName.value = ''
  result.value = null
  errorMsg.value = null
  if (fileInputRef.value) {
    fileInputRef.value.value = ''
  }
}

function loadFile(file: File) {
  errorMsg.value = null
  result.value = null
  if (!file.type.startsWith('image/')) {
    errorMsg.value = locale.value === 'pl' ? 'Wybrany plik nie jest obrazem.' : 'Selected file is not an image.'
    return
  }
  selectedFileName.value = file.name || 'clipboard-image.png'
  const reader = new FileReader()
  reader.onload = () => {
    const dataUrl = String(reader.result || '')
    previewDataUrl.value = dataUrl
    const commaIdx = dataUrl.indexOf(',')
    imageBase64.value = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : dataUrl
  }
  reader.onerror = () => {
    errorMsg.value = locale.value === 'pl' ? 'Nie udało się odczytać pliku obrazu.' : 'Failed to read image file.'
  }
  reader.readAsDataURL(file)
}

function handleFileSelect(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (file) loadFile(file)
}

function handleDrop(e: DragEvent) {
  isDragging.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) loadFile(file)
}

function handlePaste(e: ClipboardEvent) {
  if (!props.isOpen) return
  const items = e.clipboardData?.items
  if (!items) return
  for (const item of Array.from(items)) {
    if (item.type.startsWith('image/')) {
      const file = item.getAsFile()
      if (file) {
        e.preventDefault()
        loadFile(file)
        break
      }
    }
  }
}

async function runInspect() {
  if (!imageBase64.value || isInspecting.value) return
  errorMsg.value = null
  result.value = null
  let parsedRefHash: number | null = null
  const rawHash = referenceHashText.value.trim()
  if (rawHash) {
    if (!/^-?\d+$/.test(rawHash)) {
      errorMsg.value =
        locale.value === 'pl'
          ? 'Poprzedni skrót dHash musi być liczbą całkowitą.'
          : 'Baseline dHash must be an integer.'
      return
    }
    const n = Number(rawHash)
    if (!Number.isSafeInteger(n)) {
      errorMsg.value =
        locale.value === 'pl'
          ? 'Wartość dHash przekracza zakres bezpiecznych liczb całkowitych.'
          : 'Baseline dHash exceeds safe integer range.'
      return
    }
    parsedRefHash = n
  }
  isInspecting.value = true
  try {
    const res = await api.inspectVision({
      image_base64: imageBase64.value,
      prompt_hint: promptHint.value.trim(),
      reference_hash: parsedRefHash
    })
    result.value = res
    if (typeof res.dhash === 'number') {
      lastDhash.value = res.dhash
    }
  } catch (err: any) {
    errorMsg.value = err?.detail || err?.message || 'Vision inspection failed.'
  } finally {
    isInspecting.value = false
  }
}

function emitInsert() {
  if (!result.value) return
  const modelTag =
    result.value.provider && result.value.model
      ? `${result.value.provider}/${result.value.model}`
      : 'dHash-gate'
  const text = `[Visual Triage · ${result.value.status} · ${modelTag} · dHash=${result.value.dhash}]\n${result.value.finding}`
  emit('insert-diagnosis', text)
}

function handleClose() {
  emit('close')
}

onMounted(() => {
  window.addEventListener('paste', handlePaste)
})

onUnmounted(() => {
  window.removeEventListener('paste', handlePaste)
})
</script>
