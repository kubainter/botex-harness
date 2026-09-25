# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Configuration Layer
=========================
Layered configuration for the BoteX harness. Resolution order
(lowest to highest priority):

1. ``botex.config.json``       — committed defaults shipped with the repo
2. ``botex.config.local.json`` — gitignored, machine-local overrides
3. ``BOTEX_*`` environment variables

All paths declared in the config are resolved against the project root
(the directory containing this ``botex`` package) unless absolute.
"""
import fnmatch
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

CONFIG_FILE = PROJECT_ROOT / "botex.config.json"
LOCAL_CONFIG_FILE = PROJECT_ROOT / "botex.config.local.json"

DEFAULTS: Dict[str, Any] = {
    # Model providers are OpenAI-compatible API endpoints. Each provider owns
    # its base_url, API-key env var, per-request extra_body, and model
    # profiles. Exactly one provider should carry "default": true — it is the
    # startup provider when a request does not name one explicitly.
    "providers": {
        "openrouter": {
            "default": True,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            # OpenRouter-only: enforce Zero Data Retention on every request.
            # reasoning.max_tokens is a server-side cap on hidden "thinking"
            # so reasoning models cannot burn the whole max_tokens budget
            # before emitting output (TOKEN_LIMIT). Unlike "effort" it is an
            # absolute, model-agnostic limit — effort hints are ignored by
            # some upstreams (observed on deepseek-r1).
            "extra_body": {
                "provider": {"data_collection": "deny"},
                "reasoning": {"max_tokens": 3000},
            },
            "zdr": {
                "enabled": True,
                "deny_patterns": [":free", "openrouter/free"],
            },
            "models": {
                "default": "openrouter/auto",
                "auto-beta": "openrouter/auto-beta",
                "coding": "openrouter/pareto-code",
                # Cheap non-reasoning tier for read-only recon — resolved
                # via engine.mode_profiles when the caller picks no model.
                "fast": "deepseek/deepseek-v4-flash-0731",
            },
            # Optional ordered failover candidates. Used only when the first
            # model fails before touching files; explicit model= disables it.
            "model_fallbacks": {},
            # Slugs/globs of decisions-only models served by a dedicated
            # endpoint (e.g. OpenRouter /api/alpha/decisions). They answer
            # typed questions instead of running a chat tool loop — the
            # engine rejects them locally before any billable call.
            "decisions_models": [],
        },
        "nvidia": {
            "default": False,
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key_env": "NVIDIA_API_KEY",
            "extra_body": {},
            # Explicit per-model capability declarations — consulted before
            # catalog metadata. NVIDIA publishes no supported_parameters,
            # so declaring e.g. {"<slug>": {"supports_tools": true}} is how
            # a model becomes verifiable under pricing.on_unknown=deny.
            "capabilities": {},
            "models": {
                "default": "meta/llama-3.3-70b-instruct",
                "coding": "qwen/qwen3-coder-480b-a35b-instruct",
                "fast": "meta/llama-3.3-70b-instruct",
            },
        },
    },
    "engine": {
        "max_turns": 15,
        # Per-turn completion cap. This is a ceiling, not a charge — billing
        # follows actual usage. Keep it generous: reasoning models spend
        # tokens on internal thinking before emitting any content, and large
        # create_file/apply_patch calls carry whole file bodies in one turn.
        "max_tokens": 8000,
        # Raised per-turn cap applied automatically once a response reports
        # reasoning tokens (thinking shares the max_tokens budget). Also the
        # single rescue-retry cap when a turn ends with finish_reason=length
        # and no visible output. 0 disables the mechanism.
        "reasoning_max_tokens": 16000,
        # Hard per-request wall-clock bound (seconds). Without it a single
        # call is bounded only by max_tokens x model speed — a slow
        # reasoning model can think for many minutes in one turn. 0 uses
        # the HTTP client's default timeout.
        "request_timeout_s": 300,
        # Provider error strings are masked and truncated to this length
        # before they reach result dicts, MCP responses or analytics —
        # raw SDK exception bodies can carry payloads and headers.
        "max_error_chars": 1024,
        "max_concurrent_tasks": 3,
        "max_queued_tasks": 32,
        "task_result_ttl_s": 3600,
        # Total wall-clock limit per task (seconds). Checked between turns —
        # a task exceeding it ends with TIME_LIMIT. 0 = no limit.
        "max_duration_s": 900,
        # Global wall-clock budget for the whole fallback chain (seconds).
        # Each attempt still gets max_duration_s; once the chain exceeds
        # this, no further candidates are tried — the cost guard for
        # exhaustion-kind fallbacks. 0 = the chain is bounded by the
        # per-attempt max_duration_s (a caller's limit is never silently
        # multiplied by the candidate count); set a larger value to allow
        # longer chains.
        "max_task_duration_s": 0,
        "temperature": 0.1,
        "budget_limit_usd": 0.0,
        # Capability mode preset used when a request does not name one:
        # readonly | edit | destructive | full (see botex/capabilities.py).
        "default_mode": "edit",
        # Model profile used when a request does not name one
        # (e.g. default | coding | auto-beta — resolved per provider).
        "default_profile": "default",
        # Capability-mode -> model profile mapping, consulted ONLY when the
        # caller leaves both `model` and `profile` empty. readonly recon runs
        # on the cheap "fast" tier; mutating modes keep the default/coding
        # tier. Empty string falls back to default_profile.
        "mode_profiles": {
            "readonly": "fast",
            "edit": "",
            "destructive": "coding",
            "full": "coding",
        },
        # When False, delete_file/move_file are not exposed to the agent at all.
        # Callers may still gate them per-run via confirm_fn / CLI prompt.
        "allow_destructive": False,
        # When True (default), apply_patch and create_file (on existing files)
        # require the file to have been inspected via outline/read tools first.
        "require_read_before_write": True,
    },
    # Read-only public web access. Net is never part of a mode by default;
    # callers must opt in per run with allow_net=true. Policies:
    # - allowlist: config hosts only; per-run entries may narrow them.
    # - caller: per-run hosts/URL prefixes replace config defaults.
    # - public: any public http(s) host; per-run entries may narrow it.
    # - off: read_url and fetch_url are disabled.
    "net": {
        "enabled": True,
        "policy": "caller",
        "allowed_hosts": ["docs.openrouter.ai", "openrouter.ai"],
        # URL rules authorize an exact URL, or a subtree when the path ends in
        # '/'. They are useful when a caller wants to grant one document only.
        "allowed_urls": [],
        "timeout_s": 20,
        "max_bytes": 200000,
        "max_requests_per_task": 5,
        "max_redirects": 3,
    },
    # Command execution policy (run_command). Master switch ("enabled")
    # is False by default; commands run with operator privileges.
    "exec": {
        "enabled": False,
        "allowlist": ["pytest", "python", "py", "ruff", "mypy", "node", "npm", "git", "pip"],
        # Single-word entries match whole argument tokens; multi-word entries
        # match "<binary> <args>". Keep eval-style flags and git mutations out.
        "deny_args": [
            "python -c", "python3 -c", "py -c", "node -e", "node --eval",
            "node -p", "npm exec", "npx",
            "pip install", "pip uninstall",
            "git push", "git commit", "git clean", "git reset", "git checkout",
            "git switch", "git restore", "git rm", "git config", "git rebase",
            "git merge", "git pull", "git fetch", "git clone", "git apply",
            "git stash", "git tag", "git branch -d", "git branch -D",
            "sudo", "rm", "del", "format", "shutdown", "reboot",
            "curl", "wget", "iex", "invoke-expression",
        ],
        "timeout_s": 120,
        "max_output_bytes": 20000,
    },
    "paths": {
        "snapshot_dir": ".snapshots",
        "snapshot_auto_prune": True,
        "snapshot_max_age_days": 7,
        "snapshot_max_total_mb": 50,
        "analytics_file": ".agent_analytics.json",
        # Optional extra dotenv file to load (relative to project root or absolute).
        "env_file": "",
    },
    "app": {
        # Optional HTTP-Referer sent to OpenRouter for app attribution.
        # The product name ("BoteX") is NOT configurable — it is the harness'
        # released identity.
        "referer": "",
    },
    "analytics": {
        # When False (default), persisted run summaries are secret-masked and
        # truncated to a preview; the full text survives only as a SHA-256 hash.
        # When True, the full (still secret-masked) summary text is stored.
        "store_full_text": False,
        "preview_chars": 160,
    },
    "ui": {
        # CLI language for human-facing output: "auto" (OS locale), "en", "pl".
        # MCP tool responses always stay English — they are an agent-facing API.
        "language": "auto",
    },
    # Pre-flight pricing guardrail: before the first API call, the resolved
    # model is checked against the provider's published price list
    # (USD per 1M tokens). Over-cap models are rejected locally — zero cost.
    "pricing": {
        "enabled": True,
        "max_input_per_mtok": 5.0,
        "max_output_per_mtok": 5.0,
        # Router/meta models (openrouter/auto, pareto-code, ...) report dynamic
        # pricing — "allow" lets them through (cap cannot see inside a router),
        # "deny" requires an explicit priced model.
        "on_unknown": "allow",
        "cache_ttl_hours": 24,
    },
    # Secrets belong ONLY in botex.config.local.json (gitignored) — never in
    # the committed botex.config.json. Environment variables take precedence.
    "secrets": {
        "openrouter_api_key": "",
    },
}

APP_NAME = "BoteX"

_ENV_MAP = {
    "BOTEX_MAX_TURNS": ("engine", "max_turns"),
    "BOTEX_MAX_TOKENS": ("engine", "max_tokens"),
    "BOTEX_REASONING_MAX_TOKENS": ("engine", "reasoning_max_tokens"),
    "BOTEX_REQUEST_TIMEOUT_S": ("engine", "request_timeout_s"),
    "BOTEX_MAX_CONCURRENT_TASKS": ("engine", "max_concurrent_tasks"),
    "BOTEX_TASK_RESULT_TTL_S": ("engine", "task_result_ttl_s"),
    "BOTEX_MAX_DURATION_S": ("engine", "max_duration_s"),
    "BOTEX_TEMPERATURE": ("engine", "temperature"),
    "BOTEX_BUDGET_USD": ("engine", "budget_limit_usd"),
    "BOTEX_ALLOW_DESTRUCTIVE": ("engine", "allow_destructive"),
    "BOTEX_REQUIRE_READ_BEFORE_WRITE": ("engine", "require_read_before_write"),
    "BOTEX_DEFAULT_MODE": ("engine", "default_mode"),
    "BOTEX_DEFAULT_PROFILE": ("engine", "default_profile"),
    "BOTEX_EXEC_ENABLED": ("exec", "enabled"),
    "BOTEX_NET_ENABLED": ("net", "enabled"),
    "BOTEX_NET_POLICY": ("net", "policy"),
    "BOTEX_SNAPSHOT_DIR": ("paths", "snapshot_dir"),
    "BOTEX_ANALYTICS_FILE": ("paths", "analytics_file"),
    "BOTEX_ENV_FILE": ("paths", "env_file"),
    "BOTEX_REFERER": ("app", "referer"),
    "BOTEX_ANALYTICS_FULL_TEXT": ("analytics", "store_full_text"),
    "BOTEX_LANG": ("ui", "language"),
    "BOTEX_PRICE_MAX_INPUT": ("pricing", "max_input_per_mtok"),
    "BOTEX_PRICE_MAX_OUTPUT": ("pricing", "max_output_per_mtok"),
}

_config_cache: Optional[Dict[str, Any]] = None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def load_config(reload: bool = False) -> Dict[str, Any]:
    """Return the merged configuration dict (defaults < file < local < env)."""
    global _config_cache
    if _config_cache is not None and not reload:
        return _config_cache

    cfg = _deep_merge(DEFAULTS, _load_json(CONFIG_FILE))
    cfg = _deep_merge(cfg, _load_json(LOCAL_CONFIG_FILE))

    for env_name, (section, key) in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        default_val = DEFAULTS[section][key]
        try:
            if isinstance(default_val, bool):
                value: Any = raw.lower() in ("1", "true", "yes")
            elif isinstance(default_val, int):
                value = int(raw)
            elif isinstance(default_val, float):
                value = float(raw)
            else:
                value = raw
        except ValueError:
            continue
        cfg.setdefault(section, {})[key] = value

    _config_cache = cfg
    return cfg


def _resolve_path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


def set_local_config(dotted_key: str, value: Any) -> None:
    """
    Persist ``dotted_key = value`` into ``botex.config.local.json``
    (gitignored machine-local overrides) and reload the config cache.
    """
    data = _load_json(LOCAL_CONFIG_FILE)
    keys = dotted_key.split(".")
    node = data
    for k in keys[:-1]:
        node = node.setdefault(k, {})
        if not isinstance(node, dict):
            raise ValueError(f"Cannot set '{dotted_key}': '{k}' is not a section")
    node[keys[-1]] = value
    LOCAL_CONFIG_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    load_config(reload=True)


def resolve_provider(name: str = "") -> tuple[str, Dict[str, Any]]:
    """
    Resolve the active provider. Explicit ``name`` wins; otherwise the first
    provider flagged ``"default": true`` is used; otherwise ``openrouter``
    when present, else the first configured provider. Returns ``(name, cfg)``;
    ``cfg`` is empty when an explicitly named provider does not exist.
    """
    providers = load_config().get("providers", {})
    if name:
        return name, providers.get(name, {})
    for pname, pcfg in providers.items():
        if pcfg.get("default"):
            return pname, pcfg
    if "openrouter" in providers:
        return "openrouter", providers["openrouter"]
    if providers:
        return next(iter(providers.items()))
    return "", {}


def resolve_model(model: str = "", profile: str = "", provider: str = "") -> str:
    """
    Resolve which model to run. Priority (highest first):
    explicit ``model`` > ``BOTEX_<PROFILE>_MODEL`` env >
    ``providers[<provider>].models[<profile>]`` >
    ``providers[<provider>].models.default`` > legacy top-level ``models``.
    """
    if model:
        return model
    if not profile:
        profile = (
            load_config().get("engine", {}).get("default_profile") or "default"
        )
    env_override = os.environ.get(
        f"BOTEX_{profile.upper().replace('-', '_')}_MODEL"
    )
    if env_override:
        return env_override
    _pname, pcfg = resolve_provider(provider)
    models = pcfg.get("models", {}) if pcfg else {}
    resolved = models.get(profile) or models.get("default")
    if resolved:
        return resolved
    legacy = load_config().get("models", {})
    return legacy.get(profile) or legacy.get("default") or "openrouter/auto"


def resolve_model_candidates(model: str = "", profile: str = "", provider: str = "") -> list[str]:
    """Resolve the primary model plus optional configured failover models."""
    if model:
        return [model]
    if not profile:
        profile = load_config().get("engine", {}).get("default_profile") or "default"
    _pname, pcfg = resolve_provider(provider)
    primary = resolve_model("", profile, provider)
    configured = (pcfg.get("model_fallbacks", {}) if pcfg else {}).get(profile, [])
    if isinstance(configured, str):
        configured = [configured]
    return list(dict.fromkeys([primary] + [m for m in configured if m]))


def is_decisions_model(model: str, provider_cfg: Dict[str, Any]) -> bool:
    """Whether the model is a decisions-only model for this provider.

    Decision models (e.g. TypeSafe Jev on OpenRouter) answer typed
    questions about a ``state`` via a dedicated endpoint instead of
    chat/completions — routing them to the tool loop only produces a
    provider 400. Declared per provider as ``decisions_models`` (exact
    slugs or ``*``/``?`` globs).
    """
    patterns = (provider_cfg or {}).get("decisions_models") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(
        fnmatch.fnmatchcase(model, str(p)) for p in patterns if p)


def engine_setting(key: str, override: Optional[Any] = None) -> Any:
    """Return ``override`` when provided, else the configured engine value."""
    if override is not None:
        return override
    return load_config().get("engine", {}).get(key, DEFAULTS["engine"].get(key))


def snapshot_dir() -> Path:
    return _resolve_path(load_config()["paths"]["snapshot_dir"])


def analytics_file() -> Path:
    return _resolve_path(load_config()["paths"]["analytics_file"])


def env_file() -> Optional[Path]:
    value = load_config()["paths"].get("env_file") or ""
    return _resolve_path(value) if value else None


def analytics_setting(key: str):
    """Return the configured ``analytics`` value (falls back to defaults)."""
    return load_config().get("analytics", {}).get(key, DEFAULTS["analytics"].get(key))


def app_headers() -> Dict[str, str]:
    """OpenRouter app attribution headers (referer sent only when configured)."""
    headers = {"X-Title": APP_NAME}
    referer = load_config().get("app", {}).get("referer")
    if referer:
        headers["HTTP-Referer"] = referer
    return headers
