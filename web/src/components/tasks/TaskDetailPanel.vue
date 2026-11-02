<template>
  <div class="space-y-4">
    <div v-if="task.status === 'RUNNING' || task.status === 'QUEUED'" class="p-3 rounded-lg bg-cyan-950/40 border border-cyan-800/50 flex items-center space-x-3">
      <Loader2Icon class="w-4 h-4 text-cyan-400 animate-spin" />
      <span class="text-xs text-cyan-200">
        {{ locale === 'pl' ? 'Zadanie działa w tle...' : 'Task running in background harness...' }}
      </span>
    </div>

    <!-- Live progress strip -->
    <div v-if="task.progress" class="grid grid-cols-3 gap-2 text-center">
      <div class="p-2 rounded-lg bg-botex-950 border border-botex-800">
        <div class="text-xs text-slate-400 font-mono uppercase">{{ t('progressTurn') }}</div>
        <div class="text-sm font-bold text-slate-100 font-mono">
          {{ task.progress.turn ?? '—' }}<span v-if="task.progress.max_turns" class="text-slate-400">/{{ task.progress.max_turns }}</span>
        </div>
      </div>
      <div class="p-2 rounded-lg bg-botex-950 border border-botex-800">
        <div class="text-xs text-slate-400 font-mono uppercase">{{ t('progressCost') }}</div>
        <div class="text-sm font-bold text-slate-100 font-mono">
          ${{ (task.progress.cost_usd ?? 0).toFixed(4) }}
        </div>
      </div>
      <div class="p-2 rounded-lg border" :class="task.progress.waiting_for ? 'bg-amber-950/40 border-amber-700/50' : 'bg-botex-950 border-botex-800'">
        <div class="text-xs text-slate-400 font-mono uppercase">{{ locale === 'pl' ? 'Stan' : 'State' }}</div>
        <div class="text-xs font-semibold truncate" :class="task.progress.waiting_for ? 'text-amber-300' : 'text-slate-300'">
          {{ waitingLabel }}
        </div>
      </div>
    </div>

    <!-- Live events feed -->
    <div v-if="task.status === 'RUNNING' || task.status === 'QUEUED' || events.length" class="space-y-1.5">
      <label class="block text-xs font-semibold text-slate-200 uppercase font-mono">
        {{ t('taskEventsTitle') }}
      </label>
      <div
        v-if="eventMeta.dropped > 0"
        class="px-2 py-1 rounded bg-amber-950/40 border border-amber-700/50 text-xs text-amber-300 font-mono"
      >
        {{ eventMeta.dropped }} {{ t('taskEventsDropped') }}
      </div>
      <div class="rounded-lg bg-botex-950 border border-botex-800 max-h-64 overflow-y-auto font-mono text-xs divide-y divide-botex-800/60">
        <div v-if="!events.length" class="p-3 text-slate-400">{{ t('taskEventsEmpty') }}</div>
        <div
          v-for="ev in visibleEvents"
          :key="ev.seq"
          class="px-2.5 py-1.5 flex items-start space-x-2"
        >
          <span class="text-slate-500 shrink-0 w-14">{{ formatTs(ev.ts) }}</span>
          <span :class="['shrink-0 w-28 truncate font-semibold', kindColor(ev.kind)]">{{ ev.kind }}</span>
          <span class="text-slate-300 break-all min-w-0">{{ describe(ev) }}</span>
        </div>
      </div>
    </div>

    <!-- Devin session: conversation stream (turn history) -->
    <div v-if="isDevin && (sessionId || turns.length)" class="space-y-1.5">
      <label class="block text-xs font-semibold text-slate-200 uppercase font-mono">
        {{ t('devinSessionTitle') }}
      </label>
      <div
        v-if="sessionId"
        class="px-2.5 py-1.5 rounded-lg bg-botex-950 border border-botex-800 text-xs font-mono text-teal-300 truncate"
        :title="sessionId"
      >
        {{ sessionId }}
      </div>
      <div
        v-if="turns.length"
        class="rounded-lg bg-botex-950 border border-botex-800 max-h-64 overflow-y-auto divide-y divide-botex-800/60"
      >
        <div v-for="turn in turns" :key="turn.turn" class="p-2.5 space-y-1.5">
          <div class="flex items-center space-x-2">
            <span class="text-xs font-mono font-semibold text-cyan-300">#{{ turn.turn }}</span>
            <span
              :class="['w-1.5 h-1.5 rounded-full', turn.ok ? 'bg-emerald-400' : 'bg-rose-400']"
            ></span>
            <span v-if="turn.resumed" class="text-xs font-mono text-teal-400">{{ t('devinSessionResumed') }}</span>
            <span v-if="turn.ts" class="text-xs font-mono text-slate-500 ml-auto">{{ formatTs(turn.ts) }}</span>
          </div>
          <p class="text-xs text-slate-200 whitespace-pre-wrap break-words font-sans">{{ turn.prompt }}</p>
          <p
            v-if="turn.summary"
            class="text-xs text-slate-400 whitespace-pre-wrap break-words font-sans border-l-2 border-botex-700 pl-2"
          >{{ turn.summary }}</p>
        </div>
      </div>
    </div>

    <!-- Devin follow-up prompt (multi-turn continuation in the same session) -->
    <div v-if="isDevin && sessionId" class="space-y-1.5">
      <label class="block text-xs font-semibold text-slate-200 uppercase font-mono">
        {{ t('followupLabel') }}
      </label>
      <textarea
        v-model="followupText"
        rows="3"
        :disabled="!canFollowup"
        :placeholder="canFollowup ? t('followupPlaceholder') : t('followupRunning')"
        class="w-full rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 p-2.5 text-base sm:text-xs font-sans focus:outline-none focus:border-cyan-500 disabled:opacity-50"
        @keydown.enter.exact.prevent="submitFollowup"
      ></textarea>
      <Button
        variant="primary"
        size="sm"
        class="w-full"
        :loading="tasksStore.isSendingFollowup"
        :disabled="!canFollowup || !followupText.trim()"
        @click="submitFollowup"
      >
        {{ t('followupSend') }}
      </Button>
    </div>

    <!-- Structured Result -->
    <div v-if="task.result" class="space-y-1.5">
      <label class="block text-xs font-semibold text-slate-200 uppercase font-mono">
        {{ locale === 'pl' ? 'Wynik zadania' : 'Summary Result' }}
      </label>
      <div class="p-3 rounded-lg bg-botex-950 border border-botex-800 text-xs text-slate-200 max-h-48 overflow-y-auto whitespace-pre-wrap break-words">
        {{ task.result }}
      </div>
    </div>

    <!-- Result Data JSON -->
    <div v-if="task.result_data" class="space-y-1.5">
      <label class="block text-xs font-semibold text-slate-200 uppercase font-mono">
        {{ locale === 'pl' ? 'Weryfikacja kontraktu' : 'Contract Verification Data' }}
      </label>
      <div class="p-3 rounded-lg bg-botex-950 border border-botex-800 font-mono text-xs text-slate-300 max-h-56 overflow-auto">
        <pre class="m-0">{{ JSON.stringify(task.result_data, null, 2) }}</pre>
      </div>
    </div>

    <!-- Cancel Action -->
    <div v-if="task.status === 'RUNNING' || task.status === 'QUEUED'" class="pt-2">
      <Button
        variant="danger"
        size="sm"
        class="w-full"
        @click="emit('cancel', task.task_id)"
      >
        {{ t('abortTask') }}
      </Button>
    </div>

    <!-- Retry / Clone (terminal states) -->
    <div v-else-if="task.request?.task" class="pt-2">
      <Button
        variant="secondary"
        size="sm"
        class="w-full"
        @click="emit('retry', task)"
      >
        {{ t('retryTask') }}
      </Button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import type { TaskDetail, TaskEvent } from '../../api/types'
import { useI18n } from '../../i18n'
import { useTasksStore } from '../../stores/tasks'
import { wsManager } from '../../api/ws'
import Button from '../common/Button.vue'
import { Loader2 as Loader2Icon } from 'lucide-vue-next'

const props = defineProps<{
  task: TaskDetail
}>()

const emit = defineEmits<{
  (e: 'cancel', taskId: string): void
  (e: 'retry', task: TaskDetail): void
}>()

const { locale, t } = useI18n()
const tasksStore = useTasksStore()
const { taskEvents, taskEventMeta } = storeToRefs(tasksStore)

const followupText = ref('')
const isDevin = computed(
  () => props.task.executor === 'devin' || props.task.request?.executor === 'devin'
)
const sessionId = computed(
  () => props.task.session_id || props.task.result_data?.session_id || ''
)
const turns = computed(() => props.task.turns || [])
const canFollowup = computed(
  () =>
    isDevin.value &&
    !!sessionId.value &&
    props.task.status !== 'RUNNING' &&
    props.task.status !== 'QUEUED'
)

async function submitFollowup() {
  const text = followupText.value.trim()
  if (!text || !canFollowup.value) return
  const ok = await tasksStore.sendFollowup(props.task.task_id, text)
  if (ok) followupText.value = ''
}

let unsubscribe: (() => void) | null = null

const events = computed<TaskEvent[]>(() => taskEvents.value.get(props.task.task_id) || [])
const eventMeta = computed(
  () =>
    taskEventMeta.value.get(props.task.task_id) || {
      minSeq: null,
      dropped: 0,
      lastSeq: null
    }
)
const visibleEvents = computed(() => events.value.slice(-200))

const WAITING_KEYS: Record<string, string> = {
  approval: 'waitingApproval',
  retry: 'waitingRetry',
  input: 'waitingInput',
  model: 'waitingModel'
}
const waitingLabel = computed(() => {
  const w = props.task.progress?.waiting_for
  return w ? t(WAITING_KEYS[w] || w) : props.task.status
})

const KIND_COLORS: Record<string, string> = {
  provider_resolved: 'text-cyan-300',
  attempt_start: 'text-sky-300',
  attempt_end: 'text-sky-400',
  tool_call: 'text-violet-300',
  completion_parse: 'text-emerald-300',
  contract_gate: 'text-amber-300',
  retry_wait: 'text-amber-400',
  approval_wait: 'text-rose-300',
  usage: 'text-slate-400',
  no_progress: 'text-rose-400',
  devin_update: 'text-teal-300',
  waiting_for_input: 'text-amber-300',
  task_state: 'text-cyan-200',
  error: 'text-rose-400'
}
const kindColor = (kind: string) => KIND_COLORS[kind] || 'text-slate-400'

function describe(ev: TaskEvent): string {
  const d = (ev.data || {}) as Record<string, any>
  switch (ev.kind) {
    case 'tool_call':
      return `${d.tool ?? '?'} ${d.ok ? 'ok' : 'FAILED'}`
    case 'retry_wait':
      return `${d.delay_s ?? '?'}s — ${d.error ?? ''}`
    case 'usage':
      return `turn ${d.turn ?? '?'}/${d.max_turns ?? '?'} $${Number(d.cost_usd ?? 0).toFixed(4)}`
    case 'devin_update':
      return [d.update_kind, d.title, d.status].filter(Boolean).join(' · ')
    case 'provider_resolved':
      return `${d.provider ?? ''} → ${(d.candidates || []).join(', ')}`
    case 'attempt_start':
      return `${d.model ?? ''} (${d.attempt_index ?? '?'}/${d.attempt_count ?? '?'})`
    case 'attempt_end':
      return `${d.model ?? ''} ${d.status ?? ''} ${d.failure_kind ?? ''}`
    case 'task_state':
      return `${d.state ?? ''}`
    case 'contract_gate':
      return `${d.ok ? 'ok' : 'FAILED'} ${d.missing ?? ''}`
    case 'waiting_for_input':
      return `idle ${d.idle_s ?? '?'}s`
    case 'no_progress':
      return `turn ${d.turn ?? '?'}`
    case 'error':
      return `${d.message ?? d.error ?? ''}`
    default:
      return Object.entries(d)
        .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
        .join(' ')
        .slice(0, 140)
  }
}

function formatTs(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

function subscribe(taskId: string) {
  unsubscribe?.()
  unsubscribe = null
  if (!taskId) return
  tasksStore.loadTaskEvents(taskId).catch(() => {})
  unsubscribe = wsManager.subscribeTask(taskId, (ev) => tasksStore.handleTaskEvent(taskId, ev))
}

watch(() => props.task.task_id, (id) => subscribe(id), { immediate: true })
onBeforeUnmount(() => unsubscribe?.())
</script>
