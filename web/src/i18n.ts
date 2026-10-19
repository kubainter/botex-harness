import { ref, computed } from 'vue'

export type UiLang = 'en' | 'pl'
export type UiTheme = 'dark' | 'light'

const STORAGE_KEY_LANG = 'botex_ui_lang'
const STORAGE_KEY_THEME = 'botex_theme'

const initialLang = ((): UiLang => {
  if (typeof localStorage !== 'undefined') {
    const saved = localStorage.getItem(STORAGE_KEY_LANG)
    if (saved === 'pl' || saved === 'en') return saved
  }
  return 'en'
})()

const initialTheme = ((): UiTheme => {
  if (typeof localStorage !== 'undefined') {
    const saved = localStorage.getItem(STORAGE_KEY_THEME)
    if (saved === 'light' || saved === 'dark') return saved
  }
  return 'dark'
})()

export const currentLang = ref<UiLang>(initialLang)
export const currentTheme = ref<UiTheme>(initialTheme)

export function applyDomTheme(mode: UiTheme) {
  if (typeof document !== 'undefined') {
    const root = document.documentElement
    if (mode === 'light') {
      root.classList.remove('dark')
      root.classList.add('light')
      root.setAttribute('data-skin', 'studio-light')
    } else {
      root.classList.remove('light')
      root.classList.add('dark')
      const skin = (typeof localStorage !== 'undefined' ? localStorage.getItem('botex_skin') : null) || 'cyber-slate'
      root.setAttribute('data-skin', skin === 'studio-light' ? 'cyber-slate' : skin)
    }
  }
}

// Apply theme immediately on module load
applyDomTheme(initialTheme)

export function setTheme(mode: UiTheme) {
  currentTheme.value = mode
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_KEY_THEME, mode)
    if (mode === 'light') {
      localStorage.setItem('botex_skin', 'studio-light')
    } else {
      const skin = localStorage.getItem('botex_skin')
      if (skin === 'studio-light' || !skin) {
        localStorage.setItem('botex_skin', 'cyber-slate')
      }
    }
  }
  applyDomTheme(mode)
}

export function toggleTheme() {
  setTheme(currentTheme.value === 'dark' ? 'light' : 'dark')
}

export function setUiLang(lang: UiLang) {
  currentLang.value = lang
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(STORAGE_KEY_LANG, lang)
  }
}

const MESSAGES: Record<UiLang, Record<string, string>> = {
  en: {
    // Navigation
    'nav.chat': 'Chat & Plan',
    'nav.chat.short': 'Chat',
    'nav.tasks': 'Background Tasks',
    'nav.tasks.short': 'Tasks',
    'nav.approvals': 'Operator Approvals',
    'nav.approvals.short': 'Approvals',
    'nav.memory': 'Memory Vault',
    'nav.memory.short': 'Memory',
    'nav.wallet': 'Budget & Models',
    'nav.wallet.short': 'Budget',
    'nav.settings': 'Configuration',
    'nav.settings.short': 'Config',

    // Header & Sidebar
    'app.subtitle': 'Cloud Gateway & MCP',
    'app.online': 'Backend online',
    'app.offline': 'Backend unreachable',
    'app.offline.banner': 'Offline mode active. Running from cached PWA shell.',
    'app.unreachable.banner': 'Cannot reach BoteX gateway at ',
    'app.auth.bearer': 'Bearer Auth',
    'app.auth.local': 'Local Mode',
    'app.budget.low': 'Low credit balance (< $1.00)',

    // Chat & Model Selector
    'chat.mode.chat': 'Chat (Read-Only)',
    'chat.mode.agent': 'Agent (Task Runner)',
    'chat.history': 'Conversation History',
    'chat.new_session': 'New session',
    'chat.convert_task': 'Create Task from Chat',
    'chat.empty.title': 'Workspace Console',
    'chat.empty.subtitle': 'Inspect repository files, query project memory, or draft an execution plan before running a background task.',
    'chat.input.placeholder': 'Ask a question or describe a change... (Enter to send, Shift+Enter for newline)',
    'chat.send': 'Send question',
    'chat.streaming': 'Reading workspace and generating response...',
    'chat.inspector.title': 'Workspace Inspector',
    'chat.inspector.tasks': 'Active Tasks',
    'chat.inspector.approvals': 'Operator Gate',
    'chat.inspector.memory': 'Recent Memory Notes',
    'chat.history.button': 'History',
    'chat.history.title': 'Conversation History',
    'chat.history.subtitle': 'Manage and resume previous sessions saved in .botex/chat_sessions/',
    'chat.history.search': 'Filter conversations...',
    'chat.history.empty': 'No saved conversations found.',
    'chat.history.delete_confirm': 'Are you sure you want to delete this conversation?',
    'chat.save_memory.button': 'Save to Memory',
    'chat.save_memory.title': 'Save to Memory Vault',
    'chat.save_memory.subtitle': 'Store conversation transcript or context into .botex/memory/',
    'chat.save_memory.note_title': 'Note Title',
    'chat.save_memory.kind': 'Category',
    'chat.save_memory.scope': 'Storage Scope',
    'chat.save_memory.tags': 'Tags (comma separated)',
    'chat.save_memory.content': 'Note Content (Markdown)',
    'chat.save_memory.submit': 'Save to Memory',
    'chat.save_memory.success': 'Conversation saved to Memory Vault (.botex/memory/)',
    'chat.save_memory.error': 'Failed to save to memory vault',
    'chat.copy': 'Copy',
    'chat.copied': 'Copied!',
    'chat.copy_message': 'Copy message content to clipboard',

    // Model Selector Modal
    'models.title': 'Select Execution Model',
    'models.subtitle': 'Search live catalog across OpenRouter, Free Tier, and NVIDIA NIM',
    'models.search.placeholder': 'Filter by model name, slug, or provider (e.g. liquid, r1, qwen, free)...',
    'models.filter.all': 'Wszystkie (All)',
    'models.filter.free': '100% Free Tier',
    'models.filter.cheap': 'Ultra-Cheap / Fast',
    'models.filter.reasoning': 'Reasoning / Planners',
    'models.filter.coding': 'Coding / Agents',
    'models.empty': 'No models match the current filter.',
    'models.select_hint': 'Arrow keys to navigate · Enter to select · Esc to close',

    // Tasks View (dotted + camelCase aliases)
    'tasks.title': 'Background Tasks',
    'tasks.subtitle': 'Run autonomous file edits, test suites, and code verification in isolated worker jobs.',
    'tasks.new': 'New Task',
    'tasks.refresh': 'Refresh',
    'tasks.history': 'Task Runs',
    'tasks.empty': 'No tasks queued yet. Start a new task or convert a chat session.',
    'tasks.create.title': 'Run Background Task',
    'tasks.create.subtitle': 'Specify the target change, model, and workspace permissions.',
    tasksTitle: 'Background Tasks',
    tasksSubtitle: 'Run file edits, test suites, and code verification in isolated worker jobs.',
    refresh: 'Refresh',
    newTask: 'New Task',
    activeExecution: 'Active Execution',
    abortTask: 'Abort Task',
    taskHistory: 'Task Runs',
    noTasksYet: 'No tasks queued yet. Start a new task or convert a chat session.',
    cancel: 'Cancel',
    selectTaskPrompt: 'Select a task on the left to inspect its status and verification output.',
    delegateTaskTitle: 'Run Background Task',
    delegateTaskSub: 'Specify the target change, execution model, and workspace permissions.',
    taskSpecLabel: 'Task Specification *',
    taskSpecPlaceholder: 'Describe what BoteX should implement, refactor, or verify...',
    execModelLabel: 'Execution Model',
    permModeLabel: 'Permission Mode',
    budgetLimitLabel: 'Budget Cap (USD)',
    maxTurnsLabel: 'Max Turns',
    launchTask: 'Start Task',

    // Approvals View
    'approvals.title': 'Operator Approvals',
    'approvals.subtitle': 'Review and approve file writes, destructive actions, and shell commands before execution.',
    'approvals.empty.title': 'No actions waiting for review',
    'approvals.empty.subtitle': 'High-risk tool calls and shell commands requested by running tasks appear here in real time.',
    'approvals.modal.title': 'Operator Approval Required',
    'approvals.modal.subtitle': 'Review the proposed tool call before the task continues.',
    approvalsTitle: 'Operator Approvals',
    approvalsSubtitle: 'Review and approve file writes, destructive actions, and shell commands before execution.',
    noPendingApprovals: 'No Pending Approvals',
    noPendingApprovalsDesc: 'High-risk tool calls and shell commands requested by running tasks appear here in real time.',
    rememberSession: 'Remember decision for this session',
    rejectAction: 'Reject...',
    confirmReject: 'Confirm Reject',
    approveAction: 'Approve Action',

    // Memory View
    'memory.title': 'Project Memory',
    'memory.subtitle': 'Persistent notes, architecture decisions, and handoff logs stored in local and global vaults.',
    'memory.add': 'Save Note',
    'memory.empty': 'No notes match the current filter. Save a note to record project context or architecture decisions.',
    memoryTitle: 'Project Memory',
    memorySubtitle: 'Persistent notes, architecture decisions, and handoff logs stored in local and global vaults.',
    addMemory: 'Save Note',
    vaultEntries: 'Vault Entries',
    noMemoryFound: 'No notes match the current filter. Save a note to record project context or architecture decisions.',
    saveToVault: 'Save to Vault',

    // Wallet View
    'wallet.title': 'Budget & Model Catalog',
    'wallet.subtitle': 'OpenRouter credit balance, token spend breakdown, and benchmark-ranked models.',
    walletTitle: 'Budget & Model Catalog',
    walletSubtitle: 'OpenRouter credit balance, token spend breakdown, and benchmark-ranked models.',
    forceRefresh: 'Force Refresh',
    activeBalance: 'Active Balance',
    totalUsage: 'Total Usage',
    limitCap: 'Spend Cap',
    remainingQuota: 'Remaining Quota',
    ledgerTitle: 'Token & Spend Ledger',
    recommendedRoster: 'Recommended Models by Role',

    // Settings View
    'settings.title': 'Gateway Configuration',
    'settings.subtitle': 'Configure backend URL, Bearer token authentication, interface language, and theme.',
    settingsTitle: 'Gateway Configuration',
    settingsSubtitle: 'Configure backend URL, Bearer token authentication, interface language, and theme.',
    backendConnection: 'Backend Connection & Credentials',
    saveConfig: 'Save Configuration',
    testConnection: 'Test Connection',
    interfaceLanguage: 'Interface & Language'
  },
  pl: {
    // Navigation (naturalny język programisty - miodkuj)
    'nav.chat': 'Rozmowa i plan',
    'nav.chat.short': 'Czat',
    'nav.tasks': 'Zadania w tle',
    'nav.tasks.short': 'Zadania',
    'nav.approvals': 'Akceptacja operatora',
    'nav.approvals.short': 'Zgody',
    'nav.memory': 'Pamięć projektu',
    'nav.memory.short': 'Pamięć',
    'nav.wallet': 'Budżet i modele',
    'nav.wallet.short': 'Budżet',
    'nav.settings': 'Ustawienia',
    'nav.settings.short': 'Opcje',

    // Header & Sidebar
    'app.subtitle': 'Brama Cloud i MCP',
    'app.online': 'Połączono z serwerem',
    'app.offline': 'Brak połączenia z serwerem',
    'app.offline.banner': 'Tryb offline. Interfejs działa z lokalnej pamięci podręcznej.',
    'app.unreachable.banner': 'Brak połączenia z serwerem BoteX pod adresem ',
    'app.auth.bearer': 'Token Bearer',
    'app.auth.local': 'Tryb lokalny',
    'app.budget.low': 'Niski stan konta (< $1.00)',

    // Chat & Model Selector
    'chat.mode.chat': 'Czat (tylko odczyt)',
    'chat.mode.agent': 'Agent (edycja kodu)',
    'chat.history': 'Historia rozmów',
    'chat.new_session': 'Nowa rozmowa',
    'chat.convert_task': 'Przekształć w zadanie',
    'chat.empty.title': 'Konsola robocza BoteX',
    'chat.empty.subtitle': 'Przeglądaj pliki repozytorium, przeszukuj pamięć projektu lub przygotuj plan zmian przed uruchomieniem zadania.',
    'chat.input.placeholder': 'Wyślij pytanie lub opisz zmianę w kodzie... (Enter wysyła, Shift+Enter nowa linia)',
    'chat.send': 'Wyślij pytanie',
    'chat.streaming': 'Odczyt kontekstu i generowanie odpowiedzi...',
    'chat.inspector.title': 'Podgląd sesji',
    'chat.inspector.tasks': 'Aktywne zadania',
    'chat.inspector.approvals': 'Akceptacja operatora',
    'chat.inspector.memory': 'Notatki w pamięci',
    'chat.history.button': 'Historia',
    'chat.history.title': 'Historia rozmów',
    'chat.history.subtitle': 'Wracaj do poprzednich wątków zapisanych w .botex/chat_sessions/',
    'chat.history.search': 'Filtruj rozmowy...',
    'chat.history.empty': 'Brak zapisanych rozmów w historii.',
    'chat.history.delete_confirm': 'Czy na pewno chcesz usunąć tę rozmowę?',
    'chat.save_memory.button': 'Zapisz w pamięci',
    'chat.save_memory.title': 'Zapisz w pamięci projektu',
    'chat.save_memory.subtitle': 'Zachowaj ustalenia z rozmowy lub kontekst w .botex/memory/',
    'chat.save_memory.note_title': 'Tytuł notatki',
    'chat.save_memory.kind': 'Kategoria',
    'chat.save_memory.scope': 'Zakres zapisu',
    'chat.save_memory.tags': 'Tagi (oddzielone przecinkami)',
    'chat.save_memory.content': 'Treść notatki (Markdown)',
    'chat.save_memory.submit': 'Zapisz w pamięci',
    'chat.save_memory.success': 'Zapisano pomyślnie w pamięci projektu (.botex/memory/)',
    'chat.save_memory.error': 'Nie udało się zapisać notatki w pamięci',
    'chat.copy': 'Kopiuj',
    'chat.copied': 'Skopiowano!',
    'chat.copy_message': 'Skopiuj treść wiadomości do schowka',

    // Model Selector Modal
    'models.title': 'Wybierz model wykonawczy',
    'models.subtitle': 'Przeszukaj katalog modeli OpenRouter, darmowego pakietu Free oraz NVIDIA NIM',
    'models.search.placeholder': 'Szukaj po nazwie, slugu lub dostawcy (np. liquid, r1, qwen, free)...',
    'models.filter.all': 'Wszystkie',
    'models.filter.free': '100% Free Tier',
    'models.filter.cheap': 'Ultra-Cheap / Fast',
    'models.filter.reasoning': 'Reasoning / Planners',
    'models.filter.coding': 'Coding / Agents',
    'models.empty': 'Żaden model nie pasuje do wybranych filtrów.',
    'models.select_hint': 'Strzałki: nawigacja · Enter: wybór · Esc: zamknij',

    // Tasks View
    'tasks.title': 'Zadania w tle',
    'tasks.subtitle': 'Zlecaj edycję kodu, refaktoryzację i uruchamianie testów w osobnych procesach roboczych.',
    'tasks.new': 'Nowe zadanie',
    'tasks.refresh': 'Odśwież',
    'tasks.history': 'Historia zadań',
    'tasks.empty': 'Brak zleconych zadań. Utwórz nowe zadanie lub przekształć rozmowę z czatu.',
    'tasks.create.title': 'Zleć zadanie w tle',
    'tasks.create.subtitle': 'Określ cel zadania, model wykonawczy oraz uprawnienia w katalogu roboczym.',
    tasksTitle: 'Zadania w tle',
    tasksSubtitle: 'Zlecaj edycję kodu, refaktoryzację i uruchamianie testów w osobnych procesach roboczych.',
    refresh: 'Odśwież',
    newTask: 'Nowe zadanie',
    activeExecution: 'Aktywne wykonanie',
    abortTask: 'Przerwij zadanie',
    taskHistory: 'Historia zadań',
    noTasksYet: 'Brak zleconych zadań. Utwórz nowe zadanie lub przekształć rozmowę z czatu.',
    cancel: 'Anuluj',
    selectTaskPrompt: 'Wybierz zadanie z listy po lewej stronie, aby zobaczyć szczegóły i wynik weryfikacji.',
    delegateTaskTitle: 'Zleć zadanie w tle',
    delegateTaskSub: 'Określ cel zadania, model wykonawczy oraz uprawnienia w katalogu roboczym.',
    taskSpecLabel: 'Opis zadania *',
    taskSpecPlaceholder: 'Opisz co BoteX ma zaimplementować, przebudować lub przetestować...',
    execModelLabel: 'Model wykonawczy',
    permModeLabel: 'Tryb uprawnień',
    budgetLimitLabel: 'Limit kosztu (USD)',
    maxTurnsLabel: 'Maks. liczba kroków',
    launchTask: 'Uruchom zadanie',

    // Approvals View
    'approvals.title': 'Akceptacja operatora',
    'approvals.subtitle': 'Zatwierdzaj lub odrzucaj zapisy plików, usuwanie zasobów i polecenia powłoki.',
    'approvals.empty.title': 'Brak operacji czekających na decyzję',
    'approvals.empty.subtitle': 'Gdy zadanie w tle spróbuje zmienić pliki lub uruchomić polecenie wymagające zgody, zobaczysz je tutaj.',
    'approvals.modal.title': 'Wymagana akceptacja operatora',
    'approvals.modal.subtitle': 'Sprawdź szczegóły operacji przed wznowieniem pracy agenta.',
    approvalsTitle: 'Akceptacja operatora',
    approvalsSubtitle: 'Zatwierdzaj lub odrzucaj zapisy plików, usuwanie zasobów i polecenia powłoki.',
    noPendingApprovals: 'Brak operacji czekających na decyzję',
    noPendingApprovalsDesc: 'Gdy zadanie w tle spróbuje zmienić pliki lub uruchomić polecenie wymagające zgody, zobaczysz je tutaj.',
    rememberSession: 'Zapamiętaj decyzję w tej sesji',
    rejectAction: 'Odrzuć...',
    confirmReject: 'Potwierdź odrzucenie',
    approveAction: 'Zatwierdź operację',

    // Memory View
    'memory.title': 'Pamięć projektu',
    'memory.subtitle': 'Trwałe notatki techniczne, decyzje architektoniczne i podsumowania sesji (lokalne oraz globalne).',
    'memory.add': 'Dodaj notatkę',
    'memory.empty': 'Brak pasujących wpisów w pamięci. Dodaj notatkę, aby zapisać ustalenia lub kontekst projektu.',
    memoryTitle: 'Pamięć projektu',
    memorySubtitle: 'Trwałe notatki techniczne, decyzje architektoniczne i podsumowania sesji (lokalne oraz globalne).',
    addMemory: 'Dodaj notatkę',
    vaultEntries: 'Zapisane notatki',
    noMemoryFound: 'Brak pasujących wpisów w pamięci. Dodaj notatkę, aby zapisać ustalenia lub kontekst projektu.',
    saveToVault: 'Zapisz notatkę',

    // Wallet View
    'wallet.title': 'Budżet i katalog modeli',
    'wallet.subtitle': 'Saldo konta OpenRouter, zużycie tokenów oraz zestawienie modeli według zadań.',
    walletTitle: 'Budżet i katalog modeli',
    walletSubtitle: 'Saldo konta OpenRouter, zużycie tokenów oraz zestawienie modeli według zadań.',
    forceRefresh: 'Odśwież dane',
    activeBalance: 'Dostępne saldo',
    totalUsage: 'Łączne zużycie',
    limitCap: 'Limit wydatków',
    remainingQuota: 'Pozostały limit',
    ledgerTitle: 'Rejestr tokenów i kosztów',
    recommendedRoster: 'Polecane modele według roli',

    // Settings View
    'settings.title': 'Konfiguracja połączenia',
    'settings.subtitle': 'Adres serwera BoteX Cloud, token dostępowy, język interfejsu oraz motyw kolorystyczny.',
    settingsTitle: 'Konfiguracja połączenia',
    settingsSubtitle: 'Adres serwera BoteX Cloud, token dostępowy, język interfejsu oraz motyw kolorystyczny.',
    backendConnection: 'Połączenie z serwerem i autoryzacja',
    saveConfig: 'Zapisz ustawienia',
    testConnection: 'Sprawdź połączenie',
    interfaceLanguage: 'Interfejs i język'
  }
}

export function useI18n() {
  const lang = computed(() => currentLang.value)
  const theme = computed(() => currentTheme.value)
  function t(key: string): string {
    return MESSAGES[currentLang.value]?.[key] || MESSAGES.en[key] || key
  }
  return {
    lang,
    locale: lang,
    theme,
    setUiLang,
    setLocale: setUiLang,
    setTheme,
    toggleTheme,
    t
  }
}
