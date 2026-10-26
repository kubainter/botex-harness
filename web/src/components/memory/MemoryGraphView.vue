<template>
  <div class="relative w-full h-[clamp(320px,65dvh,650px)] min-h-[320px] rounded-xl border border-botex-800 bg-botex-950 overflow-hidden flex flex-col select-none">
    <!-- Top Bar: Health Stats and Controls (Safe Bounds) -->
    <div class="absolute top-2 left-2 right-2 z-10 flex items-center justify-between gap-1.5 pointer-events-none max-w-full">
      <!-- Health Badge and Topology Counters -->
      <div class="flex items-center space-x-1.5 pointer-events-auto bg-botex-900/95 backdrop-blur border border-botex-700/80 rounded-lg px-2 sm:px-3 py-1 shadow-lg text-xs sm:text-xs min-w-0 truncate">
        <span class="flex items-center space-x-1 font-medium text-slate-200 shrink-0">
          <span
            class="w-2 h-2 rounded-full inline-block"
            :class="health?.is_healthy ? 'bg-emerald-500 animate-pulse' : 'bg-amber-500'"
          ></span>
          <span class="hidden sm:inline">{{ health?.is_healthy ? (locale === 'pl' ? 'Graf Spójny' : 'Graph Healthy') : (locale === 'pl' ? 'Wykryto Uwagi' : 'Graph Issues') }}</span>
        </span>
        <span class="text-botex-700">|</span>
        <span class="text-slate-300 font-mono">{{ nodes.length }} <span class="hidden sm:inline">{{ locale === 'pl' ? 'węzłów' : 'nodes' }}</span></span>
        <span class="text-botex-700">|</span>
        <span class="text-slate-300 font-mono">{{ edges.length }} <span class="hidden sm:inline">{{ locale === 'pl' ? 'krawędzi' : 'edges' }}</span></span>
        <template v-if="health && health.orphan_count > 0">
          <span class="text-botex-700">|</span>
          <span class="text-amber-400 font-mono">{{ health.orphan_count }} <span class="hidden sm:inline">{{ locale === 'pl' ? 'sierot' : 'orphans' }}</span></span>
        </template>
      </div>

      <!-- Controls -->
      <div class="flex items-center space-x-1 pointer-events-auto shrink-0">
        <button
          type="button"
          class="w-8 h-8 rounded-lg bg-botex-900/95 hover:bg-botex-800 border border-botex-700/80 text-xs text-slate-200 font-bold transition shadow-lg flex items-center justify-center"
          @click="zoom(1.2)"
          title="Zoom In"
        >
          +
        </button>
        <button
          type="button"
          class="w-8 h-8 rounded-lg bg-botex-900/95 hover:bg-botex-800 border border-botex-700/80 text-xs text-slate-200 font-bold transition shadow-lg flex items-center justify-center"
          @click="zoom(0.8)"
          title="Zoom Out"
        >
          -
        </button>
        <button
          type="button"
          class="h-8 px-2 rounded-lg bg-botex-900/95 hover:bg-botex-800 border border-botex-700/80 text-xs text-slate-200 font-medium transition shadow-lg flex items-center space-x-1"
          @click="resetView"
          :title="locale === 'pl' ? 'Wyśrodkuj graf' : 'Center Graph'"
        >
          <svg class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="16" />
            <line x1="8" y1="12" x2="16" y2="12" />
          </svg>
          <span class="hidden sm:inline">{{ locale === 'pl' ? 'Wycentruj' : 'Center' }}</span>
        </button>
      </div>
    </div>

    <!-- Canvas Area -->
    <div ref="containerRef" class="w-full h-full relative cursor-grab active:cursor-grabbing touch-none">
      <canvas ref="canvasRef" class="block w-full h-full"></canvas>

      <!-- Hover Tooltip -->
      <div
        v-if="hoveredNode"
        class="absolute z-20 pointer-events-none bg-botex-900/95 border border-botex-700 backdrop-blur rounded-lg p-2.5 shadow-2xl text-xs space-y-1 max-w-xs transition-opacity duration-150"
        :style="{ left: `${tooltipPos.x + 15}px`, top: `${tooltipPos.y + 15}px` }"
      >
        <div class="flex items-center justify-between gap-2">
          <span class="font-bold text-slate-100 truncate">{{ hoveredNode.title }}</span>
          <span
            class="px-1.5 py-0.5 rounded text-xs uppercase font-mono font-bold"
            :style="{ backgroundColor: `${getNodeColor(hoveredNode.kind)}22`, color: getNodeColor(hoveredNode.kind) }"
          >
            {{ hoveredNode.kind }}
          </span>
        </div>
        <div class="text-xs font-mono text-slate-400">ID: {{ hoveredNode.id }}</div>
        <div class="flex items-center space-x-3 text-xs text-slate-300 pt-1 border-t border-botex-800">
          <span>In: {{ hoveredNode.in_degree }}</span>
          <span>Out: {{ hoveredNode.out_degree }}</span>
          <span v-if="hoveredNode.compiled_truth" class="text-cyan-400 font-semibold">⭐ Compiled Truth</span>
        </div>
      </div>
    </div>

    <!-- Bottom Taxonomy Legend -->
    <div class="absolute bottom-3 left-3 z-10 flex flex-wrap items-center gap-3 bg-botex-900/85 backdrop-blur border border-botex-800 rounded-lg px-3 py-1.5 text-xs text-slate-300 shadow">
      <span class="text-slate-400 text-xs uppercase font-bold tracking-wider">{{ locale === 'pl' ? 'Taksonomia' : 'Taxonomy' }}:</span>
      <div class="flex items-center space-x-1.5">
        <span class="w-2.5 h-2.5 rounded-full" style="background-color: #a855f7;"></span>
        <span>#decision</span>
      </div>
      <div class="flex items-center space-x-1.5">
        <span class="w-2.5 h-2.5 rounded-full" style="background-color: #f59e0b;"></span>
        <span>#lesson</span>
      </div>
      <div class="flex items-center space-x-1.5">
        <span class="w-2.5 h-2.5 rounded-full" style="background-color: #10b981;"></span>
        <span>#handoff</span>
      </div>
      <div class="flex items-center space-x-1.5">
        <span class="w-2.5 h-2.5 rounded-full" style="background-color: #38bdf8;"></span>
        <span>#context</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useI18n } from '../../i18n'
import type { GraphNode, GraphEdge, GraphHealth } from '../../api/types'

const { locale } = useI18n()

const props = defineProps<{
  nodes: GraphNode[]
  edges: GraphEdge[]
  health: GraphHealth | null
}>()

const emit = defineEmits<{
  (e: 'select-node', node: GraphNode): void
}>()

interface SimNode extends GraphNode {
  x: number
  y: number
  vx: number
  vy: number
  radius: number
}

const containerRef = ref<HTMLDivElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)

// Simulation & Render State
let simNodes: SimNode[] = []
let animId: number | null = null
let transform = { x: 0, y: 0, k: 1.0 }

// Interaction State
const hoveredNode = ref<SimNode | null>(null)
const tooltipPos = ref({ x: 0, y: 0 })
let isDragging = false
let draggedNode: SimNode | null = null
let dragStartPos = { x: 0, y: 0 }
let lastMousePos = { x: 0, y: 0 }

const COLOR_MAP: Record<string, string> = {
  decision: '#a855f7',
  lesson: '#f59e0b',
  handoff: '#10b981',
  context: '#38bdf8',
  deprecated: '#ef4444',
  default: '#64748b'
}

function getNodeColor(kind: string): string {
  const norm = (kind || '').toLowerCase().trim()
  return COLOR_MAP[norm] || COLOR_MAP.default
}

function initSimulation() {
  if (!containerRef.value) return
  const width = containerRef.value.clientWidth || 800
  const height = containerRef.value.clientHeight || 600

  // Map nodes to simulation nodes
  simNodes = props.nodes.map((n, idx) => {
    const existing = simNodes.find(en => en.id === n.id)
    const angle = (idx / (props.nodes.length || 1)) * Math.PI * 2
    const dist = 100 + Math.random() * 150
    const radius = Math.min(22, Math.max(10, 10 + (n.in_degree + n.out_degree) * 2))

    return {
      ...n,
      x: existing ? existing.x : width / 2 + Math.cos(angle) * dist,
      y: existing ? existing.y : height / 2 + Math.sin(angle) * dist,
      vx: 0,
      vy: 0,
      radius
    }
  })

  // Let the force layout settle a few ticks before framing, so the initial
  // fit reflects the real spread instead of the seed ring.
  for (let i = 0; i < 40; i++) updateSimulation()
  resetView()
}

// Fit the whole node cloud into the viewport with padding — every node and
// label is reachable without an initial pan, on any screen size.
function resetView() {
  if (!containerRef.value) return
  const w = containerRef.value.clientWidth
  const h = containerRef.value.clientHeight
  if (simNodes.length === 0) {
    transform = { x: w / 2, y: h / 2, k: 1.0 }
    return
  }
  const PAD = 64
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const n of simNodes) {
    minX = Math.min(minX, n.x - n.radius)
    maxX = Math.max(maxX, n.x + n.radius)
    minY = Math.min(minY, n.y - n.radius)
    maxY = Math.max(maxY, n.y + n.radius + 14) // room for the title label
  }
  const bw = Math.max(1, maxX - minX)
  const bh = Math.max(1, maxY - minY)
  const k = Math.max(0.2, Math.min(1.6, Math.min((w - PAD * 2) / bw, (h - PAD * 2) / bh)))
  transform = {
    x: w / 2 - ((minX + maxX) / 2) * k,
    y: h / 2 - ((minY + maxY) / 2) * k,
    k
  }
}

function zoom(factor: number) {
  if (!containerRef.value) return
  const w = containerRef.value.clientWidth / 2
  const h = containerRef.value.clientHeight / 2
  const newK = Math.max(0.2, Math.min(4.0, transform.k * factor))
  const curK = transform.k || 1.0
  transform.x = w - ((w - transform.x) * newK) / curK
  transform.y = h - ((h - transform.y) * newK) / curK
  transform.k = newK
}

// Simple force simulation tick
function updateSimulation() {
  const nodeMap = new Map<string, SimNode>()
  for (const n of simNodes) {
    nodeMap.set(n.id, n)
  }

  // 1. Repulsion between all nodes
  for (let i = 0; i < simNodes.length; i++) {
    for (let j = i + 1; j < simNodes.length; j++) {
      const a = simNodes[i]
      const b = simNodes[j]
      const dx = b.x - a.x
      const dy = b.y - a.y
      const distSq = dx * dx + dy * dy || 1
      const dist = Math.sqrt(distSq)
      if (dist < 400) {
        const force = (300 / (distSq + 100)) * 2
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force
        a.vx -= fx
        a.vy -= fy
        b.vx += fx
        b.vy += fy
      }
    }
  }

  // 2. Attraction along edges
  for (const e of props.edges) {
    const src = nodeMap.get(e.source)
    const tgt = nodeMap.get(e.target)
    if (src && tgt) {
      const dx = tgt.x - src.x
      const dy = tgt.y - src.y
      const dist = Math.sqrt(dx * dx + dy * dy) || 1
      const desiredDist = 120
      const force = (dist - desiredDist) * 0.02
      const fx = (dx / dist) * force
      const fy = (dy / dist) * force
      src.vx += fx
      src.vy += fy
      tgt.vx -= fx
      tgt.vy -= fy
    }
  }

  // 3. Center gravity and damping
  for (const n of simNodes) {
    if (n === draggedNode) continue
    n.vx -= n.x * 0.001
    n.vy -= n.y * 0.001
    n.vx *= 0.85
    n.vy *= 0.85
    n.x += n.vx
    n.y += n.vy
  }
}

function render() {
  const canvas = canvasRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  ctx.clearRect(0, 0, canvas.width, canvas.height)
  ctx.save()

  // Apply camera Pan & Zoom
  ctx.translate(transform.x, transform.y)
  ctx.scale(transform.k, transform.k)

  const nodeMap = new Map<string, SimNode>()
  for (const n of simNodes) nodeMap.set(n.id, n)

  // Draw Edges with Animated Flow Particles & Directional Arrows
  const nowSec = performance.now() * 0.001
  props.edges.forEach((e, edgeIdx) => {
    const src = nodeMap.get(e.source)
    const tgt = nodeMap.get(e.target)
    if (!src || !tgt) return

    const isConnected = hoveredNode.value && (hoveredNode.value.id === src.id || hoveredNode.value.id === tgt.id)
    const dx = tgt.x - src.x
    const dy = tgt.y - src.y
    const dist = Math.sqrt(dx * dx + dy * dy) || 1

    // 1. High contrast connection edge
    ctx.beginPath()
    ctx.moveTo(src.x, src.y)
    ctx.lineTo(tgt.x, tgt.y)
    ctx.strokeStyle = isConnected ? '#38bdf8' : 'rgba(56, 189, 248, 0.45)'
    ctx.lineWidth = isConnected ? 2.6 : 1.8
    ctx.stroke()

    // 2. Clear Directional Arrow on edge
    const arrowDist = dist - tgt.radius - 4
    if (arrowDist > src.radius) {
      const ax = src.x + (dx / dist) * arrowDist
      const ay = src.y + (dy / dist) * arrowDist
      const angle = Math.atan2(dy, dx)

      ctx.save()
      ctx.translate(ax, ay)
      ctx.rotate(angle)
      ctx.beginPath()
      ctx.moveTo(0, 0)
      ctx.lineTo(-7, -3.5)
      ctx.lineTo(-7, 3.5)
      ctx.closePath()
      ctx.fillStyle = isConnected ? '#38bdf8' : 'rgba(56, 189, 248, 0.85)'
      ctx.fill()
      ctx.restore()
    }

    // 3. Animated Flow Particles (Synaptic knowledge flow along connections)
    if (dist > src.radius + tgt.radius + 8) {
      const speed = 0.5
      for (let pIdx = 0; pIdx < 2; pIdx++) {
        const phase = (nowSec * speed + edgeIdx * 0.27 + pIdx * 0.5) % 1.0
        const px = src.x + dx * phase
        const py = src.y + dy * phase

        ctx.save()
        ctx.beginPath()
        ctx.arc(px, py, isConnected ? 3.5 : 2.5, 0, Math.PI * 2)
        ctx.fillStyle = isConnected ? '#e0f2fe' : '#38bdf8'
        ctx.shadowColor = '#38bdf8'
        ctx.shadowBlur = isConnected ? 8 : 4
        ctx.fill()
        ctx.restore()
      }
    }
  })

  // Draw Nodes
  for (const n of simNodes) {
    const isHovered = hoveredNode.value?.id === n.id
    const color = getNodeColor(n.kind)

    // Outer glow for hovered
    if (isHovered) {
      ctx.beginPath()
      ctx.arc(n.x, n.y, n.radius + 6, 0, Math.PI * 2)
      ctx.fillStyle = `${color}33`
      ctx.fill()
    }

    // Node body
    ctx.beginPath()
    ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2)
    ctx.fillStyle = color
    ctx.fill()
    ctx.lineWidth = n.compiled_truth ? 2.5 : 1.5
    ctx.strokeStyle = n.compiled_truth ? '#67e8f9' : '#0f172a'
    ctx.stroke()

    // Node title label
    ctx.font = '10px Inter, system-ui, sans-serif'
    ctx.fillStyle = isHovered ? '#ffffff' : '#cbd5e1'
    ctx.textAlign = 'center'
    const titleText = n.title.length > 18 ? n.title.slice(0, 16) + '…' : n.title
    ctx.fillText(titleText, n.x, n.y + n.radius + 12)
  }

  ctx.restore()
}

function loop() {
  updateSimulation()
  render()
  animId = requestAnimationFrame(loop)
}

function handleResize() {
  if (!containerRef.value || !canvasRef.value) return
  const dpr = window.devicePixelRatio || 1
  const w = containerRef.value.clientWidth
  const h = containerRef.value.clientHeight
  canvasRef.value.width = w * dpr
  canvasRef.value.height = h * dpr
  const ctx = canvasRef.value.getContext('2d')
  if (ctx) ctx.scale(dpr, dpr)
}

// Mouse Interactions
function getCanvasPoint(evt: MouseEvent) {
  if (!containerRef.value) return { x: 0, y: 0, screenX: 0, screenY: 0 }
  const rect = containerRef.value.getBoundingClientRect()
  const mouseX = evt.clientX - rect.left
  const mouseY = evt.clientY - rect.top
  return {
    x: (mouseX - transform.x) / transform.k,
    y: (mouseY - transform.y) / transform.k,
    screenX: mouseX,
    screenY: mouseY
  }
}

function onMouseDown(evt: MouseEvent) {
  const pt = getCanvasPoint(evt)
  lastMousePos = { x: evt.clientX, y: evt.clientY }

  // Check if clicked a node
  for (let i = simNodes.length - 1; i >= 0; i--) {
    const n = simNodes[i]
    const dx = pt.x - n.x
    const dy = pt.y - n.y
    if (dx * dx + dy * dy <= n.radius * n.radius) {
      draggedNode = n
      dragStartPos = { x: evt.clientX, y: evt.clientY }
      return
    }
  }

  // Otherwise drag the entire canvas
  isDragging = true
}

function onMouseMove(evt: MouseEvent) {
  const pt = getCanvasPoint(evt)
  tooltipPos.value = { x: pt.screenX, y: pt.screenY }

  if (draggedNode) {
    draggedNode.x = pt.x
    draggedNode.y = pt.y
    draggedNode.vx = 0
    draggedNode.vy = 0
    return
  }

  if (isDragging) {
    const dx = evt.clientX - lastMousePos.x
    const dy = evt.clientY - lastMousePos.y
    transform.x += dx
    transform.y += dy
    lastMousePos = { x: evt.clientX, y: evt.clientY }
    return
  }

  // Hover detection
  let found: SimNode | null = null
  for (let i = simNodes.length - 1; i >= 0; i--) {
    const n = simNodes[i]
    const dx = pt.x - n.x
    const dy = pt.y - n.y
    if (dx * dx + dy * dy <= n.radius * n.radius) {
      found = n
      break
    }
  }
  hoveredNode.value = found
}

function onMouseUp(evt: MouseEvent) {
  if (draggedNode) {
    const distSq = (evt.clientX - dragStartPos.x) ** 2 + (evt.clientY - dragStartPos.y) ** 2
    if (distSq < 9) {
      emit('select-node', draggedNode)
    }
  }
  isDragging = false
  draggedNode = null
}

function onWheel(evt: WheelEvent) {
  evt.preventDefault()
  if (!containerRef.value) return
  const rect = containerRef.value.getBoundingClientRect()
  const mouseX = evt.clientX - rect.left
  const mouseY = evt.clientY - rect.top

  const zoomFactor = evt.deltaY < 0 ? 1.15 : 0.85
  const newK = Math.max(0.2, Math.min(4.0, transform.k * zoomFactor))

  const curK = transform.k || 1.0
  transform.x = mouseX - ((mouseX - transform.x) * newK) / curK
  transform.y = mouseY - ((mouseY - transform.y) * newK) / curK
  transform.k = newK
}

let initialPinchDist: number | null = null
let initialPinchScale = 1.0
let lastTouchPos = { x: 0, y: 0 }

function getTouchPoint(touch: Touch) {
  if (!containerRef.value) return { x: 0, y: 0, screenX: 0, screenY: 0 }
  const rect = containerRef.value.getBoundingClientRect()
  const mouseX = touch.clientX - rect.left
  const mouseY = touch.clientY - rect.top
  return {
    x: (mouseX - transform.x) / transform.k,
    y: (mouseY - transform.y) / transform.k,
    screenX: mouseX,
    screenY: mouseY
  }
}

function onTouchStart(evt: TouchEvent) {
  if (evt.touches.length === 1) {
    const t = evt.touches[0]
    lastTouchPos = { x: t.clientX, y: t.clientY }
    const pt = getTouchPoint(t)
    for (let i = simNodes.length - 1; i >= 0; i--) {
      const n = simNodes[i]
      const dx = pt.x - n.x
      const dy = pt.y - n.y
      if (dx * dx + dy * dy <= n.radius * n.radius) {
        draggedNode = n
        dragStartPos = { x: t.clientX, y: t.clientY }
        evt.preventDefault()
        return
      }
    }
    isDragging = true
    evt.preventDefault()
  } else if (evt.touches.length === 2) {
    isDragging = false
    draggedNode = null
    const t1 = evt.touches[0]
    const t2 = evt.touches[1]
    initialPinchDist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY)
    initialPinchScale = transform.k
    evt.preventDefault()
  }
}

function onTouchMove(evt: TouchEvent) {
  if (evt.touches.length === 1) {
    const t = evt.touches[0]
    const pt = getTouchPoint(t)
    tooltipPos.value = { x: pt.screenX, y: pt.screenY }

    if (draggedNode) {
      draggedNode.x = pt.x
      draggedNode.y = pt.y
      draggedNode.vx = 0
      draggedNode.vy = 0
      evt.preventDefault()
      return
    }

    if (isDragging) {
      const dx = t.clientX - lastTouchPos.x
      const dy = t.clientY - lastTouchPos.y
      transform.x += dx
      transform.y += dy
      lastTouchPos = { x: t.clientX, y: t.clientY }
      evt.preventDefault()
      return
    }
  } else if (evt.touches.length === 2 && initialPinchDist && containerRef.value) {
    const t1 = evt.touches[0]
    const t2 = evt.touches[1]
    const currentDist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY)
    if (initialPinchDist > 0) {
      const scale = currentDist / initialPinchDist
      const newK = Math.max(0.2, Math.min(4.0, initialPinchScale * scale))
      const rect = containerRef.value.getBoundingClientRect()
      const midX = (t1.clientX + t2.clientX) / 2 - rect.left
      const midY = (t1.clientY + t2.clientY) / 2 - rect.top
      const curK = transform.k || 1.0
      transform.x = midX - ((midX - transform.x) * newK) / curK
      transform.y = midY - ((midY - transform.y) * newK) / curK
      transform.k = newK
    }
    evt.preventDefault()
  }
}

function onTouchEnd(evt: TouchEvent) {
  if (evt.touches.length === 0) {
    if (draggedNode) {
      const distSq = (lastTouchPos.x - dragStartPos.x) ** 2 + (lastTouchPos.y - dragStartPos.y) ** 2
      if (distSq < 16) {
        emit('select-node', draggedNode)
      }
    }
    isDragging = false
    draggedNode = null
    initialPinchDist = null
  } else if (evt.touches.length === 1) {
    initialPinchDist = null
    lastTouchPos = { x: evt.touches[0].clientX, y: evt.touches[0].clientY }
  }
}

onMounted(() => {
  handleResize()
  initSimulation()
  loop()

  window.addEventListener('resize', handleResize)
  if (containerRef.value) {
    containerRef.value.addEventListener('mousedown', onMouseDown)
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
    containerRef.value.addEventListener('wheel', onWheel, { passive: false })
    containerRef.value.addEventListener('touchstart', onTouchStart, { passive: false })
    containerRef.value.addEventListener('touchmove', onTouchMove, { passive: false })
    containerRef.value.addEventListener('touchend', onTouchEnd)
  }
})

onUnmounted(() => {
  if (animId) cancelAnimationFrame(animId)
  window.removeEventListener('resize', handleResize)
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
  if (containerRef.value) {
    containerRef.value.removeEventListener('touchstart', onTouchStart)
    containerRef.value.removeEventListener('touchmove', onTouchMove)
    containerRef.value.removeEventListener('touchend', onTouchEnd)
  }
})

watch(() => props.nodes, () => {
  initSimulation()
}, { deep: true })
</script>
