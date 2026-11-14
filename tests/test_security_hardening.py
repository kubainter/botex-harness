# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Regression tests for the pre-release security hardening (bind host, event masking,
child env sanitization, node inline-eval policy)."""

import pytest

import server as botex_server
from botex import exec_tools
from botex.events import TaskEventEmitter, _mask_data
from botex.tools.security import mask_secrets

SECRET = "sk-or-v1-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4"


def test_mask_secrets_quoted_key_value_pairs():
    # JSON object and dict-repr forms: quote between key name and separator
    masked = mask_secrets('{"api_key": "plainsecretvalue"}')
    assert "plainsecretvalue" not in masked
    assert "[REDACTED]" in masked

    masked = mask_secrets("{'token': 'abc1234567'}")
    assert "abc1234567" not in masked
    assert "[REDACTED]" in masked

    # YAML-ish single-quoted value
    masked = mask_secrets("api_key: 'val123456'")
    assert "val123456" not in masked

    # Compact JSON: the following pair must not be swallowed
    masked = mask_secrets('{"token":"abc123","next":1}')
    assert "abc123" not in masked
    assert '"next":1' in masked

    # Existing bare forms keep masking (regression)
    assert "[REDACTED]" in mask_secrets("token: ghp_xxxx")
    assert "[REDACTED]" in mask_secrets("api_key=val12345678")
    assert "[REDACTED]" in mask_secrets('token = "abc123"')

    # Identifier words ending in a key name are not secrets
    for text in ("token_ring", "tokenizer", "mytokenize"):
        assert mask_secrets(text) == text

    # Min-length guard still applies inside quotes
    assert mask_secrets('"token": "short"') == '"token": "short"'


def test_cloud_cli_refuses_public_bind_without_token(monkeypatch):
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        botex_server._cli_cloud(["--host", "0.0.0.0"])
    assert exc.value.code == 2


def test_cloud_cli_default_host_is_loopback(monkeypatch):
    import uvicorn

    captured = {}
    monkeypatch.delenv("BOTEX_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    botex_server._cli_cloud([])
    assert captured["host"] == "127.0.0.1"


def test_cloud_cli_allows_public_bind_with_token(monkeypatch):
    import uvicorn

    captured = {}
    monkeypatch.setenv("BOTEX_ACCESS_TOKEN", "tok-123")
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    botex_server._cli_cloud(["--host", "0.0.0.0"])
    assert captured["host"] == "0.0.0.0"


def test_event_masking_is_recursive_and_bounded():
    data = {
        "tool": {"args": {"auth": f"Bearer {SECRET}"}, "list": [f"KEY={SECRET}"]},
        "plain": "ok",
    }
    masked = _mask_data(data)
    assert SECRET not in repr(masked)
    assert masked["plain"] == "ok"

    cyc: dict = {"a": 1}
    cyc["self"] = cyc
    assert "[CIRCULAR]" in repr(_mask_data({"x": cyc}))

    deep: dict = {}
    cur = deep
    for _ in range(20):
        cur["n"] = {}
        cur = cur["n"]
    assert "[TRUNCATED_NESTING]" in repr(_mask_data({"x": deep}))


def test_emitter_masks_nested_payload():
    em = TaskEventEmitter("t-sec")
    em.emit("tool_call", payload={"headers": {"x": f"Bearer {SECRET}"}})
    assert SECRET not in repr(em._buf)


@pytest.mark.parametrize(
    "name,secret",
    [
        ("OPENAI_KEY", True),
        ("ANTHROPIC_KEY", True),
        ("AWS_ACCESS_KEY_ID", True),
        ("PRIVATE_KEY", True),
        ("KEY_FILE", True),
        ("OPENROUTER_API_KEY", True),
        ("GITHUB_TOKEN", True),
        ("KEYBOARD", False),
        ("MONKEYPATCH_DIR", False),
        ("PATH", False),
        ("HOME", False),
    ],
)
def test_secret_env_regex(name, secret):
    assert bool(exec_tools._SECRET_ENV_RE.search(name)) is secret


@pytest.mark.parametrize(
    "cmd",
    [
        "node -e 1",
        "node --eval 1",
        "node -p 1",
        "node --print 1",
        "node -pe 1",
        "node --eval=1",
        "node --no-warnings -e 1",
    ],
)
def test_node_inline_eval_denied(cmd):
    err = exec_tools.command_policy_error(cmd, allowlist=["node"], deny_args=[])
    assert "denied" in err


def test_node_script_with_script_args_allowed():
    err = exec_tools.command_policy_error(
        "node build.js -e production", allowlist=["node"], deny_args=[]
    )
    assert err == ""


def test_wikilink_redos_resilience():
    import time

    from botex.memory import extract_wikilinks

    payload = "`" * 100_000 + "a\n[[valid_link]]"
    t0 = time.monotonic()
    links = extract_wikilinks(payload)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.25
    assert links == ["valid_link"]
