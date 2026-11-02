import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api/client'
import type { TaskSummary, TaskDetail, TaskCreateRequest, TaskEvent } from '../api/types'

const MAX_TASK_EVENTS = 1000

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<TaskSummary[]>([])
  const currentTask = ref<TaskDetail | null>(null)
  const isLoading = ref<boolean>(false)
  const isCreating = ref<boolean>(false)
  const isSendingFollowup = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)
  let pollInterval: any = null

  // ---------------------------------------------------------------------------
  // Live task-event buffers (per task_id)
  // ---------------------------------------------------------------------------

  const taskEvents = ref<Map<string, TaskEvent[]>>(new Map())
  const taskEventMeta = ref<Map<string, { minSeq: number | null; dropped: number; lastSeq: number | null }>>(new Map())

  async function fetchTasks() {
    isLoading.value = true
    try {
      const res = await api.listTasks()
      // Sort newest first
      tasks.value = (res.tasks || []).sort((a, b) => b.created_at - a.created_at)
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to list tasks'
    } finally {
      isLoading.value = false
    }
  }

  async function fetchTask(taskId: string): Promise<TaskDetail | null> {
    try {
      const detail = await api.getTask(taskId)
      currentTask.value = detail
      return detail
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to get task details'
      return null
    }
  }

  async function createTask(req: TaskCreateRequest): Promise<string | null> {
    isCreating.value = true
    errorMessage.value = null
    try {
      const res = await api.createTask(req)
      await fetchTasks()
      await fetchTask(res.task_id)
      startPolling()
      return res.task_id
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to delegate task'
      return null
    } finally {
      isCreating.value = false
    }
  }

  async function sendFollowup(taskId: string, prompt: string): Promise<boolean> {
    if (!prompt.trim()) return false
    isSendingFollowup.value = true
    errorMessage.value = null
    try {
      const res = await api.sendTaskFollowup(taskId, prompt)
      if (res.ok) {
        if (currentTask.value?.task_id === taskId) {
          await fetchTask(taskId)
        }
        startPolling()
        return true
      }
      errorMessage.value = res.detail || 'Follow-up rejected'
      return false
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to send follow-up'
      return false
    } finally {
      isSendingFollowup.value = false
    }
  }

  async function cancelTask(taskId: string): Promise<boolean> {
    try {
      const res = await api.cancelTask(taskId)
      if (res.ok) {
        await fetchTasks()
        if (currentTask.value?.task_id === taskId) {
          await fetchTask(taskId)
        }
      }
      return res.ok
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to cancel task'
      return false
    }
  }

  // ---------------------------------------------------------------------------
  // Live task-event observability
  // ---------------------------------------------------------------------------

  function getTaskEvents(taskId: string): TaskEvent[] {
    return taskEvents.value.get(taskId) || []
  }

  function getEventMeta(taskId: string) {
    return (
      taskEventMeta.value.get(taskId) || {
        minSeq: null,
        dropped: 0,
        lastSeq: null
      }
    )
  }

  function ensureBuffer(taskId: string) {
    let buf = taskEvents.value.get(taskId)
    if (!buf) {
      buf = []
      taskEvents.value.set(taskId, buf)
    }
    if (!taskEventMeta.value.has(taskId)) {
      taskEventMeta.value.set(taskId, { minSeq: null, dropped: 0, lastSeq: null })
    }
    return buf
  }

  /** Append one event, dedup by seq, keep the buffer ordered and capped. */
  function appendEvent(taskId: string, event: TaskEvent) {
    const buf = ensureBuffer(taskId)
    const meta = taskEventMeta.value.get(taskId)!

    if (meta.lastSeq !== null && event.seq <= meta.lastSeq) return // duplicate / out-of-order
    if (meta.lastSeq !== null && event.seq > meta.lastSeq + 1) {
      // Gap detected (events dropped server-side or missed between polls)
      meta.dropped += event.seq - meta.lastSeq - 1
    }

    buf.push(event)
    meta.lastSeq = event.seq
    if (meta.minSeq === null) meta.minSeq = event.seq

    // Cap the buffer at MAX_TASK_EVENTS, dropping the oldest entries.
    if (buf.length > MAX_TASK_EVENTS) {
      const removed = buf.splice(0, buf.length - MAX_TASK_EVENTS)
      meta.minSeq = removed[removed.length - 1].seq + 1
      meta.dropped += removed.length
    }
  }

  /** REST backfill: fetch the buffered tail (or everything since `since`). */
  async function loadTaskEvents(taskId: string, since?: number): Promise<TaskEvent[]> {
    try {
      const meta = getEventMeta(taskId)
      const cursor = since ?? (meta.lastSeq !== null ? meta.lastSeq : undefined)
      const res = await api.fetchTaskEvents(taskId, cursor)

      ensureBuffer(taskId)
      const m = taskEventMeta.value.get(taskId)!

      if (typeof res.min_seq === 'number' && (m.minSeq === null || res.min_seq < m.minSeq)) {
        m.minSeq = res.min_seq
      }
      if (res.dropped) {
        m.dropped += res.dropped
      }

      for (const event of res.events || []) {
        appendEvent(taskId, event)
      }
      return getTaskEvents(taskId)
    } catch (err: any) {
      errorMessage.value = err?.message || 'Failed to load task events'
      throw err
    }
  }

  /** WS push handler for `task_event` frames. */
  function handleTaskEvent(taskId: string, event: TaskEvent) {
    appendEvent(taskId, event)
  }

  function clearTaskEvents(taskId: string) {
    taskEvents.value.delete(taskId)
    taskEventMeta.value.delete(taskId)
  }

  function startPolling() {
    stopPolling()
    pollInterval = setInterval(async () => {
      // Check if current task or any task is in non-terminal state
      const hasActive = tasks.value.some((t) => t.status === 'QUEUED' || t.status === 'RUNNING')
      if (currentTask.value && (currentTask.value.status === 'QUEUED' || currentTask.value.status === 'RUNNING')) {
        await fetchTask(currentTask.value.task_id)
      }
      if (hasActive) {
        await fetchTasks()
      } else if (!currentTask.value || currentTask.value.status === 'DONE' || currentTask.value.status === 'FAILED' || currentTask.value.status === 'CANCELLED') {
        stopPolling()
      }
    }, 2500)
  }

  function stopPolling() {
    if (pollInterval) {
      clearInterval(pollInterval)
      pollInterval = null
    }
  }

  return {
    tasks,
    currentTask,
    isLoading,
    isCreating,
    errorMessage,
    fetchTasks,
    fetchTask,
    createTask,
    cancelTask,
    sendFollowup,
    isSendingFollowup,
    startPolling,
    stopPolling,
    taskEvents,
    taskEventMeta,
    getTaskEvents,
    getEventMeta,
    loadTaskEvents,
    handleTaskEvent,
    clearTaskEvents
  }
})
