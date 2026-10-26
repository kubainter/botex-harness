# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit tests for BoteX Chat Mode (botex/chat.py)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from botex.chat import (
    DEFAULT_CHAT_SYSTEM_PROMPT,
    READ_ONLY_CHAT_TOOLS,
    ChatManager,
    ChatMessage,
    ChatSession,
    chat_turn,
    execute_chat_tool,
    stream_chat_turn,
)
from botex.config import get_chat_sessions_dir


class MockChoiceDelta:
    def __init__(self, content: str = "", tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls


class MockChoice:
    def __init__(self, delta: MockChoiceDelta):
        self.delta = delta


class MockChunk:
    def __init__(self, content: str = "", tool_calls: list | None = None):
        self.choices = [MockChoice(MockChoiceDelta(content, tool_calls))]


class MockToolCallFunction:
    def __init__(self, name: str = "", arguments: str = ""):
        self.name = name
        self.arguments = arguments


class MockDeltaToolCall:
    def __init__(self, index: int, id: str = "", name: str = "", arguments: str = ""):
        self.index = index
        self.id = id
        self.function = MockToolCallFunction(name=name, arguments=arguments)


class AsyncMockStream:
    def __init__(self, chunks: list[MockChunk]):
        self.chunks = chunks

    def __aiter__(self):
        self._iter = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class MockChatCompletions:
    def __init__(self, responses: list[list[MockChunk]]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            return AsyncMockStream([MockChunk(content="No further mock response.")])
        next_chunks = self._responses.pop(0)
        return AsyncMockStream(next_chunks)


class MockOpenAIClient:
    def __init__(self, responses: list[list[MockChunk]]):
        self.chat = type("Chat", (), {"completions": MockChatCompletions(responses)})()


def test_chat_message_creation_and_masking():
    """Verify ChatMessage normalizes role and masks secrets."""
    msg = ChatMessage(role="USER", content="My secret is sk-abcdef1234567890qwerty")
    assert msg.role == "user"
    assert "sk-" not in msg.content
    assert "[REDACTED]" in msg.content

    d = msg.to_dict()
    assert d["role"] == "user"
    assert "[REDACTED]" in d["content"]


def test_chat_session_history_and_turns(tmp_path: Path):
    """Verify ChatSession accumulates and formats multi-turn history."""
    session = ChatSession(workspace_dir=str(tmp_path), model="test-model")
    assert session.workspace_dir == str(tmp_path.resolve())
    assert session.system_prompt == DEFAULT_CHAT_SYSTEM_PROMPT

    session.add_user_message("Hello BoteX")
    session.add_assistant_message("Hello! How can I assist you with your code?")

    history = session.get_history()
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Hello BoteX"
    assert history[1]["role"] == "assistant"

    # History turn limit
    recent = session.get_history(max_turns=1)
    assert len(recent) == 1
    assert recent[0]["role"] == "assistant"

    session.clear()
    assert len(session.messages) == 0


def test_chat_manager_lifecycle(tmp_path: Path):
    """Verify ChatManager session creation, caching, and cleanup."""
    mgr = ChatManager()
    s1 = mgr.get_or_create_session(session_id="sess_1", workspace_dir=str(tmp_path))
    assert s1.session_id == "sess_1"

    # Reuse session
    s1_again = mgr.get_or_create_session(session_id="sess_1")
    assert s1_again is s1

    # List sessions
    sessions = mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess_1"

    # Delete session
    assert mgr.delete_session("sess_1") is True
    assert mgr.get_session("sess_1") is None
    assert mgr.delete_session("sess_1") is False


def test_execute_chat_tool_strictly_read_only(tmp_path: Path):
    """Verify Chat Mode only allows read-only tools and rejects mutations/commands."""
    test_file = tmp_path / "hello.py"
    test_file.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")

    async def _test():
        # Allowed read tools
        res_outline = await execute_chat_tool(
            "get_file_outline", {"path": "hello.py"}, str(tmp_path)
        )
        assert "greet" in res_outline

        res_read = await execute_chat_tool("read_file", {"path": "hello.py"}, str(tmp_path))
        assert "def greet():" in res_read

        res_lines = await execute_chat_tool(
            "read_file_lines", {"path": "hello.py", "start": 1, "end": 2}, str(tmp_path)
        )
        assert "def greet():" in res_lines

        res_list = await execute_chat_tool("list_dir", {"path": "."}, str(tmp_path))
        assert "hello.py" in res_list

        # Prohibited mutating tools
        res_patch = await execute_chat_tool(
            "apply_patch",
            {"path": "hello.py", "search_block": "greet", "replace_block": "foo"},
            str(tmp_path),
        )
        assert "not allowed in Chat Mode" in res_patch

        res_create = await execute_chat_tool(
            "create_file", {"path": "evil.py", "content": "pass"}, str(tmp_path)
        )
        assert "not allowed in Chat Mode" in res_create

        # Prohibited execution tool
        res_exec = await execute_chat_tool("run_command", {"command": "echo test"}, str(tmp_path))
        assert "not allowed in Chat Mode" in res_exec

    asyncio.run(_test())


def test_stream_chat_turn_pure_conversation(tmp_path: Path):
    """Verify streaming text chunks in conversational chat turn."""
    chunks = [
        MockChunk(content="Hello "),
        MockChunk(content="there! "),
        MockChunk(content="I can help explore the codebase."),
    ]
    client = MockOpenAIClient([chunks])
    session = ChatSession(workspace_dir=str(tmp_path), model="test-model")

    async def _test():
        events = []
        async for event in stream_chat_turn(
            session=session,
            user_input="Hi there!",
            client=client,
        ):
            events.append(event)

        types = [e["type"] for e in events]
        assert types[0] == "turn_start"
        assert "content_chunk" in types
        assert types[-1] == "done"

        final_content = events[-1]["content"]
        assert final_content == "Hello there! I can help explore the codebase."

        # Verify session history updated
        history = session.get_history()
        assert len(history) == 2
        assert history[0]["content"] == "Hi there!"
        assert history[1]["content"] == final_content

    asyncio.run(_test())


def test_stream_chat_turn_with_read_tool_call(tmp_path: Path):
    """Verify model can call read-only tools and synthesize an answer."""
    app_file = tmp_path / "app.py"
    app_file.write_text("API_VERSION = 'v2.1.0'\n", encoding="utf-8")

    # Turn 1 response: model invokes read_file("app.py")
    tc_delta = MockDeltaToolCall(
        index=0,
        id="call_read_1",
        name="read_file",
        arguments=json.dumps({"path": "app.py"}),
    )
    turn1_chunks = [MockChunk(tool_calls=[tc_delta])]

    # Turn 2 response: model synthesizes final answer after inspecting file
    turn2_chunks = [
        MockChunk(content="The current API version in app.py is v2.1.0."),
    ]

    client = MockOpenAIClient([turn1_chunks, turn2_chunks])
    session = ChatSession(workspace_dir=str(tmp_path), model="test-model")

    async def _test():
        events = []
        async for event in stream_chat_turn(
            session=session,
            user_input="What is the API version in app.py?",
            client=client,
        ):
            events.append(event)

        ev_types = [e["type"] for e in events]
        assert "turn_start" in ev_types
        assert "tool_call_start" in ev_types
        assert "tool_result" in ev_types
        assert "content_chunk" in ev_types
        assert ev_types[-1] == "done"

        done_event = events[-1]
        assert "v2.1.0" in done_event["content"]

        # History contains user, assistant (tool_calls), tool, assistant (final)
        history = session.get_history()
        assert len(history) == 4
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert history[1]["tool_calls"] is not None
        assert history[2]["role"] == "tool"
        assert "API_VERSION" in history[2]["content"]
        assert history[3]["role"] == "assistant"
        assert "v2.1.0" in history[3]["content"]

    asyncio.run(_test())


def test_chat_turn_convenience_wrapper(tmp_path: Path):
    """Verify non-streaming chat_turn wrapper returns structured result."""
    chunks = [MockChunk(content="Structured non-streaming response.")]
    client = MockOpenAIClient([chunks])
    session = ChatSession(workspace_dir=str(tmp_path), model="test-model")

    async def _test():
        result = await chat_turn(session, "Brief check", client=client)
        assert result["ok"] is True
        assert result["content"] == "Structured non-streaming response."
        assert len(result["messages"]) == 2

    asyncio.run(_test())


def test_chat_turn_error_handling(tmp_path: Path):
    """Verify chat turn yields error event on client failure without crashing."""

    class FailingCompletions:
        async def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("Provider connection timeout")

    failing_client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": FailingCompletions()})()}
    )()
    session = ChatSession(workspace_dir=str(tmp_path), model="test-model")

    async def _test():
        events = []
        async for event in stream_chat_turn(session, "Will fail", client=failing_client):
            events.append(event)

        assert any(e.get("type") == "error" for e in events)

    asyncio.run(_test())


def test_cli_chat_single_turn_and_help(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Verify botex chat CLI handler."""
    import server

    # Test single turn execution
    with patch("server.stream_chat_turn") as mock_stream:

        async def _fake_stream(*args: Any, **kwargs: Any):
            yield {"type": "content_chunk", "delta": "Hello from mocked chat!"}

        mock_stream.side_effect = _fake_stream
        server._cli_chat(["What is this?", "-w", str(tmp_path)])

    captured = capsys.readouterr()
    assert "Hello from mocked chat!" in captured.out


def test_chat_history_slicing_turn_boundary(tmp_path):
    """Verify get_history strips leading orphan tool messages when slicing."""
    sess = ChatSession(workspace_dir=str(tmp_path))
    sess.add_user_message("u1")
    sess.add_assistant_message("a1")
    sess.add_user_message("u2")
    sess.add_assistant_message(
        None,
        tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "read_file"}}],
    )
    sess.add_tool_result("call_1", "read_file", "file contents")
    sess.add_assistant_message("a2")
    sess.add_user_message("u3")
    sess.add_assistant_message("a3")

    # Slicing with max_turns=4 lands on tool result: [tool, a2, u3, a3]
    # Slicing must drop leading orphan tool message so API receives valid message structure
    h = sess.get_history(max_turns=4)
    roles = [m["role"] for m in h]
    assert roles[0] != "tool"
    assert roles == ["assistant", "user", "assistant"]


def test_chat_model_resolution_and_client_init(tmp_path: Path):
    """Verify stream_chat_turn correctly resolves model and client when client is None."""
    session = ChatSession(workspace_dir=str(tmp_path), profile="fast")

    mock_client = MockOpenAIClient([[MockChunk(content="Resolved successfully.")]])

    with (
        patch("botex.chat.build_async_client", return_value=mock_client) as mock_builder,
        patch("botex.chat.check_model_zdr", return_value=(True, "")),
        patch("botex.chat.check_model_price", return_value=(True, "")),
    ):

        async def _test():
            events = []
            async for event in stream_chat_turn(session, "Check model resolution"):
                events.append(event)
            assert any(e.get("type") == "done" for e in events)

        asyncio.run(_test())
        assert mock_builder.called
        # Verify api_key passed to build_async_client is None when empty
        _args, kwargs = mock_builder.call_args
        assert kwargs.get("api_key") is None


def test_execute_chat_tool_secret_masking(tmp_path: Path):
    """Verify execute_chat_tool masks secrets across outline, read, and memory tools."""
    secret_key = "sk-ant-api03-abcdef1234567890qwertyuiop"
    target_file = tmp_path / "secret_script.py"
    target_file.write_text(f"KEY = '{secret_key}'\ndef run():\n    pass\n", encoding="utf-8")

    async def _test():
        # 1. read_file
        res_read = await execute_chat_tool("read_file", {"path": "secret_script.py"}, str(tmp_path))
        assert secret_key not in res_read
        assert "[REDACTED]" in res_read

        # 2. read_file_lines
        res_lines = await execute_chat_tool(
            "read_file_lines", {"path": "secret_script.py", "start": 1, "end": 2}, str(tmp_path)
        )
        assert secret_key not in res_lines
        assert "[REDACTED]" in res_lines

        # 3. get_file_outline
        res_outline = await execute_chat_tool(
            "get_file_outline", {"path": "secret_script.py"}, str(tmp_path)
        )
        assert secret_key not in res_outline

    asyncio.run(_test())


def test_stream_chat_turn_error_rollback_user_message(tmp_path: Path):
    """Verify that an early failure rolls back the newly added user message."""
    session = ChatSession(workspace_dir=str(tmp_path), model="test-model")
    assert len(session.messages) == 0

    class FailingCompletions:
        async def create(self, **kwargs: Any) -> Any:
            raise RuntimeError("Immediate failure")

    failing_client = type(
        "Client", (), {"chat": type("Chat", (), {"completions": FailingCompletions()})()}
    )()

    async def _test():
        events = []
        async for event in stream_chat_turn(
            session, "Should be rolled back", client=failing_client
        ):
            events.append(event)
        assert any(e.get("type") == "error" for e in events)

    asyncio.run(_test())
    # The message history should be clean, not containing the unhandled prompt
    assert len(session.messages) == 0


def test_execute_chat_tool_fetch_url():
    """Verify execute_chat_tool handles fetch_url calls safely."""

    async def _test():
        with patch("botex.chat.fetch_url") as mock_fetch:
            mock_fetch.return_value = {"ok": True, "text": '{"rate": 4.28}', "status": 200}
            res = await execute_chat_tool("fetch_url", {"url": "https://api.nbp.pl/test"}, ".")
            assert "4.28" in res

    asyncio.run(_test())


def test_execute_chat_tool_workspace_prefix_normalization(tmp_path: Path):
    """Verify execute_chat_tool strips virtual /workspace/ prefixes emitted by smaller models."""
    sub = tmp_path / "botex"
    sub.mkdir()
    sample = sub / "sample.py"
    sample.write_text("VALUE = 42\n", encoding="utf-8")

    async def _test():
        res = await execute_chat_tool(
            "read_file", {"path": "/workspace/botex/sample.py"}, str(tmp_path)
        )
        assert "VALUE = 42" in res

    asyncio.run(_test())


def test_chat_session_persistence_and_title(tmp_path: Path):
    """Verify ChatSession saves to disk, derives clean titles, and reloads properly."""
    manager = ChatManager()
    session = manager.get_or_create_session(
        session_id="test-session-123",
        workspace_dir=str(tmp_path),
        model="liquid/lfm-2.5-2.6b:free",
        provider="openrouter-free",
    )
    session.messages.append(
        ChatMessage(role="user", content="How do we configure WebSockets in FastAPI?")
    )
    session.messages.append(
        ChatMessage(role="assistant", content="You can use @app.websocket endpoint.")
    )
    session.save_to_disk()

    disk_file = get_chat_sessions_dir(str(tmp_path)) / "test-session-123.json"
    assert disk_file.exists()
    assert session.get_title().startswith("How do we configure WebSockets")

    # Clear manager and verify it loads from disk
    new_manager = ChatManager()
    sessions = new_manager.list_sessions(str(tmp_path))
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "test-session-123"
    assert sessions[0]["message_count"] == 2

    loaded = new_manager.get_session("test-session-123", str(tmp_path))
    assert loaded is not None
    assert len(loaded.messages) == 2
    assert loaded.messages[0].content == "How do we configure WebSockets in FastAPI?"


def test_chat_manager_save_chat_to_memory(tmp_path: Path):
    """Verify saving chat session transcript into Workspace Memory Vault (.botex/memory/)."""
    manager = ChatManager()
    session = manager.get_or_create_session(
        session_id="mem-session-999",
        workspace_dir=str(tmp_path),
        model="deepseek/deepseek-r1",
        provider="openrouter",
    )
    session.messages.append(
        ChatMessage(role="user", content="Plan the migration to Tailwind CSS variables.")
    )
    session.messages.append(
        ChatMessage(role="assistant", content="Step 1: Replace hardcoded hex values with CSS vars.")
    )

    # Test saving default transcript
    res = manager.save_chat_to_memory(
        session_id="mem-session-999",
        workspace_dir=str(tmp_path),
        title="Tailwind Migration Plan",
        kind="architecture",
    )
    assert res.get("ok") is True
    assert "memory_id" in res

    memory_dir = tmp_path / ".botex" / "memory"
    assert memory_dir.exists()
    assert any(f.name.endswith(".md") for f in memory_dir.iterdir())

    # Test saving with custom content
    res2 = manager.save_chat_to_memory(
        session_id="mem-session-999",
        workspace_dir=str(tmp_path),
        title="Custom Note",
        content="Explicit summary notes",
        kind="decision",
    )
    assert res2.get("ok") is True


def test_chat_manager_delete_session(tmp_path: Path):
    """Verify deleting a chat session removes it from both in-memory cache and disk."""
    manager = ChatManager()
    session = manager.get_or_create_session(
        session_id="delete-me-session",
        workspace_dir=str(tmp_path),
        model="liquid/lfm-2.5-2.6b:free",
        provider="openrouter-free",
    )
    session.messages.append(ChatMessage(role="user", content="Temporary question"))
    session.save_to_disk()

    disk_file = get_chat_sessions_dir(str(tmp_path)) / "delete-me-session.json"
    assert disk_file.exists()

    del_res = manager.delete_session("delete-me-session", str(tmp_path))
    assert del_res is True
    assert not disk_file.exists()
    assert manager.get_session("delete-me-session", str(tmp_path)) is None


def test_tool_call_id_persistence_and_history_sanitization(tmp_path: Path):
    """Verify tool_call_id is preserved on disk and orphan tool messages are sanitized."""
    manager = ChatManager()
    session = manager.get_or_create_session(
        session_id="tool-persistence-test",
        workspace_dir=str(tmp_path),
        model="liquid/lfm-2.5-2.6b:free",
        provider="openrouter-free",
    )
    session.add_user_message("Read the config")
    session.add_assistant_message(
        content=None,
        tool_calls=[
            {
                "id": "call_abc_123",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }
        ],
    )
    session.add_tool_result(tool_call_id="call_abc_123", name="read_file", content="file content")
    session.save_to_disk()

    # Verify JSON file has tool_call_id
    disk_file = get_chat_sessions_dir(str(tmp_path)) / "tool-persistence-test.json"
    assert disk_file.exists()
    data = json.loads(disk_file.read_text(encoding="utf-8"))
    tool_msg = next(m for m in data["messages"] if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_abc_123"

    # Reload from disk in a fresh manager
    fresh_mgr = ChatManager()
    loaded_sess = fresh_mgr.get_session("tool-persistence-test", str(tmp_path))
    assert loaded_sess is not None

    hist = loaded_sess.get_history()
    assert len(hist) == 3
    assert hist[2]["role"] == "tool"
    assert hist[2]["tool_call_id"] == "call_abc_123"

    # Verify orphan tool messages are dropped in get_history to prevent provider 400s
    orphan_msg = ChatMessage(role="tool", content="orphan result", tool_call_id="")
    loaded_sess.messages.append(orphan_msg)
    cleaned_hist = loaded_sess.get_history()
    # The orphan tool message without preceding assistant tool_calls should be dropped
    assert len(cleaned_hist) == 3
