<template>
  <div class="rounded-xl border border-botex-700 bg-botex-900 p-4 shadow-lg transition-all hover:border-slate-600">
    <!-- Header -->
    <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
      <div class="flex items-center space-x-2.5">
        <div :class="['p-2 rounded-lg', riskIconBg]">
          <ShieldAlertIcon v-if="isHighRisk" class="w-4 h-4 text-rose-300" />
          <AlertTriangleIcon v-else-if="isMutation" class="w-4 h-4 text-amber-300" />
          <ShieldCheckIcon v-else class="w-4 h-4 text-emerald-300" />
        </div>
        <div>
          <div class="flex items-center space-x-2">
            <span class="text-sm font-semibold text-slate-100 font-mono">{{ action.tool_name }}</span>
            <Badge :variant="riskBadgeVariant">{{ action.risk_level }}</Badge>
          </div>
          <p class="text-xs text-slate-300 mt-0.5">
            ID: <span class="font-mono text-slate-200">{{ action.action_id }}</span>
            <span v-if="action.task_id" class="ml-2">Task: <span class="font-mono text-slate-200">{{ action.task_id }}</span></span>
          </p>
        </div>
      </div>

      <!-- Expand / Quick View Button -->
      <button
        v-if="!compact"
        type="button"
        class="text-xs text-cyan-300 hover:text-cyan-200 underline underline-offset-2"
        @click="$emit('inspect', action)"
      >
        {{ locale === 'pl' ? 'Pełny podgląd' : 'Inspect' }}
      </button>
    </div>

    <!-- Params / Description -->
    <div v-if="action.description" class="mb-3 text-xs text-slate-200 bg-botex-950 p-2.5 rounded-lg border border-botex-800">
      {{ action.description }}
    </div>

    <!-- Unified Diff or Command Preview -->
    <div class="mb-4">
      <DiffViewer
        v-if="action.preview_diff"
        :diff="action.preview_diff"
        :title="`Patch Preview (${action.params?.path || action.params?.file_path || 'file'})`"
      />
      <div v-else-if="action.params?.command" class="rounded-lg bg-botex-950 border border-botex-800 p-3 font-mono text-xs text-cyan-300">
        <span class="text-slate-400 mr-2">$</span>{{ action.params.command }}
      </div>
      <div v-else class="rounded-lg bg-botex-950 border border-botex-800 p-2 text-xs font-mono text-slate-300 overflow-x-auto max-h-36">
        <pre class="m-0">{{ JSON.stringify(action.params, null, 2) }}</pre>
      </div>
    </div>

    <!-- Decision Controls -->
    <div class="space-y-3 pt-2 border-t border-botex-800">
      <!-- Optional Rejection Reason -->
      <div v-if="showRejectInput" class="animate-fadeIn">
        <label class="block text-xs font-medium text-slate-200 mb-1">
          {{ locale === 'pl' ? 'Powód odrzucenia (wskazówka dla modelu):' : 'Rejection reason (model feedback):' }}
        </label>
        <input
          v-model="rejectionReason"
          type="text"
          :placeholder="locale === 'pl' ? 'np. Nie zmieniaj testów, popraw logikę w server.py' : 'e.g. Do not modify tests, fix server.py instead'"
          class="w-full bg-botex-950 border border-rose-500/50 rounded-lg px-3 py-2 text-xs text-slate-100 placeholder-slate-400 focus:outline-none focus:ring-1 focus:ring-rose-500"
        />
      </div>

      <div class="flex flex-wrap items-center justify-between gap-3">
        <!-- Remember session checkbox -->
        <label class="inline-flex items-center space-x-2 text-xs text-slate-300 select-none cursor-pointer min-h-[36px]">
          <input
            v-model="rememberSession"
            type="checkbox"
            class="rounded bg-botex-950 border-botex-700 text-brand-blue focus:ring-0 focus:ring-offset-0"
          />
          <span>{{ t('rememberSession') }}</span>
        </label>

        <!-- Action buttons -->
        <div class="flex items-center space-x-2">
          <!-- Reject button -->
          <button
            v-if="!showRejectInput"
            type="button"
            :disabled="isDeciding"
            class="min-h-[40px] px-3.5 py-1.5 rounded-lg bg-rose-950/60 hover:bg-rose-900/70 text-rose-200 border border-rose-700/60 text-xs font-medium transition active:scale-95 flex items-center space-x-1"
            @click="showRejectInput = true"
          >
            <XCircleIcon class="w-3.5 h-3.5" />
            <span>{{ t('rejectAction') }}</span>
          </button>
          <button
            v-else
            type="button"
            :disabled="isDeciding"
            class="min-h-[40px] px-3.5 py-1.5 rounded-lg bg-rose-600 hover:bg-rose-500 text-white text-xs font-medium transition active:scale-95 flex items-center space-x-1"
            @click="handleDecision(false)"
          >
            <XCircleIcon class="w-3.5 h-3.5" />
            <span>{{ t('confirmReject') }}</span>
          </button>

          <!-- Approve button -->
          <button
            type="button"
            :disabled="isDeciding"
            class="min-h-[40px] px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-semibold transition active:scale-95 flex items-center space-x-1.5"
            @click="handleDecision(true)"
          >
            <CheckCircleIcon class="w-3.5 h-3.5" />
            <span>{{ t('approveAction') }}</span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { PendingAction } from '../../api/types'
import { useI18n } from '../../i18n'
import Badge from '../common/Badge.vue'
import DiffViewer from '../common/DiffViewer.vue'
import {
  ShieldAlert as ShieldAlertIcon,
  AlertTriangle as AlertTriangleIcon,
  ShieldCheck as ShieldCheckIcon,
  CheckCircle as CheckCircleIcon,
  XCircle as XCircleIcon
} from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    action: PendingAction
    compact?: boolean
  }>(),
  {
    compact: false
  }
)

const emit = defineEmits<{
  (e: 'decide', actionId: string, approved: boolean, reason: string, remember: boolean): void
  (e: 'inspect', action: PendingAction): void
}>()

const { locale, t } = useI18n()

const rememberSession = ref(false)
const showRejectInput = ref(false)
const rejectionReason = ref('')
const isDeciding = ref(false)

const isHighRisk = computed(() => props.action.risk_level === 'HIGH_RISK')
const isMutation = computed(() => props.action.risk_level === 'MUTATIONS')

const riskIconBg = computed(() => {
  if (isHighRisk.value) return 'bg-rose-950/60 border border-rose-800/60'
  if (isMutation.value) return 'bg-amber-950/60 border border-amber-800/60'
  return 'bg-emerald-950/60 border border-emerald-800/60'
})

const riskBadgeVariant = computed(() => {
  if (isHighRisk.value) return 'danger'
  if (isMutation.value) return 'warning'
  return 'success'
})

function handleDecision(approved: boolean) {
  isDeciding.value = true
  emit('decide', props.action.action_id, approved, rejectionReason.value, rememberSession.value)
}
</script>
