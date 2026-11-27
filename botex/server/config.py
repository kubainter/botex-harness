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

import contextvars
import copy
import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Context variable holding the authenticated MCP client's provenance ("<agent>@<host>")
# populated by server_cloud.py from X-Agent-Id / X-Host headers on SSE/message requests.
MCP_CLIENT_ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "botex_mcp_client_origin", default=""
)

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def is_cross_host_windows_path(value: str) -> bool:
    """Return True when running on POSIX and ``value`` is a Windows drive path (e.g. ``C:\\...``)."""
    return os.name != "nt" and bool(_WINDOWS_DRIVE_RE.match(str(value or "").strip()))


def get_mcp_client_origin(default: str = "botex") -> str:
    """Return the active MCP client origin from contextvars, or ``default`` if unset."""
    val = MCP_CLIENT_ORIGIN.get("").strip()
    return val if val else default


try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def _find_project_root() -> Path:
    curr = Path(__file__).resolve().parent
    for p in [curr] + list(curr.parents):
        if (p / "botex.config.json").exists() or (p / "pyproject.toml").exists():
            return p
    return (
        curr.parent.parent
        if curr.name in ("core", "providers", "tools", "memory", "skills", "server")
        else curr.parent
    )


PROJECT_ROOT = _find_project_root()
PACKAGE_DIR = PROJECT_ROOT / "botex"

_DOTENV_DISABLED = {"1", "true", "yes", "on"}


def _dotenv_is_disabled() -> bool:
    return os.environ.get("BOTEX_DISABLE_DOTENV", "").strip().lower() in _DOTENV_DISABLED


if load_dotenv is not None and not _dotenv_is_disabled():
    _root_env = PROJECT_ROOT / ".env"
    if _root_env.exists():
        load_dotenv(dotenv_path=_root_env, override=False)
    load_dotenv(override=False)

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
            "denylist": [],
            "profile_denylists": {},
            "models": {
                "default": "openrouter/auto",
                "auto-beta": "openrouter/auto-beta",
                "coding": "openrouter/pareto-code",
                "review": "anthropic/claude-sonnet-4",
                # Cheap non-reasoning tier for read-only recon — resolved
                # via engine.mode_profiles when the caller picks no model.
                "fast": "google/gemini-2.5-flash",
            },
            # Optional ordered failover candidates. Used only when the first
            # model fails before touching files; explicit model= disables it.
            "model_fallbacks": {},
        },
        "nvidia": {
            "default": False,
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key_env": "NVIDIA_API_KEY",
            "extra_body": {},
            "denylist": [],
            "profile_denylists": {},
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
        # Free-tier preset for operators without a paid subscription.
        # ZDR is explicitly OFF here: :free models may be served by
        # endpoints that log/train on prompts — the operator opts in by
        # selecting this provider (per run, via BOTEX_PROVIDER, or by
        # marking it default). All profiles resolve to tool-capable :free
        # slugs; fallbacks stay :free — never silently paid.
        "openrouter-free": {
            "default": False,
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
            "extra_body": {
                "provider": {"data_collection": "allow"},
                "reasoning": {"max_tokens": 3000},
            },
            "zdr": {"enabled": False},
            "denylist": [],
            "profile_denylists": {},
            "models": {
                "default": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "coding": "cohere/north-mini-code:free",
                "fast": "google/gemma-4-31b-it:free",
            },
            "model_fallbacks": {
                "default": [
                    "nvidia/nemotron-3.5-lightning:free",
                    "qwen/qwen3.8-27b:free",
                ],
                "coding": [
                    "poolside/laguna-s-2.1:free",
                    "qwen/qwen3.8-27b:free",
                ],
                "fast": [
                    "nvidia/nemotron-3.5-lightning:free",
                    "liquid/lfm-2.5-2.6b:free",
                ],
            },
            # Free tier gets more patience: longer waits across more retries.
            "retry": {
                "enabled": True,
                "max_waits": 3,
                "max_wait_s": 90.0,
                "jitter_s": 1.5,
            },
        },
        # Google AI Studio (Gemini) via its OpenAI-compatible endpoint
        # (https://ai.google.dev/gemini-api/docs/openai). The /models
        # catalog publishes no pricing or supported_parameters — the
        # pre-flight gates stay verifiable through pricing_overrides and
        # capabilities below. There is no OpenRouter-style data_collection
        # flag and no credits API; ZDR is operator-declared and OFF by
        # default — selecting this provider is the opt-in. The denylist
        # keeps non-chat families (image/audio/video/embedding/robotics,
        # experimental, tuned, legacy) out of agent profiles.
        # reasoning_effort caps hidden thinking so it cannot burn the
        # whole max_tokens budget before emitting output.
        "google": {
            "default": False,
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "api_key_env": "GEMINI_API_KEY",
            "stream": True,
            "extra_body": {"reasoning_effort": "low"},
            "zdr": {"enabled": False},
            # Shared retry layer (botex/retry.py): 429 + 503-overload
            # waits, then provider_rate_limited -> model_fallbacks. Daily
            # quota errors skip the wait and fall back immediately.
            "retry": {"enabled": True, "max_waits": 2, "max_wait_s": 60.0, "jitter_s": 1.5},
            "denylist": [
                "*image*",
                "*imagen*",
                "*veo*",
                "*embedding*",
                "*gecko*",
                "*tts*",
                "*speech*",
                "*audio*",
                "*transcribe*",
                "*lyria*",
                "*music*",
                "*chirp*",
                "*realtime*",
                "*-live*",
                "*antigravity*",
                "*gemma*",
                "*paligemma*",
                "*codegemma*",
                "*robotics*",
                "*rt-*",
                "*computer-use*",
                "*deep-research*",
                "*nano*",
                "*banana*",
                "*learnlm*",
                "*aqa*",
                "*medlm*",
                "*med-gemini*",
                "*bison*",
                "*gemini-1.0*",
                "*gemini-pro*",
                "*gemini-ultra*",
                "tunedmodels/*",
                "*tuned*",
                "*-exp*",
                "*-experimental*",
            ],
            "profile_denylists": {},
            "capabilities": {
                "gemini-2.5-flash": {"supports_tools": True, "context_length": 1048576},
                "gemini-2.5-pro": {"supports_tools": True, "context_length": 1048576},
                "gemini-2.5-flash-lite": {"supports_tools": True, "context_length": 1048576},
            },
            "models": {
                "default": "gemini-2.5-flash",
                "coding": "gemini-2.5-pro",
                "fast": "gemini-2.5-flash-lite",
            },
            "model_fallbacks": {
                "default": ["gemini-2.5-flash-lite"],
                "coding": ["gemini-2.5-flash"],
                "fast": ["gemini-2.5-flash"],
            },
        },
        # GroqCloud (Groq) via its OpenAI-compatible endpoint
        # (https://console.groq.com/docs/openai). Free-plan limits are
        # per-model RPM/RPD/TPM/TPD enforced org-wide — mirrored under
        # rate_limits.groq; there is no credits API and /models
        # publishes no pricing, so free_tier keeps the pricing gate off
        # (flip to false on a paid Developer plan). The denylist keeps
        # non-chat families (speech/guard/safeguard) and enterprise-gated
        # slugs out of agent profiles. GroqProvider injects
        # reasoning_effort per model slug (gpt-oss -> low, qwen3 ->
        # none) — keep extra_body empty so no single value leaks onto
        # models that reject it.
        "groq": {
            "default": False,
            "base_url": "https://api.groq.com/openai/v1",
            "api_key_env": "GROQ_API_KEY",
            "stream": True,
            "free_tier": True,
            "zdr": {"enabled": False},
            "extra_body": {},
            "retry": {"enabled": True, "max_waits": 2, "max_wait_s": 60.0, "jitter_s": 1.5},
            "denylist": [
                "*whisper*",
                "*orpheus*",
                "*prompt-guard*",
                "*safeguard*",
                "*guard*",
                "*tts*",
                "*speech*",
                "*audio*",
                "*transcribe*",
                "*playai*",
                "*lyria*",
                "*music*",
                "llama-3.1-8b-instant",
                "llama-3.3-70b-versatile",
                "meta-models/*",
                "minimaxai/*",
            ],
            "profile_denylists": {},
            "capabilities": {
                "openai/gpt-oss-120b": {"supports_tools": True, "context_length": 131072},
                "openai/gpt-oss-20b": {"supports_tools": True, "context_length": 131072},
                "qwen/qwen3.8-27b": {"supports_tools": True, "context_length": 131072},
            },
            "models": {
                "default": "openai/gpt-oss-120b",
                "coding": "openai/gpt-oss-120b",
                "fast": "openai/gpt-oss-20b",
            },
            "model_fallbacks": {
                "default": ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
                "coding": ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
                "fast": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
            },
        },
        # Cloudflare Workers AI via its OpenAI-compatible endpoint
        # (https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/).
        # {account_id} resolves from providers.cloudflare.account_id or the
        # CLOUDFLARE_ACCOUNT_ID env var — never commit a real account id.
        # Free allocation: 10,000 Neurons/day (resets 00:00 UTC); over-quota
        # calls fail HTTP 429 instead of billing. The denylist keeps
        # non-chat families (embeddings/image/TTS/ASR/translation/
        # classification/vision-only/lora-bases/guard) and Workers-Paid-
        # gated frontier models (@cf/moonshotai/*, @cf/zai-org/glm-5*,
        # @cf/deepseek-ai/deepseek-v4*, @cf/thinkingmachines/*) out of
        # agent profiles; clef/clairic decision models are rejected by
        # is_decisions_model, not the denylist.
        "cloudflare": {
            "default": False,
            "base_url": "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
            "api_key_env": "CLOUDFLARE_API_TOKEN",
            "account_id": "",
            "free_tier": True,
            "zdr": {"enabled": False},
            "extra_body": {},
            "retry": {"enabled": True, "max_waits": 2, "max_wait_s": 60.0, "jitter_s": 1.5},
            "denylist": [
                "*embedding*",
                "*embed*",
                "@cf/baai/*",
                "@cf/plamo/*",
                "@cf/qwen/qwen3-embedding*",
                "@cf/google/embeddinggemma*",
                "@cf/black-forest-labs/*",
                "@cf/bytedance/*",
                "@cf/leonardo/*",
                "@cf/runwayml/*",
                "@cf/stabilityai/*",
                "@cf/lykon/*",
                "*flux*",
                "@cf/deepgram/*",
                "@cf/myshell/*",
                "@cf/openai/whisper*",
                "*whisper*",
                "*tts*",
                "*speech*",
                "*audio*",
                "*aura*",
                "*melotts*",
                "@cf/pipecat/*",
                "@cf/ai4bharat/*",
                "*m2m100*",
                "*indictrans*",
                "@cf/huggingface/*",
                "*distilbert*",
                "*resnet*",
                "@cf/moondream/*",
                "*llava*",
                "*lora",
                "@cf/meta/llama-2-*",
                "@cf/meta/llama-guard*",
                "*guard*",
                "@cf/swiss-ai/*",
                "@cf/utter-project/*",
                "@cf/moonshotai/*",
                "@cf/zai-org/glm-5*",
                "@cf/deepseek-ai/deepseek-v4*",
                "@cf/thinkingmachines/*",
                # Deprecated upstream 2026-11-20 (HTTP 410) — CF's catalog
                # still lists them, so keep them out of agent profiles.
                "@cf/meta/llama-3.1-8b-instruct-fp8",
                "@cf/qwen/qwen3-30b-a3b-fp8",
            ],
            "profile_denylists": {},
            "capabilities": {
                "@cf/meta/llama-3.3-70b-instruct-fp8-fast": {
                    "supports_tools": True,
                    "context_length": 131072,
                },
                "@cf/qwen/qwen2.5-coder-32b-instruct": {
                    "supports_tools": True,
                    "context_length": 32768,
                },
                "@cf/qwen/qwen3.8-27b": {
                    "supports_tools": True,
                    "context_length": 131072,
                },
                "@cf/openai/gpt-oss-120b": {
                    "supports_tools": True,
                    "context_length": 131072,
                },
                "@cf/openai/gpt-oss-20b": {
                    "supports_tools": True,
                    "context_length": 131072,
                },
                "@cf/zai-org/glm-4.7-flash": {
                    "supports_tools": True,
                    "context_length": 131072,
                },
            },
            "models": {
                "default": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "coding": "@cf/qwen/qwen2.5-coder-32b-instruct",
                "fast": "@cf/zai-org/glm-4.7-flash",
            },
            "model_fallbacks": {
                "default": ["@cf/qwen/qwen3.8-27b", "@cf/zai-org/glm-4.7-flash"],
                "coding": ["@cf/qwen/qwen3.8-27b", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"],
                "fast": ["@cf/qwen/qwen3.8-27b", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"],
            },
            "decisions_models": ["@cf/cloudflare/clef*", "@cf/cloudflare/clairic*"],
        },
        "ollama": {
            "default": False,
            "base_url": "http://127.0.0.1:11434/v1",
            "api_key_env": "OLLAMA_API_KEY",
            "api_key_fallback": "ollama",
            "local": True,
            "extra_body": {},
            "denylist": [],
            "profile_denylists": {},
            "models": {
                "default": "bielik:11b",
                "coding": "bielik:11b",
                "fast": "bielik:11b",
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
        # Project directory used when neither the caller (-w / MCP
        # workspace_dir) nor the environment names one. Empty = fall back
        # to the process working directory. Must be an existing directory.
        "default_workspace": "",
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
        # Capability-mode -> step-limit override, consulted ONLY when the
        # caller leaves `max_turns` unset. A readonly review pass legitimately
        # needs more read steps than an edit run needs write steps — observed
        # fast-tier models starving on large-file reviews: 15 turns of small
        # reads over 1500-line files end as MAX_TURNS_REACHED with nothing
        # produced. 0/missing falls back to max_turns.
        "mode_max_turns": {
            "readonly": 30,
            "edit": 0,
            "destructive": 0,
            "full": 0,
        },
        # Task-size routing: disabled by default. When enabled
        # and the caller names neither model nor profile (and mode_profiles
        # resolved empty), estimate_task_tier classifies the prompt/files
        # into a tier and `tiers` maps it to a model profile — cheap tiers
        # for small jobs, stronger tiers for multi-file work.
        "task_size_routing": {
            "enabled": False,
            "tiers": {
                "high": "coding",
                "standard": "default",
                "budget": "fast",
            },
        },
        # Provider circuit breaker: consecutive server-side or
        # network failures trip a provider OPEN so dead endpoints fail fast
        # instead of burning request budget; after recovery_timeout_s a
        # single HALF_OPEN probe may retry. Per-provider override:
        # providers.<name>.circuit_breaker.
        "circuit_breaker": {
            "enabled": True,
            "failure_threshold": 3,
            "recovery_timeout_s": 60.0,
        },
        # When False, delete_file/move_file are not exposed to the agent at all.
        # Callers may still gate them per-run via confirm_fn / CLI prompt.
        "allow_destructive": False,
        # When True (default), apply_patch and create_file (on existing files)
        # require the file to have been inspected via outline/read tools first.
        "require_read_before_write": True,
        "denylist": [],
        "profile_denylists": {},
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
            "python -c",
            "python3 -c",
            "py -c",
            "node -e",
            "node --eval",
            "node -p",
            "npm exec",
            "npx",
            "npm install",
            "npm i",
            "npm ci",
            "pip install",
            "pip uninstall",
            "pip wheel",
            "git push",
            "git commit",
            "git clean",
            "git reset",
            "git checkout",
            "git switch",
            "git restore",
            "git rm",
            "git config",
            "git rebase",
            "git merge",
            "git pull",
            "git fetch",
            "git clone",
            "git apply",
            "git stash",
            "git tag",
            "git branch -d",
            "git branch -D",
            "git -C",
            "git --git-dir",
            "git --work-tree",
            "--git-dir",
            "--work-tree",
            "git worktree",
            "git checkout-index",
            "git archive",
            "git show",
            "git remote",
            "sudo",
            "rm",
            "del",
            "format",
            "shutdown",
            "reboot",
            "curl",
            "wget",
            "iex",
            "invoke-expression",
            "..",
        ],
        "timeout_s": 120,
        "max_output_bytes": 20000,
    },
    # OpenTelemetry export: NullSpan until enabled + otel extra installed (OTEL_SDK_DISABLED wins).
    "tracing": {
        "enabled": False,
        "exporter": "none",
        "endpoint": "",
        "sample_ratio": 1.0,
    },
    # Offload large tool output to Memory Vault, returning failure-aware preview + ref.
    "context_guard": {
        "enabled": True,
        "threshold_bytes": 2048,
        "max_preview_bytes": 3072,
        "auto_vault_logs": True,
        "vault_retention_days": 3,
    },
    "paths": {
        "snapshot_dir": ".snapshots",
        "snapshot_auto_prune": True,
        "snapshot_max_age_days": 7,
        "snapshot_max_total_mb": 50,
        "analytics_file": ".agent_analytics.json",
        "global_memory_dir": "~/.botex/memory",
        # Third READ-ONLY search root for memory scope "swarm"/"all"
        # Empty = OFF — no implicit cross-workspace
        # reads; write tools never inherit this root.
        "swarm_memory_dir": "",
        # Cloud Soul store (botex/core/swarm_store.py). Empty = default
        # ~/.botex/swarm_store.db; relative paths resolve against PROJECT_ROOT.
        "swarm_store_db": "",
        "chat_sessions_dir": "",
        # Optional extra dotenv file to load (relative to project root or absolute).
        "env_file": "",
    },
    "app": {
        # Optional HTTP-Referer for OpenRouter app attribution (product name "BoteX" is fixed).
        "referer": "",
    },
    "analytics": {
        # False = secret-masked preview + SHA-256 hash; True = full secret-masked summary.
        "store_full_text": False,
        "preview_chars": 160,
        # Periodic local-LLM digest (<500 tok prompt for small local models); manual: POST /api/analytics/digest.
        "digest_enabled": False,
        "digest_interval_s": 3600,
        "digest_provider": "",
        "digest_model": "",
        "digest_max_tokens": 320,
        "digest_timeout_s": 180,
    },
    "memory": {
        # Recall cascade tiers. Schema v2 invariants:
        # local free tier first, quota tiers as fallback. Env vars
        # BOTEX_RECALL_*_MODEL remain per-run overrides.
        "recall": {
            "ollama": {"model": "bielik:latest", "enabled": True, "timeout_s": 12.0},
            "nvidia": {
                "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
                "enabled": True,
                "timeout_s": 12.0,
            },
            "gemini": {"model": "gemini-2.5-flash", "enabled": True, "timeout_s": 10.0},
            "openrouter_free": {
                "model": "nvidia/nemotron-3.5-lightning:free",
                "enabled": True,
                "timeout_s": 10.0,
            },
            # Tier-2 fires only after tier-1 stays idle for hedge_s
            # (hedged race — zero quota spend on a healthy local model).
            "hedge_s": 4.0,
            "cache_ttl_s": 300,
            "queue_wait_s": 3.0,
            "num_ctx_guard_tokens": 4000,
            # Pre-compiled per-project briefings (plan 055): served as a
            # zero-LLM first-line answer when coverage+freshness pass.
            "briefing": {"enabled": True, "serve": True, "max_age_h": 72},
        }
    },
    "cloud": {
        # VPS containment: realpath allowlist for
        # workspace_dir on memory routes over non-loopback callers.
        # Empty list = default_workspace only. Loopback callers are
        # unrestricted (local MCP sessions keep today's semantics).
        "allowed_workspaces": []
    },
    # Cloud Soul: per-bot brief token budget (est. ceil(len/4)),
    # default host-lease TTL when ttl_s=None, and Background Brief Curator trigger thresholds.
    "bots": {
        "brief_budget_tokens": 250,
        "default_lease_ttl_s": 900,
        "brief_curator_enabled": True,
        "brief_review_turn_threshold": 20,
        "brief_review_idle_s": 900,
        "brief_review_min_idle_turns": 3,
        "brief_review_cooldown_s": 300,
        "brief_review_max_per_tick": 3,
    },
    "security": {
        # Browser Origin guard on mutating /api/* & /api/chat/ws (active when Origin header present; [] = same host, ["*"] = opt-out).
        "allowed_origins": [],
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
        # Strictest gate: reject every non-free model up front unless it is
        # allowlisted here or the run carries allow_expensive_model consent.
        "paid_consent_required": False,
        "paid_allowlist": [],
    },
    "catalog": {
        "enabled": True,
        "cache_file": ".model_pricing.json",
        "ttl_hours": 24.0,
        "cache_ttl_hours": 24,
        "timeout_s": 10.0,
        "on_unknown": "allow",
    },
    # Night-Pulse live catalog availability sync & config drift detection.
    # One GET /models per provider per cycle, results cached in the shared
    # pricing cache; configured model ids absent from the live catalog are
    # reported in .botex/catalog_drift.json — config is never auto-written.
    # Ollama is intentionally excluded (local endpoint, not catalog drift).
    # When new providers are added to `providers`, add them here too.
    "catalog_sync": {
        "enabled": True,
        "providers": ["google", "groq", "nvidia", "cloudflare", "openrouter", "openrouter-free"],
        "timeout_s": 10.0,
        # Minimum seconds between live syncs. Night-Pulse can loop every 60s
        # in continuous mode — without this gate each cycle would hit every
        # provider's /models endpoint. 12h tolerates daily-timer jitter while
        # keeping the catalog effectively per-day. 0 = sync every cycle.
        "interval_s": 43200.0,
    },
    # Offline dynamic model-candidate tails (plan 059). Default ``off`` —
    # byte-identical static behavior. ``shadow`` computes would-append
    # diagnostics without changing the chain; ``append`` adds a gated tail
    # after the static head from the Night-Pulse snapshot artifact. The
    # runtime hot path reads only the local snapshot — never the network.
    "dynamic_candidates": {
        "mode": "off",
        "snapshot_file": ".botex/dynamic_model_candidates.json",
        "max_per_provider": 2,
        "max_total_per_task": 2,
        "max_snapshot_age_hours": 72,
        # Per-provider allowlist gate: a dynamic model is invisible unless
        # its provider is enabled here AND the id matches allow_models.
        "providers": {},
        "task_classes": {
            "analysis": {
                "enabled": True,
                "allow_unproven": True,
                "min_context_length": 32768,
                "allow_paid": False,
            },
            "bugfix": {
                "enabled": True,
                "allow_unproven": False,
                "min_context_length": 65536,
                "allow_paid": False,
            },
            "test": {
                "enabled": True,
                "allow_unproven": False,
                "min_context_length": 65536,
                "allow_paid": False,
            },
            "feature": {"enabled": False},
            "refactor": {"enabled": False},
            "infra": {"enabled": False},
            "general": {"enabled": False},
        },
        "modes": {
            "readonly": {"enabled": True},
            "edit": {"enabled": True},
            "destructive": {"enabled": False},
            "full": {"enabled": False},
        },
        "promotion": {
            "window_days": 30,
            "min_runs": 3,
            "min_done_runs": 2,
            "min_done_rate": 0.6,
            "max_rollback_rate": 0.34,
            "max_syntax_rejections_per_run": 0.5,
            "quarantine_after_consecutive_quality_failures": 2,
            "quarantine_hours": 72,
        },
    },
    "benchmarks": {
        "enabled": True,
        "cache_file": "",
        "ttl_hours": 24,
        "timeout_s": 10.0,
    },
    "wallet": {
        "enabled": True,
        "cache_ttl_s": 60.0,
        "timeout_s": 10.0,
        "auto_check": False,
        "min_balance_usd": 0.0,
    },
    # Secrets belong ONLY in botex.config.local.json (gitignored) — never in
    # the committed botex.config.json. Environment variables take precedence.
    "secrets": {
        "openrouter_api_key": "",
        "google_api_key": "",
    },
    # SkillSpector pre-flight audit (botex/skills/skillspector_gate.py).
    # allow_unverified=False fails CLOSED when no scanner binary exists
    # (verdict BLOCKED_NO_SCANNER); True restores the legacy fail-open
    # UNVERIFIED_NO_SCANNER verdict for hosts accepting unaudited skills.
    "skills": {
        "audit": {
            "allow_unverified": False,
        },
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
    "BOTEX_DEFAULT_WORKSPACE": ("engine", "default_workspace"),
    "BOTEX_DEFAULT_PROFILE": ("engine", "default_profile"),
    "BOTEX_DENYLIST": ("engine", "denylist"),
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
    "BOTEX_PRICE_PAID_CONSENT_REQUIRED": ("pricing", "paid_consent_required"),
    "BOTEX_PRICE_PAID_ALLOWLIST": ("pricing", "paid_allowlist"),
    "BOTEX_CATALOG_ENABLED": ("catalog", "enabled"),
    "BOTEX_CATALOG_CACHE_FILE": ("catalog", "cache_file"),
    "BOTEX_CATALOG_TTL_HOURS": ("catalog", "ttl_hours"),
    "BOTEX_CATALOG_SYNC_ENABLED": ("catalog_sync", "enabled"),
    "BOTEX_CATALOG_SYNC_PROVIDERS": ("catalog_sync", "providers"),
    "BOTEX_CATALOG_SYNC_TIMEOUT_S": ("catalog_sync", "timeout_s"),
    "BOTEX_WALLET_ENABLED": ("wallet", "enabled"),
    "BOTEX_WALLET_CACHE_TTL_S": ("wallet", "cache_ttl_s"),
    "BOTEX_GLOBAL_MEMORY_DIR": ("paths", "global_memory_dir"),
    "BOTEX_SWARM_MEMORY_DIR": ("paths", "swarm_memory_dir"),
    "BOTEX_SWARM_STORE_DB": ("paths", "swarm_store_db"),
    "BOTEX_CHAT_SESSIONS_DIR": ("paths", "chat_sessions_dir"),
    "BOTEX_BENCHMARKS_CACHE_FILE": ("benchmarks", "cache_file"),
    "BOTEX_SKILLS_AUDIT_ALLOW_UNVERIFIED": ("skills", "audit", "allow_unverified"),
    "BOTEX_ALLOWED_ORIGINS": ("security", "allowed_origins"),
}

_config_cache: Optional[Dict[str, Any]] = None
_config_sig: Optional[tuple] = None


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _file_sig(path: Path) -> Optional[int]:
    """Return the file's mtime in ns, or None if it does not exist."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _current_sig() -> tuple:
    return (_file_sig(CONFIG_FILE), _file_sig(LOCAL_CONFIG_FILE))


def load_config(reload: bool = False) -> Dict[str, Any]:
    """Return the merged configuration dict (defaults < file < local < env).

    The result is cached, but the cache is invalidated automatically when
    botex.config.json or botex.config.local.json is modified, created or
    deleted (cheap mtime check), so long-running processes pick up edits.
    """
    global _config_cache, _config_sig
    sig = _current_sig()
    if _config_cache is not None and not reload and sig == _config_sig:
        return _config_cache

    cfg = _deep_merge(DEFAULTS, _load_json(CONFIG_FILE))
    cfg = _deep_merge(cfg, _load_json(LOCAL_CONFIG_FILE))

    # _ENV_MAP values are key paths of arbitrary depth (e.g. ("skills", "audit",
    # "allow_unverified")); two-element tuples keep the original section/key shape.
    for env_name, path in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        default_val: Any = DEFAULTS
        for part in path:
            default_val = default_val[part]
        try:
            if isinstance(default_val, bool):
                value: Any = raw.lower() in ("1", "true", "yes")
            elif isinstance(default_val, int):
                value = int(raw)
            elif isinstance(default_val, float):
                value = float(raw)
            elif isinstance(default_val, list):
                raw_s = raw.strip()
                if raw_s.startswith("[") and raw_s.endswith("]"):
                    try:
                        parsed = json.loads(raw_s)
                        value = parsed if isinstance(parsed, list) else [str(parsed)]
                    except Exception:
                        value = [p.strip() for p in raw_s.split(",") if p.strip()]
                else:
                    value = [p.strip() for p in raw_s.split(",") if p.strip()]
            else:
                value = raw
        except (ValueError, Exception):
            continue
        node = cfg
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    _config_cache = cfg
    _config_sig = sig
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
    if os.name != "nt":
        os.chmod(LOCAL_CONFIG_FILE, 0o600)
    load_config(reload=True)


_PROVIDER_ALIASES: Dict[str, str] = {
    "nvidia": "nvidia",
    "nvmi": "nvidia",
    "nim": "nvidia",
    "nvidia-nim": "nvidia",
    "nvidia_nim": "nvidia",
    "nv": "nvidia",
    "openrouter-free": "openrouter-free",
    "openrouter_free": "openrouter-free",
    "openrouter:free": "openrouter-free",
    "or-free": "openrouter-free",
    "or_free": "openrouter-free",
    "or:free": "openrouter-free",
    "free-openrouter": "openrouter-free",
    "free_openrouter": "openrouter-free",
    "openrouter": "openrouter",
    "open-router": "openrouter",
    "open_router": "openrouter",
    "or": "openrouter",
    "openrouter-paid": "openrouter",
    "openrouter_paid": "openrouter",
    "google": "google",
    "gemini": "google",
    "google-ai": "google",
    "google_ai": "google",
    "googleai": "google",
    "aistudio": "google",
    "ai-studio": "google",
    "google-aistudio": "google",
    "ollama": "ollama",
    "local": "ollama",
    "ollama-local": "ollama",
    "ollama_local": "ollama",
    "bielik": "ollama",
    "bielik-local": "ollama",
    "groq": "groq",
    "groqcloud": "groq",
    "groq-cloud": "groq",
    "groq_cloud": "groq",
    "cloudflare": "cloudflare",
    "cloudflare-workers-ai": "cloudflare",
    "cloudflare_workers_ai": "cloudflare",
    "workers-ai": "cloudflare",
    "workers_ai": "cloudflare",
    "cf-workers-ai": "cloudflare",
    "cf_workers_ai": "cloudflare",
    "cfai": "cloudflare",
}


def normalize_provider_name(name: str = "", providers: Optional[Dict[str, Any]] = None) -> str:
    """Normalize a provider identifier or alias (e.g. 'nvmi' -> 'nvidia').

    Returns a static canonical literal to sever taint flow (CodeQL) —
    never the mutated user string.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    clean = raw.lower()
    if providers is None:
        providers = load_config().get("providers", {})
    if clean in providers:
        for pk in providers.keys():
            if pk.lower() == clean:
                return pk
    mapped = _PROVIDER_ALIASES.get(clean)
    if mapped and (not providers or mapped in providers):
        return mapped
    if providers:
        for pk in providers.keys():
            if pk.lower() == clean:
                return pk
    return mapped or raw


def _normalize_model_for_provider(model: str, provider_name: str, explicit_provider: bool) -> str:
    """Align an explicit model slug with its explicitly selected provider."""
    if not model or not explicit_provider:
        return model
    m = model.strip()
    if provider_name == "openrouter-free":
        if "/" in m and not m.lower().endswith(":free") and m.lower() != "openrouter/free":
            return f"{m}:free"
    elif provider_name == "nvidia":
        if m.lower().endswith(":free"):
            m = m[:-5]
        if m.startswith("meta-llama/"):
            m = "meta/" + m[len("meta-llama/") :]
    return m


def resolve_provider(name: str = "") -> tuple[str, Dict[str, Any]]:
    """Resolve the active provider: explicit ``name`` > ``BOTEX_PROVIDER`` env
    > ``"default": true`` provider > ``openrouter`` > first configured.
    Returns ``(name, cfg)``; ``cfg`` is empty when a named provider is missing.
    """
    providers = load_config().get("providers", {})
    if not name:
        name = os.environ.get("BOTEX_PROVIDER", "") or ""
    if name:
        canon = normalize_provider_name(name, providers)
        return canon, providers.get(canon, {})
    for pname, pcfg in providers.items():
        if pcfg.get("default"):
            return pname, pcfg
    if "openrouter" in providers:
        return "openrouter", providers["openrouter"]
    if providers:
        return next(iter(providers.items()))
    return "", {}


def resolve_model(model: str = "", profile: str = "", provider: str = "") -> str:
    """Resolve the model: explicit ``model`` > ``BOTEX_<PROFILE>_MODEL`` env
    > provider ``models[profile]`` > ``models.default`` > legacy ``models``.
    """
    _pname, pcfg = resolve_provider(provider)
    if model:
        return _normalize_model_for_provider(model, _pname, bool(provider and provider.strip()))
    if not profile:
        profile = load_config().get("engine", {}).get("default_profile") or "default"
    env_override = os.environ.get(f"BOTEX_{profile.upper().replace('-', '_')}_MODEL")
    if env_override:
        return env_override
    models = pcfg.get("models", {}) if pcfg else {}
    resolved = models.get(profile) or models.get("default")
    if resolved:
        return resolved
    legacy = load_config().get("models", {})
    return legacy.get(profile) or legacy.get("default") or "openrouter/auto"


def resolve_model_candidates(
    model: str = "",
    profile: str = "",
    provider: str = "",
    *,
    require_tools: bool = False,
    catalog: Optional[Any] = None,
    denylist_filter: Optional[Any] = None,
) -> list[str]:
    """Resolve the primary model plus configured failover models, pruned by denylist.

    Preserves 100% backward compatibility with all callers using positional args.
    If an explicit `model` is provided, candidate list starts as [model];
    otherwise starts with [primary] + failovers for `profile`.
    Applies `DenylistFilter` (exact, glob, regex across global, provider, profile)
    to prune blocked models.
    """
    cfg = load_config()
    if not profile:
        profile = cfg.get("engine", {}).get("default_profile") or "default"
    _pname, pcfg = resolve_provider(provider)

    if model:
        norm_model = _normalize_model_for_provider(
            model, _pname, bool(provider and provider.strip())
        )
        raw_candidates = [norm_model]
    else:
        primary = resolve_model("", profile, provider)
        configured = (pcfg.get("model_fallbacks", {}) if pcfg else {}).get(profile, [])
        if isinstance(configured, str):
            configured = [configured]
        raw_candidates = list(dict.fromkeys([primary] + [m for m in configured if m]))

    if denylist_filter is None:
        try:
            from botex.providers.denylist import DenylistFilter

            denylist_filter = DenylistFilter(cfg)
        except (ImportError, ValueError):
            from denylist import DenylistFilter

            denylist_filter = DenylistFilter(cfg)

    return denylist_filter.filter_candidates(
        raw_candidates,
        provider=_pname,
        profile=profile,
        require_tools=require_tools,
        catalog=catalog,
    )


def resolve_task_model_candidates(
    model: str = "",
    profile: str = "",
    provider: str = "",
    *,
    task_class: str = "general",
    capability_mode: str = "edit",
    zdr_required: bool = False,
    remaining_task_cap: Optional[int] = None,
    snapshot: Optional[Dict[str, Any]] = None,
    snapshot_state: Optional[str] = None,
    require_tools: bool = False,
    catalog: Optional[Any] = None,
    denylist_filter: Optional[Any] = None,
    static_resolver: Optional[Callable] = None,
) -> Dict[str, Any]:
    """Resolve the full execution chain: static head + optional dynamic tail.

    ``resolve_model_candidates`` stays the static compatibility surface; this
    wrapper layers the plan-059 offline dynamic tail on top. ``mode == "off"``
    (the default) and an explicit ``model`` are zero-I/O — no snapshot read,
    byte-identical output. ``shadow`` computes ``would_append`` diagnostics
    without changing the chain; ``append`` extends the chain with the
    snapshot's gated tail. Runtime resolution never touches the network: the
    only disk input is the Night-Pulse snapshot artifact. Any failure in the
    dynamic path degrades to the static chain with ``snapshot_state="error"``.
    ``static_resolver`` defaults to ``resolve_model_candidates``; the engine
    passes its own module-level binding so the legacy patch surface keeps
    working.
    """
    cfg = load_config()
    resolver = static_resolver or resolve_model_candidates
    static_kwargs: Dict[str, Any] = {}
    if require_tools:
        static_kwargs["require_tools"] = require_tools
    if catalog is not None:
        static_kwargs["catalog"] = catalog
    if denylist_filter is not None:
        static_kwargs["denylist_filter"] = denylist_filter
    static_list = resolver(model, profile, provider, **static_kwargs)

    def _static_payload(mode: str, state: str) -> Dict[str, Any]:
        return {
            "candidates": list(static_list),
            "static_candidates": list(static_list),
            "dynamic_candidates": [],
            "would_append": [],
            "dynamic_mode": mode,
            "snapshot_state": state,
            "excluded_reasons": {},
        }

    try:
        from botex.providers.dynamic_candidates import (
            DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
            DEFAULT_SNAPSHOT_PATH,
            load_dynamic_snapshot_offline,
            normalize_dynamic_mode,
            resolve_execution_candidates,
        )
    except (ImportError, ValueError):
        try:
            from dynamic_candidates import (
                DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
                DEFAULT_SNAPSHOT_PATH,
                load_dynamic_snapshot_offline,
                normalize_dynamic_mode,
                resolve_execution_candidates,
            )
        except (ImportError, ValueError):
            return _static_payload("off", "not_loaded")

    _pname, _pcfg = resolve_provider(provider)
    emergency_env = os.environ.get("BOTEX_DYNAMIC_CANDIDATES_MODE")
    dc_cfg = cfg.get("dynamic_candidates")
    configured_mode = dc_cfg.get("mode") if isinstance(dc_cfg, dict) else None
    mode = normalize_dynamic_mode(configured_mode, emergency_env=emergency_env)
    eff_profile = profile or (cfg.get("engine", {}).get("default_profile") or "default")
    try:
        if mode == "off" or (model and str(model).strip()):
            # Zero-I/O path: never read the snapshot for off/explicit runs.
            return resolve_execution_candidates(
                static_list,
                provider=_pname,
                profile=eff_profile,
                task_class=task_class,
                capability_mode=capability_mode,
                explicit_model=model,
                cfg=cfg,
                zdr_required=zdr_required,
                remaining_task_cap=remaining_task_cap,
                emergency_env=emergency_env,
            )

        if snapshot is None and snapshot_state is None:
            raw_path = (
                str(dc_cfg.get("snapshot_file") or "").strip() if isinstance(dc_cfg, dict) else ""
            ) or str(DEFAULT_SNAPSHOT_PATH)
            try:
                max_age = float(dc_cfg.get("max_snapshot_age_hours"))
            except (TypeError, ValueError, AttributeError):
                max_age = DEFAULT_MAX_SNAPSHOT_AGE_HOURS
            snapshot, snapshot_state = load_dynamic_snapshot_offline(
                _resolve_path(raw_path), max_age_hours=max_age
            )
        if snapshot_state is None:
            snapshot_state = "not_loaded"

        if denylist_filter is None:
            try:
                from botex.providers.denylist import DenylistFilter

                denylist_filter = DenylistFilter(cfg)
            except (ImportError, ValueError):
                from denylist import DenylistFilter

                denylist_filter = DenylistFilter(cfg)

        def _denied(mid: str, _df: Any = denylist_filter) -> bool:
            # Fail-closed: a broken denylist lookup must deny, never allow.
            try:
                return bool(_df.is_denied(mid, provider=_pname, profile=eff_profile))
            except Exception:
                return True

        return resolve_execution_candidates(
            static_list,
            provider=_pname,
            profile=eff_profile,
            task_class=task_class,
            capability_mode=capability_mode,
            explicit_model=model,
            cfg=cfg,
            snapshot=snapshot,
            snapshot_state=snapshot_state,
            zdr_required=zdr_required,
            denylist_fn=_denied,
            remaining_task_cap=remaining_task_cap,
            emergency_env=emergency_env,
        )
    except Exception:
        return _static_payload(mode, "error")


def is_decisions_model(model: str, provider_cfg: Dict[str, Any]) -> bool:
    """Whether the model is decisions-only for this provider (``decisions_models``,
    exact slugs or ``*``/``?`` globs). Such models bypass chat/completions;
    routing them into the tool loop only yields a provider 400.
    """
    patterns = (provider_cfg or {}).get("decisions_models") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(fnmatch.fnmatchcase(model, str(p)) for p in patterns if p)


def engine_setting(key: str, override: Optional[Any] = None) -> Any:
    """Return ``override`` when provided, else the configured engine value."""
    if override is not None:
        return override
    return load_config().get("engine", {}).get(key, DEFAULTS["engine"].get(key))


def snapshot_dir() -> Path:
    value = load_config().get("paths", {}).get("snapshot_dir")
    return _resolve_path(value or DEFAULTS["paths"]["snapshot_dir"])


def analytics_file() -> Path:
    value = load_config().get("paths", {}).get("analytics_file")
    return _resolve_path(value or DEFAULTS["paths"]["analytics_file"])


def env_file() -> Optional[Path]:
    value = load_config().get("paths", {}).get("env_file") or ""
    return _resolve_path(value) if value else None


def swarm_memory_dir() -> str:
    """Configured swarm vault dir for the third READ-ONLY search root.

    Relative paths resolve against ``PROJECT_ROOT`` (portability local
    <-> VPS). Empty means OFF — scope "swarm"/"all" simply skips it."""
    value = load_config().get("paths", {}).get("swarm_memory_dir") or ""
    raw = str(value).strip()
    if not raw:
        return ""
    expanded = Path(raw).expanduser()
    return str(expanded if expanded.is_absolute() else (PROJECT_ROOT / expanded).resolve())


def get_swarm_store_db_path() -> Path:
    """Resolved path to ``swarm_store.db``: ``paths.swarm_store_db`` /
    ``BOTEX_SWARM_STORE_DB`` (relative -> PROJECT_ROOT), else the per-host
    ``~/.botex/swarm_store.db`` shared across workspaces."""
    raw = str(load_config().get("paths", {}).get("swarm_store_db") or "").strip()
    if raw:
        expanded = Path(raw).expanduser()
        return expanded if expanded.is_absolute() else (PROJECT_ROOT / expanded).resolve()
    return Path.home().resolve() / ".botex" / "swarm_store.db"


def get_bots_config() -> Dict[str, Any]:
    """Merged ``bots`` config (deep-merge of committed config over DEFAULTS)."""
    defaults = DEFAULTS.get("bots", {})
    override = load_config().get("bots")
    if not isinstance(override, dict):
        return copy.deepcopy(defaults)
    return _deep_merge(defaults, override)


def get_recall_config() -> Dict[str, Any]:
    """Merged ``memory.recall`` config (partial operator overrides keep
    unspecified DEFAULTS via deep-merge)."""
    defaults = DEFAULTS.get("memory", {}).get("recall", {})
    override = load_config().get("memory", {}).get("recall")
    if not isinstance(override, dict):
        return copy.deepcopy(defaults)
    return _deep_merge(defaults, override)


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


def resolve_workspace_source(value: str = "") -> tuple:
    """Resolve the target project directory and report where it came from.

    Order: explicit ``value`` (``-w`` / MCP ``workspace_dir``) >
    ``engine.default_workspace`` (when it is an existing directory) >
    the process working directory. An empty/blank ``value`` means "not
    specified"; an explicit ``"."`` is honoured as the current directory.

    Returns ``(path, source)`` where source is ``"explicit"``,
    ``"default_workspace"`` or ``"cwd"``.
    """
    if value and str(value).strip():
        return str(value), "explicit"
    default = str(engine_setting("default_workspace") or "").strip()
    if default:
        p = Path(default).expanduser()
        if p.is_dir():
            return str(p.resolve()), "default_workspace"
        import sys

        sys.stderr.write(
            f"[botex] engine.default_workspace '{default}' is not an existing "
            "directory; falling back to the current directory.\n"
        )
    return ".", "cwd"


class WorkspaceOutOfBounds(ValueError):
    """Raised when a workspace path falls outside ``cloud.allowed_workspaces``."""


def allowed_workspaces() -> list[str]:
    """Realpath-normalized entries of ``cloud.allowed_workspaces`` (``~``-expanded,
    realpath-resolved so comparisons close ``..`` traversal and symlink escapes).
    Empty does NOT disable containment — ``_enforce_workspace_containment``
    substitutes the default-workspace root instead (fail-closed).
    """
    raw = (load_config().get("cloud", {}) or {}).get("allowed_workspaces")
    if raw is None:
        raw = DEFAULTS.get("cloud", {}).get("allowed_workspaces") or []
    roots: list[str] = []
    for entry in raw or []:
        text = str(entry).strip()
        if not text:
            continue
        try:
            roots.append(os.path.realpath(os.path.abspath(Path(text).expanduser())))
        except (OSError, ValueError):
            continue
    return roots


def _default_workspace_root() -> str:
    """Realpath of the fallback workspace for an empty ``workspace_dir``;
    shares ``resolve_workspace_source``'s chain so the empty-allowlist root
    cannot drift from what ``resolve_workspace`` resolves."""
    return os.path.realpath(os.path.abspath(resolve_workspace_source("")[0]))


def _enforce_workspace_containment(real_path: str) -> None:
    """Fail-closed containment: empty allowlist = default-workspace-only
    (same fallback ``resolve_workspace`` uses), else under one allowed root.
    Runs BEFORE any ``mkdir`` in ``get_tasks_dir``/``get_chat_sessions_dir``
    so out-of-bounds paths never materialize on disk.
    """
    roots = allowed_workspaces()
    if not roots:
        roots = [_default_workspace_root()]
    candidate = os.path.normcase(real_path)
    for root in roots:
        norm_root = os.path.normcase(root)
        if candidate == norm_root or candidate.startswith(norm_root + os.sep):
            return
    # Path detail is for SERVER LOGS ONLY — HTTP/MCP boundaries must return
    # a generic message (absolute-path disclosure to remote callers).
    raise WorkspaceOutOfBounds(f"workspace_dir {real_path!r} is outside cloud.allowed_workspaces")


def resolve_workspace(value: str = "") -> str:
    """Return the effective workspace path (see ``resolve_workspace_source``)."""
    if is_cross_host_windows_path(value):
        # Prevent path mangling on Linux VPS (e.g. /opt/botex/C:\Users\...) when a Windows
        # client passes its local host path. Fall back to the server's default workspace / cwd.
        raw_path = resolve_workspace_source("")[0]
    else:
        raw_path = resolve_workspace_source(value)[0]
    # The cwd fallback ("." when no explicit/default workspace) goes through
    # containment too — otherwise an empty workspace_dir bypasses the
    # allowlist entirely (cross-review blocker: containment bypass).
    real_path = os.path.realpath(os.path.abspath(raw_path))
    _enforce_workspace_containment(real_path)
    if raw_path == ".":
        return "."
    return "".join(chr(cp) for cp in [ord(c) for c in real_path])


def catalog_setting(key: str, override: Optional[Any] = None) -> Any:
    """Return the configured catalog setting (falls back to defaults)."""
    if override is not None:
        return override
    val = load_config().get("catalog", {}).get(key)
    if val is not None:
        return val
    return DEFAULTS.get("catalog", {}).get(key)


def catalog_cache_file() -> Path:
    """Resolved catalog cache path; ``catalog.cache_file`` (or env) is canonical,
    ``paths.catalog_file``/``catalog.cache_path`` stay honoured as legacy aliases."""
    path_str = (
        load_config().get("paths", {}).get("catalog_file")
        or catalog_setting("cache_file")
        or catalog_setting("cache_path")
        or ".model_pricing.json"
    )
    return _resolve_path(str(path_str))


def wallet_setting(key: str, override: Optional[Any] = None) -> Any:
    """Return the configured wallet setting (falls back to defaults)."""
    if override is not None:
        return override
    val = load_config().get("wallet", {}).get(key)
    if val is not None:
        return val
    return DEFAULTS.get("wallet", {}).get(key)


def denylist_setting(key: str = "denylist") -> Any:
    """Return configured engine denylist setting (falls back to defaults)."""
    val = load_config().get("engine", {}).get(key)
    if val is not None:
        return val
    return DEFAULTS.get("engine", {}).get(key, [] if key == "denylist" else {})


def get_tasks_dir(workspace_dir: str = "") -> Path:
    """Resolve directory for storing background tasks persistent history."""
    ws = resolve_workspace(workspace_dir) if workspace_dir else resolve_workspace("")
    target_dir = Path(ws) / ".botex" / "tasks"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def get_chat_sessions_dir(workspace_dir: str = "") -> Path:
    """Resolve directory for storing chat sessions.

    Resolution:
    1. paths.chat_sessions_dir or BOTEX_CHAT_SESSIONS_DIR if configured.
    2. Legacy check: If <workspace_dir>/.botex/chat_sessions/ exists and has JSON files, use it.
    3. Global ~/.botex/chat_sessions/<ws_hash>/ where ws_hash is 12-char SHA-256 of canonical workspace path.
    """
    configured = (
        os.environ.get("BOTEX_CHAT_SESSIONS_DIR", "").strip()
        or str(load_config().get("paths", {}).get("chat_sessions_dir") or "").strip()
    )
    if configured:
        p = Path(os.path.expanduser(configured)).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    ws = resolve_workspace(workspace_dir) if workspace_dir else resolve_workspace("")
    canonical_ws = os.path.realpath(os.path.abspath(ws))

    legacy_dir = Path(canonical_ws) / ".botex" / "chat_sessions"
    if legacy_dir.is_dir() and any(legacy_dir.glob("*.json")):
        return legacy_dir

    import hashlib

    ws_hash = hashlib.sha256(canonical_ws.encode("utf-8")).hexdigest()[:12]
    target_dir = Path.home().resolve() / ".botex" / "chat_sessions" / ws_hash
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def benchmarks_cache_file() -> Path:
    """Return resolved path to the OpenRouter benchmarks cache file."""
    env_override = os.environ.get("BOTEX_BENCHMARKS_CACHE_FILE", "").strip()
    if env_override:
        return _resolve_path(env_override)
    path_str = str(load_config().get("benchmarks", {}).get("cache_file") or "").strip()
    if path_str:
        return _resolve_path(path_str)
    return Path.home().resolve() / ".botex" / "cache" / "benchmarks.json"
