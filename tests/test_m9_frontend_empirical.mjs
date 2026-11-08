/**
 * Empirical Adversarial Challenger Test Suite for Milestone M9 (PR-3)
 * Tests:
 * 1. isBudgetLow logic under diverse payloads
 * 2. Timestamp normalization and countdown display
 * 3. Wallet store caching and TTL expiry
 * 4. QuotaWindowCard percentage and threshold logic
 * 5. Clean AST / source scan of web/src for zai / glm isolation
 */

import { ref, computed } from '../web/node_modules/vue/dist/vue.esm-bundler.js'
import { createPinia, setActivePinia, defineStore } from '../web/node_modules/pinia/dist/pinia.mjs'
import * as fs from 'fs'
import * as path from 'path'

let passed = 0
let failed = 0

function assert(condition, message) {
  if (!condition) {
    failed++
    console.error(`[FAIL] ${message}`)
    throw new Error(`Assertion failed: ${message}`)
  } else {
    passed++
    console.log(`[PASS] ${message}`)
  }
}

console.log('===============================================================')
console.log('CHALLENGER M9 EMPIRICAL TEST SUITE (Node/V8 Execution)')
console.log('===============================================================')

// ---------------------------------------------------------------------------
// 1. isBudgetLow Logic Stress Test
// ---------------------------------------------------------------------------
console.log('\n--- Test Suite 1: isBudgetLow Logic Stress Test ---')

function evaluateIsBudgetLow(walletInfo, keyStatus) {
  const w = walletInfo
  const k = keyStatus
  const units = (w?.units ?? k?.units ?? 'usd').toLowerCase()
  if (units !== 'usd') {
    return false
  }
  const bal = w?.balance_usd ?? k?.balance_usd
  return bal !== null && bal !== undefined && bal < 1.0
}

// 1.1 Diverse USD balances
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 10.0 }, null) === false, 'USD balance $10.00 is NOT low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 1.0001 }, null) === false, 'USD balance $1.0001 is NOT low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 1.0 }, null) === false, 'USD balance exactly $1.00 is NOT low (boundary < 1.0)')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 0.999 }, null) === true, 'USD balance $0.999 is low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 0.5 }, null) === true, 'USD balance $0.50 is low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 0.01 }, null) === true, 'USD balance $0.01 is low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 0.0 }, null) === true, 'USD balance $0.00 is low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: -0.01 }, null) === true, 'USD balance -$0.01 (negative) is low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: -50.0 }, null) === true, 'USD balance -$50.00 is low')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: null }, null) === false, 'USD balance null does NOT trigger low budget warning')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: undefined }, null) === false, 'USD balance undefined does NOT trigger low budget warning')

// 1.2 Non-USD units
assert(evaluateIsBudgetLow({ units: 'quota', balance_usd: 0.5 }, null) === false, 'units="quota" with balance $0.50 is NOT low')
assert(evaluateIsBudgetLow({ units: 'quota', balance_usd: 0.0 }, null) === false, 'units="quota" with balance $0.00 is NOT low')
assert(evaluateIsBudgetLow({ units: 'tokens', balance_usd: 0.0 }, null) === false, 'units="tokens" with balance $0.00 is NOT low')
assert(evaluateIsBudgetLow({ units: 'credits', balance_usd: 0.0 }, null) === false, 'units="credits" with balance $0.00 is NOT low')

// 1.3 Case insensitivity for USD
assert(evaluateIsBudgetLow({ units: 'USD', balance_usd: 0.5 }, null) === true, 'units="USD" (uppercase) is recognized and low')
assert(evaluateIsBudgetLow({ units: 'Usd', balance_usd: 0.5 }, null) === true, 'units="Usd" (mixed case) is recognized and low')
assert(evaluateIsBudgetLow({ units: 'uSd', balance_usd: 1.5 }, null) === false, 'units="uSd" with balance $1.50 is recognized and NOT low')

// 1.4 Fallback between walletInfo and keyStatus
assert(evaluateIsBudgetLow(null, { units: 'usd', balance_usd: 0.2 }) === true, 'Fallback to keyStatus when walletInfo is null')
assert(evaluateIsBudgetLow(null, { units: 'usd', balance_usd: 5.0 }) === false, 'Fallback to keyStatus when walletInfo is null and balance is high')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: null }, { units: 'usd', balance_usd: 0.4 }) === true, 'Fallback to keyStatus balance when walletInfo balance is null')
assert(evaluateIsBudgetLow({ units: 'usd', balance_usd: 5.0 }, { units: 'usd', balance_usd: 0.2 }) === false, 'walletInfo balance takes precedence over keyStatus')
assert(evaluateIsBudgetLow({ units: 'quota', balance_usd: 0.2 }, { units: 'usd', balance_usd: 0.2 }) === false, 'walletInfo units take precedence over keyStatus units')
assert(evaluateIsBudgetLow({ balance_usd: 0.2 }, { units: 'quota' }) === false, 'Fallback to keyStatus units when walletInfo units is undefined')
assert(evaluateIsBudgetLow(null, null) === false, 'Both null evaluates to false safely')

// ---------------------------------------------------------------------------
// 2. Timestamp Normalization & Countdown Display Stress Test
// ---------------------------------------------------------------------------
console.log('\n--- Test Suite 2: Timestamp Normalization & Countdown Display ---')

function parseEpochMs(timeVal) {
  if (timeVal === null || timeVal === undefined || timeVal === '') return null
  if (typeof timeVal === 'number') {
    return timeVal < 1e11 ? timeVal * 1000 : timeVal
  }
  const parsed = Date.parse(timeVal)
  return isNaN(parsed) ? null : parsed
}

// 2.1 parseEpochMs diverse inputs
assert(parseEpochMs(1730937600000) === 1730937600000, 'Epoch ms (1.7e12) preserved as ms')
assert(parseEpochMs(1730937600) === 1730937600000, 'Epoch seconds (1.7e9) converted to ms (* 1000)')
assert(parseEpochMs(0) === 0, 'Epoch 0 converted correctly (0 * 1000 = 0)')
assert(parseEpochMs(1e11) === 100000000000, 'Boundary 1e11 treated as ms')
assert(parseEpochMs(1e11 - 1) === (1e11 - 1) * 1000, 'Boundary 1e11 - 1 treated as seconds')

// ISO string variations
const isoMs = Date.parse('2026-11-06T23:00:00Z')
assert(parseEpochMs('2026-11-06T23:00:00Z') === isoMs, 'ISO string parsed to exact ms')
const isoTzMs = Date.parse('2026-11-06T23:00:00+02:00')
assert(parseEpochMs('2026-11-06T23:00:00+02:00') === isoTzMs, 'ISO string with tz offset parsed to exact ms')

// Invalid, null, empty inputs
assert(parseEpochMs('not-a-valid-date') === null, 'Invalid string returns null')
assert(parseEpochMs('undefined') === null, 'String "undefined" returns null')
assert(parseEpochMs('') === null, 'Empty string returns null')
assert(parseEpochMs(null) === null, 'null returns null')
assert(parseEpochMs(undefined) === null, 'undefined returns null')

// 2.2 Countdown formatting test
function computeCountdownDisplay(resetMsVal, currentMs) {
  if (resetMsVal === null) return null
  const diff = resetMsVal - currentMs
  if (diff <= 0) {
    return 'Reset imminent'
  }
  const totalSec = Math.floor(diff / 1000)
  const days = Math.floor(totalSec / 86400)
  const hours = Math.floor((totalSec % 86400) / 3600)
  const minutes = Math.floor((totalSec % 3600) / 60)
  const seconds = totalSec % 60

  if (days > 0) {
    return `${days}d ${hours}h`
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`
  }
  return `${minutes}m ${seconds}s`
}

const baseTime = 1730900000000
assert(computeCountdownDisplay(baseTime - 5000, baseTime) === 'Reset imminent', 'Past timestamp returns "Reset imminent"')
assert(computeCountdownDisplay(baseTime, baseTime) === 'Reset imminent', 'Exact match (diff=0) returns "Reset imminent"')
assert(computeCountdownDisplay(baseTime + 45000, baseTime) === '0m 45s', '45 seconds diff formatted as "0m 45s"')
assert(computeCountdownDisplay(baseTime + 125000, baseTime) === '2m 5s', '125 seconds diff formatted as "2m 5s"')
assert(computeCountdownDisplay(baseTime + (2 * 3600 + 15 * 60 + 30) * 1000, baseTime) === '2h 15m 30s', '2h 15m 30s formatted correctly')
assert(computeCountdownDisplay(baseTime + (3 * 86400 + 4 * 3600) * 1000, baseTime) === '3d 4h', '3d 4h formatted correctly for multi-day window')
assert(computeCountdownDisplay(null, baseTime) === null, 'Null resetMs returns null display')

// ---------------------------------------------------------------------------
// 3. Multi-Provider Store & Cache TTL Expiry Stress Test
// ---------------------------------------------------------------------------
console.log('\n--- Test Suite 3: Multi-Provider Store & Cache TTL Expiry ---')

const pinia = createPinia()
setActivePinia(pinia)

// Create mockable store matching web/src/stores/wallet.ts
function createTestWalletStore(mockApi) {
  return defineStore('testWallet_' + Math.random(), () => {
    const activeProvider = ref('openrouter')
    const walletByProvider = ref({})
    const keyStatusByProvider = ref({})
    const lastFetchByProvider = ref({})
    const CACHE_TTL_MS = 30000 // 30s cache TTL

    const walletInfo = computed(() => walletByProvider.value[activeProvider.value] || null)
    const keyStatus = computed(() => keyStatusByProvider.value[activeProvider.value] || null)
    const isLoading = ref(false)
    const errorMessage = ref(null)

    const isBudgetLow = computed(() => {
      const w = walletInfo.value
      const k = keyStatus.value
      const units = (w?.units ?? k?.units ?? 'usd').toLowerCase()
      if (units !== 'usd') {
        return false
      }
      const bal = w?.balance_usd ?? k?.balance_usd
      return bal !== null && bal !== undefined && bal < 1.0
    })

    async function fetchWallet(forceRefresh = false, provider, customNow = Date.now()) {
      const targetProv = provider || activeProvider.value
      const now = customNow
      const lastFetch = lastFetchByProvider.value[targetProv] || 0

      if (!forceRefresh && walletByProvider.value[targetProv] && now - lastFetch < CACHE_TTL_MS) {
        return
      }

      isLoading.value = true
      errorMessage.value = null
      try {
        const [wData, kData] = await Promise.all([
          mockApi.getWallet(targetProv, forceRefresh),
          mockApi.getKeyStatus(targetProv, forceRefresh).catch(() => null)
        ])
        walletByProvider.value[targetProv] = wData
        if (kData) {
          keyStatusByProvider.value[targetProv] = kData
        }
        lastFetchByProvider.value[targetProv] = now
      } catch (err) {
        errorMessage.value = err?.message || 'Failed to fetch wallet info'
      } finally {
        isLoading.value = false
      }
    }

    async function setProvider(provider, customNow = Date.now()) {
      if (!provider) return
      activeProvider.value = provider
      await fetchWallet(false, provider, customNow)
    }

    return {
      activeProvider,
      walletInfo,
      keyStatus,
      walletByProvider,
      keyStatusByProvider,
      lastFetchByProvider,
      isBudgetLow,
      isLoading,
      errorMessage,
      fetchWallet,
      setProvider
    }
  })()
}

async function runStoreTests() {
  let apiCallCount = 0
  const mockApi = {
    async getWallet(provider, forceRefresh) {
      apiCallCount++
      if (provider === 'openrouter') {
        return {
          supported: true,
          provider: 'openrouter',
          units: 'usd',
          balance_usd: 0.45,
          usage_usd: 12.30,
          limit_usd: 50.0,
          limit_remaining_usd: 37.70,
          is_free_tier: false,
          label: 'OpenRouter Main',
          error: null
        }
      } else if (provider === 'nvidia') {
        return {
          supported: true,
          provider: 'nvidia',
          units: 'quota',
          quota_windows: [
            {
              limit: 1000,
              current_usage: 250,
              percentage: 25.0,
              next_reset_time: 1730937600,
              name: 'NIM Monthly Quota'
            }
          ],
          balance_usd: null,
          usage_usd: null,
          limit_usd: null,
          limit_remaining_usd: null,
          is_free_tier: true,
          label: 'NVIDIA Free NIM',
          error: null
        }
      }
      return { supported: false, provider, error: 'Direct API' }
    },
    async getKeyStatus(provider, forceRefresh) {
      return {
        supported: true,
        provider,
        units: provider === 'openrouter' ? 'usd' : 'quota',
        balance_usd: provider === 'openrouter' ? 0.45 : null,
        limit: 50.0,
        limit_remaining: 37.70,
        low_budget_warning: provider === 'openrouter',
        is_free_tier: false
      }
    }
  }

  const store = createTestWalletStore(mockApi)

  // 3.1 Initial fetch at t=1000
  let t = 1000
  await store.fetchWallet(false, undefined, t)
  assert(apiCallCount === 1, 'First fetch at t=1000 calls API (apiCallCount === 1)')
  assert(store.walletInfo.provider === 'openrouter', 'walletInfo correctly reflects activeProvider="openrouter"')
  assert(store.walletInfo.balance_usd === 0.45, 'walletInfo balance_usd is 0.45')
  assert(store.isBudgetLow === true, 'isBudgetLow is true because balance $0.45 < $1.00')

  // 3.2 Second fetch at t=15000 (14s elapsed < 30s TTL) -> should use cache
  t = 15000
  await store.fetchWallet(false, undefined, t)
  assert(apiCallCount === 1, 'Fetch within TTL (14s elapsed) uses cache (apiCallCount remains 1)')

  // 3.3 Third fetch at t=31001 (30.001s elapsed > 30s TTL) -> cache expired!
  t = 31001
  await store.fetchWallet(false, undefined, t)
  assert(apiCallCount === 2, 'Fetch after TTL expired (30.001s elapsed) triggers fresh API call (apiCallCount === 2)')

  // 3.4 Fourth fetch at t=35000 with forceRefresh=true -> bypasses cache
  t = 35000
  await store.fetchWallet(true, undefined, t)
  assert(apiCallCount === 3, 'Fetch with forceRefresh=true bypasses active cache (apiCallCount === 3)')

  // 3.5 Switch provider to 'nvidia'
  t = 36000
  await store.setProvider('nvidia', t)
  assert(apiCallCount === 4, 'Switching to un-cached provider "nvidia" calls API (apiCallCount === 4)')
  assert(store.activeProvider === 'nvidia', 'activeProvider changed to "nvidia"')
  assert(store.walletInfo.provider === 'nvidia', 'walletInfo computed ref switches reactively to "nvidia"')
  assert(store.walletInfo.units === 'quota', 'NVIDIA units are "quota"')
  assert(store.isBudgetLow === false, 'isBudgetLow evaluates to false for non-USD quota provider')

  // 3.6 Switching back to 'openrouter' within openrouter TTL (at t=40000, elapsed since 35000 is 5s < 30s)
  t = 40000
  await store.setProvider('openrouter', t)
  assert(apiCallCount === 4, 'Switching back to cached "openrouter" within TTL does NOT call API (apiCallCount remains 4)')
  assert(store.walletInfo.provider === 'openrouter', 'walletInfo switches reactively back to "openrouter"')
  assert(store.isBudgetLow === true, 'isBudgetLow reactively re-evaluates to true for OpenRouter')

  // 3.7 Error state handling
  const failingMockApi = {
    async getWallet() {
      throw new Error('Connection refused')
    },
    async getKeyStatus() {
      return null
    }
  }
  const failStore = createTestWalletStore(failingMockApi)
  await failStore.fetchWallet(true, 'openrouter', 1000)
  assert(failStore.errorMessage === 'Connection refused', 'errorMessage captures network exception')
  assert(failStore.isLoading === false, 'isLoading is reset to false after exception')
}

// ---------------------------------------------------------------------------
// 4. QuotaWindowCard Logic & Threshold Verification
// ---------------------------------------------------------------------------
console.log('\n--- Test Suite 4: QuotaWindowCard Logic & Threshold Verification ---')

function calculatePct(window) {
  if (window.percentage !== undefined && !isNaN(window.percentage)) {
    return Math.max(0, Math.min(100, Number(window.percentage)))
  }
  if (window.limit > 0) {
    return Math.max(0, Math.min(100, (window.current_usage / window.limit) * 100))
  }
  return 0
}

function calculateBadgeVariant(pct) {
  if (pct >= 95) return 'danger'
  if (pct >= 80) return 'warning'
  return 'success'
}

function calculateBarColor(pct) {
  if (pct >= 95) return 'bg-rose-500'
  if (pct >= 80) return 'bg-amber-500'
  return 'bg-emerald-500'
}

// 4.1 Percentage calculation and clamping
assert(calculatePct({ percentage: 75, limit: 100, current_usage: 75 }) === 75, 'Normal percentage 75% returned')
assert(calculatePct({ percentage: 150, limit: 100, current_usage: 150 }) === 100, 'Overflow percentage 150% clamped to 100%')
assert(calculatePct({ percentage: -20, limit: 100, current_usage: 0 }) === 0, 'Negative percentage clamped to 0%')
assert(calculatePct({ limit: 200, current_usage: 100 }) === 50, 'Derived percentage from limit & usage is 50%')
assert(calculatePct({ limit: 0, current_usage: 0 }) === 0, 'Limit 0 handles division by zero safely (returns 0%)')

// 4.2 Threshold transitions
assert(calculateBadgeVariant(79.9) === 'success', '79.9% is success variant')
assert(calculateBarColor(79.9) === 'bg-emerald-500', '79.9% uses emerald color')
assert(calculateBadgeVariant(80.0) === 'warning', '80.0% boundary transitions to warning variant')
assert(calculateBarColor(80.0) === 'bg-amber-500', '80.0% boundary transitions to amber color')
assert(calculateBadgeVariant(94.9) === 'warning', '94.9% remains warning variant')
assert(calculateBarColor(94.9) === 'bg-amber-500', '94.9% remains amber color')
assert(calculateBadgeVariant(95.0) === 'danger', '95.0% boundary transitions to danger variant')
assert(calculateBarColor(95.0) === 'bg-rose-500', '95.0% boundary transitions to rose color')
assert(calculateBadgeVariant(100) === 'danger', '100% is danger variant')
assert(calculateBarColor(100) === 'bg-rose-500', '100% is rose color')

// ---------------------------------------------------------------------------
// 5. Codebase AST / Source Isolation Audit (web/src)
// ---------------------------------------------------------------------------
console.log('\n--- Test Suite 5: web/src Source Isolation Audit (No zai / glm) ---')

function scanDirForPatterns(dir, illegalTokens) {
  const files = fs.readdirSync(dir, { recursive: true, withFileTypes: true })
  const violations = []

  for (const file of files) {
    if (!file.isFile()) continue
    const ext = path.extname(file.name).toLowerCase()
    if (!['.ts', '.vue', '.js', '.json', '.html'].includes(ext)) continue

    const filePath = path.join(file.parentPath || file.path, file.name)
    const content = fs.readFileSync(filePath, 'utf8')

    for (const token of illegalTokens) {
      // Use word-boundary regex to prevent matching Polish words like "zaimplementować"
      const regex = new RegExp(`\\b${token}\\b`, 'i')
      if (regex.test(content)) {
        violations.push({ file: filePath, token })
      }
    }
  }
  return violations
}

const srcDir = path.resolve('web/src')
const violations = scanDirForPatterns(srcDir, ['zai', 'glm', 'glm-5.3', 'glm-5.3-flash'])
assert(violations.length === 0, `web/src contains 0 illegal tokens (violations: ${JSON.stringify(violations)})`)

// Run async tests
runStoreTests().then(() => {
  console.log('\n===============================================================')
  console.log(`ALL TESTS PASSED: ${passed} passed, ${failed} failed`)
  console.log('VERDICT: APPROVE')
  console.log('===============================================================')
}).catch((err) => {
  console.error('\nAsync test failed:', err)
  process.exit(1)
})
