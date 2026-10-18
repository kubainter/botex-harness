<template>
  <div class="h-full flex flex-col overflow-y-auto bg-botex-950 p-4 sm:p-6 space-y-6">
    <!-- View Header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4">
      <div>
        <div class="flex items-center space-x-3">
          <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
            <ShieldAlertIcon class="w-5 h-5 text-amber-400" />
            <span>{{ t('approvalsTitle') }}</span>
          </h2>
          <Badge v-if="pendingActions.length > 0" variant="warning">
            {{ pendingActions.length }}
          </Badge>
        </div>
        <p class="text-xs text-slate-300 mt-1">
          {{ t('approvalsSubtitle') }}
        </p>
      </div>

      <button
        type="button"
        class="min-h-[40px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5"
        @click="approvalsStore.fetchApprovals()"
      >
        <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': approvalsStore.isLoading }" />
        <span>{{ t('refresh') }}</span>
      </button>
    </div>

    <!-- Empty state -->
    <div
      v-if="pendingActions.length === 0"
      class="rounded-xl border border-botex-800 bg-botex-900/50 p-10 text-center flex flex-col items-center justify-center space-y-3"
    >
      <div class="w-11 h-11 rounded-lg bg-emerald-950/60 border border-emerald-700/50 flex items-center justify-center text-emerald-300">
        <CheckCircle2Icon class="w-5 h-5" />
      </div>
      <h3 class="text-base font-semibold text-slate-100">{{ t('noPendingApprovals') }}</h3>
      <p class="text-xs text-slate-300 max-w-sm leading-relaxed">
        {{ t('noPendingApprovalsDesc') }}
      </p>
    </div>

    <!-- Approvals List -->
    <div v-else class="space-y-4 max-w-4xl">
      <ApprovalCard
        v-for="action in pendingActions"
        :key="action.action_id"
        :action="action"
        @decide="onDecide"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { storeToRefs } from 'pinia'
import { useApprovalsStore } from '../stores/approvals'
import { useI18n } from '../i18n'
import ApprovalCard from '../components/approvals/ApprovalCard.vue'
import Badge from '../components/common/Badge.vue'
import {
  ShieldAlert as ShieldAlertIcon,
  RefreshCw as RefreshCwIcon,
  CheckCircle2 as CheckCircle2Icon
} from 'lucide-vue-next'

const approvalsStore = useApprovalsStore()
const { pendingActions } = storeToRefs(approvalsStore)
const { t } = useI18n()

onMounted(() => {
  approvalsStore.fetchApprovals()
})

async function onDecide(actionId: string, approved: boolean, reason: string, remember: boolean) {
  await approvalsStore.decide(actionId, {
    approved,
    reason,
    remember
  })
}
</script>
