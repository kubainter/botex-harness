<template>
  <div class="h-full flex flex-col overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6">
    <!-- View Header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4">
      <div>
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <CpuIcon class="w-5 h-5 text-cyan-400" />
          <span>{{ t('tasksTitle') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">
          {{ t('tasksSubtitle') }}
        </p>
      </div>

      <div class="flex items-center space-x-2">
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5"
          @click="tasksStore.fetchTasks"
        >
          <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': tasksStore.isLoading }" />
          <span>{{ t('refresh') }}</span>
        </button>
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[36px] px-4 py-1.5 rounded-lg bg-brand-blue hover:bg-blue-600 text-xs font-semibold text-white transition flex items-center space-x-1.5"
          @click="showCreateModal = true"
        >
          <PlusIcon class="w-4 h-4" />
          <span>{{ t('newTask') }}</span>
        </button>
      </div>
    </div>

    <!-- Error notice -->
    <div
      v-if="tasksStore.errorMessage"
      class="p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ tasksStore.errorMessage }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="tasksStore.fetchTasks()">{{ t('refresh') }}</button>
    </div>

    <!-- Connected Devices Strip -->
    <div v-if="connectedDevices.length > 0" class="rounded-xl border border-botex-800 bg-botex-900/70 p-3 space-y-2">
      <div class="flex items-center justify-between text-xs">
        <span class="font-mono font-semibold uppercase tracking-wider text-slate-300">
          {{ t('connectedDevices') }} ({{ connectedDevices.length }})
        </span>
        <button
          type="button"
          class="text-xs text-cyan-400 hover:text-cyan-300 font-mono"
          @click="refreshConnectedDevices"
        >
          {{ t('refresh') }}
        </button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2">
        <div
          v-for="dev in connectedDevices"
          :key="`${dev.device_id}:${dev.workspace_id}`"
          class="flex items-center justify-between gap-2 px-3 py-2 rounded-lg bg-botex-950 border border-botex-800 text-xs"
        >
          <div class="min-w-0">
            <div class="flex items-center space-x-1.5">
              <span :class="['w-2 h-2 rounded-full shrink-0', dev.healthy ? 'bg-emerald-400' : 'bg-amber-400']"></span>
              <span class="font-mono font-semibold text-slate-100 truncate">{{ dev.device_id }}</span>
              <Badge :variant="dev.healthy ? 'success' : 'warning'">
                {{ dev.healthy ? t('deviceOnline') : t('deviceStale') }}
              </Badge>
            </div>
            <p class="font-mono text-slate-400 truncate mt-0.5">{{ dev.workspace_id }}</p>
          </div>
          <button
            v-if="dev.can_delegate"
            type="button"
            class="shrink-0 min-h-[36px] px-2.5 py-1 rounded bg-botex-800 hover:bg-botex-700 border border-cyan-500/40 text-cyan-300 font-medium transition"
            @click="openDeviceDelegateModal(dev)"
          >
            {{ t('delegateDeviceTitle') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Active Task Banner / Quick Card if any is running -->
    <div v-if="activeRunningTask" class="rounded-xl border border-cyan-500/40 bg-cyan-950/20 p-4 relative overflow-hidden">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center space-x-3">
          <div class="p-2 rounded-lg bg-cyan-500/20 text-cyan-300">
            <Loader2Icon class="w-5 h-5 animate-spin" />
          </div>
          <div>
            <div class="flex items-center space-x-2">
              <span class="text-xs font-mono font-bold text-cyan-300 uppercase tracking-wider">{{ t('activeExecution') }}</span>
              <Badge variant="info">{{ activeRunningTask.status }}</Badge>
            </div>
            <p class="text-xs text-slate-200 font-mono mt-0.5">Task ID: {{ activeRunningTask.task_id }}</p>
          </div>
        </div>

        <div class="flex items-center space-x-2">
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-cyan-300 transition"
            @click="inspectTask(activeRunningTask.task_id)"
          >
            {{ locale === 'pl' ? 'Podgląd' : 'Inspect' }}
          </button>
          <button
            type="button"
            class="min-h-[44px] lg:min-h-[36px] px-3 py-1.5 rounded-lg bg-rose-950/60 hover:bg-rose-900/70 text-rose-200 border border-rose-700/60 text-xs font-medium transition"
            @click="cancelTask(activeRunningTask.task_id)"
          >
            {{ t('abortTask') }}
          </button>
        </div>
      </div>
    </div>

    <!-- Tasks List Grid -->
    <div class="grid grid-cols-1 lg:grid-cols-3 gap-6 flex-1">
      <!-- Task List (2 cols on large screen) -->
      <div class="lg:col-span-2 space-y-3">
        <div class="flex items-center justify-between text-xs text-slate-300 px-1">
          <span class="font-semibold uppercase tracking-wider font-mono">{{ t('taskHistory') }} ({{ tasks.length }})</span>
          <span class="text-slate-400">{{ locale === 'pl' ? 'Najnowsze przebiegi' : 'Latest runs' }}</span>
        </div>

        <!-- Skeleton Loader -->
        <div v-if="tasksStore.isLoading && tasks.length === 0" class="space-y-3">
          <div
            v-for="idx in 3"
            :key="idx"
            class="p-4 rounded-xl border border-botex-800 bg-botex-900/60 animate-pulse flex items-center justify-between"
          >
            <div class="space-y-2">
              <div class="h-4 w-36 bg-botex-800 rounded"></div>
              <div class="h-3 w-48 bg-botex-800/70 rounded"></div>
            </div>
            <div class="h-5 w-16 bg-botex-800 rounded"></div>
          </div>
        </div>

        <div
          v-else-if="tasks.length === 0"
          class="rounded-xl border border-botex-800 bg-botex-900/50 p-8 text-center space-y-3"
        >
          <p class="text-slate-300 text-sm">{{ t('noTasksYet') }}</p>
          <button
            type="button"
            class="inline-flex items-center space-x-1.5 min-h-[44px] px-3.5 py-2 rounded-lg bg-botex-800 hover:bg-botex-700 border border-botex-700 text-xs font-medium text-slate-100 transition"
            @click="showCreateModal = true"
          >
            <PlusIcon class="w-3.5 h-3.5 text-cyan-400" />
            <span>{{ t('newTask') }}</span>
          </button>
        </div>

        <div
          v-for="taskItem in tasks"
          :key="taskItem.task_id"
          :class="[
            'p-4 rounded-xl border transition-all cursor-pointer bg-botex-900 hover:border-slate-600 flex flex-wrap items-center justify-between gap-3',
            selectedTaskId === taskItem.task_id ? 'border-cyan-500 ring-1 ring-cyan-500/20' : 'border-botex-800'
          ]"
          @click="inspectTask(taskItem.task_id)"
        >
          <div class="flex items-center space-x-3">
            <div :class="['w-2.5 h-2.5 rounded-full', statusDotColor(taskItem.status)]"></div>
            <div>
              <div class="flex items-center space-x-2">
                <span class="text-sm font-semibold text-slate-100 font-mono">{{ taskItem.task_id }}</span>
                <Badge :variant="statusBadgeVariant(taskItem.status)">{{ taskItem.status }}</Badge>
              </div>
              <p class="text-xs text-slate-300 mt-0.5 font-mono">
                {{ formatDate(taskItem.created_at) }}
                <span v-if="taskItem.finished_at" class="ml-2 text-slate-400">
                  ({{ formatDuration(taskItem.created_at, taskItem.finished_at) }})
                </span>
              </p>
            </div>
          </div>

          <div class="flex items-center space-x-2">
            <button
              v-if="taskItem.status === 'RUNNING' || taskItem.status === 'QUEUED'"
              type="button"
              class="min-h-[44px] lg:min-h-[36px] px-2.5 py-1 rounded bg-rose-950/60 hover:bg-rose-900/70 text-rose-200 text-xs border border-rose-700/60 transition"
              @click.stop="cancelTask(taskItem.task_id)"
            >
              {{ t('cancel') }}
            </button>
            <ChevronRightIcon class="w-4 h-4 text-slate-400" />
          </div>
        </div>
      </div>

      <!-- Detail Panel (1 col, desktop side column only) -->
      <div class="lg:col-span-1 hidden lg:block">
        <div v-if="currentTask" class="rounded-xl border border-botex-800 bg-botex-900 p-5 space-y-4 sticky top-4">
          <div class="flex items-center justify-between border-b border-botex-800 pb-3">
            <div class="min-w-0">
              <h3 class="text-sm font-bold text-slate-100 font-mono truncate">{{ currentTask.task_id }}</h3>
              <p class="text-xs text-slate-300 font-mono">{{ formatDate(currentTask.created_at) }}</p>
            </div>
            <Badge :variant="statusBadgeVariant(currentTask.status)">{{ currentTask.status }}</Badge>
          </div>

          <TaskDetailPanel
            :task="currentTask"
            @cancel="cancelTask"
            @retry="retryTask"
          />
        </div>

        <div v-else class="rounded-xl border border-botex-800 bg-botex-900/50 p-6 text-center text-slate-300 text-xs">
          {{ t('selectTaskPrompt') }}
        </div>
      </div>
    </div>

    <!-- Task Detail as bottom-sheet modal on mobile (<lg): tapping a card
         opens the sheet immediately instead of pinning the panel under the
         whole list at the bottom of the page. -->
    <Modal
      :is-open="showDetailModal && Boolean(currentTask)"
      :title="currentTask?.task_id || ''"
      :subtitle="currentTask ? formatDate(currentTask.created_at) : ''"
      max-width="lg"
      @close="showDetailModal = false"
    >
      <div v-if="currentTask" class="space-y-4">
        <Badge :variant="statusBadgeVariant(currentTask.status)">{{ currentTask.status }}</Badge>
        <TaskDetailPanel
          :task="currentTask"
          @cancel="(id) => { cancelTask(id); showDetailModal = false }"
          @retry="(task) => { retryTask(task); showDetailModal = false }"
        />
      </div>
    </Modal>

    <!-- Create Task Modal -->
    <Modal
      :is-open="showCreateModal"
      :title="t('delegateTaskTitle')"
      :subtitle="t('delegateTaskSub')"
      max-width="2xl"
      @close="showCreateModal = false"
    >
      <form @submit.prevent="handleCreateTask" class="space-y-4 text-xs">
        <!-- Task Prompt -->
        <div>
          <label class="block font-medium text-slate-200 mb-1">{{ t('taskSpecLabel') }}</label>
          <textarea
            v-model="newTask.task"
            rows="4"
            :required="selectedExecutorMode !== 'device'"
            :placeholder="t('taskSpecPlaceholder')"
            class="w-full rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 p-2.5 text-base sm:text-xs font-sans focus:outline-none focus:border-cyan-500"
          ></textarea>
        </div>

        <!-- Executor & Model/Agent -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block font-medium text-slate-200 mb-1">{{ t('executorLabel') }}</label>
            <select
              v-model="selectedExecutorMode"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100"
            >
              <option value="botex">{{ t('executorBotex') }}</option>
              <option value="devin" :disabled="devinStatus !== null && !devinStatus.available">
                {{ t('executorDevin') }}{{ devinStatus && !devinStatus.available ? ` (${t('devinOffline')})` : '' }}
              </option>
              <option value="device">
                {{ t('executorDevice') }} ({{ connectedDevices.filter((d) => d.can_delegate).length }})
              </option>
            </select>
            <p
              v-if="selectedExecutorMode === 'devin' && devinStatus && !devinStatus.available"
              class="mt-1 text-xs text-amber-400 font-mono"
            >
              {{ t('devinOffline') }}: {{ devinStatus.reason }}
            </p>
          </div>

          <div v-if="selectedExecutorMode === 'devin'">
            <label class="block font-medium text-slate-200 mb-1">{{ t('agentTypeLabel') }}</label>
            <select
              v-model="newTask.agent_type"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100"
            >
              <option value="default">Agent (full tools)</option>
              <option value="review">Reviewer (read-only)</option>
              <option value="summarizer">Summarizer (no tools)</option>
            </select>
          </div>

          <div v-else-if="selectedExecutorMode === 'device'">
            <label class="block font-medium text-slate-200 mb-1">{{ t('targetDeviceLabel') }}</label>
            <select
              v-model="selectedDeviceKey"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100"
            >
              <option v-if="connectedDevices.length === 0" value="" disabled>{{ t('noDevicesConnected') }}</option>
              <option
                v-for="dev in connectedDevices"
                :key="`${dev.device_id}:${dev.workspace_id}`"
                :value="`${dev.device_id}:${dev.workspace_id}`"
              >
                {{ dev.device_id }} · {{ dev.workspace_id }}{{ dev.can_delegate ? '' : ' (locked)' }}
              </option>
            </select>
          </div>

          <div v-else>
            <label class="block font-medium text-slate-200 mb-1">{{ t('execModelLabel') }}</label>
            <button
              type="button"
              class="w-full min-h-[44px] lg:min-h-[36px] flex items-center justify-between px-3 py-2 rounded-lg bg-botex-950 hover:bg-botex-900 border border-botex-700 text-left transition"
              @click="showModelSelector = true"
            >
              <div class="min-w-0 pr-2">
                <div class="text-xs font-mono text-slate-100 truncate">{{ newTask.model }}</div>
                <div class="text-xs font-mono text-slate-400 uppercase">{{ newTask.provider || 'openrouter' }}</div>
              </div>
              <span class="px-2 py-0.5 rounded bg-botex-800 border border-botex-700 text-xs font-mono text-cyan-300 shrink-0">
                {{ locale === 'pl' ? 'Zmień' : 'Browse' }}
              </span>
            </button>
          </div>
        </div>

        <!-- Controlled Device Delegation Panel -->
        <div v-if="selectedExecutorMode === 'device'" class="space-y-3 p-3 bg-botex-950 rounded-lg border border-botex-800">
          <div v-if="!selectedDevice || !selectedDevice.can_delegate" class="text-xs text-amber-300 font-mono">
            {{ t('deviceOptInDisabled') }}
          </div>
          <template v-else>
            <div>
              <div class="flex items-center justify-between mb-1">
                <label class="font-medium text-slate-200">{{ t('deviceSkillLabel') }}</label>
                <Badge v-if="selectedDeviceSkill" :variant="selectedDeviceSkill.read_only ? 'success' : 'warning'">
                  {{ selectedDeviceSkill.read_only ? t('deviceReadOnlyBadge') : t('deviceMutationBadge') }}
                </Badge>
              </div>
              <select
                v-model="deviceDelegateForm.skill"
                class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-900 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100"
              >
                <option v-if="availableDeviceSkills.length === 0" value="" disabled>{{ t('deviceNoSkills') }}</option>
                <option v-for="sk in availableDeviceSkills" :key="sk.name" :value="sk.name">
                  {{ sk.name }}{{ sk.description ? ` — ${sk.description}` : '' }}
                </option>
              </select>
              <p v-if="selectedDeviceSkill" class="mt-1 text-xs font-mono text-slate-400">
                {{ t('deviceSkillDigest') }}: {{ selectedDeviceSkill.sha256.slice(0, 16) }}…
              </p>
            </div>

            <div>
              <label class="block font-medium text-slate-200 mb-1">{{ t('deviceParamsLabel') }}</label>
              <textarea
                v-model="deviceDelegateForm.paramsJson"
                rows="3"
                placeholder="{}"
                class="w-full rounded-lg bg-botex-900 border border-botex-700 text-slate-100 placeholder-slate-500 p-2 text-xs font-mono focus:outline-none focus:border-cyan-500"
              ></textarea>
            </div>

            <label class="inline-flex items-center space-x-2 text-slate-200 cursor-pointer min-h-[36px]">
              <input
                v-model="deviceDelegateForm.requireApproval"
                :disabled="selectedDeviceSkill ? !selectedDeviceSkill.read_only : false"
                type="checkbox"
                class="w-4 h-4 rounded bg-botex-900 border-botex-700 text-cyan-500"
              />
              <span>{{ t('deviceRequireApproval') }}</span>
            </label>
          </template>
          <p v-if="deviceDelegateError" class="text-xs font-mono text-rose-400">{{ deviceDelegateError }}</p>
        </div>

        <!-- Mode (botex executor only) -->
        <div v-if="selectedExecutorMode === 'botex'" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block font-medium text-slate-200 mb-1">{{ t('permModeLabel') }}</label>
            <select
              v-model="newTask.mode"
              class="w-full min-h-[44px] lg:min-h-[36px] bg-botex-950 border border-botex-700 rounded-lg p-2 text-base sm:text-xs text-slate-100"
            >
              <option value="edit">Edit (Modify workspace files)</option>
              <option value="readonly">Read-Only (No disk mutations)</option>
              <option value="full">Full (Edits & shell execution)</option>
              <option value="destructive">Destructive (Allow file deletion)</option>
            </select>
          </div>
        </div>

        <!-- Capability Toggles (botex executor only) -->
        <div v-if="selectedExecutorMode === 'botex'" class="p-3 bg-botex-950 rounded-lg border border-botex-800 space-y-2">
          <span class="block font-mono text-xs font-semibold text-slate-300 uppercase">
            {{ locale === 'pl' ? 'Uprawnienia wykonawcze' : 'Capabilities & Guards' }}
          </span>
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-2">
            <label class="inline-flex items-center space-x-2 text-slate-200 cursor-pointer min-h-[44px] lg:min-h-[36px]">
              <input v-model="newTask.allow_exec" type="checkbox" class="w-5 h-5 shrink-0 rounded bg-botex-900 border-botex-700 text-cyan-500" />
              <span>Allow Exec</span>
            </label>
            <label class="inline-flex items-center space-x-2 text-slate-200 cursor-pointer min-h-[44px] lg:min-h-[36px]">
              <input v-model="newTask.allow_net" type="checkbox" class="w-5 h-5 shrink-0 rounded bg-botex-900 border-botex-700 text-cyan-500" />
              <span>Allow Net (SSRF safe)</span>
            </label>
            <label class="inline-flex items-center space-x-2 text-slate-200 cursor-pointer min-h-[44px] lg:min-h-[36px]">
              <input v-model="newTask.allow_destructive" type="checkbox" class="w-5 h-5 shrink-0 rounded bg-botex-900 border-botex-700 text-rose-500" />
              <span class="text-rose-300">Allow Destructive</span>
            </label>
          </div>
        </div>

        <!-- Budget & Turns (botex executor only — the devin harness runs its own loop) -->
        <div v-if="selectedExecutorMode === 'botex'" class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <Input
            v-model="newTask.budget_limit_usd"
            type="number"
            :label="t('budgetLimitLabel')"
            placeholder="0.50 (-1 = no limit)"
            hint="-1 = no limit"
          />
          <Input
            v-model="newTask.max_turns"
            type="number"
            :label="t('maxTurnsLabel')"
            placeholder="20 (0 = default)"
            hint="0 = harness default"
          />
        </div>
      </form>

      <template #footer>
        <Button variant="secondary" size="sm" @click="showCreateModal = false">{{ t('cancel') }}</Button>
        <Button
          variant="primary"
          size="sm"
          :loading="tasksStore.isCreating"
          @click="handleCreateTask"
        >
          {{ t('launchTask') }}
        </Button>
      </template>
    </Modal>

    <!-- Dynamic Model Selector Modal -->
    <ModelSelectorModal
      :is-open="showModelSelector"
      :models="chatStore.catalogModels"
      :is-loading="chatStore.isLoadingModels"
      :selected-model="newTask.model || chatStore.selectedModel"
      :selected-provider="newTask.provider || chatStore.selectedProvider"
      :catalog-stale="chatStore.catalogStale"
      @close="showModelSelector = false"
      @select="onSelectTaskModel"
      @refresh="chatStore.fetchCatalogModels(true)"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useTasksStore } from '../stores/tasks'
import { useChatStore } from '../stores/chat'
import { useApprovalsStore } from '../stores/approvals'
import { useI18n } from '../i18n'
import type {
  CatalogModel,
  ConnectedDeviceInfo,
  ConnectedDeviceSkill,
  TaskCreateRequest,
  TaskDetail,
  TaskSummary
} from '../api/types'
import { api } from '../api/client'
import Modal from '../components/common/Modal.vue'
import Button from '../components/common/Button.vue'
import Badge from '../components/common/Badge.vue'
import Input from '../components/common/Input.vue'
import ModelSelectorModal from '../components/chat/ModelSelectorModal.vue'
import TaskDetailPanel from '../components/tasks/TaskDetailPanel.vue'
import { statusDotColor, statusBadgeVariant } from '../components/tasks/statusMaps'
import {
  Cpu as CpuIcon,
  Plus as PlusIcon,
  RefreshCw as RefreshCwIcon,
  Loader2 as Loader2Icon,
  ChevronRight as ChevronRightIcon
} from 'lucide-vue-next'

const tasksStore = useTasksStore()
const chatStore = useChatStore()
const approvalsStore = useApprovalsStore()
const { tasks, currentTask } = storeToRefs(tasksStore)
const { locale, t } = useI18n()

const showCreateModal = ref(false)
const showModelSelector = ref(false)
const showDetailModal = ref(false)
const selectedTaskId = ref<string>('')

// <lg the detail lives in a bottom-sheet modal; >=lg it pins in the side column.
const lgQuery = typeof window !== 'undefined' ? window.matchMedia('(min-width: 1024px)') : null
const isDesktop = ref<boolean>(lgQuery?.matches ?? true)
const onLgChange = (e: MediaQueryListEvent) => {
  isDesktop.value = e.matches
  if (e.matches) showDetailModal.value = false
}

const newTask = ref<TaskCreateRequest>({
  task: chatStore.pendingTaskDraft || '',
  model: chatStore.selectedModel || 'qwen/qwen3-coder',
  provider: chatStore.selectedProvider || 'openrouter',
  mode: 'edit',
  workspace_dir: '.',
  allow_exec: false,
  allow_net: false,
  allow_destructive: false,
  allow_non_zdr: false,
  budget_limit_usd: -1,
  max_turns: 0,
  executor: 'botex',
  agent_type: 'default'
})

const selectedExecutorMode = ref<'botex' | 'devin' | 'device'>('botex')
const connectedDevices = ref<ConnectedDeviceInfo[]>([])
const selectedDeviceKey = ref<string>('')
const deviceDelegateForm = ref<{ skill: string; paramsJson: string; requireApproval: boolean }>({
  skill: '',
  paramsJson: '{}',
  requireApproval: false
})
const deviceDelegateError = ref<string>('')

const selectedDevice = computed<ConnectedDeviceInfo | null>(() => {
  return (
    connectedDevices.value.find(
      (d) => `${d.device_id}:${d.workspace_id}` === selectedDeviceKey.value
    ) || null
  )
})

const availableDeviceSkills = computed<ConnectedDeviceSkill[]>(() => {
  return selectedDevice.value?.capabilities?.delegation?.skills || []
})

const selectedDeviceSkill = computed<ConnectedDeviceSkill | null>(() => {
  return (
    availableDeviceSkills.value.find((s) => s.name === deviceDelegateForm.value.skill) || null
  )
})

watch(selectedDevice, (dev) => {
  if (!dev) {
    deviceDelegateForm.value.skill = ''
    return
  }
  const skills = dev.capabilities?.delegation?.skills || []
  if (skills.length > 0 && !skills.some((s) => s.name === deviceDelegateForm.value.skill)) {
    deviceDelegateForm.value.skill = skills[0].name
  }
})

watch(selectedDeviceSkill, (sk) => {
  if (sk && !sk.read_only) {
    deviceDelegateForm.value.requireApproval = true
  }
})

watch(selectedExecutorMode, (val) => {
  if (val === 'botex' || val === 'devin') {
    newTask.value.executor = val
  }
})

async function refreshConnectedDevices() {
  try {
    const res = await api.listConnectedDevices()
    connectedDevices.value = res.devices || []
    if (!selectedDeviceKey.value && connectedDevices.value.length > 0) {
      const preferred = connectedDevices.value.find((d) => d.can_delegate) || connectedDevices.value[0]
      selectedDeviceKey.value = `${preferred.device_id}:${preferred.workspace_id}`
    }
  } catch {
    connectedDevices.value = []
  }
}

function openDeviceDelegateModal(dev: ConnectedDeviceInfo) {
  selectedDeviceKey.value = `${dev.device_id}:${dev.workspace_id}`
  selectedExecutorMode.value = 'device'
  deviceDelegateError.value = ''
  showCreateModal.value = true
}

const devinStatus = ref<Awaited<ReturnType<typeof api.fetchDevinExecutorStatus>> | null>(null)

async function refreshDevinStatus() {
  try {
    const s = await api.fetchDevinExecutorStatus()
    devinStatus.value = s
  } catch (err: any) {
    devinStatus.value = {
      enabled: false,
      available: false,
      exe_resolved: false,
      credential_resolved: false,
      reason: err?.detail || err?.message || 'probe_failed'
    }
  }
}

watch(showCreateModal, (open) => {
  if (open) {
    deviceDelegateError.value = ''
    refreshDevinStatus()
    refreshConnectedDevices()
  }
})

onMounted(() => {
  lgQuery?.addEventListener('change', onLgChange)
  tasksStore.fetchTasks()
  tasksStore.startPolling()
  if (chatStore.catalogModels.length === 0) {
    chatStore.fetchCatalogModels()
  }
  refreshDevinStatus()
  refreshConnectedDevices()

  if (chatStore.pendingTaskDraft) {
    newTask.value.task = chatStore.pendingTaskDraft
    newTask.value.model = chatStore.selectedModel
    newTask.value.provider = chatStore.selectedProvider
    chatStore.pendingTaskDraft = ''
    showCreateModal.value = true
  }
})

onUnmounted(() => {
  lgQuery?.removeEventListener('change', onLgChange)
  tasksStore.stopPolling()
})

const activeRunningTask = computed(() => {
  return tasks.value.find((taskItem: TaskSummary) => taskItem.status === 'RUNNING' || taskItem.status === 'QUEUED')
})

function onSelectTaskModel(model: CatalogModel) {
  newTask.value.model = model.id
  newTask.value.provider = model.provider
  newTask.value.allow_non_zdr = !model.is_zdr_compliant || model.is_free
  chatStore.selectModel(model.id, model.provider)
}

function inspectTask(id: string) {
  selectedTaskId.value = id
  tasksStore.fetchTask(id)
  if (!isDesktop.value) showDetailModal.value = true
}

async function cancelTask(id: string) {
  if (confirm(`Abort task ${id}?`)) {
    await tasksStore.cancelTask(id)
  }
}

function retryTask(task: TaskDetail) {
  if (!task.request) return
  selectedExecutorMode.value = (task.request.executor as 'botex' | 'devin') || 'botex'
  newTask.value = {
    ...newTask.value,
    ...task.request,
    task: task.request.task || task.task || newTask.value.task
  }
  showCreateModal.value = true
}

function hasForbiddenCommandKey(obj: unknown, depth = 0): boolean {
  if (depth > 8 || !obj || typeof obj !== 'object') return false
  if (Array.isArray(obj)) {
    return obj.some((item) => hasForbiddenCommandKey(item, depth + 1))
  }
  const blocked = new Set([
    'command',
    'cmd',
    'shell',
    'script',
    'code',
    'eval',
    'exec',
    'argv',
    'args',
    'powershell',
    'pwsh',
    'bash'
  ])
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    if (blocked.has(k.normalize('NFKC').trim().toLowerCase()) || hasForbiddenCommandKey(v, depth + 1)) {
      return true
    }
  }
  return false
}

async function handleCreateTask() {
  if (selectedExecutorMode.value === 'device') {
    deviceDelegateError.value = ''
    const dev = selectedDevice.value
    const sk = selectedDeviceSkill.value
    if (!dev || !dev.can_delegate || !sk) {
      deviceDelegateError.value = t('deviceOptInDisabled')
      return
    }
    let parsedParams: Record<string, unknown> = {}
    try {
      const rawText = (deviceDelegateForm.value.paramsJson || '{}').trim() || '{}'
      const parsed = JSON.parse(rawText)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        deviceDelegateError.value = 'Parameters must be a JSON object.'
        return
      }
      if (hasForbiddenCommandKey(parsed)) {
        deviceDelegateError.value = 'Forbidden parameter key rejected.'
        return
      }
      parsedParams = parsed as Record<string, unknown>
    } catch {
      deviceDelegateError.value = 'Invalid JSON parameters.'
      return
    }
    try {
      const res = await api.delegateToDevice({
        device_id: dev.device_id,
        workspace_id: dev.workspace_id,
        skill: sk.name,
        sha256: sk.sha256,
        params: parsedParams,
        task: newTask.value.task.trim(),
        require_approval: deviceDelegateForm.value.requireApproval
      })
      showCreateModal.value = false
      selectedTaskId.value = res.task_id
      await tasksStore.fetchTasks()
      await tasksStore.fetchTask(res.task_id)
      if (res.approval_required) {
        await approvalsStore.fetchApprovals(res.task_id)
      }
    } catch (err: any) {
      deviceDelegateError.value = err?.detail || err?.message || 'Delegation failed.'
    }
    return
  }

  if (!newTask.value.task.trim()) return
  const createdId = await tasksStore.createTask({
    ...newTask.value,
    executor: selectedExecutorMode.value,
    budget_limit_usd: Number(newTask.value.budget_limit_usd),
    max_turns: Number(newTask.value.max_turns)
  })
  if (createdId) {
    showCreateModal.value = false
    selectedTaskId.value = createdId
  }
}

function formatDate(ts?: number | null) {
  if (!ts) return '-'
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatDuration(start: number, end: number | null) {
  if (!end) return 'running'
  const diff = Math.max(0, Math.round(end - start))
  return `${diff}s`
}
</script>
