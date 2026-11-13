# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
Edge Skill Delegation
=====================
Opt-in (disabled by default) local skill execution on the edge. The cloud may
only invoke skills the operator pinned by SHA-256 in the ``delegation``
section of ``edge_caps.json``: strict ``additionalProperties: false`` params
validation, a recursive forbidden-key guard, ROOT_JAIL path checks, secret
masking and byte-capped replies. Fail-closed everywhere; handlers never
touch shell or subprocess.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from botex.tools.file_tools import list_dir, read_file
from botex.tools.security import coerce_text, mask_secrets, resolve_safe_path

_LOG = logging.getLogger("botex.edge.delegation")

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_STR_LEN_DEFAULT = 2000
_MAX_PARAM_DEPTH = 64
_BOUND_MAX_ITERS = 256
_TRUNCATED_MARKER = "\n[truncated on edge]"

# Parameter keys that would smuggle arbitrary execution into a "safe" skill
# call; compared after NFKC + strip + lower normalization, at any depth.
FORBIDDEN_PARAM_KEYS = frozenset(
    {
        "command",
        "cmd",
        "shell",
        "script",
        "code",
        "eval",
        "exec",
        "argv",
        "args",
        "powershell",
        "pwsh",
        "bash",
    }
)

_PATH_PARAM_KEYS = frozenset({"path", "target_path", "dir"})
_PATH_LIST_PARAM_KEYS = frozenset({"files"})


class DelegationError(Exception):
    """A delegate_skill request failed validation or policy checks."""


def _norm_key(key: Any) -> str:
    return unicodedata.normalize("NFKC", str(key)).strip().lower()


def _contains_forbidden_key(value: Any, _depth: int = 0) -> bool:
    if _depth > _MAX_PARAM_DEPTH:
        return True  # fail-closed on adversarial nesting
    if isinstance(value, dict):
        return any(
            _norm_key(k) in FORBIDDEN_PARAM_KEYS or _contains_forbidden_key(v, _depth + 1)
            for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item, _depth + 1) for item in value)
    return False


def _mask_recursive(value: Any) -> Any:
    if isinstance(value, str):
        return coerce_text(mask_secrets(value))
    if isinstance(value, dict):
        return {k: _mask_recursive(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_recursive(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return coerce_text(mask_secrets(str(value)))  # non-JSON types -> masked str


def _serialized_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _largest_string_leaf(node: Any) -> Optional[Tuple[Any, Any]]:
    """Return ``(container, key)`` of the longest string leaf, or None."""
    best: List[Any] = [0, None, None]

    def _walk(value: Any, container: Any, key: Any) -> None:
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
            if size > best[0]:
                best[0], best[1], best[2] = size, container, key
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(v, value, k)
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                _walk(v, value, i)

    _walk(node, None, None)
    return (best[1], best[2]) if best[1] is not None else None


@dataclass
class EdgeSkillSpec:
    """Operator-pinned skill admitted for edge delegation."""

    name: str
    sha256: str  # 64-hex pin; also the digest of skill_path bytes when set
    read_only: bool = True
    description: str = ""
    params_schema: Dict[str, Any] = field(default_factory=dict)
    skill_path: str = ""

    def to_public_dict(self) -> Dict[str, Any]:
        schema = self.params_schema if isinstance(self.params_schema, dict) else {}
        try:
            json.dumps(schema)
        except (TypeError, ValueError):
            schema = {}
        return {
            "name": self.name,
            "sha256": self.sha256,
            "read_only": bool(self.read_only),
            "description": mask_secrets(str(self.description)),
            "params_schema": _mask_recursive(schema),
        }


def _skill_spec_from_dict(name: Any, entry: Any) -> Optional[EdgeSkillSpec]:
    if not isinstance(name, str) or not 1 <= len(name) <= 128 or not isinstance(entry, dict):
        return None
    sha256 = str(entry.get("sha256") or "").strip().lower()
    if not _SHA256_RE.fullmatch(sha256):
        _LOG.warning("skipping edge delegation skill '%s': sha256 is not 64 hex", name)
        return None
    schema = entry.get("params_schema")
    return EdgeSkillSpec(
        name=name,
        sha256=sha256,
        read_only=bool(entry.get("read_only", True)),
        description=str(entry.get("description") or ""),
        params_schema=dict(schema) if isinstance(schema, dict) else {},
        skill_path=str(entry.get("skill_path") or ""),
    )


@dataclass
class EdgeDelegationPolicy:
    """Opt-in skill delegation ceiling; disabled unless explicitly enabled."""

    enabled: bool = False
    allowed_skills: Dict[str, EdgeSkillSpec] = field(default_factory=dict)

    def to_public_dict(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "skills": []}
        return {
            "enabled": True,
            "skills": [s.to_public_dict() for s in self.allowed_skills.values()],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EdgeDelegationPolicy":
        policy = cls()
        if not isinstance(data, dict):
            return policy
        policy.enabled = data.get("enabled") is True  # opt-in is literal
        raw = data.get("allowed_skills")
        if not isinstance(raw, dict):
            return policy
        for name, entry in raw.items():
            spec = _skill_spec_from_dict(name, entry)
            if spec is not None:
                policy.allowed_skills[spec.name] = spec
        return policy

    @classmethod
    def _from_document(cls, data: Any) -> "EdgeDelegationPolicy":
        if not isinstance(data, dict):
            return cls()
        section = data.get("delegation")
        if section is None and ("enabled" in data or "allowed_skills" in data):
            section = data  # standalone delegation document without the wrapper
        return cls.from_dict(section)

    @classmethod
    def load(
        cls,
        workspace_root: Optional[str | Path] = None,
        explicit_path: Optional[str | Path] = None,
    ) -> "EdgeDelegationPolicy":
        """Read the ``delegation`` section from ``explicit_path`` or from
        ``<workspace_root>/.botex/edge_caps.json`` (then ``~/.botex/edge_caps.json``).
        Missing, unreadable or malformed config resolves to a disabled
        policy — delegation is never implicitly enabled."""
        candidates: List[Path] = []
        if explicit_path is not None:
            candidates.append(Path(explicit_path))
        else:
            if workspace_root is not None:
                candidates.append(Path(workspace_root) / ".botex" / "edge_caps.json")
            candidates.append(Path.home() / ".botex" / "edge_caps.json")
        for candidate in candidates:
            try:
                if not candidate.is_file():
                    continue
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception as exc:
                _LOG.warning(
                    "ignoring unreadable %s (%s)",
                    candidate.name,
                    type(exc).__name__,
                )
                return cls()
            return cls._from_document(data)
        return cls()


def _check_param_type(name: str, decl: Any, value: Any, max_str_len: int) -> None:
    t = str(decl).strip().lower()
    if t == "bool":
        ok = isinstance(value, bool)
    elif t == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif t == "float":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif t == "str":
        ok = isinstance(value, str) and len(value) <= max_str_len
    elif t == "list[str]":
        ok = (
            isinstance(value, list)
            and all(isinstance(item, str) for item in value)
            and all(len(item) <= max_str_len for item in value)
        )
    else:
        raise DelegationError(f"param '{name}' declares unsupported type '{decl}'")
    if not ok:
        raise DelegationError(f"param '{name}' must be {t} (max_str_len={max_str_len})")


def _check_path_params(params: Dict[str, Any], workspace_root: Optional[str | Path]) -> None:
    targets: List[str] = []
    for key, value in params.items():
        nk = _norm_key(key)
        if nk in _PATH_PARAM_KEYS and isinstance(value, str):
            targets.append(value)
        elif nk in _PATH_LIST_PARAM_KEYS and isinstance(value, list):
            targets.extend(item for item in value if isinstance(item, str))
    if not targets:
        return
    if workspace_root is None:
        raise DelegationError("path params require a workspace_root jail")
    for target in targets:
        resolve_safe_path(workspace_root, target)


def validate_params(
    spec: EdgeSkillSpec,
    params: Any,
    workspace_root: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Strict ``additionalProperties: false`` validation of skill params.

    Raises :class:`DelegationError` on schema violations and
    ``SecurityError`` on ROOT_JAIL escapes."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise DelegationError("params must be a JSON object")
    if _contains_forbidden_key(params):
        raise DelegationError("params contain a forbidden key")
    schema = spec.params_schema if isinstance(spec.params_schema, dict) else {}
    raw_allowed = schema.get("allowed_params")
    allowed: Dict[str, Any] = dict(raw_allowed) if isinstance(raw_allowed, dict) else {}
    raw_required = schema.get("required")
    required = [str(r) for r in raw_required] if isinstance(raw_required, (list, tuple)) else []
    try:
        max_str_len = max(0, int(schema.get("max_str_len", _MAX_STR_LEN_DEFAULT)))
    except (TypeError, ValueError):
        max_str_len = _MAX_STR_LEN_DEFAULT
    for name in required:
        if name not in params:
            raise DelegationError(f"missing required param '{name}'")
    for key, value in params.items():
        if key not in allowed:
            raise DelegationError(f"unexpected param '{key}'")
        _check_param_type(str(key), allowed[key], value, max_str_len)
    _check_path_params(params, workspace_root)
    return dict(params)


def verify_skill_digest(
    spec: EdgeSkillSpec,
    expected_sha256: Optional[str] = None,
    workspace_root: Optional[str | Path] = None,
) -> None:
    """Constant-time pin check: the frame's claimed digest must equal the
    policy pin, and on-disk ``skill_path`` bytes must still match it."""
    pin = str(spec.sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(pin):
        raise DelegationError(f"skill '{spec.name}' has no valid sha256 pin")
    if expected_sha256 is not None:
        expected = str(expected_sha256).strip().lower()
        if not expected:
            raise DelegationError("delegate_skill requires a non-empty sha256 pin")
        if not hmac.compare_digest(expected, pin):
            raise DelegationError(f"sha256 mismatch for skill '{spec.name}'")
    if spec.skill_path:
        if workspace_root is None:
            raise DelegationError("skill_path verification requires workspace_root")
        safe = resolve_safe_path(workspace_root, spec.skill_path)
        if not safe.is_file():
            raise DelegationError(f"skill file '{spec.skill_path}' missing on edge")
        actual = hashlib.sha256(safe.read_bytes()).hexdigest()
        if not hmac.compare_digest(actual, pin):
            raise DelegationError(f"on-disk digest mismatch for skill '{spec.name}'")


SkillHandlerFn = Callable[[EdgeSkillSpec, Dict[str, Any], Path], Any]


def _builtin_workspace_summary(
    spec: EdgeSkillSpec, params: Dict[str, Any], workspace_root: Path
) -> Dict[str, Any]:
    listing = list_dir(".", workspace_root)
    if not listing.get("ok"):
        return {"ok": False, "skill": spec.name, "error": listing.get("error")}
    return {
        "ok": True,
        "skill": spec.name,
        "workspace": workspace_root.name,
        "total_entries": listing.get("total_entries", 0),
        "entries": listing.get("entries", []),
    }


def _builtin_skill_read(
    spec: EdgeSkillSpec, params: Dict[str, Any], workspace_root: Path
) -> Dict[str, Any]:
    path = str(params.get("path") or "")
    if not path:
        raise DelegationError("skill-read requires a 'path' param")
    result = read_file(path, workspace_root)
    result["skill"] = spec.name
    return result


class EdgeDelegationHandler:
    """Executes policy-pinned skills inside ROOT_JAIL — never shell/subprocess."""

    def __init__(
        self,
        policy: Optional[EdgeDelegationPolicy] = None,
        workspace_root: str | Path = ".",
        *,
        max_output_bytes: int = 65536,
        handlers: Optional[Dict[str, SkillHandlerFn]] = None,
    ) -> None:
        self.policy = policy if isinstance(policy, EdgeDelegationPolicy) else EdgeDelegationPolicy()
        self.workspace_root = Path(workspace_root)
        self.max_output_bytes = max(1, int(max_output_bytes))
        self._handlers: Dict[str, SkillHandlerFn] = {
            "workspace-summary": _builtin_workspace_summary,
            "skill-read": _builtin_skill_read,
        }
        for name, fn in (handlers or {}).items():
            self.register_handler(name, fn)

    def register_handler(self, name: str, fn: SkillHandlerFn) -> None:
        if not isinstance(name, str) or not name or not callable(fn):
            raise ValueError("register_handler requires a non-empty name and a callable")
        self._handlers[name] = fn

    async def handle(
        self, skill: Any, expected_sha256: Any = "", params: Any = None
    ) -> Dict[str, Any]:
        """Validate and run one delegated skill; raise ``DelegationError``
        (or ``SecurityError``) on any rejection — the caller maps those to a
        ``delegate_error`` frame."""
        if not self.policy.enabled:
            raise DelegationError("edge skill delegation is disabled")
        spec = self.policy.allowed_skills.get(str(skill))
        if spec is None:
            raise DelegationError(f"unknown skill '{skill}'")
        expected = "" if expected_sha256 is None else str(expected_sha256)
        await asyncio.to_thread(verify_skill_digest, spec, expected, self.workspace_root)
        clean = await asyncio.to_thread(validate_params, spec, params, self.workspace_root)
        fn = self._handlers.get(spec.name)
        if fn is None:
            raise DelegationError(f"skill '{spec.name}' has no edge handler")
        if inspect.iscoroutinefunction(fn):
            out = await fn(spec, clean, self.workspace_root)
        else:
            out = await asyncio.to_thread(fn, spec, clean, self.workspace_root)
        return self._sanitize_result(out)

    def _sanitize_result(self, result: Any) -> Dict[str, Any]:
        """Mask every outbound string, then shrink string leaves until the
        serialized payload fits ``max_output_bytes`` — before ``json.dumps``."""
        masked = _mask_recursive(result if isinstance(result, dict) else {"result": result})
        budget = int(self.max_output_bytes)
        if _serialized_size(masked) <= budget:
            return masked
        masked["truncated"] = True  # accounted for in the bound below
        marker = _TRUNCATED_MARKER.encode("utf-8")
        for _ in range(_BOUND_MAX_ITERS):
            size = _serialized_size(masked)
            if size <= budget:
                return masked
            leaf = _largest_string_leaf(masked)
            if leaf is None:
                break
            container, key = leaf
            excess = size - budget + len(marker)
            keep = max(0, len(container[key].encode("utf-8")) - excess)
            container[key] = (
                container[key].encode("utf-8")[:keep].decode("utf-8", errors="ignore")
                + _TRUNCATED_MARKER
            )
        return {
            "ok": False,
            "error": "delegate result exceeded max_output_bytes",
            "truncated": True,
        }
