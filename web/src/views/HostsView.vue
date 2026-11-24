<template>
  <div class="h-full overflow-y-auto bg-botex-950 p-4 sm:p-6">
    <!-- View header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-botex-800 pb-4 mb-6">
      <div>
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <MonitorSmartphoneIcon class="w-5 h-5 text-cyan-400" aria-hidden="true" />
          <span>{{ t('hosts.title') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">
          {{ t('hosts.subtitle') }}
        </p>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-[11px] font-mono text-slate-300 bg-botex-900 border border-botex-800 rounded-lg px-2.5 py-1.5 flex items-center gap-1.5">
          <RadioIcon class="w-3.5 h-3.5" :class="groupedHosts.length ? 'text-emerald-400' : 'text-slate-500'" aria-hidden="true" />
          {{ groupedHosts.length }} {{ tr('online', 'online') }}
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
    <div v-if="swarm.loadingHosts && !groupedHosts.length" class="grid grid-cols-1 xl:grid-cols-2 gap-4" aria-busy="true">
      <div v-for="i in 2" :key="i" class="rounded-xl border border-botex-800 bg-botex-900 p-4 space-y-3">
        <div class="h-4 w-1/3 rounded bg-botex-950 animate-pulse" />
        <div class="h-3 w-1/2 rounded bg-botex-950 animate-pulse" />
        <div class="h-3 w-2/3 rounded bg-botex-950 animate-pulse" />
      </div>
    </div>

    <!-- Hosts -->
    <div v-else-if="groupedHosts.length" class="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
      <article
        v-for="host in groupedHosts"
        :key="host.device_id"
        class="rounded-xl border border-botex-800 bg-botex-900 shadow-sm overflow-hidden"
      >
        <!-- Host header -->
        <div class="p-4 space-y-3">
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="text-sm font-bold font-mono text-slate-100 break-all">{{ host.device_id }}</h3>
                <Badge :variant="host.healthy ? 'success' : 'danger'">
                  {{ host.healthy ? tr('healthy', 'sprawny') : tr('unhealthy', 'niesprawny') }}
                </Badge>
                <Badge v-if="host.can_delegate" variant="info">{{ tr('delegable', 'delegowalny') }}</Badge>
              </div>
              <p v-if="host.workspace_ids.length" class="text-[11px] font-mono text-slate-400 mt-1 truncate">
                {{ tr('workspace', 'przestrzeń') }}:
                <span class="text-slate-300">{{ host.workspace_ids.join(', ') }}</span>
              </p>
            </div>
            <span
              class="shrink-0 h-2.5 w-2.5 rounded-full mt-1"
              :class="host.healthy ? 'bg-emerald-400 animate-pulse' : 'bg-rose-400'"
              :title="host.healthy ? tr('healthy', 'sprawny') : tr('unhealthy', 'niesprawny')"
            />
          </div>

          <dl class="flex flex-wrap gap-x-5 gap-y-1.5 text-[11px] font-mono text-slate-400">
            <div class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">{{ tr('seen', 'widziany') }}</dt>
              <dd class="text-slate-200">{{ formatRelativeMs(host.last_seen_at) }}</dd>
            </div>
            <div class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">{{ tr('uptime', 'połączono') }}</dt>
              <dd class="text-slate-200">{{ formatRelativeMs(host.connected_at) }}</dd>
            </div>
            <div v-if="host.pending_calls > 0" class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">{{ tr('pending', 'w toku') }}</dt>
              <dd class="text-amber-300">{{ host.pending_calls }}</dd>
            </div>
            <div v-if="host.client_version" class="flex items-center gap-1.5">
              <dt class="uppercase tracking-wide">ver</dt>
              <dd class="text-slate-200 truncate max-w-[140px]">{{ host.client_version }}</dd>
            </div>
          </dl>

          <!-- Capability pills -->
          <div v-if="deviceCapabilityPills(host).length" class="flex flex-wrap gap-1.5">
            <span
              v-for="pill in deviceCapabilityPills(host)"
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

        <!-- Per-host Workspaces -->
        <div class="border-t border-botex-800">
          <button
            type="button"
            :aria-expanded="expandedHosts.has(host.device_id)"
            class="w-full min-h-[44px] px-4 py-2 flex items-center justify-between gap-2 text-left hover:bg-botex-800/40 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-500/60"
            @click="toggleHostDetails(host.device_id)"
          >
            <span class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
              <LayersIcon class="w-3.5 h-3.5 text-cyan-400" aria-hidden="true" />
              {{ t('hosts.workspaces.title') }}
              <span v-if="workspaceCounts(host.device_id)" class="font-normal normal-case tracking-normal text-slate-400">
                {{ workspaceCounts(host.device_id) }}
              </span>
              <RefreshCwIcon
                v-if="swarm.loadingWorkspacesHost.has(host.device_id)"
                class="w-3 h-3 animate-spin text-cyan-300"
                aria-hidden="true"
              />
            </span>
            <ChevronDownIcon
              class="w-4 h-4 text-slate-400 transition-transform"
              :class="expandedHosts.has(host.device_id) ? '' : '-rotate-90'"
              aria-hidden="true"
            />
          </button>

          <div v-if="expandedHosts.has(host.device_id)" class="px-4 pb-4 space-y-2">
            <div
              v-if="swarm.workspaceErrors[host.device_id]"
              role="alert"
              class="p-2.5 rounded-lg bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 font-mono"
            >
              {{ swarm.workspaceErrors[host.device_id] }}
            </div>
            <div
              v-if="!swarm.loadingWorkspacesHost.has(host.device_id) && !hostWorkspaces(host.device_id).length"
              class="py-3 text-center text-xs text-slate-400"
            >
              {{ t('hosts.workspaces.empty') }}
            </div>
            <div
              v-for="ws in hostWorkspaces(host.device_id)"
              :key="ws.workspace_id"
              class="rounded-lg border border-botex-800 bg-botex-950 p-3 flex flex-wrap items-center justify-between gap-2"
            >
              <div class="flex items-center gap-2 min-w-0">
                <span
                  class="h-2 w-2 shrink-0 rounded-full"
                  :class="ws.connected ? 'bg-emerald-400 animate-pulse' : 'bg-slate-500'"
                  :title="ws.connected ? t('hosts.workspaces.connected') : t('hosts.workspaces.disconnected')"
                />
                <span class="text-xs font-semibold text-slate-100 truncate">
                  {{ ws.label || ws.workspace_id }}
                </span>
                <span
                  class="text-[10px] font-mono text-cyan-300 bg-botex-900 border border-botex-800 rounded px-1.5 py-0.5 shrink-0"
                >
                  {{ ws.workspace_id }}
                </span>
              </div>

              <div class="flex flex-wrap items-center gap-2 shrink-0">
                <span
                  class="text-[10px] font-mono px-1.5 py-0.5 rounded border"
                  :class="
                    ws.connected
                      ? 'bg-emerald-950/60 text-emerald-300 border-emerald-500/40'
                      : 'bg-botex-900 text-slate-400 border-botex-800'
                  "
                >
                  {{ ws.connected ? t('hosts.workspaces.connected') : t('hosts.workspaces.disconnected') }}
                </span>
                <Badge :variant="workspaceStateVariant(ws.state)">
                  {{ workspaceStateLabel(ws.state) }}
                </Badge>
                <span v-if="ws.last_seen" class="text-[10px] font-mono text-slate-400">
                  {{ formatIsoAge(ws.last_seen) }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- Harness inventory -->
        <div class="border-t border-botex-800">
          <button
            type="button"
            :aria-expanded="expandedHosts.has(host.device_id)"
            class="w-full min-h-[44px] px-4 py-2 flex items-center justify-between gap-2 text-left hover:bg-botex-800/40 transition focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-cyan-500/60"
            @click="toggleHostDetails(host.device_id)"
          >
            <span class="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
              <CpuIcon class="w-3.5 h-3.5" aria-hidden="true" />
              {{ t('hosts.harnesses.title') }}
              <span v-if="harnessCounts(host.device_id)" class="font-normal normal-case tracking-normal text-slate-400">
                {{ harnessCounts(host.device_id) }}
              </span>
              <RefreshCwIcon
                v-if="swarm.loadingHarnessHost.has(host.device_id)"
                class="w-3 h-3 animate-spin text-cyan-300"
                aria-hidden="true"
              />
            </span>
            <ChevronDownIcon
              class="w-4 h-4 text-slate-400 transition-transform"
              :class="expandedHosts.has(host.device_id) ? '' : '-rotate-90'"
              aria-hidden="true"
            />
          </button>

          <div v-if="expandedHosts.has(host.device_id)" class="px-4 pb-4 space-y-2">
            <div
              v-if="swarm.harnessErrors[host.device_id]"
              role="alert"
              class="p-2.5 rounded-lg bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 font-mono"
            >
              {{ swarm.harnessErrors[host.device_id] }}
            </div>
            <div
              v-if="!swarm.loadingHarnessHost.has(host.device_id) && !(swarm.harnessesByHost[host.device_id] || []).length"
              class="py-3 text-center text-xs text-slate-400"
            >
              {{ t('hosts.harnesses.empty') }}
            </div>
            <div
              v-for="harness in swarm.harnessesByHost[host.device_id] || []"
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

              <!-- Per-harness limits telemetry -->
              <div class="flex flex-wrap items-center justify-between gap-2 rounded bg-botex-900/70 border border-botex-800/80 px-2.5 py-1.5 text-[11px] font-mono">
                <div class="flex flex-wrap items-center gap-2 min-w-0">
                  <GaugeIcon class="w-3.5 h-3.5 shrink-0 text-cyan-400" aria-hidden="true" />
                  <span class="text-slate-400">{{ t('hosts.limits.label') }}:</span>
                  <span :class="limitsValueClass(harness.limits)">
                    {{ formatHarnessLimits(harness.limits) }}
                  </span>
                </div>
                <div class="flex flex-wrap items-center gap-2 shrink-0">
                  <span v-if="harness.limits?.reset_at" class="text-[10px] text-slate-400">
                    {{ t('hosts.limits.resetsAt') }}:
                    <span class="text-slate-200">{{ formatResetTime(harness.limits.reset_at) }}</span>
                  </span>
                  <Badge :variant="limitsConfidenceVariant(harness.limits)">
                    {{ limitsConfidenceLabel(harness.limits) }}
                  </Badge>
                </div>
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
        <div v-if="(swarm.leasesByHost[host.device_id] || []).length" class="border-t border-botex-800 px-4 py-3 space-y-1.5">
          <div class="text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300 flex items-center gap-1.5">
            <KeyIcon class="w-3 h-3 text-amber-400" aria-hidden="true" />
            {{ tr('Active leases', 'Aktywne dzierżawy') }}
          </div>
          <div
            v-for="lease in swarm.leasesByHost[host.device_id]"
            :key="lease.lease_key"
            class="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] font-mono"
          >
            <span class="text-slate-100">{{ lease.bot_slug }}</span>
            <span class="text-slate-400 truncate min-w-0 max-w-[220px]">
              {{ formatSafeLeaseScope(lease.scope_path) }}
            </span>
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
            'Connect a device with an Edge runner to monitor its workspaces, harnesses, and leases here.',
            'Podłącz urządzenie z runnerem Edge, aby monitorować tutaj jego przestrzenie robocze, harnessy i dzierżawy.'
          )
        }}
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import { useSwarmStore } from '../stores/swarm'
import { useI18n } from '../i18n'
import type { ConnectedDeviceInfo, HarnessLimits, HostHarnessInfo, HostWorkspace } from '../api/types'
import Badge from '../components/common/Badge.vue'
import {
  ChevronDown as ChevronDownIcon,
  Cpu as CpuIcon,
  Gauge as GaugeIcon,
  KeyRound as KeyIcon,
  Layers as LayersIcon,
  MonitorSmartphone as MonitorSmartphoneIcon,
  Radio as RadioIcon,
  RefreshCw as RefreshCwIcon,
  Server as ServerIcon
} from 'lucide-vue-next'

const swarm = useSwarmStore()
const { locale, t } = useI18n()

function tr(en: string, pl: string): string {
  return locale.value === 'pl' ? pl : en
}

interface GroupedHost {
  device_id: string
  workspace_ids: string[]
  healthy: boolean
  can_delegate: boolean
  connected_at: number
  last_seen_at: number
  pending_calls: number
  client_version?: string
  sessions: ConnectedDeviceInfo[]
}

const OPAQUE_ID_RE = /^[A-Za-z0-9_-]{1,128}$/

function isOpaqueWorkspaceId(val: string): boolean {
  return OPAQUE_ID_RE.test(val)
}

const groupedHosts = computed<GroupedHost[]>(() => {
  const byHost = new Map<string, GroupedHost>()
  for (const dev of swarm.devices) {
    const existing = byHost.get(dev.device_id)
    const safeWs = dev.workspace_id && isOpaqueWorkspaceId(dev.workspace_id) ? dev.workspace_id : ''
    if (!existing) {
      byHost.set(dev.device_id, {
        device_id: dev.device_id,
        workspace_ids: safeWs ? [safeWs] : [],
        healthy: Boolean(dev.healthy),
        can_delegate: Boolean(dev.can_delegate),
        connected_at: dev.connected_at,
        last_seen_at: dev.last_seen_at,
        pending_calls: dev.pending_calls ?? 0,
        client_version: dev.client_version,
        sessions: [dev]
      })
    } else {
      if (safeWs && !existing.workspace_ids.includes(safeWs)) {
        existing.workspace_ids.push(safeWs)
      }
      existing.healthy = existing.healthy || Boolean(dev.healthy)
      existing.can_delegate = existing.can_delegate || Boolean(dev.can_delegate)
      existing.connected_at = Math.min(existing.connected_at, dev.connected_at)
      existing.last_seen_at = Math.max(existing.last_seen_at, dev.last_seen_at)
      existing.pending_calls += dev.pending_calls ?? 0
      if (!existing.client_version && dev.client_version) {
        existing.client_version = dev.client_version
      }
      existing.sessions.push(dev)
    }
  }
  return Array.from(byHost.values())
})

const expandedHosts = ref<Set<string>>(new Set())

function toggleHostDetails(hostId: string): void {
  const next = new Set(expandedHosts.value)
  if (next.has(hostId)) {
    next.delete(hostId)
    expandedHosts.value = next
    return
  }
  next.add(hostId)
  expandedHosts.value = next
  if (!swarm.loadingWorkspacesHost.has(hostId)) void swarm.fetchHostWorkspaces(hostId)
  if (!swarm.loadingHarnessHost.has(hostId)) void swarm.fetchHostHarnesses(hostId)
}

function refreshAll(): void {
  void swarm.fetchHosts()
  void swarm.fetchLeases()
  for (const hostId of expandedHosts.value) {
    void swarm.fetchHostWorkspaces(hostId)
    void swarm.fetchHostHarnesses(hostId)
  }
}

function hostWorkspaces(hostId: string): HostWorkspace[] {
  return swarm.workspacesByHost[hostId] || []
}

function workspaceCounts(hostId: string): string {
  const list = swarm.workspacesByHost[hostId]
  if (!list || !list.length) return ''
  const connectedCount = list.filter((w) => w.connected).length
  return `${list.length} · ${connectedCount} ${tr('online', 'online')}`
}

function workspaceStateVariant(state: string): 'success' | 'warning' | 'danger' | 'default' {
  switch (state) {
    case 'enrolled':
      return 'success'
    case 'candidate':
      return 'warning'
    case 'disabled':
      return 'default'
    default:
      return 'default'
  }
}

function workspaceStateLabel(state: string): string {
  switch (state) {
    case 'enrolled':
      return t('hosts.workspaces.state.enrolled')
    case 'candidate':
      return t('hosts.workspaces.state.candidate')
    case 'disabled':
      return t('hosts.workspaces.state.disabled')
    default:
      return state
  }
}

type Pill = { label: string; on?: boolean; title?: string }

function capabilityPills(caps: Record<string, unknown>): Pill[] {
  const out: Pill[] = []
  for (const [key, value] of Object.entries(caps)) {
    if (value === true) out.push({ label: key, on: true })
    else if (typeof value === 'number') out.push({ label: `${key}: ${value}` })
    else if (typeof value === 'string' && value && value.length <= 18 && !value.includes('/') && !value.includes('\\')) {
      out.push({ label: `${key}: ${value}` })
    }
  }
  return out.slice(0, 8)
}

function deviceCapabilityPills(host: GroupedHost): Pill[] {
  const pills: Pill[] = []
  const anyExec = host.sessions.some((s) => s.capabilities?.allow_exec)
  const anyDestructive = host.sessions.some((s) => s.capabilities?.allow_destructive)
  const anySpawn = host.sessions.some((s) => s.capabilities?.harness_spawn)
  if (anyExec) pills.push({ label: tr('exec', 'exec'), on: true })
  if (anySpawn) pills.push({ label: tr('spawn', 'spawn'), on: true })
  if (anyDestructive) pills.push({ label: tr('destructive', 'destrukcja'), on: true })

  const skillSet = new Map<string, string>()
  for (const s of host.sessions) {
    const delegation = s.capabilities?.delegation
    if (delegation?.enabled) {
      for (const sk of delegation.skills || []) skillSet.set(sk.name, sk.name)
    }
  }
  if (skillSet.size > 0 || host.sessions.some((s) => s.capabilities?.delegation?.enabled)) {
    const names = Array.from(skillSet.keys()).join(', ')
    pills.push({
      label: tr(`delegation: ${skillSet.size} skills`, `delegacja: ${skillSet.size} umiejętności`),
      title: names || undefined
    })
  }
  const repCaps = host.sessions[0]?.capabilities
  if (Array.isArray(repCaps?.allowed_tools) && repCaps.allowed_tools.length) {
    pills.push({ label: tr(`${repCaps.allowed_tools.length} tools`, `${repCaps.allowed_tools.length} narzędzi`) })
  }
  if (typeof repCaps?.max_output_bytes === 'number' && repCaps.max_output_bytes > 0) {
    pills.push({ label: `${(repCaps.max_output_bytes / 1024).toFixed(0)} KiB out` })
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

function formatMetricNum(val: number): string {
  return Number.isInteger(val) ? String(val) : val.toFixed(2).replace(/\.?0+$/, '')
}

function formatUnitSuffix(unit?: string): string {
  if (!unit || unit === 'unknown') return ''
  return ` ${unit}`
}

function formatHarnessLimits(limits?: HarnessLimits): string {
  if (!limits) return t('hosts.limits.unavailable')
  const hasRemaining = typeof limits.remaining === 'number' && Number.isFinite(limits.remaining)
  const hasLimit = typeof limits.limit === 'number' && Number.isFinite(limits.limit)
  const hasUsed = typeof limits.used === 'number' && Number.isFinite(limits.used)

  if (hasRemaining && hasLimit) {
    return `${formatMetricNum(limits.remaining!)} / ${formatMetricNum(limits.limit!)}${formatUnitSuffix(limits.unit)}`
  }
  if (hasUsed && hasLimit) {
    const rem = Math.max(0, limits.limit! - limits.used!)
    return `${formatMetricNum(rem)} / ${formatMetricNum(limits.limit!)}${formatUnitSuffix(limits.unit)}`
  }
  if (hasRemaining) {
    return `${formatMetricNum(limits.remaining!)}${formatUnitSuffix(limits.unit)}`
  }
  if (hasUsed) {
    return `${formatMetricNum(limits.used!)}${formatUnitSuffix(limits.unit)}`
  }
  return t('hosts.limits.unavailable')
}

function limitsValueClass(limits?: HarnessLimits): string {
  if (!limits || (limits.confidence || 'unknown') === 'unknown') return 'text-slate-400'
  if (typeof limits.remaining === 'number' && limits.remaining <= 0 && typeof limits.limit === 'number' && limits.limit > 0) {
    return 'text-rose-300 font-semibold'
  }
  if (
    typeof limits.remaining === 'number' &&
    typeof limits.limit === 'number' &&
    limits.limit > 0 &&
    limits.remaining / limits.limit <= 0.15
  ) {
    return 'text-amber-300'
  }
  return 'text-slate-100'
}

function limitsConfidenceVariant(limits?: HarnessLimits): 'success' | 'info' | 'warning' | 'default' {
  const conf = (limits?.confidence || 'unknown').toLowerCase()
  switch (conf) {
    case 'exact':
      return 'success'
    case 'estimated':
      return 'info'
    default:
      return 'default'
  }
}

function limitsConfidenceLabel(limits?: HarnessLimits): string {
  const conf = (limits?.confidence || 'unknown').toLowerCase()
  switch (conf) {
    case 'exact':
      return t('hosts.limits.confidence.exact')
    case 'estimated':
      return t('hosts.limits.confidence.estimated')
    default:
      return t('hosts.limits.confidence.unknown')
  }
}

function formatResetTime(resetAt: string): string {
  const parsed = Date.parse(resetAt)
  if (Number.isNaN(parsed)) return resetAt
  const diffS = Math.round((parsed - now.value) / 1000)
  if (diffS <= 0) return tr('now', 'teraz')
  if (diffS < 60) return `${diffS}s`
  if (diffS < 3600) return `${Math.floor(diffS / 60)}m`
  if (diffS < 86400) return `${Math.floor(diffS / 3600)}h`
  return new Date(parsed).toLocaleDateString()
}

function formatSafeLeaseScope(rawScope: string): string {
  const clean = (rawScope || '').trim()
  if (!clean) return t('hosts.lease.scopedWorkspace')
  if (clean.startsWith('edge://')) {
    const rest = clean.slice('edge://'.length)
    const parts = rest.split('/').filter(Boolean)
    if (parts.length === 2 && isOpaqueWorkspaceId(parts[0]) && isOpaqueWorkspaceId(parts[1])) {
      return `${parts[0]} · ${parts[1]}`
    }
    return t('hosts.lease.scopedWorkspace')
  }
  if (isOpaqueWorkspaceId(clean)) return clean
  return t('hosts.lease.scopedWorkspace')
}

function harnessAge(harness: HostHarnessInfo): string {
  const ageS =
    typeof harness.age_s === 'number'
      ? harness.age_s
      : Number.isNaN(Date.parse(harness.last_seen))
        ? null
        : (now.value - Date.parse(harness.last_seen)) / 1000
  if (ageS === null) return harness.last_seen
  if (ageS < 60) return tr(`${Math.max(0, Math.floor(ageS))}s ago`, `${Math.max(0, Math.floor(ageS))} s temu`)
  if (ageS < 3600) return tr(`${Math.floor(ageS / 60)}m ago`, `${Math.floor(ageS / 60)} min temu`)
  if (ageS < 86400) return tr(`${Math.floor(ageS / 3600)}h ago`, `${Math.floor(ageS / 3600)} godz. temu`)
  return tr(`${Math.floor(ageS / 86400)}d ago`, `${Math.floor(ageS / 86400)} dni temu`)
}

function formatIsoAge(iso: string): string {
  const parsed = Date.parse(iso)
  if (Number.isNaN(parsed)) return ''
  return formatRelativeMs(parsed)
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
