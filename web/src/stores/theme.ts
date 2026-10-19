import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export type SkinId = 'cyber-slate' | 'monolith' | 'arctic-dusk' | 'amber-console' | 'studio-light'

export interface SkinMeta {
  id: SkinId
  name: string
  namePl: string
  description: string
  descriptionPl: string
  isDark: boolean
  accentColor: string
}

export const SKINS: SkinMeta[] = [
  {
    id: 'cyber-slate',
    name: 'Cyber Slate',
    namePl: 'Cyber Slate',
    description: 'Balanced dark slate with neon emerald & sky accents (Default).',
    descriptionPl: 'Zrównoważony ciemny grafit z akcentami neonowego szmaragdu i błękitu.',
    isDark: true,
    accentColor: '#10b981',
  },
  {
    id: 'monolith',
    name: 'Monolith',
    namePl: 'Monolit',
    description: 'High-contrast obsidian black with silver borders and crisp typography.',
    descriptionPl: 'Wysokokontrastowa głęboka czerń z platynowo-srebrnymi liniami.',
    isDark: true,
    accentColor: '#f8fafc',
  },
  {
    id: 'arctic-dusk',
    name: 'Arctic Dusk',
    namePl: 'Arktyczny Zmierzch',
    description: 'Deep navy and midnight blue with ice cyan highlights.',
    descriptionPl: 'Głęboki granat z lodowo-błękitnymi akcentami cyan.',
    isDark: true,
    accentColor: '#06b6d4',
  },
  {
    id: 'amber-console',
    name: 'Amber Console',
    namePl: 'Bursztynowa Konsola',
    description: 'Retro terminal aesthetic with warm amber phosphor on dark obsidian.',
    descriptionPl: 'Estetyka terminala z ciepłym bursztynowym luminoforem.',
    isDark: true,
    accentColor: '#f59e0b',
  },
  {
    id: 'studio-light',
    name: 'Studio Light',
    namePl: 'Studio Jasne',
    description: 'Clean, glare-free daylight canvas with slate indigo contrast (WCAG AAA).',
    descriptionPl: 'Czysty, nieoślepiający motyw dzienny o kontraście zgodnym z WCAG AAA.',
    isDark: false,
    accentColor: '#4f46e5',
  },
]

const STORAGE_KEY_SKIN = 'botex_skin'
const STORAGE_KEY_THEME = 'botex_theme'

export const useThemeStore = defineStore('theme', () => {
  const currentSkinId = ref<SkinId>((() => {
    if (typeof localStorage !== 'undefined') {
      const savedSkin = localStorage.getItem(STORAGE_KEY_SKIN) as SkinId | null
      if (savedSkin && SKINS.some(s => s.id === savedSkin)) return savedSkin
      const legacyTheme = localStorage.getItem(STORAGE_KEY_THEME)
      if (legacyTheme === 'light') return 'studio-light'
    }
    return 'cyber-slate'
  })())

  const currentSkin = computed<SkinMeta>(() => {
    return SKINS.find(s => s.id === currentSkinId.value) || SKINS[0]
  })

  const isDark = computed<boolean>(() => currentSkin.value.isDark)

  function applySkinToDom(skinId: SkinId) {
    if (typeof document === 'undefined') return
    const root = document.documentElement
    root.setAttribute('data-skin', skinId)
    const meta = SKINS.find(s => s.id === skinId) || SKINS[0]
    if (meta.isDark) {
      root.classList.remove('light')
      root.classList.add('dark')
    } else {
      root.classList.remove('dark')
      root.classList.add('light')
    }
  }

  function setSkin(skinId: SkinId) {
    currentSkinId.value = skinId
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(STORAGE_KEY_SKIN, skinId)
      localStorage.setItem(STORAGE_KEY_THEME, isDark.value ? 'dark' : 'light')
    }
    applySkinToDom(skinId)
  }

  function toggleMode() {
    if (isDark.value) {
      setSkin('studio-light')
    } else {
      setSkin('cyber-slate')
    }
  }

  // Apply immediately on store creation
  applySkinToDom(currentSkinId.value)

  return {
    currentSkinId,
    currentSkin,
    isDark,
    skins: SKINS,
    setSkin,
    toggleMode,
    applySkinToDom,
  }
})
