# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Server-Side Skill Vault & Autonomous L0 Triage
====================================================
Manages server-side engineering skills stored in `<harness>/skills/<name>/SKILL.md`
(or configured `paths.skills_dir` / `BOTEX_SKILLS_DIR`).

Capabilities:
1. Skill Catalog Discovery: Lists installed skills with YAML frontmatter metadata
   (`name`, `description`, `version`, `origin_repo`, `commit_sha`, `installed_at`, `taxonomy`).
2. Safe Skill Administration (Bielik / Operator): Install or update skills from raw Markdown
   or public repository URLs (GitHub blob/repo -> raw resolver) with DLP secret masking,
   prompt-injection sanitization, and path traversal protection.
3. Autonomous L0 Skill Triage: Matches incoming tasks and target files against installed
   skills via taxonomy/keyword scoring and optional local Bielik L0 classification,
   returning compact actionable instructions and `applied_skills` telemetry.
"""

from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .config import PROJECT_ROOT, load_config
    from .memory import _parse_frontmatter, _serialize_frontmatter
    from .net_tools import fetch_url
    from .security import (
        SecurityError,
        coerce_text,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
    )
except (ImportError, ValueError):
    from config import PROJECT_ROOT, load_config
    from memory import _parse_frontmatter, _serialize_frontmatter
    from net_tools import fetch_url
    from security import (
        SecurityError,
        coerce_text,
        mask_secrets,
        resolve_safe_path,
        sanitize_for_storage,
    )

__all__ = [
    "get_skills_dir",
    "list_skills",
    "get_skill",
    "install_skill",
    "install_skill_async",
    "delete_skill",
    "triage_skills_for_task",
    "format_skills_prompt_block",
    "MAX_SKILL_BYTES",
    "VALID_SKILL_TAXONOMIES",
]

MAX_SKILL_BYTES = 131_072  # 128 KB max per SKILL.md
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")

VALID_SKILL_TAXONOMIES = frozenset(
    {
        "bugfix",
        "refactor",
        "test",
        "infra",
        "analysis",
        "feature",
        "general",
        "security",
        "memory",
        "all",
    }
)

# Heuristic patterns guarding against adversarial prompt-injection inside external skills
_INJECTION_PATTERNS = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?system\s+prompts?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+in\s+developer\s+mode", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(your|previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(
        r"override\s+(all\s+)?(safety|system|security)\s+(rules|instructions|policies)",
        re.IGNORECASE,
    ),
)

_UNTRUSTED_WEB_WRAPPER_RE = re.compile(
    r"^\s*<untrusted_web_content[^>]*>\r?\n?(.*?)\r?\n?</untrusted_web_content>\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _unwrap_untrusted_web_content(raw_text: str) -> str:
    """Strip <untrusted_web_content ...> wrapper returned by fetch_url before parsing SKILL.md."""
    text = str(raw_text or "").strip()
    m = _UNTRUSTED_WEB_WRAPPER_RE.match(text)
    if m:
        return m.group(1).strip()
    return text


def _infer_skill_name_from_url(url: str) -> str:
    """Extract a sensible skill slug from a GitHub/raw URL, skipping branch/structural tokens."""
    clean_url = str(url or "").strip().partition("?")[0].partition("#")[0].rstrip("/")
    parts = [p for p in clean_url.split("/") if p and not p.lower().startswith("http")]
    # Remove domain part if present
    if parts and (
        "." in parts[0] or parts[0].lower() in ("github.com", "raw.githubusercontent.com")
    ):
        parts = parts[1:]
    skip_tokens = {
        "skill.md",
        "readme.md",
        "main",
        "master",
        "head",
        "develop",
        "trunk",
        "blob",
        "tree",
        "raw",
        "refs",
        "heads",
        "skills",
    }
    for token in reversed(parts):
        low = token.lower()
        if low in skip_tokens or low.endswith(".md"):
            continue
        slug = re.sub(r"\.git$", "", token, flags=re.IGNORECASE)
        if _SKILL_NAME_RE.match(slug):
            return slug.lower()
    return ""


def _sanitize_skill_name(name: str) -> str:
    """Validate skill identifier against path traversal and CWE-020."""
    clean = str(name or "").strip().lower()
    if clean.endswith(".md"):
        clean = clean[:-3]
    if not _SKILL_NAME_RE.match(clean):
        raise SecurityError(
            f"Invalid skill name '{name}'. Only alphanumeric characters, '-' and '_' (max 64 chars) are allowed."
        )
    return coerce_text(clean)


def get_skills_dir() -> Path:
    """Resolve and ensure the server-side skills catalog directory exists."""
    env_dir = os.environ.get("BOTEX_SKILLS_DIR", "").strip()
    if env_dir:
        p = Path(os.path.expanduser(env_dir)).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    try:
        cfg = load_config()
        cfg_dir = cfg.get("paths", {}).get("skills_dir")
        if cfg_dir:
            raw = str(cfg_dir).strip()
            cand = Path(os.path.expanduser(raw))
            if not cand.is_absolute():
                cand = (PROJECT_ROOT / cand).resolve()
            else:
                cand = cand.resolve()
            cand.mkdir(parents=True, exist_ok=True)
            return cand
    except Exception:
        pass

    default_dir = (PROJECT_ROOT / "skills").resolve()
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def _normalize_repo_raw_url(url: str) -> str:
    """Convert GitHub blob or repository root URLs into raw SKILL.md URLs when applicable."""
    u = str(url or "").strip()
    if not u:
        return ""
    # Convert https://github.com/<owner>/<repo>/blob/<branch>/<path>
    blob_match = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
        u,
        re.IGNORECASE,
    )
    if blob_match:
        owner, repo, branch, path = blob_match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    # Convert https://github.com/<owner>/<repo>/tree/<branch>/<path>
    tree_match = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$",
        u,
        re.IGNORECASE,
    )
    if tree_match:
        owner, repo, branch, path = tree_match.groups()
        if not path.lower().endswith(".md"):
            path = f"{path}/SKILL.md"
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"

    # Convert https://github.com/<owner>/<repo>/tree/<branch> -> <branch>/SKILL.md
    tree_root_match = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/tree/([^/#?]+)/?$",
        u,
        re.IGNORECASE,
    )
    if tree_root_match:
        owner, repo, branch = tree_root_match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/SKILL.md"

    # Convert bare repo https://github.com/<owner>/<repo> -> main/SKILL.md
    repo_match = re.match(
        r"^https://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?$",
        u,
        re.IGNORECASE,
    )
    if repo_match:
        owner, repo = repo_match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md"

    return u


def _parse_skill_file(skill_file: Path, skills_root: Path) -> Dict[str, Any]:
    """Read and parse a SKILL.md file into structured metadata + body."""
    raw_text = skill_file.read_text(encoding="utf-8", errors="replace")
    meta, body = _parse_frontmatter(raw_text)

    folder_name = skill_file.parent.name
    nested_meta = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}

    name = str(meta.get("name") or folder_name).strip()
    description = str(meta.get("description") or "").strip()
    version = str(
        meta.get("version") or (nested_meta.get("version") if nested_meta else "") or "1.0.0"
    ).strip()
    origin_repo = str(
        meta.get("origin_repo")
        or (nested_meta.get("origin_repo") if nested_meta else "")
        or "builtin"
    ).strip()
    commit_sha = str(
        meta.get("commit_sha") or (nested_meta.get("commit_sha") if nested_meta else "") or ""
    ).strip()
    installed_at = str(
        meta.get("installed_at") or (nested_meta.get("installed_at") if nested_meta else "") or ""
    ).strip()
    if not installed_at:
        try:
            installed_at = datetime.fromtimestamp(
                skill_file.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except Exception:
            installed_at = ""

    raw_tax = meta.get("taxonomy") or []
    if isinstance(raw_tax, str):
        raw_tax = [t.strip().lower() for t in raw_tax.strip("[]").split(",") if t.strip()]
    elif isinstance(raw_tax, list):
        raw_tax = [str(t).strip().lower() for t in raw_tax if str(t).strip()]
    else:
        raw_tax = []

    if not raw_tax:
        # Infer sensible taxonomy tags from name + description
        combined = f"{name} {description}".lower()
        inferred: List[str] = []
        if any(k in combined for k in ("review", "audit", "security", "check")):
            inferred.append("analysis")
        if any(k in combined for k in ("test", "tdd", "pytest", "verification")):
            inferred.append("test")
        if any(k in combined for k in ("deploy", "vps", "systemd", "docker", "infra")):
            inferred.append("infra")
        if any(k in combined for k in ("bug", "fix", "debug", "error")):
            inferred.append("bugfix")
        if any(k in combined for k in ("refactor", "clean", "humanizer", "miodkuj")):
            inferred.append("refactor")
        if any(k in combined for k in ("memory", "knowledge", "synthesis", "handoff")):
            inferred.append("general")
        raw_tax = inferred or ["general"]

    try:
        rel_path = skill_file.relative_to(skills_root).as_posix()
    except ValueError:
        rel_path = skill_file.name

    return {
        "name": folder_name,
        "display_name": name,
        "description": description,
        "version": version,
        "origin_repo": origin_repo,
        "commit_sha": commit_sha,
        "installed_at": installed_at,
        "taxonomy": raw_tax,
        "size_bytes": len(raw_text.encode("utf-8")),
        "path": f"skills/{rel_path}",
        "content": body.strip(),
    }


def list_skills(skills_dir: Optional[Path] = None) -> Dict[str, Any]:
    """List all installed skills in the BoteX Skill Vault."""
    try:
        root = (skills_dir or get_skills_dir()).resolve()
        if not root.exists():
            return {"ok": True, "count": 0, "skills": [], "skills_dir": str(root)}

        skills: List[Dict[str, Any]] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                parsed = _parse_skill_file(skill_file, root)
                # Omit full markdown body in summary list to keep payloads compact
                summary_item = {k: v for k, v in parsed.items() if k != "content"}
                summary_item["preview"] = parsed["content"][:240] + (
                    "…" if len(parsed["content"]) > 240 else ""
                )
                skills.append(summary_item)
            except Exception:
                continue

        return {
            "ok": True,
            "count": len(skills),
            "skills": skills,
            "skills_dir": str(root),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to list skills: {coerce_text(mask_secrets(str(exc)))}",
            "skills": [],
            "count": 0,
        }


def get_skill(name: str, skills_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Retrieve full metadata and Markdown instructions for a specific installed skill."""
    try:
        clean_name = _sanitize_skill_name(name)
        root = (skills_dir or get_skills_dir()).resolve()
        skill_dir = resolve_safe_path(root, clean_name)
        skill_file = resolve_safe_path(skill_dir, "SKILL.md")
        if not skill_file.is_file():
            return {"ok": False, "error": f"Skill '{clean_name}' not found."}

        parsed = _parse_skill_file(skill_file, root)
        return {"ok": True, "skill": parsed}
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to read skill: {coerce_text(mask_secrets(str(exc)))}",
        }


def _validate_and_write_skill(
    clean_name: str,
    raw_content: str,
    *,
    description: str = "",
    version: str = "",
    origin_repo: str = "",
    commit_sha: str = "",
    taxonomy: Optional[List[str]] = None,
    skills_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Sanitize, enrich frontmatter, and write SKILL.md to disk."""
    if not raw_content or not raw_content.strip():
        return {"ok": False, "error": "Skill content cannot be empty."}

    encoded_len = len(raw_content.encode("utf-8"))
    if encoded_len > MAX_SKILL_BYTES:
        return {
            "ok": False,
            "error": f"Skill content exceeds maximum allowed size ({MAX_SKILL_BYTES} bytes).",
        }

    for pat in _INJECTION_PATTERNS:
        if pat.search(raw_content):
            return {
                "ok": False,
                "error": "Security policy rejected skill: detected adversarial prompt-injection pattern.",
            }

    existing_meta, body = _parse_frontmatter(raw_content)
    if not body.strip() and not raw_content.startswith("---"):
        body = raw_content.strip()
        existing_meta = {}

    nested_meta = (
        existing_meta.get("metadata") if isinstance(existing_meta.get("metadata"), dict) else {}
    )

    eff_desc = (
        description.strip()
        or str(existing_meta.get("description") or "").strip()
        or f"Installed skill {clean_name}"
    )
    eff_ver = (
        version.strip()
        or str(
            existing_meta.get("version") or (nested_meta.get("version") if nested_meta else "")
        ).strip()
        or "1.0.0"
    )
    eff_repo = (
        origin_repo.strip()
        or str(
            existing_meta.get("origin_repo")
            or (nested_meta.get("origin_repo") if nested_meta else "")
        ).strip()
        or "custom"
    )
    eff_sha = (
        commit_sha.strip()
        or str(
            existing_meta.get("commit_sha")
            or (nested_meta.get("commit_sha") if nested_meta else "")
        ).strip()
    )

    raw_tax = existing_meta.get("taxonomy") or []
    if isinstance(raw_tax, str):
        fm_tax = [t.strip().lower() for t in raw_tax.strip("[]").split(",") if t.strip()]
    elif isinstance(raw_tax, list):
        fm_tax = [str(t).strip().lower() for t in raw_tax if str(t).strip()]
    else:
        fm_tax = []

    cleaned_param_tax = (
        [sanitize_for_storage(str(t).strip().lower()) for t in taxonomy if str(t).strip()]
        if taxonomy
        else []
    )
    if cleaned_param_tax and (cleaned_param_tax != ["general"] or not fm_tax):
        eff_tax = cleaned_param_tax
    elif fm_tax:
        eff_tax = [sanitize_for_storage(t) for t in fm_tax]
    else:
        eff_tax = ["general"]

    now_iso = datetime.now(timezone.utc).isoformat()
    meta_to_save: Dict[str, Any] = {
        "name": clean_name,
        "description": sanitize_for_storage(eff_desc),
        "version": sanitize_for_storage(eff_ver),
        "origin_repo": sanitize_for_storage(eff_repo),
        "installed_at": now_iso,
        "taxonomy": eff_tax,
    }
    if eff_sha:
        meta_to_save["commit_sha"] = sanitize_for_storage(eff_sha)

    full_doc = sanitize_for_storage(_serialize_frontmatter(meta_to_save, body.strip()))

    root = (skills_dir or get_skills_dir()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target_dir = resolve_safe_path(root, clean_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = resolve_safe_path(target_dir, "SKILL.md")
    target_file.write_text(full_doc, encoding="utf-8")

    parsed = _parse_skill_file(target_file, root)
    return {
        "ok": True,
        "name": clean_name,
        "version": parsed["version"],
        "origin_repo": parsed["origin_repo"],
        "commit_sha": parsed["commit_sha"],
        "installed_at": parsed["installed_at"],
        "taxonomy": parsed["taxonomy"],
        "path": parsed["path"],
        "skill": {k: v for k, v in parsed.items() if k != "content"},
    }


async def install_skill_async(
    name: str = "",
    content: str = "",
    source_url: str = "",
    origin_repo: str = "",
    version: str = "",
    commit_sha: str = "",
    description: str = "",
    taxonomy: Optional[List[str]] = None,
    skills_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Install or update a skill asynchronously from raw Markdown content or a public URL/repo."""
    try:
        raw_content = content.strip() if content else ""
        resolved_origin = origin_repo.strip() if origin_repo else ""

        if not raw_content and source_url:
            fetch_target = _normalize_repo_raw_url(source_url)
            if not resolved_origin:
                resolved_origin = source_url.strip()
            net_cfg = load_config().get("net", {})
            fetched = await fetch_url(fetch_target, net_cfg=net_cfg, max_bytes=MAX_SKILL_BYTES)
            if not isinstance(fetched, dict) or not fetched.get("ok"):
                err_detail = (
                    fetched.get("error") if isinstance(fetched, dict) else "Failed to fetch URL"
                )
                return {
                    "ok": False,
                    "error": f"Failed to fetch skill from URL: {coerce_text(mask_secrets(str(err_detail)))}",
                }
            if fetched.get("truncated"):
                return {
                    "ok": False,
                    "error": f"Remote skill content from URL exceeded max size ({MAX_SKILL_BYTES} bytes) and was truncated.",
                }
            raw_content = _unwrap_untrusted_web_content(
                str(fetched.get("text") or fetched.get("content") or "")
            )
            if re.match(r"^\s*(?:<!doctype\s+html|<html\b)", raw_content, re.IGNORECASE):
                return {
                    "ok": False,
                    "error": "Fetched URL returned an HTML document instead of a valid Markdown SKILL.md.",
                }
        else:
            raw_content = _unwrap_untrusted_web_content(raw_content)

        if not raw_content:
            return {
                "ok": False,
                "error": "Provide either 'content' (SKILL.md text) or 'source_url' to install a skill.",
            }

        # Derive skill name from frontmatter or URL if not explicitly provided
        candidate_name = (name or "").strip()
        if not candidate_name:
            fm, _ = _parse_frontmatter(raw_content)
            candidate_name = str(fm.get("name") or "").strip()
        if not candidate_name and source_url:
            candidate_name = _infer_skill_name_from_url(source_url)

        clean_name = _sanitize_skill_name(candidate_name)
        return _validate_and_write_skill(
            clean_name,
            raw_content,
            description=description,
            version=version,
            origin_repo=resolved_origin,
            commit_sha=commit_sha,
            taxonomy=taxonomy,
            skills_dir=skills_dir,
        )
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to install skill: {coerce_text(mask_secrets(str(exc)))}",
        }


def install_skill(
    name: str = "",
    content: str = "",
    source_url: str = "",
    origin_repo: str = "",
    version: str = "",
    commit_sha: str = "",
    description: str = "",
    taxonomy: Optional[List[str]] = None,
    skills_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Synchronous wrapper for installing a skill from content or local/remote source."""
    if content and not source_url:
        try:
            candidate_name = (name or "").strip()
            if not candidate_name:
                fm, _ = _parse_frontmatter(content)
                candidate_name = str(fm.get("name") or "").strip()
            clean_name = _sanitize_skill_name(candidate_name)
            return _validate_and_write_skill(
                clean_name,
                content,
                description=description,
                version=version,
                origin_repo=origin_repo,
                commit_sha=commit_sha,
                taxonomy=taxonomy,
                skills_dir=skills_dir,
            )
        except SecurityError as se:
            return {
                "ok": False,
                "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Failed to install skill: {coerce_text(mask_secrets(str(exc)))}",
            }

    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        import concurrent.futures

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(
                    asyncio.run,
                    install_skill_async(
                        name=name,
                        content=content,
                        source_url=source_url,
                        origin_repo=origin_repo,
                        version=version,
                        commit_sha=commit_sha,
                        description=description,
                        taxonomy=taxonomy,
                        skills_dir=skills_dir,
                    ),
                )
                return fut.result(timeout=30)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Failed to install skill: {coerce_text(mask_secrets(str(exc)))}",
            }
    return asyncio.run(
        install_skill_async(
            name=name,
            content=content,
            source_url=source_url,
            origin_repo=origin_repo,
            version=version,
            commit_sha=commit_sha,
            description=description,
            taxonomy=taxonomy,
            skills_dir=skills_dir,
        )
    )


def delete_skill(name: str, skills_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Delete an installed skill from the server-side Skill Vault."""
    try:
        clean_name = _sanitize_skill_name(name)
        root = (skills_dir or get_skills_dir()).resolve()
        target_dir = resolve_safe_path(root, clean_name)
        skill_file = resolve_safe_path(target_dir, "SKILL.md")
        if not skill_file.exists():
            return {"ok": False, "error": f"Skill '{clean_name}' not found."}

        shutil.rmtree(target_dir)
        return {"ok": True, "name": clean_name, "deleted": True}
    except SecurityError as se:
        return {
            "ok": False,
            "error": f"Security violation: {coerce_text(mask_secrets(str(se)))}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Failed to delete skill: {coerce_text(mask_secrets(str(exc)))}",
        }


def triage_skills_for_task(
    task: str,
    files: Optional[List[str]] = None,
    taxonomy: str = "general",
    max_skills: int = 2,
    skills_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Autonomous L0 Triage: selects the most relevant installed skills for the given task.

    Scoring rules:
    1. Explicit skill mention in prompt (`@skill-name`, `/skill-name`, or exact `skill-name`): +100
    2. Taxonomy match (`taxonomy` in skill's `taxonomy` list or `'all'` in skill's `taxonomy`): +25
    3. Keyword overlap between task/files and skill name/description: +10 per keyword match
    """
    try:
        root = (skills_dir or get_skills_dir()).resolve()
        if not root.exists():
            return []

        task_lower = (task or "").lower()
        task_tokens = set(re.findall(r"\b[a-z0-9_]{3,}\b", task_lower))
        files_lower = " ".join(files or []).lower()
        norm_tax = (taxonomy or "general").strip().lower()

        scored: List[tuple[int, Dict[str, Any]]] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                parsed = _parse_skill_file(skill_file, root)
            except Exception:
                continue

            sname = parsed["name"].lower()
            s_tax = [str(t).lower() for t in (parsed.get("taxonomy") or [])]
            score = 0

            # 1. Explicit mention in task prompt
            if sname in task_lower or sname.replace("-", "_") in task_lower:
                score += 100

            # 2. Taxonomy match
            if "all" in s_tax:
                score += 20
            elif norm_tax in s_tax:
                score += 30

            # 3. Keyword overlap with description and skill name tokens
            name_tokens = set(re.findall(r"[a-z0-9]{3,}", sname))
            desc_tokens = set(re.findall(r"\b[a-z0-9]{4,}\b", parsed["description"].lower()))
            overlap_name = len(task_tokens & name_tokens)
            overlap_desc = len(task_tokens & desc_tokens)
            score += overlap_name * 15 + min(overlap_desc * 5, 25)

            # File extension / domain hints
            if any(ext in files_lower for ext in ("test_", "_test.py", ".spec.ts")) and (
                "test" in s_tax or "tdd" in sname
            ):
                score += 25

            if score >= 20:
                scored.append((score, parsed))

        scored.sort(key=lambda item: (-item[0], item[1]["name"]))
        selected = [item[1] for item in scored[: max(1, int(max_skills))]]
        return selected
    except Exception:
        return []


def format_skills_prompt_block(
    skills: List[Dict[str, Any]], max_chars_per_skill: int = 1800
) -> str:
    """Format matched skills into a compact, zero-bloat system prompt block."""
    if not skills:
        return ""
    blocks: List[str] = ["ACTIVE HARNESS SKILLS (Autonomous L0 Triage):"]
    for s in skills:
        name = s.get("name", "skill")
        ver = s.get("version", "1.0.0")
        body = str(s.get("content") or "").strip()
        if len(body) > max_chars_per_skill:
            body = body[:max_chars_per_skill].rstrip() + "\n...[truncated for context efficiency]"
        blocks.append(f"--- [SKILL: {name} (v{ver})] ---\n{body}")
    return "\n\n".join(blocks) + "\n"
