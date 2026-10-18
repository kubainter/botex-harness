<template>
  <span
    :class="[
      'inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium tracking-wide uppercase font-mono',
      variantClasses
    ]"
  >
    <slot />
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'purple'
  }>(),
  {
    variant: 'default'
  }
)

const variantClasses = computed(() => {
  switch (props.variant) {
    case 'success':
      return 'bg-emerald-950/80 text-emerald-300 border border-emerald-500/40'
    case 'warning':
      return 'bg-amber-950/80 text-amber-300 border border-amber-500/40'
    case 'danger':
      return 'bg-rose-950/80 text-rose-300 border border-rose-500/40'
    case 'info':
      return 'bg-sky-950/80 text-sky-300 border border-sky-500/40'
    case 'purple':
      return 'bg-slate-800 text-sky-200 border border-slate-600'
    default:
      return 'bg-slate-800 text-slate-200 border border-slate-700'
  }
})
</script>
