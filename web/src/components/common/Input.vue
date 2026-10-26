<template>
  <div class="w-full">
    <label v-if="label" class="block text-xs font-medium text-slate-300 mb-1.5 flex items-center justify-between">
      <span>{{ label }}</span>
      <span v-if="hint" class="text-slate-500 font-normal">{{ hint }}</span>
    </label>
    <div class="relative rounded-lg shadow-sm">
      <input
        :type="type"
        :value="modelValue"
        :placeholder="placeholder"
        :disabled="disabled"
        :class="[
          'block w-full min-h-[44px] lg:min-h-[36px] rounded-lg bg-botex-900 border border-botex-700 text-slate-100 placeholder-slate-500 text-base sm:text-sm px-3 py-2 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-blue focus:border-brand-blue disabled:opacity-50 disabled:bg-botex-950 font-mono',
          inputClass
        ]"
        @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
        @keydown.enter="$emit('enter')"
      />
      <div v-if="$slots.suffix" class="absolute inset-y-0 right-0 pr-3 flex items-center pointer-events-none">
        <slot name="suffix" />
      </div>
    </div>
    <p v-if="error" class="mt-1 text-xs text-rose-400">{{ error }}</p>
  </div>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    modelValue?: string | number | null
    label?: string
    placeholder?: string
    type?: string
    hint?: string
    error?: string
    disabled?: boolean
    inputClass?: string
  }>(),
  {
    modelValue: '',
    type: 'text',
    disabled: false,
    inputClass: ''
  }
)

defineEmits<{
  (e: 'update:modelValue', value: string): void
  (e: 'enter'): void
}>()
</script>
