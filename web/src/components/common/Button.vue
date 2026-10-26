<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    :class="[
      'inline-flex items-center justify-center font-medium rounded-lg transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-offset-botex-900 active:scale-[0.98] disabled:opacity-50 disabled:pointer-events-none disabled:active:scale-100',
      sizeClasses,
      variantClasses,
      customClass
    ]"
    @click="$emit('click', $event)"
  >
    <svg
      v-if="loading"
      class="animate-spin -ml-1 mr-2 h-4 w-4 text-current"
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
    >
      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
    </svg>
    <slot />
  </button>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'success' | 'danger' | 'ghost' | 'amber'
    size?: 'sm' | 'md' | 'lg'
    type?: 'button' | 'submit' | 'reset'
    disabled?: boolean
    loading?: boolean
    customClass?: string
  }>(),
  {
    variant: 'primary',
    size: 'md',
    type: 'button',
    disabled: false,
    loading: false,
    customClass: ''
  }
)

defineEmits<{
  (e: 'click', event: MouseEvent): void
}>()

const sizeClasses = computed(() => {
  switch (props.size) {
    case 'sm':
      return 'min-h-[44px] lg:min-h-[36px] px-2.5 py-1.5 text-xs'
    case 'lg':
      return 'min-h-[44px] px-5 py-2.5 text-base'
    default:
      return 'min-h-[44px] lg:min-h-[36px] px-3.5 py-2 text-sm'
  }
})

const variantClasses = computed(() => {
  switch (props.variant) {
    case 'secondary':
      return 'bg-botex-800 hover:bg-botex-700 text-slate-200 border border-botex-700 focus:ring-slate-500'
    case 'success':
      return 'bg-emerald-600 hover:bg-emerald-500 text-white shadow-sm focus:ring-emerald-500'
    case 'danger':
      return 'bg-rose-600 hover:bg-rose-500 text-white shadow-sm focus:ring-rose-500'
    case 'amber':
      return 'bg-amber-600 hover:bg-amber-500 text-white shadow-sm focus:ring-amber-500'
    case 'ghost':
      return 'bg-transparent hover:bg-slate-800 text-slate-300 hover:text-white focus:ring-slate-500'
    default:
      return 'bg-brand-blue hover:bg-blue-600 text-white shadow-sm focus:ring-blue-500'
  }
})
</script>
