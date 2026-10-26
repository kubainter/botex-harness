<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="isOpen"
        class="fixed inset-0 z-50 overflow-y-auto overscroll-contain bg-botex-950/85 backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-6"
        @click.self="onBackdropClick"
        @keydown.esc="onEsc"
      >
        <Transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="opacity-0 translate-y-4 sm:scale-95 sm:translate-y-2"
          enter-to-class="opacity-100 translate-y-0 sm:scale-100"
          leave-active-class="transition duration-100 ease-in"
          leave-from-class="opacity-100 translate-y-0 sm:scale-100"
          leave-to-class="opacity-0 translate-y-4 sm:scale-95 sm:translate-y-2"
        >
          <div
            v-if="isOpen"
            :class="[
              'relative w-full rounded-t-2xl sm:rounded-xl bg-botex-900 border-t sm:border border-botex-700 shadow-2xl text-slate-100 flex flex-col max-h-[92dvh] sm:max-h-[90dvh] px-safe pb-safe overflow-hidden',
              maxWidthClass
            ]"
          >
            <!-- Header -->
            <div class="px-4 sm:px-5 py-3.5 sm:py-4 border-b border-botex-800 flex items-center justify-between gap-3">
              <div>
                <slot name="title">
                  <h3 class="text-sm sm:text-base font-semibold leading-6 text-slate-100">{{ title }}</h3>
                </slot>
                <p v-if="subtitle" class="mt-0.5 text-xs text-slate-300">{{ subtitle }}</p>
              </div>
              <button
                type="button"
                aria-label="Close modal"
                class="min-w-[44px] min-h-[44px] lg:min-w-[36px] lg:min-h-[36px] rounded-lg p-1.5 text-slate-300 hover:text-white hover:bg-botex-800 transition flex items-center justify-center"
                @click="$emit('close')"
              >
                <XIcon class="w-5 h-5" />
              </button>
            </div>

            <!-- Body -->
            <div class="px-4 sm:px-5 py-4 overflow-y-auto overscroll-contain flex-1">
              <slot />
            </div>

            <!-- Footer -->
            <div v-if="$slots.footer" class="px-4 sm:px-5 py-3.5 bg-botex-950/70 border-t border-botex-800 flex items-center justify-end space-x-3">
              <slot name="footer" />
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { X as XIcon } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    isOpen: boolean
    title?: string
    subtitle?: string
    maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '4xl'
    closeOnBackdrop?: boolean
  }>(),
  {
    title: '',
    maxWidth: 'lg',
    closeOnBackdrop: true
  }
)

const emit = defineEmits<{
  (e: 'close'): void
}>()

const maxWidthClass = computed(() => {
  switch (props.maxWidth) {
    case 'sm':
      return 'max-w-sm'
    case 'md':
      return 'max-w-md'
    case 'xl':
      return 'max-w-xl'
    case '2xl':
      return 'max-w-2xl'
    case '4xl':
      return 'max-w-4xl'
    default:
      return 'max-w-lg'
  }
})

function onBackdropClick() {
  if (props.closeOnBackdrop) {
    emit('close')
  }
}

function onEsc(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    emit('close')
  }
}

onMounted(() => {
  window.addEventListener('keydown', onEsc)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onEsc)
})
</script>
