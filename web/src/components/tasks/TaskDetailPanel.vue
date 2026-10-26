<template>
  <div class="space-y-4">
    <div v-if="task.status === 'RUNNING' || task.status === 'QUEUED'" class="p-3 rounded-lg bg-cyan-950/40 border border-cyan-800/50 flex items-center space-x-3">
      <Loader2Icon class="w-4 h-4 text-cyan-400 animate-spin" />
      <span class="text-xs text-cyan-200">
        {{ locale === 'pl' ? 'Zadanie działa w tle...' : 'Task running in background harness...' }}
      </span>
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
import type { TaskDetail } from '../../api/types'
import { useI18n } from '../../i18n'
import Button from '../common/Button.vue'
import { Loader2 as Loader2Icon } from 'lucide-vue-next'

defineProps<{
  task: TaskDetail
}>()

const emit = defineEmits<{
  (e: 'cancel', taskId: string): void
  (e: 'retry', task: TaskDetail): void
}>()

const { locale, t } = useI18n()
</script>
