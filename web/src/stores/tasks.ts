import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../api/client'
import type { TaskSummary, TaskDetail, TaskCreateRequest } from '../api/types'

export const useTasksStore = defineStore('tasks', () => {
  const tasks = ref<TaskSummary[]>([])
  const currentTask = ref<TaskDetail | null>(null)
  const isLoading = ref<boolean>(false)
  const isCreating = ref<boolean>(false)
  const errorMessage = ref<string | null>(null)
  let pollInterval: any = null

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
    startPolling,
    stopPolling
  }
})
