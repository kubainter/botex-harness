<template>
  <Modal
    :is-open="isOpen"
    :title="t('approvalsTitle')"
    :subtitle="t('approvalsSubtitle')"
    max-width="2xl"
    :close-on-backdrop="false"
    @close="$emit('close')"
  >
    <div v-if="action" class="py-1">
      <ApprovalCard
        :action="action"
        compact
        @decide="onDecide"
      />
    </div>
  </Modal>
</template>

<script setup lang="ts">
import type { PendingAction } from '../../api/types'
import { useI18n } from '../../i18n'
import Modal from '../common/Modal.vue'
import ApprovalCard from './ApprovalCard.vue'

defineProps<{
  isOpen: boolean
  action: PendingAction | null
}>()

const emit = defineEmits<{
  (e: 'close'): void
  (e: 'decide', actionId: string, approved: boolean, reason: string, remember: boolean): void
}>()

const { t } = useI18n()

function onDecide(actionId: string, approved: boolean, reason: string, remember: boolean) {
  emit('decide', actionId, approved, reason, remember)
}
</script>
