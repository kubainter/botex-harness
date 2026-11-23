<template>
  <div class="h-full overflow-y-auto bg-botex-950 p-4 sm:p-6">
    <!-- View header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4 mb-6">
      <div>
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <MonitorSmartphoneIcon class="w-5 h-5 text-cyan-400" aria-hidden="true" />
          <span>{{ tr('Edge Hosts', 'Hosty Edge') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">
          {{
            tr(
              `Connected devices, live harness inventory, and active leases.`,
              `Podłączone urządzenia, żywy inwentarz harnessów i aktywne dzierżawy.`
            )
          }}
        </p>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-[11px] font-mono text-slate-300 bg-botex-900 border border-botex-800 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5">
          <RadioIcon class="w-3.5 h-3.5" :class="swarm.devices.length ? 'text-emerald-400' : 'text-slate-500'" aria-hidden="true" />
          {{ swarm.devices.length }} {{ tr('online', 'online') }}
        </span>
        <span class="text-[11px] font-mono text-slate-300 bg-botex-900 border border-botex-800 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5">
          <KeyIcon class="w-3.5 h-3.5 text-amber-400" aria-hidden="true" />
          {{ swarm.activeLeasesCount }} {{ tr('leases', 'dzierżaw') }}
        </span>
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[32px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60"
          @click="refreshAll()"
        >
          <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': swarm.loadingHosts }" aria-hidden="true" />
          <span>{{ tr('Refresh', 'Odśwież') }}</span>
        </button>
      </div>
    </div>

    <!-- Global error -->
    <div
      v-if="swarm.error"
      role="alert"
      class="mb-4 p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ swarm.error }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="refreshAll()">
        {{ tr('Retry', 'Ponów') }}
      </button>
    </div>

    <!-- Loading skeleton -->
    <div v-if="swarm.loadingHosts && !swarm.devices.length" class="grid grid-cols-1 xl:grid-cols-2 gap-4" aria-busy="true">
      <div v-for="i in 2" :key="i" class="rounded-xl border border-botex-800 bg-botex-900 p-4 space-y-3">
        <div class="h-4 w-1/3 rounded bg-botex-950 animate-pulse" />
        <div class="h-3 w-1/2 rounded bg-botex-950 animate-pulse" />
        <div class="h-3 w-2/3 rounded bg-botex-950 animate-pulse" />
      </div>
    </div>

    <!-- Devices -->
    <div v-else-if="swarm.devices.length" class="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
      <article
        v-for="device in swarm.devices"
        :key="device.device_id"
        class="rounded-xl border border-botex-800 bg-botex-900 shadow-sm overflow-hidden"
      >
        <!-- Device header -->
        <div class="p-4 space-y-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="text-sm font-bold font-mono text-slate-100 break-all">{{ device.device_id }}</h3>
                <Badge :variant="device.healthy ? 'success' : 'danger'">
                  {{ device.healthy ? tr('healthy', 'sprawny') : tr('unhealthy', 'niesprawny') }}
                </Badge>
                <Badge v-if="device.can_delegate" variant="info">{{ tr('delegable', 'delegowalny') }}</Badge>
              </div>
              <p class="text-[11px] font-mono text-slate-400 mt-1 truncate">
                {{ tr('workspace', 'przestrzeń') }}: {{ device.workspace_id }}
              </p>
            </div>
            <span
              class="shrink-0 h-2.5 w-2.5 rounded-full mt-1"
              :class="device.healthy ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'"
              :title="device.healthy ? tr('healthy', 'sprawny') : tr('unhealthy', 'niesprawny')"
            />
          </div>

          <dl class="flex flex-wrap gap-x-5 gap-y-1.5 text-[11px] font-mono text-slate-400">
            <div class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">{{ tr('seen', 'widziany') }}</dt>
              <dd class="text-slate-200">{{ formatRelativeMs(device.last_seen_at) }}</dd>
            </div>
            <div class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">{{ tr('uptime', 'połączono') }}</dt>
              <dd class="text-slate-200">{{ formatRelativeMs(device.connected_at) }}</dd>
            </div>
            <div v-if="device.pending_calls > 0" class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">{{ tr('pending', 'w toku') }}</dt>
              <dd class="text-amber-300">{{ device.pending_calls }}</dd>
            </div>
            <div v-if="device.client_version" class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">ver</dt>
              <dd class="text-slate-200 truncate max-w-[140px]">{{ device.client_version }}</dd>
            </div>
          </dl>

          <!-- Capability pills -->
          <div v-if="deviceCapabilityPills(device).length" class="flex flex-wrap gap-1.5">
            <span
              v-for="pill in deviceCapabilityPills(device)"
              :key="pill.label"
              class="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-mono tracking-wide border"
              :class="pill.on
                ? 'bg-emerald-950/70 text-emerald-300 border-emerald-500/40'
                : 'bg-botex-950 text-slate-300 border-botex-700'"
              :title="pill.title"
            >
              {{ pill.label }}
            </span>
          </div>
        </div>

        <!-- Harness inventory -->
        <div class="border-t border-botex-800">
          <button
            type="button"
            :aria-expanded="expandedHosts.has(device.device_id)"
            class="w-full min-h-[44px] px-4 py-2 flex items-center justify-between gap-2 text-left hover:bg-botex-800/40 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-500/60"
            @click="toggleHarnesses(device.device_id)"
          >
            <span class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
              <CpuIcon class="w-3.5 h-3.5" aria-hidden="true" />
              {{ tr('Harnesses', 'Harnessy') }}
              <span v-if="harnessCounts(device.device_id)" class="font-normal normal-case tracking-normal text-slate-400">
                {{ harnessCounts(device.device_id) }}
              </span>
              <RefreshCwIcon
                v-if="swarm.loadingHarnessHost.has(device.device_id)"
                class="w-3 h-3 animate-spin text-cyan-300"
                aria-hidden="true"
              />
            </span>
            <ChevronDownIcon
              class="w-4 h-4 text-slate-400 transition-transform"
              :class="expandedHosts.has(device.device_id) ? '' : '-rotate-90'"
              aria-hidden="true"
            />
          </button>

          <div v-if="expandedHosts.has(device.device_id)" class="px-4 pb-4 space-y-2">
            <div
              v-if="swarm.harnessErrors[device.device_id]"
              role="alert"
              class="p-2.5 rounded-lg bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 font-mono"
            >
              {{ swarm.harnessErrors[device.device_id] }}
            </div>
            <div v-if="!swarm.loadingHarnessHost.has(device.device_id) && !(swarm.harnessesByHost[device.device_id] || []).length" class="py-3 text-center text-xs text-slate-400">
              {{ tr('No harnesses reported by this host.', 'Ten host nie zgłosił żadnych harnessów.') }}
            </div>
            <div
              v-for="harness in swarm.harnessesByHost[device.device_id] || []"
              :key="harness.harness_id"
              class="rounded-lg border border-botex-800 bg-botex-950 p-3 space-y-2"
            >
              <div class="flex items-center justify-between gap-2">
                <span class="flex items-center gap-2 min-w-0">
                  <span class="h-2 w-2 shrink-0 rounded-full" :class="harnessDotClass(harness.status)" :title="harness.status" />
                  <span class="font-mono text-xs font-semibold text-slate-100 truncate">{{ harness.harness_id }}</span>
                </span>
                <span class="flex items-center gap-2 shrink-0">
                  <span class="text-[10px] font-mono text-slate-400 bg-botex-900 border border-botex-800 rounded px-1.5 py-0.5">{{ harness.transport }}</span>
                  <Badge :variant="harnessStatusVariant(harness.status)">{{ harness.status }}</Badge>
                </span>
              </div>
              <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[11px] font-mono text-slate-400">
                <span>
                  {{ tr('last seen', 'ostatnio widziany') }}
                  <span :class="harness.status === 'stale' || harness.status === 'offline' ? 'text-amber-300' : 'text-slate-200'">
                    {{ harnessAge(harness) }}
                  </span>
                </span>
                <span v-if="capabilityPills(harness.capabilities).length" class="flex flex-wrap gap-1.5 w-full pt-1">
                  <span
                    v-for="pill in capabilityPills(harness.capabilities)"
                    :key="pill.label"
                    class="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-mono tracking-wide border bg-botex-900 text-slate-300 border-botex-700"
                  >
                    {{ pill.label }}
                  </span>
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- Active leases on this host -->
        <div v-if="(swarm.leasesByHost[device.device_id] || []).length" class="border-t border-botex-800 px-4 py-3 space-y-1.5">
          <div class="text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300 flex items-center gap-1.5">
            <KeyIcon class="w-3 h-3 text-amber-400" aria-hidden="true" />
            {{ tr('Active leases', 'Aktywne dzierżawy') }}
          </div>
          <div
            v-for="lease in swarm.leasesByHost[device.device_id]"
            :key="lease.lease_key"
            class="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] font-mono"
          >
            <span class="text-slate-100">{{ lease.bot_slug }}</span>
            <span class="text-slate-400 truncate min-w-0 max-w-[220px]" :title="lease.scope_path">{{ lease.scope_path }}</span>
            <span class="ml-auto" :class="leaseCountdownClass(lease.expires_at)">{{ formatCountdown(lease.expires_at) }}</span>
          </div>
        </div>
      </article>
    </div>

    <!-- Empty state -->
    <div v-else class="rounded-xl border border-botex-800 bg-botex-900/40 p-10 text-center">
      <div class="mx-auto w-11 h-11 rounded-full bg-botex-950 border border-botex-800 flex items-center justify-center">
        <ServerIcon class="w-5 h-5 text-slate-400" aria-hidden="true" />
      </div>
      <h3 class="mt-4 text-sm font-semibold text-slate-200">
        {{ tr('No Edge hosts connected', 'Brak podłączonych hostów Edge') }}
      </h3>
      <p class="mt-1.5 text-xs text-slate-400 max-w-sm mx-auto">
        {{
          tr(
            'Connect a device with an Edge runner to monitor its harnesses and leases here.',
            'Podłącz urządzenie z runnerem Edge, aby monitorować tutaj jego harnessy i dzierżawy.'
          )
        }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import { useSwarmStore } from '../stores/swarm'
import { useI18n } from '../i18n'
import type { ConnectedDeviceInfo, HostHarnessInfo } from '../api/types'
import Badge from '../components/common/Badge.vue'
import {
  ChevronDown as ChevronDownIcon,
  Cpu as CpuIcon,
  KeyRound as KeyIcon,
  MonitorSmartphone as MonitorSmartphoneIcon,
  Radio as RadioIcon,
  RefreshCw as RefreshCwIcon,
  Server as ServerIcon
} from 'lucide-vue-next'

const swarm = useSwarmStore()
const { locale } = useI18n()

function tr(en: string, pl: string): string {
  return locale.value === 'pl' ? pl : en
}

const expandedHosts = ref<Set<string>>(new Set())

function toggleHarnesses(hostId: string): void {
  if (expandedHosts.value.has(hostId)) {
    expandedHosts.value.delete(hostId)
    return
  }
  expandedHosts.value.add(hostId)
  if (!swarm.harnessesByHost[hostId]) void swarm.fetchHostHarnesses(hostId)
}

function refreshAll(): void {
  void swarm.fetchHosts()
  void swarm.fetchLeases()
  for (const hostId of expandedHosts.value) void swarm.fetchHostHarnesses(hostId)
}

type Pill = { label: string; on?: boolean; title?: string }

function capabilityPills(caps: Record<string, unknown>): Pill[] {
  const out: Pill[] = []
  for (const [key, value] of Object.entries(caps)) {
    if (value === true) out.push({ label: key, on: true })
    else if (typeof value === 'number') out.push({ label: `${key}: ${value}` })
    else if (typeof value === 'string' && value && value.length <= 18) out.push({ label: `${key}: ${value}` })
  }
  return out.slice(0, 8)
}

function deviceCapabilityPills(device: ConnectedDeviceInfo): Pill[] {
  const caps = device.capabilities
  if (!caps) return []
  const pills: Pill[] = []
  if (caps.allow_exec) pills.push({ label: tr('exec', 'exec'), on: true })
  if (caps.allow_destructive) pills.push({ label: tr('destructive', 'destrukcja'), on: true })
  const delegation = caps.delegation
  if (delegation?.enabled) {
    const names = (delegation.skills || []).map((s) => s.name).join(', ')
    pills.push({
      label: tr(`delegation: ${delegation.skills?.length ?? 0} skills`, `delegacja: ${delegation.skills?.length ?? 0} umiejętności`),
      title: names || undefined
    })
  }
  if (Array.isArray(caps.allowed_tools) && caps.allowed_tools.length) {
    pills.push({ label: tr(`${caps.allowed_tools.length} tools`, `${caps.allowed_tools.length} narzędzi`) })
  }
  if (typeof caps.max_output_bytes === 'number' && caps.max_output_bytes > 0) {
    pills.push({ label: `${(caps.max_output_bytes / 1024).toFixed(0)} KiB out` })
  }
  return pills.slice(0, 8)
}

function harnessStatusVariant(status: string): 'success' | 'warning' | 'danger' | 'default' {
  switch (status) {
    case 'online':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'offline':
      return 'danger'
    default:
      return 'default'
  }
}

function harnessDotClass(status: string): string {
  switch (status) {
    case 'online':
      return 'bg-emerald-400 animate-pulse'
    case 'degraded':
      return 'bg-amber-400'
    case 'offline':
      return 'bg-rose-400'
    default:
      return 'bg-slate-500'
  }
}

function harnessCounts(hostId: string): string {
  const list = swarm.harnessesByHost[hostId]
  if (!list || !list.length) return ''
  const counts = new Map<string, number>()
  for (const h of list) counts.set(h.status, (counts.get(h.status) || 0) + 1)
  return [...counts.entries()].map(([s, n]) => `${n} ${s}`).join(' · ')
}

function harnessAge(harness: HostHarnessInfo): string {
  const ageS =
    typeof harness.age_s === 'number'
      ? harness.age_s
      : Number.isNaN(Date.parse(harness.last_seen))
        ? null
        : (Date.now() - Date.parse(harness.last_seen)) / 1000
  if (ageS === null) return harness.last_seen
  if (ageS < 60) return tr(`${Math.max(0, Math.floor(ageS))}s ago`, `${Math.max(0, Math.floor(ageS))} s temu`)
  if (ageS < 3600) return tr(`${Math.floor(ageS / 60)}m ago`, `${Math.floor(ageS / 60)} min temu`)
  if (ageS < 86400) return tr(`${Math.floor(ageS / 3600)}h ago`, `${Math.floor(ageS / 3600)} godz. temu`)
  return tr(`${Math.floor(ageS / 86400)}d ago`, `${Math.floor(ageS / 86400)} dni temu`)
}

// --- Time formatting --------------------------------------------------------
const now = ref(Date.now())
const tickTimer = setInterval(() => {
  now.value = Date.now()
}, 30000)
onUnmounted(() => clearInterval(tickTimer))

function formatRelativeMs(ts: number): string {
  const diffS = Math.max(0, (now.value - ts) / 1000)
  if (diffS < 60) return tr('just now', 'właśnie teraz')
  if (diffS < 3600) return tr(`${Math.floor(diffS / 60)}m ago`, `${Math.floor(diffS / 60)} min temu`)
  if (diffS < 86400) return tr(`${Math.floor(diffS / 3600)}h ago`, `${Math.floor(diffS / 3600)} godz. temu`)
  return tr(`${Math.floor(diffS / 86400)}d ago`, `${Math.floor(diffS / 86400)} dni temu`)
}

function formatCountdown(expiresAt: number): string {
  const diffS = expiresAt - now.value / 1000
  if (diffS <= 0) return tr('expired', 'wygasła')
  if (diffS < 60) return `${Math.ceil(diffS)}s`
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m`
  if (diffS < 86400) return `${Math.floor(diffS / 3600)}h`
  return `${Math.floor(diffS / 86400)}d`
}

function leaseCountdownClass(expiresAt: number): string {
  const diffS = expiresAt - now.value / 1000
  if (diffS <= 0) return 'text-rose-300'
  if (diffS < 300) return 'text-amber-300'
  return 'text-emerald-300'
}

// Initial load
void swarm.fetchHosts()
void swarm.fetchLeases()
</script>
