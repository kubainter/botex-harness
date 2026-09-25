# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Security Layer
====================
Path traversal guard, secret masking and file/directory access controls.
Cross-platform: Windows 10+, Windows 11, Linux, macOS.
"""
import os
import re
import fnmatch
from pathlib import Path
from typing import Any, Dict, Tuple

# ---------------------------------------------------------------------------
# Blocked file patterns — never read or write these
# ---------------------------------------------------------------------------
BLOCKED_FILE_PATTERNS = {
    ".env", ".env.local", ".env.production", ".env.staging",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.crt", "*.cer",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*.secret", "*.secrets", "*.private",
    "credentials.json", "service-account*.json",
    ".netrc", ".npmrc",
    "botex.config.local.json",  # may hold secrets.openrouter_api_key
}

# Directories always skipped by list_dir and outline scanning
IGNORED_DIRS = {
    "vendor", "node_modules", ".git", ".svn", ".hg",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".next", ".nuxt", "target",
    ".venv", "venv", "env", ".env",
    ".snapshots",  # our own backup dir
}
_IGNORED_DIRS_LOWER = frozenset(d.lower() for d in IGNORED_DIRS)

# Path segments that are never traversable at all — VCS internals can
# carry credentials (remote URLs, hooks) and the snapshot registry is our
# own rollback state, not task content.
BLOCKED_DIR_SEGMENTS = {".git", ".svn", ".hg", ".snapshots"}

# ---------------------------------------------------------------------------
# Secret masking regexes — applied to any text before returning to the model
# ---------------------------------------------------------------------------
_SECRET_PATTERNS = [
    # PEM private key blocks (RSA/EC/DSA/OPENSSH/PKCS#8, encrypted or not) —
    # the whole block incl. BEGIN/END markers; an unterminated block is
    # masked to the end of the text
    re.compile(
        r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?'
        r'(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)',
        re.DOTALL,
    ),
    # OpenRouter / OpenAI style keys
    re.compile(r'sk-[a-zA-Z0-9\-_]{20,}'),
    # AWS
    re.compile(r'AKIA[0-9A-Z]{16}'),
    re.compile(r'(?i)aws[_\-]?secret[_\-]?access[_\-]?key\s*[=:]\s*\S+'),
    # Generic tokens / passwords in config lines
    re.compile(r'(?i)(password|passwd|token|secret|api[_\-]?key|auth[_\-]?key)\s*[=:]\s*["\']?[^\s"\']{6,}["\']?'),
    # Bearer tokens
    re.compile(r'Bearer\s+[a-zA-Z0-9\-_\.]{20,}'),
    # Provider-prefixed tokens (GitHub, GitLab, Slack, Stripe, GCP, npm, PyPI)
    re.compile(
        r'\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|xox[bapors]-|'
        r'sk_live_|sk_test_|rk_live_|AIza|npm_|pypi-)[A-Za-z0-9_\-]{10,}'
    ),
    # Base64-ish long strings often used as secrets — pure-hex tokens are
    # left alone so hashes/digests in file content survive masking
    re.compile(
        r'(?<![a-zA-Z0-9/+])(?![0-9a-fA-F]{40,}={0,2}(?![a-zA-Z0-9/+]))'
        r'[a-zA-Z0-9/+]{40,}={0,2}(?![a-zA-Z0-9/+])'
    ),
]


def mask_secrets(text: str) -> str:
    """Replace detected secrets in text with [REDACTED]."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub('[REDACTED]', text)
    return text


# ---------------------------------------------------------------------------
# .gitignore parsing
# ---------------------------------------------------------------------------
def load_gitignore_patterns(workspace_root: Path) -> list[str]:
    """Parse .gitignore and return list of glob patterns.

    Approximation of gitignore semantics: comments and escaped ``#``/``!``
    are honored, negations (``!``) are kept so is_gitignored() can
    un-ignore, and trailing ``/`` (dir-only) is stripped — ignored
    directories are pruned before recursion anyway."""
    gi_path = workspace_root / ".gitignore"
    patterns = []
    if gi_path.exists():
        try:
            for line in gi_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#") and not line.startswith("\\#"):
                    continue
                if line.startswith("\\#") or line.startswith("\\!"):
                    line = line[1:]
                patterns.append(line.rstrip("/"))
        except Exception:
            pass
    return patterns


def is_gitignored(rel_path: str, gitignore_patterns: list[str]) -> bool:
    """Check if a relative path matches any gitignore pattern.

    Anchored patterns (``/foo``) match the full relative path only;
    unanchored patterns also match the basename. ``!`` patterns restore
    a previously-ignored path (last match wins, like git)."""
    ignored = False
    name = Path(rel_path).name
    for pattern in gitignore_patterns:
        negate = pattern.startswith("!")
        if negate:
            pattern = pattern[1:]
        anchored = pattern.startswith("/")
        if anchored:
            pattern = pattern.lstrip("/")
        if not pattern:
            continue
        if fnmatch.fnmatch(rel_path, pattern) or (
            not anchored and fnmatch.fnmatch(name, pattern)
        ):
            ignored = not negate
    return ignored


# ---------------------------------------------------------------------------
# Path traversal guard — THE CRITICAL SECURITY CHECK
# ---------------------------------------------------------------------------
class SecurityError(Exception):
    """Raised when a security constraint is violated."""


def resolve_safe_path(workspace_root: str | Path, requested_path: str | Path) -> Path:
    """
    Resolve requested_path against workspace_root and verify it stays within.

    Raises SecurityError if:
    - The resolved path escapes workspace_root (path traversal)
    - The filename matches a BLOCKED_FILE_PATTERNS entry
    - The path resolves to a blocked directory segment

    Returns the absolute resolved Path on success.
    """
    root = Path(workspace_root).resolve()
    # Join and resolve (handles .., symlinks, Windows drive letters)
    try:
        target = (root / requested_path).resolve()
    except (ValueError, OSError) as e:
        raise SecurityError(f"Cannot resolve path '{requested_path}': {e}") from e

    # --- Traversal check ---
    try:
        common = os.path.commonpath([str(root), str(target)])
    except ValueError:
        # On Windows, commonpath raises ValueError if paths are on different drives
        raise SecurityError(
            f"Path '{target}' is on a different drive from workspace '{root}'. Access denied."
        )

    if common != str(root):
        raise SecurityError(
            f"Path traversal detected: '{requested_path}' resolves to '{target}' "
            f"which is outside workspace '{root}'."
        )

    # --- Blocked path check ---
    # Every segment matters, not just the filename: `.env/backup.txt` must
    # not be readable just because `backup.txt` itself is a safe name.
    for segment in target.relative_to(root).parts:
        if segment.lower() in BLOCKED_DIR_SEGMENTS:
            raise SecurityError(
                f"Access through '{segment}' is blocked for security reasons."
            )
        for pattern in BLOCKED_FILE_PATTERNS:
            if fnmatch.fnmatch(segment, pattern) or segment == pattern:
                raise SecurityError(
                    f"Access to '{segment}' is blocked for security reasons (matches pattern '{pattern}')."
                )

    return target


def is_safe_dir(dir_name: str) -> bool:
    """Return True if directory name is not in the ignored/blocked list."""
    return dir_name.lower() not in _IGNORED_DIRS_LOWER


def check_model_zdr(model: str, provider_cfg: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Pre-flight Zero Data Retention (ZDR) gate.
    Rejects models that do not provide ZDR (e.g. :free, openrouter/free)
    locally before any network call is dispatched.

    Returns (is_allowed, rejection_reason).
    """
    if not model:
        return True, ""

    zdr_cfg = provider_cfg.get("zdr")
    # If explicit zdr section is missing, check provider.data_collection in extra_body
    if zdr_cfg is None:
        data_col = (
            provider_cfg.get("extra_body", {})
            .get("provider", {})
            .get("data_collection")
        )
        is_enabled = (data_col == "deny")
        deny_patterns = [":free", "openrouter/free"]
    else:
        is_enabled = bool(zdr_cfg.get("enabled", True))
        deny_patterns = zdr_cfg.get("deny_patterns") or [":free", "openrouter/free"]

    if not is_enabled:
        return True, ""

    model_lower = model.lower()
    for pattern in deny_patterns:
        if pattern.lower() in model_lower:
            return False, (
                f"[ZDR BLOCK] Model '{model}' violates Zero Data Retention policy "
                f"(matches pattern '{pattern}'). Rejected locally by BoteX Pre-Flight Guardrail."
            )

    return True, ""

