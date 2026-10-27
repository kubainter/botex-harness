# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Chat Mode
===============
Conversational execution loop with streaming chunk generation, multi-turn history
management, read-only workspace inspection (zero workspace file mutation, zero command execution),
and governed Memory Vault / Skill Vault administration.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

try:
    from .config import (
        get_chat_sessions_dir,
        load_config,
        resolve_model,
        resolve_provider,
        resolve_workspace,
    )
    from .file_tools import (
        get_file_outline,
        list_dir,
        read_file,
        read_file_lines,
    )
    from .memory import (
        delete_memory,
        patch_memory_section,
        read_memory,
        save_memory,
        search_memory,
    )
    from .net_tools import fetch_url
    from .pricing import check_model_price
    from .providers import (
        ProviderRegistry,
        build_async_client,
        sanitize_provider_error,
    )
    from .retry import call_with_backoff
    from .security import (
        SecurityError,
        check_model_zdr,
        coerce_text,
        mask_secrets,
        sanitize_identifier,
    )
    from .skills import install_skill_async, list_skills
except (ImportError, ValueError):
    from config import (
        get_chat_sessions_dir,
        load_config,
        resolve_model,
        resolve_provider,
        resolve_workspace,
    )
    from file_tools import (
        get_file_outline,
        list_dir,
        read_file,
        read_file_lines,
    )
    from memory import (
        delete_memory,
        patch_memory_section,
        read_memory,
        save_memory,
        search_memory,
    )
    from net_tools import fetch_url
    from pricing import check_model_price
    from providers import (
        ProviderRegistry,
        build_async_client,
        sanitize_provider_error,
    )
    from retry import call_with_backoff
    from security import (
        SecurityError,
        check_model_zdr,
        coerce_text,
        mask_secrets,
        sanitize_identifier,
    )

    from skills import install_skill_async, list_skills

__all__ = [
    "ChatMessage",
    "ChatSession",
    "ChatManager",
    "default_chat_manager",
    "stream_chat_turn",
    "chat_turn",
    "execute_chat_tool",
    "READ_ONLY_CHAT_TOOLS",
    "DEFAULT_CHAT_SYSTEM_PROMPT",
]

_VALID_ROLES_MAP: dict[str, str] = {
    "system": "system",
    "user": "user",
    "assistant": "assistant",
    "tool": "tool",
}

DEFAULT_CHAT_SYSTEM_PROMPT = (
    "You are BoteX — an intelligent, versatile AI assistant and Vault Administrator in Chat Mode.\n"
    "In Chat Mode, you converse freely, answer general questions, explore topics, assist with software engineering, "
    "and curate the BoteX Memory Vault and Server-Side Skill Vault.\n"
    "You are strictly helpful, analytical, witty, and direct. You never judge, moralize, or deliver unsolicited ethics lectures.\n"
    "LANGUAGE INVARIANT: Strictly detect and match the user's primary language. If the user communicates in Polish, you MUST always reply in fluent, natural, grammatically correct Polish.\n"
    "You have access to workspace inspection and Vault administration tools:\n"
    "- fetch_url(url): Fetch live public web pages, documentation, or public REST APIs (e.g. currency rates, public data).\n"
    "- get_file_outline(path): Inspect file classes, methods, and functions with line numbers.\n"
    "- read_file_lines(path, start, end): Read specific line slices of files.\n"
    "- read_file(path): Read entire small files (<150 lines).\n"
    "- list_dir(path): List files and directories (respects .gitignore).\n"
    "- search_memory(query, tag, kind, scope): Search local workspace or global shared memory.\n"
    "- read_memory(memory_id, scope): Retrieve stored decisions, handoffs, or architectural notes.\n"
    "- save_memory(title, content, kind, tags, scope): Save a structured note, decision, or lesson into the Memory Vault when requested.\n"
    "- patch_memory_section(memory_id, section, new_content, scope, append): Surgically update or append a Markdown section (e.g. 'Powiązania' or 'Compiled Truth') in an existing memory note.\n"
    "- delete_memory(memory_id, scope): Delete an obsolete or duplicate note from the Memory Vault when explicitly requested.\n"
    "- find_graph_health(scope): Audit Memory Vault graph topology (broken links, orphans).\n"
    "- list_skills(): Inspect installed server-side engineering skills in the BoteX Skill Vault.\n"
    "- install_skill(name, source_url, content, version, origin_repo, taxonomy): Install or update a skill in the BoteX Skill Vault when requested by the operator.\n\n"
    "Rules & Local Model Guidelines (Bielik / Vault Admin Protocol):\n"
    "1. ZERO TOOL SIMULATION (No Fast-Forwarding): Never simulate, narrate, or print raw JSON tool calls in your text response. Always invoke the actual tool via function calling and wait for its result.\n"
    "2. SINGLE-TARGET CURATION: When curating the Memory Vault (fixing links, updating sections, or linking orphans), operate on ONE memory note at a time using `patch_memory_section` or `save_memory` — never attempt to batch-edit dozens of files in a single turn.\n"
    "3. WIKILINK & GRAPH HYGIENE: Only create `[[memory_id]]` wikilinks to real, existing memory IDs (without `.md` extension). Never save ephemeral telemetry or raw logs into the Memory Vault.\n"
    "4. WORKSPACE CODE SAFETY: You CANNOT mutate workspace source code files or execute terminal commands in Chat Mode. For multi-file code edits or test runs, advise using Agent Mode."
)

# Tool Declarations for OpenAI / OpenRouter Function Calling in Chat Mode
READ_ONLY_CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetches raw text, JSON, or markdown content from a public URL or public API endpoint "
                "(e.g. for currency rates, documentation, public data)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The public HTTP or HTTPS URL to fetch.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_outline",
            "description": (
                "Returns class, method, and function signatures with line numbers. "
                "Saves 90% of tokens compared to reading the full file. "
                "ALWAYS call this first before reading lines of a file."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path to file."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_lines",
            "description": "Reads a line range (1-indexed, inclusive) from a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file."},
                    "start": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed).",
                    },
                    "end": {"type": "integer", "description": "Ending line number (inclusive)."},
                },
                "required": ["path", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Reads the entire file. Use ONLY for small files (<150 lines).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path to file."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Lists files and subdirectories, respecting .gitignore and ignoring vendor/build dirs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to directory (use '.' for root).",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Searches the Memory Vault by query, tag, or kind across local workspace "
                "or global shared scope."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword search query."},
                    "tag": {"type": "string", "description": "Filter by tag."},
                    "kind": {
                        "type": "string",
                        "description": "Filter by kind (context, decision, handoff, lesson).",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local', 'global', or 'all'). Default is 'local'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Reads full content and metadata of a memory entry by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Memory ID to retrieve."},
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local' or 'global'). Default is 'local'.",
                    },
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_scratchpad",
            "description": "Reads the full offloaded terminal or execution log from .botex/scratchpad/ by memory_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "The scratchpad log ID (e.g. 'mem_exec_a1b2c3d4').",
                    },
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_backlinks",
            "description": "Finds all memories that link TO the given memory_id via [[wikilinks]].",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "Memory ID to retrieve backlinks for.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local', 'global', or 'all').",
                    },
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_graph_health",
            "description": "Audits Memory Vault knowledge graph integrity: broken links, orphans, cycles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local', 'global', or 'all').",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Saves a structured note, architectural decision, lesson, or handoff into the Memory Vault "
                "when the user asks to save or remember something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short descriptive title."},
                    "content": {
                        "type": "string",
                        "description": "Markdown content (include [[wikilinks]] when relevant).",
                    },
                    "kind": {
                        "type": "string",
                        "description": "Category ('context', 'decision', 'lesson', 'handoff').",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of tags.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local' or 'global'). Default is 'local'.",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_memory_section",
            "description": (
                "Surgically replaces or appends content to a specific Markdown section (e.g. 'Powiązania' or 'Compiled Truth') "
                "of an existing memory note without needing unified diffs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "Target memory ID (without .md).",
                    },
                    "section": {
                        "type": "string",
                        "description": "Markdown section heading name (e.g. 'Powiązania', 'Compiled Truth').",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "Markdown text to write or append into the section (e.g. '- Powiązano z [[other_id]]').",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local' or 'global'). Default is 'local'.",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If true, appends to the section instead of replacing it.",
                    },
                },
                "required": ["memory_id", "section", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_memory",
            "description": "Deletes a memory note by ID from the local or global Memory Vault when requested by the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Memory ID to delete."},
                    "scope": {
                        "type": "string",
                        "description": "Scope ('local', 'global', or 'all'). Default is 'local'.",
                    },
                },
                "required": ["memory_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "Lists all installed server-side skills in the BoteX Skill Vault with version and origin repo.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "install_skill",
            "description": (
                "Installs or updates a server-side skill in the BoteX Skill Vault from a repository/URL "
                "or Markdown content when requested by the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill identifier (e.g. 'code-review-security').",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Public GitHub repo/blob URL or raw SKILL.md URL to install from.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Optional raw SKILL.md markdown content if not fetching from source_url.",
                    },
                    "version": {
                        "type": "string",
                        "description": "Skill version string (e.g. '1.0.0').",
                    },
                    "origin_repo": {
                        "type": "string",
                        "description": "Source repository URL or identifier.",
                    },
                    "taxonomy": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task taxonomy tags ('bugfix', 'refactor', 'test', 'infra', 'analysis', 'feature', 'general').",
                    },
                },
            },
        },
    },
]


@dataclass
class ChatMessage:
    """A single chat message in multi-turn conversation with secret masking."""

    role: str
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    # Provider extras echoed verbatim on the next request (e.g. Gemini
    # extra_content.google.thought_signature). Never masked — the blob is
    # opaque and must survive byte-exact for thinking models.
    extra_content: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        norm = str(self.role).strip().lower()
        self.role = _VALID_ROLES_MAP.get(norm, "user")
        if self.content is not None:
            self.content = coerce_text(mask_secrets(str(self.content)))

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict representation matching OpenAI message format."""
        d: Dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = mask_secrets(self.content)
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.extra_content is not None:
            d["extra_content"] = self.extra_content
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        elif self.role == "tool":
            d["tool_call_id"] = "call_fallback"
        if self.name is not None:
            d["name"] = self.name
        return d


class ChatSession:
    """Manages multi-turn conversation history, configuration, and workspace context."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        workspace_dir: str = "",
        model: str = "",
        provider: str = "",
        profile: str = "",
        system_prompt: Optional[str] = None,
    ) -> None:
        self.session_id = session_id or f"chat_{uuid.uuid4().hex[:8]}"
        self.workspace_dir = resolve_workspace(workspace_dir)
        self.model = model
        self.provider = provider
        self.profile = profile
        self.messages: List[ChatMessage] = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.system_prompt = system_prompt or DEFAULT_CHAT_SYSTEM_PROMPT
        self._lock: Optional[asyncio.Lock] = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def add_user_message(self, content: str) -> ChatMessage:
        msg = ChatMessage(role="user", content=content)
        self.messages.append(msg)
        self.updated_at = time.time()
        self.save_to_disk()
        return msg

    def add_assistant_message(
        self,
        content: Optional[str],
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        extra_content: Optional[Dict[str, Any]] = None,
    ) -> ChatMessage:
        msg = ChatMessage(
            role="assistant", content=content, tool_calls=tool_calls, extra_content=extra_content
        )
        self.messages.append(msg)
        self.updated_at = time.time()
        self.save_to_disk()
        return msg

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> ChatMessage:
        effective_id = tool_call_id or f"call_{uuid.uuid4().hex[:8]}"
        msg = ChatMessage(role="tool", content=content, tool_call_id=effective_id, name=name)
        self.messages.append(msg)
        self.updated_at = time.time()
        self.save_to_disk()
        return msg

    def get_history(self, max_turns: Optional[int] = None) -> List[Dict[str, Any]]:
        history = [m.to_dict() for m in self.messages]
        if max_turns is not None and max_turns > 0 and history:
            sliced = history[-max_turns:]
        else:
            sliced = list(history)

        # Sanitize tool calls and prevent orphan/malformed tool messages
        sanitized: List[Dict[str, Any]] = []
        for msg in sliced:
            role = msg.get("role")
            if role == "tool":
                prev = sanitized[-1] if sanitized else None
                if not prev or prev.get("role") != "assistant" or not prev.get("tool_calls"):
                    # Drop orphan tool message without triggering assistant
                    continue
                call_id = msg.get("tool_call_id")
                if not call_id:
                    prev_tcs = prev.get("tool_calls") or []
                    call_id = prev_tcs[0].get("id") if prev_tcs else f"call_{uuid.uuid4().hex[:8]}"
                    msg["tool_call_id"] = call_id
                sanitized.append(msg)
            else:
                sanitized.append(msg)

        # Clean trailing assistant with unresolved tool_calls if any
        while (
            sanitized
            and sanitized[-1].get("role") == "assistant"
            and sanitized[-1].get("tool_calls")
        ):
            sanitized.pop()

        return sanitized

    def get_title(self) -> str:
        """Derive a human-readable title from the first user message or fallback."""
        for m in self.messages:
            if m.role == "user" and m.content:
                clean = re.sub(r"\s+", " ", m.content).strip()
                if clean:
                    return clean[:60] + ("..." if len(clean) > 60 else "")
        return f"Chat {self.session_id[:8]}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert session to full dictionary representation with message history."""
        return {
            "session_id": self.session_id,
            "title": self.get_title(),
            "workspace_dir": self.workspace_dir,
            "model": self.model,
            "provider": self.provider,
            "profile": self.profile,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
            "messages": [
                {
                    "id": f"msg_{i}_{int(m.timestamp)}",
                    "role": m.role,
                    "content": m.content or "",
                    "timestamp": int(m.timestamp * 1000),
                    "model": self.model if m.role == "assistant" else None,
                    "tool_calls": m.tool_calls,
                    "tool_call_id": m.tool_call_id,
                    "name": m.name,
                }
                for i, m in enumerate(self.messages)
            ],
        }

    def save_to_disk(self) -> None:
        """Persist session JSON to configured chat sessions directory."""
        if not self.workspace_dir or not self.messages:
            return
        try:
            safe_id = sanitize_identifier(self.session_id)
            if not safe_id:
                return
            sessions_dir = get_chat_sessions_dir(self.workspace_dir)
            file_path = sessions_dir / f"{safe_id}.json"
            data = self.to_dict()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def clear(self) -> None:
        self.messages.clear()
        self.updated_at = time.time()
        self.save_to_disk()


class ChatManager:
    """Session registry managing concurrent multi-turn chat sessions with disk persistence."""

    def __init__(self) -> None:
        self._sessions: Dict[str, ChatSession] = {}

    def _load_session_from_disk(self, session_id: str, workspace_dir: str) -> Optional[ChatSession]:
        safe_id = sanitize_identifier(session_id)
        if not safe_id:
            return None
        ws = resolve_workspace(workspace_dir)
        sessions_dir = get_chat_sessions_dir(ws)
        file_path = sessions_dir / f"{safe_id}.json"
        if not file_path.is_file():
            legacy_path = Path(ws) / ".botex" / "chat_sessions" / f"{safe_id}.json"
            if legacy_path.is_file():
                file_path = legacy_path
            else:
                return None
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            sess = ChatSession(
                session_id=data.get("session_id", safe_id),
                workspace_dir=ws,
                model=data.get("model", ""),
                provider=data.get("provider", ""),
                profile=data.get("profile", ""),
            )
            sess.created_at = data.get("created_at", time.time())
            sess.updated_at = data.get("updated_at", time.time())
            for m in data.get("messages", []):
                role = m.get("role", "user")
                content = m.get("content")
                tc = m.get("tool_calls")
                tcid = m.get("tool_call_id")
                name = m.get("name")
                extra = m.get("extra_content")
                if role == "user":
                    sess.messages.append(ChatMessage(role="user", content=content))
                elif role == "assistant":
                    sess.messages.append(
                        ChatMessage(
                            role="assistant",
                            content=content,
                            tool_calls=tc,
                            extra_content=extra if isinstance(extra, dict) else None,
                        )
                    )
                elif role == "tool":
                    # Self-heal missing tool_call_id from preceding assistant tool_calls if available
                    if not tcid and sess.messages and sess.messages[-1].tool_calls:
                        tcid = sess.messages[-1].tool_calls[0].get("id")
                    sess.messages.append(
                        ChatMessage(
                            role="tool",
                            content=content,
                            tool_call_id=tcid or f"call_{uuid.uuid4().hex[:8]}",
                            name=name,
                        )
                    )
            return sess
        except Exception:
            return None

    def get_or_create_session(
        self,
        session_id: Optional[str] = None,
        workspace_dir: str = "",
        model: str = "",
        provider: str = "",
        profile: str = "",
        system_prompt: Optional[str] = None,
    ) -> ChatSession:
        ws = resolve_workspace(workspace_dir)
        if session_id and session_id in self._sessions:
            sess = self._sessions[session_id]
            if ws:
                sess.workspace_dir = ws
            if model:
                sess.model = model
            if provider:
                sess.provider = provider
            if profile:
                sess.profile = profile
            return sess

        if session_id:
            loaded = self._load_session_from_disk(session_id, ws)
            if loaded:
                if model:
                    loaded.model = model
                if provider:
                    loaded.provider = provider
                self._sessions[loaded.session_id] = loaded
                return loaded

        sess = ChatSession(
            session_id=session_id,
            workspace_dir=ws,
            model=model,
            provider=provider,
            profile=profile,
            system_prompt=system_prompt,
        )
        self._sessions[sess.session_id] = sess
        return sess

    def get_session(self, session_id: str, workspace_dir: str = "") -> Optional[ChatSession]:
        if session_id in self._sessions:
            return self._sessions[session_id]
        sess = self._load_session_from_disk(session_id, workspace_dir)
        if sess:
            self._sessions[sess.session_id] = sess
        return sess

    def delete_session(self, session_id: str, workspace_dir: str = "") -> bool:
        removed = self._sessions.pop(session_id, None) is not None
        safe_id = sanitize_identifier(session_id)
        if safe_id:
            ws = resolve_workspace(workspace_dir)
            sessions_dir = get_chat_sessions_dir(ws)
            file_path = sessions_dir / f"{safe_id}.json"
            if file_path.is_file():
                try:
                    file_path.unlink()
                    removed = True
                except Exception:
                    pass
            legacy_path = Path(ws) / ".botex" / "chat_sessions" / f"{safe_id}.json"
            if legacy_path.is_file():
                try:
                    legacy_path.unlink()
                    removed = True
                except Exception:
                    pass
        return removed

    def list_sessions(self, workspace_dir: str = "") -> List[Dict[str, Any]]:
        ws = resolve_workspace(workspace_dir) if workspace_dir else ""
        if ws:
            sessions_dir = get_chat_sessions_dir(ws)
            dirs_to_scan = [sessions_dir]
            legacy_dir = Path(ws) / ".botex" / "chat_sessions"
            if legacy_dir.is_dir() and legacy_dir.resolve() != sessions_dir.resolve():
                dirs_to_scan.append(legacy_dir)
            for sdir in dirs_to_scan:
                if sdir.is_dir():
                    try:
                        for entry in sdir.glob("*.json"):
                            sid = entry.stem
                            if sid not in self._sessions:
                                loaded = self._load_session_from_disk(sid, ws)
                                if loaded:
                                    self._sessions[loaded.session_id] = loaded
                    except Exception:
                        pass

        results = []
        for s in self._sessions.values():
            if ws and s.workspace_dir and os.path.abspath(s.workspace_dir) != os.path.abspath(ws):
                continue
            results.append(
                {
                    "session_id": s.session_id,
                    "title": s.get_title(),
                    "workspace_dir": s.workspace_dir,
                    "model": s.model,
                    "provider": s.provider,
                    "message_count": len(s.messages),
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
            )
        results.sort(key=lambda x: x["updated_at"], reverse=True)
        return results

    def save_chat_to_memory(
        self,
        session_id: str,
        workspace_dir: str = "",
        title: Optional[str] = None,
        kind: str = "context",
        scope: str = "local",
        content: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        sess = self.get_session(session_id, workspace_dir)
        if not sess or not sess.messages:
            return {"ok": False, "error": f"Session '{session_id}' not found or empty."}

        effective_title = (
            title.strip() if title and title.strip() else f"Chat Summary: {sess.get_title()}"
        )
        if content and content.strip():
            content_body = content.strip()
        else:
            lines = [
                f"# {effective_title}",
                f"- **Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                f"- **Session ID**: `{sess.session_id}`",
                f"- **Model**: `{sess.model}` ({sess.provider})",
                "",
                "## Conversation Transcript",
                "",
            ]
            for m in sess.messages:
                role_label = "Operator" if m.role == "user" else "BoteX"
                if m.content:
                    lines.append(f"### {role_label}")
                    lines.append(m.content)
                    lines.append("")
            content_body = "\n".join(lines)

        custom_tags = (
            tags
            if tags and len(tags) > 0
            else ["chat", "session", sess.model.split("/")[-1] if sess.model else "agent"]
        )
        res = save_memory(
            workspace_dir=sess.workspace_dir,
            title=effective_title,
            content=content_body,
            kind=kind,
            scope=scope,
            tags=custom_tags,
        )
        if isinstance(res, dict) and "id" in res and "memory_id" not in res:
            res["memory_id"] = res["id"]
        return res

    def clear(self) -> None:
        self._sessions.clear()


default_chat_manager = ChatManager()


def _normalize_chat_tool_path(raw_path: Any, default: str = "") -> str:
    """Strip virtual /workspace/ container prefixes commonly emitted by smaller models."""
    p = str(raw_path if raw_path is not None else default).strip()
    if not p:
        return default
    norm = p.replace("\\", "/")
    if norm in ("/workspace", "/workspace/"):
        return "."
    if norm.startswith("/workspace/"):
        return norm[len("/workspace/") :] or "."
    return p


async def execute_chat_tool(name: str, arguments: dict[str, Any], workspace_dir: str) -> str:
    """Execute a strictly read-only chat tool with path traversal protection and secret masking."""
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        if name == "get_file_outline":
            path_arg = _normalize_chat_tool_path(arguments.get("path", ""))
            res = get_file_outline(path_arg, workspace_dir)
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "read_file_lines":
            path_arg = _normalize_chat_tool_path(arguments.get("path", ""))
            start_raw = arguments.get("start", arguments.get("start_line", 1))
            end_raw = arguments.get("end", arguments.get("end_line", 1))
            try:
                start_int = int(start_raw)
            except Exception:
                start_int = 1
            try:
                end_int = int(end_raw)
            except Exception:
                end_int = 1
            res = read_file_lines(
                path_arg,
                start_int,
                end_int,
                workspace_dir,
            )
            if isinstance(res, dict):
                return mask_secrets(
                    str(res.get("content") or res.get("error") or json.dumps(res, indent=2))
                )
            return mask_secrets(str(res))
        elif name == "read_file":
            path_arg = _normalize_chat_tool_path(arguments.get("path", ""))
            res = read_file(path_arg, workspace_dir)
            if isinstance(res, dict):
                return mask_secrets(
                    str(res.get("content") or res.get("error") or json.dumps(res, indent=2))
                )
            return mask_secrets(str(res))
        elif name == "list_dir":
            path_arg = _normalize_chat_tool_path(arguments.get("path", "."), default=".")
            entries = list_dir(path_arg, workspace_dir)
            return mask_secrets(json.dumps(entries, indent=2))
        elif name == "search_memory":
            res = search_memory(
                workspace_dir=workspace_dir,
                query=str(arguments.get("query", "")),
                tag=arguments.get("tag") or None,
                kind=arguments.get("kind") or None,
                scope=str(arguments.get("scope", "local")),
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "read_memory":
            res = read_memory(
                workspace_dir=workspace_dir,
                memory_id=str(arguments.get("memory_id", "")),
                scope=str(arguments.get("scope", "local")),
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "read_scratchpad":
            from botex.context_guard import read_scratchpad as _read_pad

            res = _read_pad(
                memory_id=str(arguments.get("memory_id", "")),
                cwd=workspace_dir,
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "get_backlinks":
            from botex.memory import get_backlinks as _get_bl

            res = _get_bl(
                workspace_dir=workspace_dir,
                memory_id=str(arguments.get("memory_id", "")),
                scope=str(arguments.get("scope", "local")),
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "find_graph_health":
            from botex.memory import find_graph_health as _fgh

            res = _fgh(
                workspace_dir=workspace_dir,
                scope=str(arguments.get("scope", "local")),
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "save_memory":
            raw_tags = arguments.get("tags")
            tags_list = (
                [str(t) for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else None
            )
            res = save_memory(
                workspace_dir=workspace_dir,
                title=str(arguments.get("title", "Chat Note")),
                content=str(arguments.get("content", "")),
                kind=str(arguments.get("kind", "context")),
                tags=tags_list,
                scope=str(arguments.get("scope", "local")),
                origin="chat_tool",
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "patch_memory_section":
            res = patch_memory_section(
                workspace_dir=workspace_dir,
                memory_id=str(arguments.get("memory_id", "")),
                section=str(arguments.get("section", "Compiled Truth")),
                new_content=str(arguments.get("new_content", "")),
                scope=str(arguments.get("scope", "local")),
                append=bool(arguments.get("append", False)),
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "delete_memory":
            res = delete_memory(
                workspace_dir=workspace_dir,
                memory_id=str(arguments.get("memory_id", "")),
                scope=str(arguments.get("scope", "local")),
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "list_skills":
            res = list_skills()
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "install_skill":
            chat_cfg = load_config().get("chat", {})
            if not chat_cfg.get("allow_skill_admin", True):
                return "Error: Skill installation via Chat Mode is disabled by policy ('chat.allow_skill_admin' is false)."
            raw_tax = arguments.get("taxonomy")
            tax_list = (
                [str(t) for t in raw_tax if str(t).strip()] if isinstance(raw_tax, list) else None
            )
            src_url = str(arguments.get("source_url", "")).strip()
            orig_repo = str(arguments.get("origin_repo", "")).strip() or (src_url or "chat_admin")
            res = await install_skill_async(
                name=str(arguments.get("name", "")),
                content=str(arguments.get("content", "")),
                source_url=src_url,
                origin_repo=orig_repo,
                version=str(arguments.get("version", "")),
                taxonomy=tax_list,
            )
            return mask_secrets(json.dumps(res, indent=2))
        elif name == "fetch_url":
            url = str(arguments.get("url", "")).strip()
            net_cfg = load_config().get("net", {})
            res = await fetch_url(url, net_cfg=net_cfg)
            if isinstance(res, dict):
                return mask_secrets(
                    str(res.get("text") or res.get("error") or json.dumps(res, indent=2))
                )
            return mask_secrets(str(res))
        else:
            return mask_secrets(
                f"Error: Tool '{name}' is not allowed in Chat Mode. "
                "Chat Mode strictly allows read-only tools without mutation or command execution."
            )
    except SecurityError as se:
        return f"Security error: {mask_secrets(str(se))}"
    except Exception as exc:
        return f"Tool execution failed: {mask_secrets(str(exc))}"


async def stream_chat_turn(
    session: ChatSession,
    user_input: str,
    *,
    client: Optional[Any] = None,
    api_key: Optional[str] = None,
    allow_non_zdr: bool = False,
    temperature: float = 0.2,
    max_tokens: int = 4000,
    max_tool_iterations: int = 5,
    custom_tools: Optional[list[dict[str, Any]]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Execute a conversational chat turn with token streaming and recursive tool resolution.
    Strictly constrained to read-only operations.
    """
    # 1. Append user input to session history
    session.add_user_message(user_input)
    yield {
        "type": "turn_start",
        "session_id": session.session_id,
        "user_input": mask_secrets(user_input),
    }

    # 2. Resolve client, provider, and model if not explicitly injected
    active_tools = custom_tools if custom_tools is not None else READ_ONLY_CHAT_TOOLS
    active_provider_name = session.provider
    active_provider_cfg: dict[str, Any] = {}
    effective_model = session.model
    effective_allow_non_zdr = allow_non_zdr

    def _rollback_user_message() -> None:
        if (
            session.messages
            and session.messages[-1].role == "user"
            and session.messages[-1].content == user_input
        ):
            session.messages.pop()

    if client is None:
        requested_provider = session.provider
        if (
            (not requested_provider or requested_provider == "openrouter")
            and session.model
            and (":free" in session.model.lower() or "openrouter/free" in session.model.lower())
        ):
            requested_provider = "openrouter-free"
        active_provider_name, active_provider_cfg = resolve_provider(requested_provider)
        effective_model = session.model or resolve_model(
            "", profile=session.profile or "fast", provider=active_provider_name
        )
        if active_provider_name == "openrouter-free" or (
            effective_model
            and (":free" in effective_model.lower() or "openrouter/free" in effective_model.lower())
        ):
            effective_allow_non_zdr = True

        # ZDR preflight guard
        zdr_ok, zdr_reason = check_model_zdr(
            effective_model, active_provider_cfg, allow_non_zdr=effective_allow_non_zdr
        )
        if not zdr_ok:
            _rollback_user_message()
            yield {
                "type": "error",
                "session_id": session.session_id,
                "error": f"ZDR Violation: {zdr_reason}",
            }
            return

        # Price guardrail
        _pricing_cfg = load_config().get("pricing", {})
        price_ok, price_reason = check_model_price(
            effective_model, active_provider_cfg, _pricing_cfg, api_key=api_key or ""
        )
        if not price_ok:
            _rollback_user_message()
            yield {
                "type": "error",
                "session_id": session.session_id,
                "error": f"Price Exceeded: {price_reason}",
            }
            return

        try:
            client = build_async_client(
                active_provider_name, active_provider_cfg, api_key=api_key or None
            )
        except Exception as exc:
            _rollback_user_message()
            sanitized = sanitize_provider_error(exc)
            yield {
                "type": "error",
                "session_id": session.session_id,
                "error": f"Client Init Error: {sanitized}",
            }
            return

    provider_adapter = ProviderRegistry.get(active_provider_name, active_provider_cfg)
    extra_body = provider_adapter.get_extra_body(allow_non_zdr=effective_allow_non_zdr)

    # 3. Conversational execution loop (model call -> tool loop -> answer)
    iteration = 0
    final_content = ""
    tools_supported = True

    while iteration < max_tool_iterations:
        iteration += 1

        # Build message history for the provider request
        request_messages: list[dict[str, Any]] = [
            {"role": "system", "content": session.system_prompt}
        ]
        request_messages.extend(session.get_history())

        accumulated_text = ""
        accumulated_reasoning = ""
        tool_calls_map: dict[int, dict[str, Any]] = {}
        # Message-level provider extras (e.g. Gemini thought_signature) —
        # echoed verbatim on the next request.
        assistant_extra: Optional[Dict[str, Any]] = None

        # Force final synthesis without further tool calls on the last iteration
        use_tools_this_turn = (
            bool(active_tools)
            and tools_supported
            and (iteration < max_tool_iterations or max_tool_iterations <= 1)
        )

        try:
            create_kwargs: dict[str, Any] = {
                "model": effective_model,
                "messages": request_messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "extra_body": extra_body,
                "stream": True,
            }
            if use_tools_this_turn:
                create_kwargs["tools"] = active_tools
                create_kwargs["tool_choice"] = "auto"
            # Retry covers only request creation — once the first chunk
            # has streamed the turn is committed and must not retry.
            response = await call_with_backoff(
                provider_adapter,
                lambda: client.chat.completions.create(**create_kwargs),
                active_provider_cfg,
            )
        except Exception as exc:
            err_lower = str(exc).lower()
            if use_tools_this_turn and ("tool" in err_lower or "function" in err_lower):
                tools_supported = False
                try:
                    response = await call_with_backoff(
                        provider_adapter,
                        lambda: client.chat.completions.create(
                            model=effective_model,
                            messages=request_messages,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            extra_body=extra_body,
                            stream=True,
                        ),
                        active_provider_cfg,
                    )
                except Exception as retry_exc:
                    if iteration == 1:
                        _rollback_user_message()
                    sanitized = sanitize_provider_error(retry_exc)
                    yield {
                        "type": "error",
                        "session_id": session.session_id,
                        "error": f"API Error: {sanitized}",
                    }
                    return
            else:
                if iteration == 1:
                    _rollback_user_message()
                sanitized = sanitize_provider_error(exc)
                yield {
                    "type": "error",
                    "session_id": session.session_id,
                    "error": f"API Error: {sanitized}",
                }
                return

        # 4. Stream response chunks
        try:
            if hasattr(response, "__aiter__"):
                async for chunk in response:
                    choices = getattr(chunk, "choices", None) or (
                        chunk.get("choices") if isinstance(chunk, dict) else []
                    )
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = getattr(choice, "delta", None) or (
                        choice.get("delta") if isinstance(choice, dict) else None
                    )
                    if not delta:
                        continue

                    # Reasoning delta (NVIDIA NIM thinking, DeepSeek-R1, QwQ, etc.)
                    reasoning_delta = (
                        getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None)
                        or (
                            delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(delta, dict)
                            else None
                        )
                    )
                    if reasoning_delta:
                        accumulated_reasoning += str(reasoning_delta)
                        yield {
                            "type": "reasoning_chunk",
                            "delta": str(reasoning_delta),
                            "accumulated": accumulated_reasoning,
                        }

                    # Text delta
                    text_delta = getattr(delta, "content", None) or (
                        delta.get("content") if isinstance(delta, dict) else None
                    )
                    if text_delta:
                        accumulated_text += text_delta
                        yield {
                            "type": "content_chunk",
                            "delta": text_delta,
                            "accumulated": accumulated_text,
                        }

                    # Tool calls delta
                    tc_deltas = getattr(delta, "tool_calls", None) or (
                        delta.get("tool_calls") if isinstance(delta, dict) else None
                    )
                    delta_extra = getattr(delta, "extra_content", None) or (
                        delta.get("extra_content") if isinstance(delta, dict) else None
                    )
                    if isinstance(delta_extra, dict):
                        assistant_extra = delta_extra

                    if tc_deltas:
                        for tc in tc_deltas:
                            raw_idx = (
                                getattr(tc, "index", 0)
                                if not isinstance(tc, dict)
                                else tc.get("index", 0)
                            )
                            idx = 0 if raw_idx is None else int(raw_idx)
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                    "extra_content": None,
                                }
                            tc_extra = getattr(tc, "extra_content", None) or (
                                tc.get("extra_content") if isinstance(tc, dict) else None
                            )
                            if isinstance(tc_extra, dict):
                                tool_calls_map[idx]["extra_content"] = tc_extra
                            tc_id = getattr(tc, "id", None) or (
                                tc.get("id") if isinstance(tc, dict) else None
                            )
                            if tc_id:
                                tool_calls_map[idx]["id"] += tc_id
                            fn = getattr(tc, "function", None) or (
                                tc.get("function") if isinstance(tc, dict) else None
                            )
                            if fn:
                                fn_name = getattr(fn, "name", None) or (
                                    fn.get("name") if isinstance(fn, dict) else None
                                )
                                if fn_name:
                                    tool_calls_map[idx]["name"] += fn_name
                                fn_args = getattr(fn, "arguments", None) or (
                                    fn.get("arguments") if isinstance(fn, dict) else None
                                )
                                if fn_args:
                                    tool_calls_map[idx]["arguments"] += fn_args
            else:
                # Non-streaming fallback (e.g. sync mock or completion object)
                choices = getattr(response, "choices", []) or (
                    response.get("choices") if isinstance(response, dict) else []
                )
                if choices:
                    c = choices[0]
                    msg = getattr(c, "message", None) or (
                        c.get("message") if isinstance(c, dict) else None
                    )
                    if msg:
                        msg_extra = getattr(msg, "extra_content", None) or (
                            msg.get("extra_content") if isinstance(msg, dict) else None
                        )
                        if isinstance(msg_extra, dict):
                            assistant_extra = msg_extra
                        text_val = getattr(msg, "content", None) or (
                            msg.get("content") if isinstance(msg, dict) else None
                        )
                        if text_val:
                            accumulated_text = text_val
                            yield {
                                "type": "content_chunk",
                                "delta": text_val,
                                "accumulated": text_val,
                            }
                        tc_list = getattr(msg, "tool_calls", None) or (
                            msg.get("tool_calls") if isinstance(msg, dict) else None
                        )
                        if tc_list:
                            for idx, tc in enumerate(tc_list):
                                tc_id = (
                                    getattr(tc, "id", None)
                                    or (tc.get("id") if isinstance(tc, dict) else None)
                                    or f"call_{uuid.uuid4().hex[:8]}"
                                )
                                fn = getattr(tc, "function", None) or (
                                    tc.get("function") if isinstance(tc, dict) else None
                                )
                                fn_name = (
                                    getattr(fn, "name", "")
                                    if hasattr(fn, "name")
                                    else (fn.get("name", "") if isinstance(fn, dict) else "")
                                )
                                fn_args = (
                                    getattr(fn, "arguments", "")
                                    if hasattr(fn, "arguments")
                                    else (fn.get("arguments", "") if isinstance(fn, dict) else "")
                                )
                                if fn_args and not isinstance(fn_args, str):
                                    try:
                                        fn_args = json.dumps(fn_args, ensure_ascii=False)
                                    except (TypeError, ValueError):
                                        fn_args = str(fn_args)
                                tc_extra = getattr(tc, "extra_content", None) or (
                                    tc.get("extra_content") if isinstance(tc, dict) else None
                                )
                                tool_calls_map[idx] = {
                                    "id": tc_id,
                                    "name": fn_name,
                                    "arguments": fn_args,
                                    "extra_content": tc_extra
                                    if isinstance(tc_extra, dict)
                                    else None,
                                }
        except Exception as exc:
            if iteration == 1:
                _rollback_user_message()
            sanitized = sanitize_provider_error(exc)
            yield {
                "type": "error",
                "session_id": session.session_id,
                "error": f"Streaming Error: {sanitized}",
            }
            return

        # 5. Process tool calls or finalize
        if tool_calls_map:
            formatted_tool_calls: list[dict[str, Any]] = []
            for idx in sorted(tool_calls_map.keys()):
                entry = tool_calls_map[idx]
                call_id = entry["id"] or f"call_{uuid.uuid4().hex[:8]}"
                tc_dict: dict[str, Any] = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": entry["name"],
                        "arguments": entry["arguments"],
                    },
                }
                if isinstance(entry.get("extra_content"), dict):
                    tc_dict["extra_content"] = entry["extra_content"]
                formatted_tool_calls.append(tc_dict)

            session.add_assistant_message(
                content=accumulated_text or None,
                tool_calls=formatted_tool_calls,
                extra_content=assistant_extra,
            )

            for tc in formatted_tool_calls:
                fn_name = tc["function"]["name"]
                args_raw = tc["function"]["arguments"]
                call_id = tc["id"]
                try:
                    args_parsed = json.loads(args_raw) if args_raw else {}
                    if not isinstance(args_parsed, dict):
                        args_parsed = {}
                except Exception:
                    args_parsed = {}

                yield {
                    "type": "tool_call_start",
                    "name": fn_name,
                    "arguments": args_parsed,
                    "tool_call_id": call_id,
                }

                tool_result_str = await execute_chat_tool(
                    fn_name, args_parsed, session.workspace_dir
                )

                yield {
                    "type": "tool_result",
                    "name": fn_name,
                    "tool_call_id": call_id,
                    "result": tool_result_str,
                }

                session.add_tool_result(
                    tool_call_id=call_id,
                    name=fn_name,
                    content=tool_result_str,
                )
        else:
            # No tool calls; final assistant message completed
            final_content = accumulated_text
            session.add_assistant_message(content=final_content)
            yield {
                "type": "done",
                "session_id": session.session_id,
                "content": final_content,
                "reasoning": accumulated_reasoning or None,
                "iterations": iteration,
            }
            return

    # Loop exceeded max tool iterations
    final_content = accumulated_text or "Reached maximum tool execution turns."
    session.add_assistant_message(content=final_content)
    yield {
        "type": "done",
        "session_id": session.session_id,
        "content": final_content,
        "iterations": iteration,
    }


async def chat_turn(
    session: ChatSession,
    user_input: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Convenience non-streaming execution wrapper over stream_chat_turn."""
    final_content = ""
    async for event in stream_chat_turn(session, user_input, **kwargs):
        if event.get("type") == "content_chunk":
            final_content = event.get("accumulated", "")
        elif event.get("type") == "done":
            return {
                "ok": True,
                "session_id": session.session_id,
                "content": event.get("content", final_content),
                "messages": session.get_history(),
            }
        elif event.get("type") == "error":
            return {
                "ok": False,
                "session_id": session.session_id,
                "error": event.get("error", "Unknown chat error"),
                "messages": session.get_history(),
            }
    return {
        "ok": True,
        "session_id": session.session_id,
        "content": final_content,
        "messages": session.get_history(),
    }
