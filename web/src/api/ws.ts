import { getStoredBackendUrl, getStoredAccessToken } from './client'
import type { ChatChunk, PendingAction } from './types'

export type ConnectionStatus = 'DISCONNECTED' | 'CONNECTING' | 'CONNECTED' | 'RECONNECTING'

type ChunkListener = (chunk: ChatChunk) => void
type ApprovalListener = (action: PendingAction) => void
type StatusListener = (status: ConnectionStatus) => void

class WebSocketManager {
  private ws: WebSocket | null = null
  private status: ConnectionStatus = 'DISCONNECTED'
  private reconnectTimer: any = null
  private reconnectAttempts = 0
  private maxReconnectAttempts = 10
  private baseReconnectDelay = 1000
  private manualClose = false

  private chunkListeners: Set<ChunkListener> = new Set()
  private approvalListeners: Set<ApprovalListener> = new Set()
  private statusListeners: Set<StatusListener> = new Set()

  public getStatus(): ConnectionStatus {
    return this.status
  }

  public onChunk(fn: ChunkListener): () => void {
    this.chunkListeners.add(fn)
    return () => this.chunkListeners.delete(fn)
  }

  public onApproval(fn: ApprovalListener): () => void {
    this.approvalListeners.add(fn)
    return () => this.approvalListeners.delete(fn)
  }

  public onStatusChange(fn: StatusListener): () => void {
    this.statusListeners.add(fn)
    fn(this.status)
    return () => this.statusListeners.delete(fn)
  }

  private updateStatus(newStatus: ConnectionStatus) {
    if (this.status !== newStatus) {
      this.status = newStatus
      this.statusListeners.forEach((fn) => fn(newStatus))
    }
  }

  public connect(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return
    }

    this.manualClose = false
    this.clearReconnectTimer()
    this.updateStatus(this.reconnectAttempts > 0 ? 'RECONNECTING' : 'CONNECTING')

    try {
      const baseUrl = getStoredBackendUrl()
      const token = getStoredAccessToken()
      const wsProto = baseUrl.startsWith('https:') ? 'wss:' : 'ws:'
      const hostPath = baseUrl.replace(/^https?:\/\//, '')
      let url = `${wsProto}//${hostPath}/api/chat/ws`

      if (token) {
        url += `?token=${encodeURIComponent(token)}`
      }

      this.ws = new WebSocket(url)

      this.ws.onopen = () => {
        this.reconnectAttempts = 0
        this.updateStatus('CONNECTED')

        // Send auth frame if token exists as backup
        if (token) {
          this.send({ type: 'auth', token })
        }
      }

      this.ws.onmessage = (event) => {
        try {
          const data: ChatChunk = JSON.parse(event.data)

          // 15s Heartbeat ping -> respond with pong
          if (data.type === 'ping') {
            this.send({ type: 'pong', timestamp: Date.now() })
            return
          }

          // Approval required push notification
          if (data.type === 'approval_required' && data.action) {
            this.approvalListeners.forEach((fn) => fn(data.action!))
            return
          }

          // Regular chat chunk / tool execution / error / done
          this.chunkListeners.forEach((fn) => fn(data))
        } catch (e) {
          console.warn('[WS] Failed to parse message:', event.data, e)
        }
      }

      this.ws.onerror = (err) => {
        console.warn('[WS] Socket error:', err)
      }

      this.ws.onclose = (event) => {
        this.ws = null
        this.updateStatus('DISCONNECTED')

        if (!this.manualClose) {
          // If closed due to unauthorized (1008), don't rapidly loop
          if (event.code === 1008) {
            console.error('[WS] Connection rejected: 1008 Unauthorized')
            return
          }
          this.scheduleReconnect()
        }
      }
    } catch (err) {
      console.error('[WS] Connect initiation error:', err)
      this.updateStatus('DISCONNECTED')
      this.scheduleReconnect()
    }
  }

  public disconnect(): void {
    this.manualClose = true
    this.clearReconnectTimer()
    this.reconnectAttempts = 0
    if (this.ws) {
      this.ws.close()
      this.ws = null
    }
    this.updateStatus('DISCONNECTED')
  }

  public reconnect(): void {
    this.disconnect()
    this.connect()
  }

  public send(payload: Record<string, any>): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(payload))
      return true
    }
    return false
  }

  public sendChatMessage(
    message: string,
    sessionId?: string,
    model?: string,
    workspaceDir?: string,
    provider?: string
  ): boolean {
    return this.send({
      type: 'message',
      message,
      session_id: sessionId || null,
      model: model || '',
      provider: provider || '',
      workspace_dir: workspaceDir || ''
    })
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer()
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.warn('[WS] Max reconnect attempts reached.')
      return
    }

    const delay = Math.min(
      this.baseReconnectDelay * Math.pow(1.5, this.reconnectAttempts) + Math.random() * 500,
      15000
    )
    this.reconnectAttempts++
    this.reconnectTimer = setTimeout(() => {
      this.connect()
    }, delay)
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
  }
}

export const wsManager = new WebSocketManager()
