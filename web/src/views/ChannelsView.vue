<template>
  <div class="h-full flex flex-col bg-botex-950 min-h-0">
    <!-- View header -->
    <header class="shrink-0 border-b border-botex-800 px-4 sm:px-6 py-3 flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <div class="min-w-0">
        <h2 class="text-xl font-bold text-slate-100 flex items-center space-x-2">
          <HashIcon class="w-5 h-5 text-sky-400" aria-hidden="true" />
          <span>{{ t('channels.title') }}</span>
        </h2>
        <p class="text-xs text-slate-300 mt-1">{{ t('channels.subtitle') }}</p>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <span
          class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border text-[11px] font-mono uppercase tracking-wide"
          :class="realtimePillClass"
          role="status"
        >
          <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="realtimeDotClass" aria-hidden="true" />
          {{ t(realtimeLabelKey) }}
        </span>
        <button
          type="button"
          class="min-h-[44px] lg:min-h-[32px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-xs font-medium text-slate-200 transition flex items-center space-x-1.5 focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/60"
          @click="reload()"
        >
          <RefreshCwIcon class="w-3.5 h-3.5" :class="{ 'animate-spin': swarm.loadingChannels }" aria-hidden="true" />
          <span class="hidden sm:inline">{{ t('channels.refresh') }}</span>
        </button>
        <Button variant="primary" size="sm" @click="openCreate()">
          <PlusIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
          {{ t('channels.new') }}
        </Button>
      </div>
    </header>

    <!-- Channel feed error -->
    <div
      v-if="swarm.channelError"
      role="alert"
      class="shrink-0 mx-4 sm:mx-6 mt-3 p-3 rounded-xl bg-rose-950/50 border border-rose-700/60 text-xs text-rose-200 flex items-center justify-between gap-3"
    >
      <span class="font-mono truncate">{{ swarm.channelError }}</span>
      <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-2" @click="reload()">
        {{ t('channels.retry') }}
      </button>
    </div>

    <!-- Split pane: channel list + message stream -->
    <div class="flex-1 min-h-0 flex">
      <!-- Channel list -->
      <aside
        :class="[
          swarm.selectedChannelId ? 'hidden lg:flex' : 'flex',
          'flex-col min-h-0 min-w-0 flex-1 lg:flex-none lg:w-[340px] bg-botex-900/50 border-b lg:border-b-0 lg:border-r border-botex-800'
        ]"
      >
        <div class="shrink-0 px-3 py-2.5 border-b border-botex-800 flex items-center justify-between gap-2">
          <span class="text-[11px] font-semibold uppercase tracking-wider font-mono text-slate-300">
            {{ t('channels.list.title') }}
          </span>
          <span class="text-[11px] text-slate-400 font-mono">{{ swarm.channels.length }}</span>
        </div>

        <ul v-if="swarm.channels.length" class="flex-1 min-h-0 overflow-y-auto divide-y divide-botex-800">
          <li v-for="ch in swarm.channels" :key="ch.channel_id">
            <button
              type="button"
              :aria-current="swarm.selectedChannelId === ch.channel_id ? 'true' : undefined"
              class="w-full text-left px-3 py-2.5 min-h-[44px] transition focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-sky-500/60"
              :class="swarm.selectedChannelId === ch.channel_id ? 'bg-sky-950/40' : 'hover:bg-botex-800/40'"
              @click="openChannel(ch.channel_id)"
            >
              <span class="flex items-center justify-between gap-2">
                <span class="min-w-0 flex items-center gap-1.5">
                  <HashIcon
                    class="w-3.5 h-3.5 shrink-0"
                    :class="swarm.selectedChannelId === ch.channel_id ? 'text-sky-300' : 'text-slate-400'"
                    aria-hidden="true"
                  />
                  <span
                    class="font-mono text-xs font-semibold truncate"
                    :class="swarm.selectedChannelId === ch.channel_id ? 'text-sky-100' : 'text-slate-100'"
                  >{{ ch.name }}</span>
                </span>
                <span class="text-[10px] font-mono text-slate-400 shrink-0">{{ formatRelative(ch.updated_at) }}</span>
              </span>
              <span v-if="ch.topic" class="block text-[11px] text-slate-400 truncate mt-0.5 pl-5">{{ ch.topic }}</span>
              <span class="mt-1.5 pl-5 flex items-center gap-1.5 flex-wrap">
                <Badge :variant="ch.acl_mode === 'open' ? 'success' : 'info'">{{ aclLabel(ch.acl_mode) }}</Badge>
                <span
                  v-for="p in ch.participants.slice(0, 3)"
                  :key="p"
                  class="text-[10px] font-mono text-slate-300 bg-botex-950 border border-botex-800 rounded px-1.5 py-0.5 max-w-[110px] truncate"
                >{{ p }}</span>
                <span v-if="ch.participants.length > 3" class="text-[10px] font-mono text-slate-400">
                  +{{ ch.participants.length - 3 }}
                </span>
              </span>
            </button>
          </li>
        </ul>

        <div v-else-if="swarm.loadingChannels" class="p-3 space-y-2" aria-busy="true">
          <div v-for="i in 4" :key="i" class="h-14 rounded-lg bg-botex-950 border border-botex-800 animate-pulse" />
        </div>

        <div v-else class="flex-1 flex flex-col items-center justify-center text-center p-6 gap-3">
          <div class="w-11 h-11 rounded-full bg-botex-950 border border-botex-800 flex items-center justify-center">
            <HashIcon class="w-5 h-5 text-slate-400" aria-hidden="true" />
          </div>
          <p class="text-xs text-slate-400 max-w-[230px]">{{ t('channels.list.empty') }}</p>
          <Button variant="secondary" size="sm" @click="openCreate()">
            <PlusIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
            {{ t('channels.new') }}
          </Button>
        </div>
      </aside>

      <!-- Message stream -->
      <section
        :class="[
          swarm.selectedChannelId ? 'flex' : 'hidden lg:flex',
          'flex-col flex-1 min-w-0 min-h-0'
        ]"
      >
        <!-- Nothing selected (desktop) -->
        <div v-if="!swarm.selectedChannel" class="flex-1 flex flex-col items-center justify-center text-center p-6 gap-3">
          <div class="w-11 h-11 rounded-full bg-botex-950 border border-botex-800 flex items-center justify-center">
            <HashIcon class="w-5 h-5 text-slate-400" aria-hidden="true" />
          </div>
          <h3 class="text-sm font-semibold text-slate-200">{{ t('channels.noSelection.title') }}</h3>
          <p class="text-xs text-slate-400 max-w-sm">{{ t('channels.noSelection.subtitle') }}</p>
        </div>

        <template v-else>
          <!-- Channel header -->
          <div class="shrink-0 border-b border-botex-800 bg-botex-900/50 px-3 sm:px-4 py-2.5 space-y-1.5">
            <div class="flex items-center gap-2">
              <button
                type="button"
                class="lg:hidden -ml-1.5 min-h-[44px] min-w-[44px] rounded-lg p-2 text-slate-300 hover:text-white hover:bg-botex-800 transition flex items-center justify-center shrink-0"
                :aria-label="t('channels.back')"
                :title="t('channels.back')"
                @click="swarm.selectChannel(null)"
              >
                <ArrowLeftIcon class="w-4 h-4" aria-hidden="true" />
              </button>
              <HashIcon class="w-4 h-4 text-sky-400 shrink-0 hidden sm:block" aria-hidden="true" />
              <h3 class="text-sm sm:text-base font-bold font-mono text-slate-100 truncate">{{ swarm.selectedChannel.name }}</h3>
              <Badge :variant="swarm.selectedChannel.acl_mode === 'open' ? 'success' : 'info'">
                {{ aclLabel(swarm.selectedChannel.acl_mode) }}
              </Badge>
              <span class="ml-auto text-[10px] font-mono text-slate-400 shrink-0">
                {{ formatRelative(swarm.selectedChannel.updated_at) }}
              </span>
            </div>
            <p v-if="swarm.selectedChannel.topic" class="text-[11px] text-slate-400 truncate pl-0.5">
              {{ swarm.selectedChannel.topic }}
            </p>
            <div class="flex items-center gap-1.5 flex-wrap">
              <span class="text-[10px] uppercase tracking-wider font-mono text-slate-400">
                {{ t('channels.participants') }}:
              </span>
              <span
                v-for="p in swarm.selectedChannel.participants"
                :key="p"
                class="text-[10px] font-mono text-slate-300 bg-botex-950 border border-botex-800 rounded px-1.5 py-0.5 max-w-[140px] truncate"
              >{{ p }}</span>
              <span v-if="!swarm.selectedChannel.participants.length" class="text-[10px] font-mono text-slate-500">
                {{ t('channels.participants.none') }}
              </span>
            </div>
          </div>

          <!-- Messages -->
          <div
            ref="streamRef"
            role="log"
            :aria-label="t('channels.list.title')"
            class="flex-1 min-h-0 overflow-y-auto px-3 sm:px-4 py-3 space-y-2.5"
          >
            <div v-if="hasMoreOlder" class="flex justify-center py-1">
              <button
                type="button"
                class="min-h-[44px] lg:min-h-[32px] px-3 py-1.5 rounded-lg bg-botex-900 hover:bg-botex-800 border border-botex-700 text-[11px] font-medium text-slate-300 transition disabled:opacity-50"
                :disabled="swarm.loadingMessages"
                @click="loadOlder()"
              >
                {{ swarm.loadingMessages ? t('channels.loadingOlder') : t('channels.loadOlder') }}
              </button>
            </div>

            <div v-if="swarm.loadingMessages && !swarm.selectedChannelMessages.length" class="space-y-2" aria-busy="true">
              <div v-for="i in 4" :key="i" class="h-12 rounded-xl bg-botex-900 border border-botex-800 animate-pulse" />
            </div>

            <div v-else-if="!swarm.selectedChannelMessages.length" class="h-full flex flex-col items-center justify-center text-center gap-2 py-10">
              <MessageSquareIcon class="w-5 h-5 text-slate-400" aria-hidden="true" />
              <p class="text-xs text-slate-400 max-w-[260px]">{{ t('channels.messages.empty') }}</p>
            </div>

            <template v-else>
              <article
                v-for="msg in swarm.selectedChannelMessages"
                :key="msg.message_id"
                class="flex"
                :class="msg.sender_slug === effectiveSender ? 'justify-end' : 'justify-start'"
              >
                <div
                  class="max-w-[85%] lg:max-w-[72%] rounded-xl border px-3 py-2"
                  :class="msg.sender_slug === effectiveSender
                    ? 'bg-sky-950/60 border-sky-500/30'
                    : 'bg-botex-900 border-botex-800'"
                >
                  <div class="flex flex-wrap items-center gap-x-2 gap-y-0.5 mb-1">
                    <span
                      class="font-mono text-[11px] font-semibold"
                      :class="msg.sender_slug === effectiveSender ? 'text-sky-200' : 'text-slate-200'"
                    >{{ msg.sender_slug }}</span>
                    <span
                      v-if="msg.sender_origin"
                      class="text-[9px] font-mono uppercase tracking-wide text-slate-400 bg-botex-950 border border-botex-800 rounded px-1 py-px"
                    >{{ msg.sender_origin }}</span>
                    <Badge v-if="msg.untrusted" variant="warning">
                      <ShieldAlertIcon class="w-3 h-3 mr-1 -mt-px inline" aria-hidden="true" />
                      {{ t('channels.untrusted') }}
                    </Badge>
                    <span class="text-[10px] font-mono text-slate-400 ml-auto">{{ formatTimestamp(msg.created_at) }}</span>
                  </div>
                  <p class="text-xs sm:text-[13px] leading-relaxed text-slate-100 whitespace-pre-wrap break-words">
                    {{ stripUntrustedWrapper(msg.content) }}
                  </p>
                </div>
              </article>
            </template>
          </div>

          <!-- Compose -->
          <div class="shrink-0 border-t border-botex-800 bg-botex-900/50 px-3 sm:px-4 py-2.5 space-y-2">
            <div
              v-if="postError"
              role="alert"
              class="p-2 rounded-lg bg-rose-950/50 border border-rose-700/60 text-[11px] text-rose-200 flex items-center justify-between gap-2"
            >
              <span class="font-mono truncate">{{ postError }}</span>
              <button type="button" class="shrink-0 underline hover:text-white min-h-[44px] px-1.5" @click="postError = null">
                {{ t('channels.dismiss') }}
              </button>
            </div>
            <div class="flex flex-col lg:flex-row lg:items-end gap-2">
              <div class="flex items-center gap-2 lg:shrink-0">
                <label class="sr-only" for="channel-sender">{{ t('channels.compose.sender') }}</label>
                <select
                  id="channel-sender"
                  v-model="senderSlug"
                  :aria-label="t('channels.compose.sender')"
                  class="block w-full lg:w-auto min-h-[44px] lg:min-h-[36px] rounded-lg bg-botex-900 border border-botex-700 text-slate-100 text-xs font-mono px-2.5 py-2 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-blue focus:border-brand-blue"
                >
                  <option value="operator">{{ t('channels.compose.senderOperator') }}</option>
                  <option v-for="b in swarm.bots" :key="b.slug" :value="b.slug">{{ b.slug }}</option>
                  <option value="__custom__">{{ t('channels.compose.senderCustom') }}</option>
                </select>
                <input
                  v-if="senderSlug === '__custom__'"
                  v-model="customSlug"
                  type="text"
                  :placeholder="t('channels.compose.senderCustomLabel')"
                  :aria-label="t('channels.compose.senderCustomLabel')"
                  class="block w-full lg:w-56 min-h-[44px] lg:min-h-[36px] rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 text-xs font-mono px-2.5 py-2 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-blue focus:border-brand-blue"
                >
              </div>
              <textarea
                id="channel-compose"
                v-model="draft"
                rows="2"
                :placeholder="t('channels.compose.placeholder')"
                :aria-label="t('channels.compose.placeholder')"
                class="block w-full min-h-[44px] max-h-40 rounded-lg bg-botex-950 border border-botex-700 text-slate-100 placeholder-slate-400 text-xs sm:text-sm px-3 py-2 transition-colors focus:outline-none focus:ring-1 focus:ring-brand-blue focus:border-brand-blue resize-y"
                @keydown="onComposeKeydown"
              />
              <Button
                variant="primary"
                class="w-full lg:w-auto"
                :disabled="!canSend"
                :loading="swarm.postingMessage"
                @click="send()"
              >
                <SendIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
                {{ swarm.postingMessage ? t('channels.compose.sending') : t('channels.compose.send') }}
              </Button>
            </div>
          </div>
        </template>
      </section>
    </div>

    <!-- New channel modal -->
    <Modal
      :is-open="showCreate"
      :title="t('channels.modal.title')"
      :subtitle="t('channels.modal.subtitle')"
      max-width="md"
      @close="closeCreate()"
    >
      <form class="space-y-3.5" @submit.prevent="submitCreate()">
        <Input
          v-model="form.channelId"
          :label="t('channels.form.channelId')"
          :hint="t('channels.form.channelIdHint')"
          :error="errors.channelId"
          placeholder="coord-deploy"
        />
        <Input
          v-model="form.name"
          :label="t('channels.form.name')"
          :error="errors.name"
          placeholder="Deployment coordination"
        />
        <Input
          v-model="form.topic"
          :label="t('channels.form.topic')"
          :hint="t('channels.form.topicHint')"
        />
        <div>
          <span class="block text-xs font-medium text-slate-300 mb-1.5">{{ t('channels.form.acl') }}</span>
          <div
            class="flex items-center bg-botex-950 p-0.5 rounded-lg border border-botex-800 space-x-1"
            role="group"
            :aria-label="t('channels.form.acl')"
          >
            <button
              type="button"
              :aria-pressed="form.aclMode === 'participants'"
              class="flex-1 min-h-[44px] lg:min-h-[36px] px-2 py-1.5 rounded text-xs font-medium transition"
              :class="form.aclMode === 'participants'
                ? 'bg-botex-800 text-sky-200 border border-sky-500/30'
                : 'text-slate-300 hover:text-white'"
              @click="form.aclMode = 'participants'"
            >
              {{ t('channels.form.aclParticipants') }}
            </button>
            <button
              type="button"
              :aria-pressed="form.aclMode === 'open'"
              class="flex-1 min-h-[44px] lg:min-h-[36px] px-2 py-1.5 rounded text-xs font-medium transition"
              :class="form.aclMode === 'open'
                ? 'bg-botex-800 text-sky-200 border border-sky-500/30'
                : 'text-slate-300 hover:text-white'"
              @click="form.aclMode = 'open'"
            >
              {{ t('channels.form.aclOpen') }}
            </button>
          </div>
        </div>
        <Input
          v-model="form.participantsRaw"
          :label="t('channels.form.participants')"
          :hint="t('channels.form.participantsHint')"
          :error="errors.participants"
          placeholder="worker-alpha, reviewer-beta"
        />
        <div
          v-if="formError"
          role="alert"
          class="p-2.5 rounded-lg bg-rose-950/50 border border-rose-700/60 text-[11px] text-rose-200 font-mono break-words"
        >
          {{ formError }}
        </div>
      </form>
      <template #footer>
        <Button variant="ghost" size="sm" @click="closeCreate()">{{ t('channels.form.cancel') }}</Button>
        <Button variant="primary" size="sm" :loading="creating" @click="submitCreate()">
          <PlusIcon class="w-3.5 h-3.5 mr-1.5" aria-hidden="true" />
          {{ creating ? t('channels.form.creating') : t('channels.form.submit') }}
        </Button>
      </template>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useSwarmStore } from '../stores/swarm'
import { useI18n } from '../i18n'
import Badge from '../components/common/Badge.vue'
import Button from '../components/common/Button.vue'
import Input from '../components/common/Input.vue'
import Modal from '../components/common/Modal.vue'
import {
  ArrowLeft as ArrowLeftIcon,
  Hash as HashIcon,
  MessageSquare as MessageSquareIcon,
  Plus as PlusIcon,
  RefreshCw as RefreshCwIcon,
  Send as SendIcon,
  ShieldAlert as ShieldAlertIcon
} from 'lucide-vue-next'

const swarm = useSwarmStore()
const { t } = useI18n()

// Matches the backend identifier sanitizer: alphanumerics, underscores, hyphens (max 128).
const SAFE_ID_RE = /^[A-Za-z0-9_-]{1,128}$/
const UNTRUSTED_WRAPPER_RE = /^\s*<untrusted_channel_message\b[^>]*>([\s\S]*?)<\/untrusted_channel_message>\s*$/

// --- Realtime connection pill ------------------------------------------------
const realtimeLabelKey = computed(() => {
  switch (swarm.realtimeStatus) {
    case 'CONNECTED': return 'channels.realtime.connected'
    case 'CONNECTING': return 'channels.realtime.connecting'
    case 'RECONNECTING': return 'channels.realtime.reconnecting'
    default: return 'channels.realtime.disconnected'
  }
})

const realtimePillClass = computed(() => {
  switch (swarm.realtimeStatus) {
    case 'CONNECTED': return 'bg-emerald-950/70 text-emerald-300 border-emerald-500/40'
    case 'CONNECTING':
    case 'RECONNECTING': return 'bg-amber-950/70 text-amber-300 border-amber-500/40'
    default: return 'bg-botex-950 text-slate-400 border-botex-700'
  }
})

const realtimeDotClass = computed(() => {
  switch (swarm.realtimeStatus) {
    case 'CONNECTED': return 'bg-emerald-400'
    case 'CONNECTING':
    case 'RECONNECTING': return 'bg-amber-400 animate-pulse'
    default: return 'bg-slate-500'
  }
})

// --- Channel list & stream ----------------------------------------------------
const hasMoreOlder = computed(() => {
  const id = swarm.selectedChannelId
  return id ? (swarm.hasMoreOlderByChannel[id] ?? false) : false
})

const streamRef = ref<HTMLElement | null>(null)

function openChannel(channelId: string): void {
  void swarm.selectChannel(channelId)
}

async function reload(): Promise<void> {
  if (!swarm.bots.length) void swarm.fetchBots()
  await swarm.fetchChannels()
}

// Displays the clean message body; the untrusted wrapper stays server-side data,
// never rendered markup. Vue text interpolation keeps this XSS-safe.
function stripUntrustedWrapper(raw: string): string {
  const m = raw.match(UNTRUSTED_WRAPPER_RE)
  return m ? m[1].replace(/^\r?\n/, '').replace(/\r?\n$/, '') : raw
}

function aclLabel(mode: string): string {
  return t(mode === 'open' ? 'channels.acl.open' : 'channels.acl.participants')
}

// --- Compose ------------------------------------------------------------------
const draft = ref('')
const postError = ref<string | null>(null)
const senderSlug = ref<string>('operator')
const customSlug = ref<string>('')

const effectiveSender = computed(() =>
  senderSlug.value === '__custom__' ? customSlug.value.trim() : senderSlug.value
)

const canSend = computed(() => Boolean(swarm.selectedChannelId && draft.value.trim() && effectiveSender.value))

function onComposeKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    void send()
  }
}

function composeErrorText(message: string): string {
  return /ACL_DENIED/i.test(message) ? `${t('channels.err.acl')}: ${message}` : message
}

async function send(): Promise<void> {
  const channelId = swarm.selectedChannelId
  if (!channelId || swarm.postingMessage) return
  postError.value = null
  const content = draft.value.trim()
  if (!content) return
  const sender = effectiveSender.value
  if (!sender) {
    postError.value = t('channels.compose.customRequired')
    return
  }
  if (!SAFE_ID_RE.test(sender)) {
    postError.value = t('channels.compose.senderInvalid')
    return
  }
  const res = await swarm.postChannelMessage(channelId, {
    sender_slug: sender,
    content,
    sender_origin: 'ui-operator'
  })
  if (res.ok) draft.value = ''
  else postError.value = composeErrorText(res.message ?? '')
}

// --- Scroll management ---------------------------------------------------------
watch(() => swarm.selectedChannelId, async () => {
  postError.value = null
  await nextTick()
  const el = streamRef.value
  if (el) el.scrollTop = el.scrollHeight
})

watch(() => swarm.selectedChannelMessages.length, async () => {
  await nextTick()
  const el = streamRef.value
  if (!el) return
  // Only pin to bottom when the user is already reading the tail.
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 160) {
    el.scrollTop = el.scrollHeight
  }
})

async function loadOlder(): Promise<void> {
  const channelId = swarm.selectedChannelId
  const msgs = swarm.selectedChannelMessages
  if (!channelId || !msgs.length || swarm.loadingMessages) return
  const el = streamRef.value
  const prevHeight = el?.scrollHeight ?? 0
  await swarm.fetchChannelMessages(channelId, { before: msgs[0].created_at, appendOlder: true })
  await nextTick()
  if (el) el.scrollTop += el.scrollHeight - prevHeight
}

// --- New channel modal ---------------------------------------------------------
const showCreate = ref(false)
const creating = ref(false)
const formError = ref<string | null>(null)
const errors = ref<{ channelId?: string; name?: string; participants?: string }>({})
const form = ref({
  channelId: '',
  name: '',
  topic: '',
  aclMode: 'participants' as 'participants' | 'open',
  participantsRaw: ''
})

function openCreate(): void {
  form.value = { channelId: '', name: '', topic: '', aclMode: 'participants', participantsRaw: '' }
  errors.value = {}
  formError.value = null
  showCreate.value = true
}

function closeCreate(): void {
  showCreate.value = false
  formError.value = null
}

function parseParticipants(raw: string): string[] | null {
  const seen = new Set<string>()
  for (const part of raw.split(',')) {
    const slug = part.trim()
    if (!slug) continue
    if (!SAFE_ID_RE.test(slug)) return null
    seen.add(slug)
  }
  return [...seen]
}

async function submitCreate(): Promise<void> {
  if (creating.value) return
  errors.value = {}
  formError.value = null
  const channelId = form.value.channelId.trim()
  const name = form.value.name.trim()
  if (!SAFE_ID_RE.test(channelId)) {
    errors.value.channelId = t('channels.form.errId')
    return
  }
  if (!name) {
    errors.value.name = t('channels.form.errName')
    return
  }
  const participants = parseParticipants(form.value.participantsRaw)
  if (participants === null) {
    errors.value.participants = t('channels.form.errParticipant')
    return
  }
  creating.value = true
  try {
    const res = await swarm.createChannel({
      channel_id: channelId,
      name,
      topic: form.value.topic.trim(),
      acl_mode: form.value.aclMode,
      participants,
      created_by: effectiveSender.value || 'operator'
    })
    if (!res.ok) {
      formError.value = res.message ?? t('channels.form.errSubmit')
      return
    }
    closeCreate()
    openChannel(channelId)
  } finally {
    creating.value = false
  }
}

// --- Time formatting ------------------------------------------------------------
const now = ref(Date.now())
const tickTimer = setInterval(() => {
  now.value = Date.now()
}, 30000)
onUnmounted(() => clearInterval(tickTimer))

function formatRelative(iso: string): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return iso
  const diffS = Math.max(0, (now.value - ts) / 1000)
  if (diffS < 60) return t('channels.time.now')
  if (diffS < 3600) return t('channels.time.minutesAgo').replace('{n}', String(Math.floor(diffS / 60)))
  if (diffS < 86400) return t('channels.time.hoursAgo').replace('{n}', String(Math.floor(diffS / 3600)))
  return t('channels.time.daysAgo').replace('{n}', String(Math.floor(diffS / 86400)))
}

function formatTimestamp(iso: string): string {
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return iso
  const d = new Date(ts)
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (d.toDateString() === new Date().toDateString()) return time
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`
}

// Initial load
void swarm.fetchChannels()
if (!swarm.bots.length) void swarm.fetchBots()
</script>
