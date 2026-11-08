# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Memory Recall - Layer A LLM Synthesis Cascade
====================================================
Multi-tier model cascade that synthesizes a structured, hallucination-free
answer from pre-gathered Memory Vault notes for the ``recall_memory`` tool
(arena-recall synthesis S, interface contract v1.0 §3).

Cascade order (first VALID structured answer wins):
1. local-ollama    - native POST /api/chat with keep_alive (fallback: /v1 shim);
                     L7 /api/tags model probe (800ms, tolerant), prompt-size
                     guard, serialized via Semaphore(1) with bounded FIFO wait.
2. nvidia          - NVIDIA NIM via ProviderRegistry (NVIDIA_API_KEY); hedged:
                     starts in parallel after ``hedge_s`` of tier-1 silence.
3. gemini          - Google AI Studio via ProviderRegistry (sequential).
4. openrouter-free - OpenRouter :free model via ProviderRegistry (sequential).

Additional machinery (contract §3): LRU answer cache keyed by
sha256(query+scope+node_ids+max(mtimes)) with TTL, negative-tier TTL markers,
per-tier enable/timeout/model from ``memory.recall`` config (env overrides for
models preserved). ``cfg`` is injected by the caller (``get_recall_config()``);
when absent it is lazily imported with try/except and falls back to the module
defaults below, so the cascade stays backward-safe while config is unavailable.

On success returns ``{answer, sources, confidence, tier_used, tier_attempts}``;
on total cascade failure returns the same shape with ``answer=None`` so the
caller can still report per-tier observability; returns None only when
``context_nodes`` is empty.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import socket
import time
import urllib.request
import weakref
from collections import OrderedDict
from typing import Any, Callable, Coroutine, Dict, List, Optional
from urllib.parse import urlparse

from botex.security import mask_secrets

from ops.curator.prompts import RECALL_SYSTEM_PROMPT, build_recall_user_prompt

logger = logging.getLogger(__name__)

# --- Contract §1 defaults (must mirror get_recall_config() fallbacks) ---
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
DEFAULT_NVIDIA_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3.5-lightning:free"

DEFAULT_HEDGE_S = 4.0
DEFAULT_CACHE_TTL_S = 300.0
DEFAULT_QUEUE_WAIT_S = 3.0
DEFAULT_NUM_CTX_GUARD_TOKENS = 4000
DEFAULT_NEGATIVE_TIER_TTL_S = 60.0
DEFAULT_OLLAMA_TIMEOUT_S = 12.0
DEFAULT_NVIDIA_TIMEOUT_S = 12.0
DEFAULT_GEMINI_TIMEOUT_S = 10.0
DEFAULT_OPENROUTER_TIMEOUT_S = 10.0

OLLAMA_TAGS_PROBE_TIMEOUT_S = 0.8
OLLAMA_TCP_PROBE_TIMEOUT_S = 0.4
OLLAMA_KEEP_ALIVE = "10m"
ANSWER_CACHE_MAX_ENTRIES = 64

RECALL_TEMPERATURE = 0.1
RECALL_MAX_TOKENS = 2048

VALID_CONFIDENCES = frozenset({"HIGH", "MEDIUM", "LOW", "NOT_FOUND"})
NOT_FOUND_ANSWER = "The provided Memory Vault notes do not contain an answer to the query."

# tier_attempts status literals (contract §4: [{tier, status, ms}])
ST_OK = "ok"
ST_INVALID = "invalid"
ST_ERROR = "error"
ST_TIMEOUT = "timeout"
ST_UNAVAILABLE = "unavailable"
ST_DISABLED = "disabled"
ST_NEG_CACHED = "neg_cached"
ST_PROMPT_GUARD = "prompt_guard"
ST_QUEUE_TIMEOUT = "queue_timeout"
ST_MODEL_ABSENT = "model_absent"
ST_CANCELLED = "cancelled"
ST_CACHE_HIT = "hit"

# Tiers marked down are skipped for this many seconds (infra failures only).
_NEGATIVE_TIER_MARKS: Dict[str, float] = {}
# LRU answer cache: key -> (expires_at_monotonic, payload)
_ANSWER_CACHE: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
# Per-event-loop Semaphore(1) instances serializing tier-1 (asyncio semaphores
# bind to the running loop; tests use a fresh loop per asyncio.run call).
_TIER1_SEMS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _now() -> float:
    """Monotonic clock for TTL/ms bookkeeping (patchable in tests)."""
    return time.monotonic()


def _elapsed_ms(start: float) -> int:
    return max(0, int((_now() - start) * 1000))


def _probe_tcp(url: str, timeout: float) -> bool:
    """Return True if a TCP connection to the URL's host:port succeeds quickly."""
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
        finally:
            sock.close()
        return True
    except Exception:
        return False


def _ollama_base_url() -> str:
    """Resolve the Ollama OpenAI-compatible base URL from OLLAMA_HOST or default."""
    raw = (os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    if "://" not in raw:
        raw = f"http://{raw}"
    if not raw.endswith("/v1"):
        raw = f"{raw}/v1"
    return raw


def _ollama_native_base() -> str:
    """Resolve the native Ollama API root (scheme://host:port, no /v1 path)."""
    raw = (os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_URL).strip().rstrip("/")
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 11434
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}:{port}"


def _get_json(url: str, timeout_s: float) -> dict:
    """Synchronous JSON GET (run via asyncio.to_thread by callers)."""
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout_s: float) -> dict:
    """Synchronous JSON POST (run via asyncio.to_thread by callers)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _normalize_ollama_tag(name: str) -> str:
    """Normalize an Ollama model tag (bare names imply :latest)."""
    return name if ":" in name else f"{name}:latest"


def _probe_ollama_model(native_base: str, model: str, timeout_s: float) -> Optional[bool]:
    """L7 probe: GET /api/tags and verify the model is INSTALLED (not loaded).

    Tri-state: True = tags confirm installed, False = tags confirm absent,
    None = probe inconclusive (daemon down/unreachable/timeout) — callers
    tolerate the inconclusive case via the L4 TCP probe fallback.
    """
    try:
        data = _get_json(f"{native_base}/api/tags", timeout_s)
        if not isinstance(data, dict):
            return None
        models = {
            _normalize_ollama_tag(str(m.get("name") or m.get("model") or ""))
            for m in (data.get("models") or [])
            if isinstance(m, dict)
        }
        models.discard(":latest")
        return _normalize_ollama_tag(model) in models
    except Exception:
        return None


def _estimate_prompt_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for the prompt-size guard."""
    return max(1, len(text) // 4)


def _cfg_num(cfg: dict, key: str, default: float) -> float:
    """Numeric config value with scalar hygiene (bool is not a number here)."""
    val = cfg.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return default
    return float(val)


def _get_recall_config_from_memory() -> Optional[dict]:
    """Lazy import of Worker A's get_recall_config(); None when unavailable."""
    for modname in ("botex.memory", "botex.memory.memory"):
        try:
            fn = getattr(importlib.import_module(modname), "get_recall_config", None)
            if callable(fn):
                cfg = fn()
                return cfg if isinstance(cfg, dict) else None
        except Exception:
            continue
    return None


def _tier_params(
    cfg: dict, key: str, env_var: str, default_model: str, default_timeout_s: float
) -> dict:
    """Resolve per-tier params: env model override > cfg section > defaults."""
    section = cfg.get(key)
    if not isinstance(section, dict):
        section = {}
    model = (os.environ.get(env_var) or section.get("model") or default_model).strip()
    enabled = bool(section.get("enabled", True))
    return {
        "model": model,
        "enabled": enabled,
        "timeout_s": _cfg_num(section, "timeout_s", default_timeout_s),
    }


def _tier1_semaphore() -> asyncio.Semaphore:
    """Per-event-loop Semaphore(1) serializing tier-1 (bounded FIFO wait)."""
    loop = asyncio.get_running_loop()
    try:
        sem = _TIER1_SEMS.get(loop)
    except TypeError:
        sem = None
    if sem is None:
        sem = asyncio.Semaphore(1)
        try:
            _TIER1_SEMS[loop] = sem
        except TypeError:
            pass  # non-weakref-able loop: degrade to an unshared semaphore
    return sem


def _mark_tier_down(tier: str) -> None:
    _NEGATIVE_TIER_MARKS[tier] = _now()


def _tier_marked_down(tier: str, ttl_s: float) -> bool:
    marked = _NEGATIVE_TIER_MARKS.get(tier)
    return marked is not None and (_now() - marked) < ttl_s


def _cache_key(query: str, nodes: List[dict]) -> str:
    """sha256(norm(query) + scope + sorted(node_ids) + max(node_mtimes)).

    Nodes without an explicit mtime fall back to a content digest so edits
    still invalidate the cache (contract shape preserved).
    """
    norm_q = " ".join(str(query or "").lower().split())
    scope_sig = ",".join(sorted({str(n.get("scope") or "") for n in nodes}))
    ids = sorted(str(n.get("id")) for n in nodes if n.get("id") is not None)
    mtimes = [
        float(n["mtime"])
        for n in nodes
        if isinstance(n.get("mtime"), (int, float)) and not isinstance(n.get("mtime"), bool)
    ]
    if mtimes:
        freshness = str(max(mtimes))
    else:
        digest = hashlib.sha256()
        for n in sorted(nodes, key=lambda x: str(x.get("id"))):
            digest.update(str(n.get("id")).encode("utf-8"))
            digest.update(b"\x00")
            digest.update(str(n.get("content") or "").encode("utf-8"))
            digest.update(b"\x00")
        freshness = f"content:{digest.hexdigest()[:24]}"
    raw = "|".join([norm_q, scope_sig, ",".join(ids), freshness])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    entry = _ANSWER_CACHE.get(key)
    if entry is None:
        return None
    expires_at, payload = entry
    if _now() >= expires_at:
        _ANSWER_CACHE.pop(key, None)
        return None
    _ANSWER_CACHE.move_to_end(key)
    return payload


def _cache_put(key: str, ttl_s: float, payload: dict) -> None:
    _ANSWER_CACHE[key] = (_now() + ttl_s, payload)
    _ANSWER_CACHE.move_to_end(key)
    while len(_ANSWER_CACHE) > ANSWER_CACHE_MAX_ENTRIES:
        _ANSWER_CACHE.popitem(last=False)


async def _try_local_ollama(prompt: str, tcfg: dict) -> Optional[str]:
    """Tier 1 request: native POST /api/chat with keep_alive (model stays hot).

    Fallback: OpenAI-compatible /v1 shim — keep_alive is unsupported there
    (shim path is untested upstream; see arena-recall ACCEPTED-COST).
    """
    model = tcfg["model"]
    timeout_s = float(tcfg["timeout_s"])
    messages = [
        {"role": "system", "content": RECALL_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    native_payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": tcfg.get("keep_alive") or OLLAMA_KEEP_ALIVE,
        "options": {"temperature": RECALL_TEMPERATURE, "num_predict": RECALL_MAX_TOKENS},
    }
    try:
        data = await asyncio.to_thread(
            _post_json, f"{_ollama_native_base()}/api/chat", native_payload, timeout_s
        )
        message = data.get("message") if isinstance(data, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if content:
            return str(content)
    except Exception as exc:
        logger.info(
            "Recall tier local-ollama native /api/chat failed, trying shim: %s",
            mask_secrets(str(exc)),
        )
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(base_url=_ollama_base_url(), api_key="ollama", timeout=timeout_s)
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=RECALL_TEMPERATURE,
            max_tokens=RECALL_MAX_TOKENS,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logger.info("Recall tier local-ollama unavailable or failed: %s", mask_secrets(str(exc)))
        return None


async def _try_nvidia(prompt: str, tcfg: dict) -> Optional[str]:
    """Tier 2: NVIDIA NIM via ProviderRegistry (mirrors curator nemotron tier)."""
    api_key = (os.environ.get("NVIDIA_API_KEY") or "").strip()
    if not api_key:
        return None
    model = tcfg["model"]
    try:
        from botex.providers import ProviderRegistry

        provider = ProviderRegistry.get("nvidia")
        client = provider.get_client(api_key=api_key)
        extra_body = (
            {"chat_template_kwargs": {"thinking": False}} if "nemotron-3" in model else None
        )
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RECALL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=RECALL_TEMPERATURE,
            max_tokens=RECALL_MAX_TOKENS,
            timeout=float(tcfg["timeout_s"]),
            extra_body=extra_body,
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logger.info("Recall tier nvidia unavailable or failed: %s", mask_secrets(str(exc)))
        return None


async def _try_gemini(prompt: str, tcfg: dict) -> Optional[str]:
    """Tier 3: Google AI Studio (Gemini) via ProviderRegistry."""
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        return None
    model = tcfg["model"]
    try:
        from botex.providers import ProviderRegistry

        provider = ProviderRegistry.get("google")
        client = provider.get_client(api_key=api_key)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RECALL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=RECALL_TEMPERATURE,
            max_tokens=RECALL_MAX_TOKENS,
            timeout=float(tcfg["timeout_s"]),
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logger.info("Recall tier gemini unavailable or failed: %s", mask_secrets(str(exc)))
        return None


async def _try_openrouter_free(prompt: str, tcfg: dict) -> Optional[str]:
    """Tier 4: OpenRouter :free model via ProviderRegistry."""
    api_key = (os.environ.get("OPENROUTER_API_FREE_KEY") or "").strip()
    if not api_key:
        return None
    model = tcfg["model"]
    try:
        from botex.providers import ProviderRegistry

        provider = ProviderRegistry.get("openrouter-free")
        client = provider.get_client(api_key=api_key)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": RECALL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=RECALL_TEMPERATURE,
            max_tokens=RECALL_MAX_TOKENS,
            timeout=float(tcfg["timeout_s"]),
        )
        return resp.choices[0].message.content
    except Exception as exc:
        logger.info("Recall tier openrouter-free unavailable or failed: %s", mask_secrets(str(exc)))
        return None


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    """Robustly parse a JSON object from LLM output, stripping markdown fences."""
    cleaned = raw.strip()
    if "```json" in cleaned:
        cleaned = cleaned.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in cleaned:
        cleaned = cleaned.split("```", 1)[1].split("```", 1)[0].strip()
    else:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            cleaned = match.group(0)
    try:
        parsed = json.loads(cleaned)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_recall_payload(
    parsed: Dict[str, Any], valid_ids: frozenset
) -> Optional[Dict[str, Any]]:
    """Validate and normalize a parsed recall payload against the input node ids.

    Drops source ids not present in the input set, coerces invalid confidence
    to LOW, and forces empty sources on NOT_FOUND. Returns None when the
    payload is unusable (caller tries the next tier).
    """
    answer = parsed.get("answer")
    if not isinstance(answer, str):
        return None
    answer = answer.strip()

    confidence_raw = str(parsed.get("confidence") or "").strip().upper()
    confidence = confidence_raw if confidence_raw in VALID_CONFIDENCES else "LOW"

    if not answer:
        if confidence == "NOT_FOUND":
            answer = NOT_FOUND_ANSWER
        else:
            return None

    sources: List[str] = []
    raw_sources = parsed.get("sources")
    if isinstance(raw_sources, list):
        seen = set()
        for item in raw_sources:
            if item is None:
                continue
            sid = str(item)
            if sid in valid_ids and sid not in seen:
                seen.add(sid)
                sources.append(sid)

    if confidence == "NOT_FOUND":
        sources = []

    return {"answer": answer, "sources": sources, "confidence": confidence}


def _validate_raw(raw: Optional[str], valid_ids: frozenset) -> Optional[Dict[str, Any]]:
    """Extract+validate raw tier output; None means invalid/unusable."""
    if not raw:
        return None
    parsed = _extract_json_object(raw)
    if parsed is None:
        return None
    return _validate_recall_payload(parsed, valid_ids)


async def _attempt(
    name: str,
    fn: Callable[[str, dict], Coroutine[Any, Any, Optional[str]]],
    prompt: str,
    tcfg: dict,
    neg_ttl_s: float,
    valid_ids: frozenset,
    attempts: List[dict],
) -> Optional[dict]:
    """Generic cloud-tier attempt with timeout, validation and attempt record."""
    start = _now()

    def rec(status: str) -> None:
        attempts.append({"tier": name, "status": status, "ms": _elapsed_ms(start)})

    if not tcfg["enabled"]:
        rec(ST_DISABLED)
        return None
    if _tier_marked_down(name, neg_ttl_s):
        rec(ST_NEG_CACHED)
        return None
    try:
        raw = await asyncio.wait_for(fn(prompt, tcfg), timeout=float(tcfg["timeout_s"]) + 1.0)
    except asyncio.CancelledError:
        rec(ST_CANCELLED)
        raise
    except TimeoutError:
        _mark_tier_down(name)
        rec(ST_TIMEOUT)
        return None
    except Exception as exc:
        _mark_tier_down(name)
        logger.info("Recall tier %s raised unexpectedly: %s", name, mask_secrets(str(exc)))
        rec(ST_ERROR)
        return None
    if not raw:
        rec(ST_UNAVAILABLE)
        return None
    validated = _validate_raw(raw, valid_ids)
    if validated is None:
        rec(ST_INVALID)
        return None
    rec(ST_OK)
    validated["tier_used"] = name
    return validated


async def _tier1_flow(
    prompt: str,
    tcfg: dict,
    queue_wait_s: float,
    guard_tokens: float,
    neg_ttl_s: float,
    valid_ids: frozenset,
    attempts: List[dict],
) -> Optional[dict]:
    """Tier-1 pipeline: enabled/neg-marker checks -> prompt-size guard ->
    Semaphore(1) bounded FIFO wait -> L7 tags probe -> hedged request."""
    name = "local-ollama"
    start = _now()

    def rec(status: str) -> None:
        attempts.append({"tier": name, "status": status, "ms": _elapsed_ms(start)})

    if not tcfg["enabled"]:
        rec(ST_DISABLED)
        return None
    if _tier_marked_down(name, neg_ttl_s):
        rec(ST_NEG_CACHED)
        return None
    estimated = _estimate_prompt_tokens(RECALL_SYSTEM_PROMPT) + _estimate_prompt_tokens(prompt)
    if estimated > guard_tokens:
        logger.info(
            "Recall tier local-ollama skipped: prompt ~%d tokens > num_ctx_guard %d",
            estimated,
            int(guard_tokens),
        )
        rec(ST_PROMPT_GUARD)
        return None
    sem = _tier1_semaphore()
    try:
        await asyncio.wait_for(sem.acquire(), timeout=queue_wait_s)
    except TimeoutError:
        rec(ST_QUEUE_TIMEOUT)
        return None
    except asyncio.CancelledError:
        rec(ST_CANCELLED)
        raise
    try:
        native_base = _ollama_native_base()
        verdict = await asyncio.to_thread(
            _probe_ollama_model, native_base, tcfg["model"], OLLAMA_TAGS_PROBE_TIMEOUT_S
        )
        if verdict is False:
            _mark_tier_down(name)
            rec(ST_MODEL_ABSENT)
            return None
        if verdict is None and not await asyncio.to_thread(
            _probe_tcp, native_base, OLLAMA_TCP_PROBE_TIMEOUT_S
        ):
            _mark_tier_down(name)
            rec(ST_UNAVAILABLE)
            return None
        raw = await asyncio.wait_for(
            _try_local_ollama(prompt, tcfg), timeout=float(tcfg["timeout_s"]) + 1.0
        )
    except asyncio.CancelledError:
        rec(ST_CANCELLED)
        raise
    except TimeoutError:
        _mark_tier_down(name)
        rec(ST_TIMEOUT)
        return None
    except Exception as exc:
        _mark_tier_down(name)
        logger.info("Recall tier %s raised unexpectedly: %s", name, mask_secrets(str(exc)))
        rec(ST_ERROR)
        return None
    finally:
        sem.release()
    if not raw:
        rec(ST_UNAVAILABLE)
        return None
    validated = _validate_raw(raw, valid_ids)
    if validated is None:
        rec(ST_INVALID)
        return None
    rec(ST_OK)
    validated["tier_used"] = name
    return validated


def _task_result(task: "asyncio.Task[Optional[dict]]") -> Optional[dict]:
    """Extract a completed task's result, tolerating cancellation/errors."""
    if task.cancelled():
        return None
    try:
        return task.result()
    except Exception:
        return None


async def _hedged_first_valid(
    tier1_factory: Callable[[], Coroutine[Any, Any, Optional[dict]]],
    tier2_factory: Callable[[], Coroutine[Any, Any, Optional[dict]]],
    hedge_s: float,
) -> Optional[dict]:
    """Race tier-1 against tier-2: tier-2 starts after ``hedge_s`` of tier-1
    silence; the first VALID answer wins and the loser is cancelled."""
    t1 = asyncio.ensure_future(tier1_factory())
    done, _ = await asyncio.wait({t1}, timeout=hedge_s)
    if done:
        result = _task_result(t1)
        if result is not None:
            return result
        # Tier-1 answered fast but invalid — escalate sequentially.
        return await tier2_factory()
    t2 = asyncio.ensure_future(tier2_factory())
    pending = {t1, t2}
    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            result = _task_result(finished)
            if result is not None:
                for loser in pending:
                    loser.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                return result
    return None


async def synthesize_recall(
    query: str, context_nodes: list[dict], cfg: Optional[dict] = None
) -> Optional[dict]:
    """Synthesize a grounded answer from Memory Vault notes via the LLM cascade.

    ``cfg`` is the ``memory.recall`` config section (get_recall_config()); when
    None it is lazily imported with fallback to the module defaults.

    Returns ``{answer, sources, confidence, tier_used, tier_attempts}`` on
    success; on total cascade failure the same shape with ``answer=None``
    (caller applies the deterministic fallback); None only when
    ``context_nodes`` is empty.
    """
    if not context_nodes:
        return None
    if not isinstance(cfg, dict):
        cfg = _get_recall_config_from_memory() or {}

    hedge_s = _cfg_num(cfg, "hedge_s", DEFAULT_HEDGE_S)
    cache_ttl_s = _cfg_num(cfg, "cache_ttl_s", DEFAULT_CACHE_TTL_S)
    queue_wait_s = _cfg_num(cfg, "queue_wait_s", DEFAULT_QUEUE_WAIT_S)
    guard_tokens = _cfg_num(cfg, "num_ctx_guard_tokens", DEFAULT_NUM_CTX_GUARD_TOKENS)
    neg_ttl_s = _cfg_num(cfg, "negative_tier_ttl_s", DEFAULT_NEGATIVE_TIER_TTL_S)

    valid_ids = frozenset(
        str(node.get("id")) for node in context_nodes if node.get("id") is not None
    )
    prompt = build_recall_user_prompt(str(query or ""), context_nodes)

    attempts: List[dict] = []
    cache_key = _cache_key(query, context_nodes)
    cache_start = _now()
    cached = _cache_get(cache_key)
    if cached is not None:
        attempts.append(
            {"tier": "answer-cache", "status": ST_CACHE_HIT, "ms": _elapsed_ms(cache_start)}
        )
        return {**cached, "tier_attempts": attempts, "cached": True}

    t1 = _tier_params(
        cfg, "ollama", "BOTEX_RECALL_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_TIMEOUT_S
    )
    t1["keep_alive"] = (
        (cfg.get("ollama") or {}).get("keep_alive") if isinstance(cfg.get("ollama"), dict) else None
    ) or OLLAMA_KEEP_ALIVE
    t2 = _tier_params(
        cfg, "nvidia", "BOTEX_RECALL_NVIDIA_MODEL", DEFAULT_NVIDIA_MODEL, DEFAULT_NVIDIA_TIMEOUT_S
    )
    t3 = _tier_params(
        cfg, "gemini", "BOTEX_RECALL_GEMINI_MODEL", DEFAULT_GEMINI_MODEL, DEFAULT_GEMINI_TIMEOUT_S
    )
    t4 = _tier_params(
        cfg,
        "openrouter_free",
        "BOTEX_RECALL_OPENROUTER_MODEL",
        DEFAULT_OPENROUTER_MODEL,
        DEFAULT_OPENROUTER_TIMEOUT_S,
    )

    validated = await _hedged_first_valid(
        lambda: _tier1_flow(prompt, t1, queue_wait_s, guard_tokens, neg_ttl_s, valid_ids, attempts),
        lambda: _attempt("nvidia", _try_nvidia, prompt, t2, neg_ttl_s, valid_ids, attempts),
        hedge_s,
    )
    if validated is None:
        for name, fn, tcfg in (
            ("gemini", _try_gemini, t3),
            ("openrouter-free", _try_openrouter_free, t4),
        ):
            validated = await _attempt(name, fn, prompt, tcfg, neg_ttl_s, valid_ids, attempts)
            if validated is not None:
                break

    if validated is None:
        return {
            "answer": None,
            "sources": [],
            "confidence": None,
            "tier_used": None,
            "tier_attempts": attempts,
        }

    payload = {
        "answer": validated["answer"],
        "sources": validated["sources"],
        "confidence": validated["confidence"],
        "tier_used": validated["tier_used"],
    }
    _cache_put(cache_key, cache_ttl_s, payload)
    return {**payload, "tier_attempts": attempts}
