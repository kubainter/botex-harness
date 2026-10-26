<template>
  <div class="w-full overflow-x-auto">
    <svg
      :viewBox="`0 0 ${W} ${H}`"
      :width="W"
      :height="H"
      class="min-w-[320px] w-full h-auto select-none"
      role="img"
      :aria-label="t('dailyTitle')"
    >
      <!-- baseline -->
      <line :x1="padL" :y1="H - padB" :x2="W - padR" :y2="H - padB" class="stroke-botex-700" stroke-width="1" />
      <!-- bars -->
      <g v-for="(d, i) in points" :key="d.date">
        <rect
          :x="barX(i)"
          :y="barY(d.cost_usd)"
          :width="barW"
          :height="barH(d.cost_usd)"
          rx="2"
          class="fill-cyan-500/70 hover:fill-cyan-400 transition-colors"
        >
          <title>{{ d.date }} — ${{ d.cost_usd.toFixed(4) }} · {{ d.runs }} {{ t('colRuns').toLowerCase() }}</title>
        </rect>
        <!-- day label every ~7th or all if few -->
        <text
          v-if="labelEvery && i % labelEvery === 0"
          :x="barX(i) + barW / 2"
          :y="H - padB + 14"
          text-anchor="middle"
          class="fill-slate-400"
          font-size="9"
        >
          {{ d.date.slice(5) }}
        </text>
      </g>
      <!-- max value label -->
      <text v-if="maxCost > 0" :x="padL" :y="padT - 2" class="fill-slate-400" font-size="9">
        ${{ maxCost.toFixed(4) }}
      </text>
    </svg>
    <p v-if="!points.length" class="text-xs text-slate-400 py-6 text-center">{{ t('noStats') }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '../../i18n'
import type { AnalyticsDailyPoint } from '../../api/types'

const props = defineProps<{ data: AnalyticsDailyPoint[] }>()
const { t } = useI18n()

const W = 640
const H = 150
const padL = 8
const padR = 8
const padT = 14
const padB = 22

const points = computed(() => props.data || [])
const maxCost = computed(() => Math.max(0, ...points.value.map((d) => d.cost_usd)))
const plotH = computed(() => H - padT - padB)
const slotW = computed(() => (W - padL - padR) / Math.max(points.value.length, 1))
const barW = computed(() => Math.max(4, Math.min(28, slotW.value * 0.66)))
const labelEvery = computed(() => {
  const n = points.value.length
  return n > 21 ? 7 : n > 10 ? 3 : 1
})

const barX = (i: number) => padL + slotW.value * i + (slotW.value - barW.value) / 2
const barH = (c: number) => (maxCost.value > 0 ? Math.max(2, (c / maxCost.value) * plotH.value) : 0)
const barY = (c: number) => H - padB - barH(c)
</script>
