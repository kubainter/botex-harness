# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Unit & Integration Test Suite
====================================
Tests security guard, fuzzy matcher, pre-write syntax validation,
snapshot rollback, outline generator, and file tools.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from botex.security import resolve_safe_path, SecurityError, mask_secrets
from botex.patch_engine import (
    apply_fuzzy_patch, apply_patch, validate_syntax, snapshot_manager,
)
from botex.file_tools import (
    get_file_outline, read_file_lines, create_file, list_dir,
    delete_file, move_file,
)
from botex.capabilities import (
    MODES, resolve_capabilities, tool_allowed,
    CAP_READ, CAP_WRITE, CAP_DESTRUCTIVE, CAP_EXEC, CAP_NET,
)
from botex.exec_tools import run_command, _sanitized_env
from botex.net_tools import _validate_url, resolve_net_scope
import botex.net_tools as net_tools
import botex.pricing as pricing


def test_security():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # 1. Normal safe path
        safe = resolve_safe_path(root, "src/main.py")
        assert str(safe).startswith(str(root.resolve())), "Safe path should stay in root"

        # 2. Path traversal should raise SecurityError
        traversal_caught = False
        try:
            resolve_safe_path(root, "../../etc/passwd")
        except SecurityError:
            traversal_caught = True
        assert traversal_caught, "Path traversal must be blocked"

        # 3. Blocked file (.env) should raise SecurityError
        env_caught = False
        try:
            resolve_safe_path(root, ".env")
        except SecurityError:
            env_caught = True
        assert env_caught, ".env must be blocked"

        # 4. Secret masking
        raw = "My api key is sk-or-v1-abcdef1234567890abcdef1234567890 and password is secret123"
        masked = mask_secrets(raw)
        assert "sk-or-v1-" not in masked, "API key must be redacted"
        assert "[REDACTED]" in masked, "Masked text must contain [REDACTED]"

        # 5. PEM private key blocks are masked whole, markers included
        for kind in ("RSA ", "EC ", "OPENSSH ", "ENCRYPTED ", ""):
            pem = (
                f"key = '''-----BEGIN {kind}PRIVATE KEY-----\n"
                "MIIEowIBAAKCAQEA0\nAbCdEf123\n"
                f"-----END {kind}PRIVATE KEY-----'''\nprint('after')"
            )
            masked = mask_secrets(pem)
            assert "PRIVATE KEY" not in masked and "MIIEow" not in masked, masked
            assert "print('after')" in masked, "Text after the block must survive"
        unterminated = mask_secrets("x\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow")
        assert unterminated == "x\n[REDACTED]", unterminated
        cert = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
        assert mask_secrets(cert) == cert, "Public certificates are not secrets"
    print("[PASS] Security Layer Tests")


def test_pre_write_syntax():
    with tempfile.TemporaryDirectory() as tmpdir:
        p = Path(tmpdir) / "test.py"

        # Valid python
        valid, err = validate_syntax("def add(a, b):\n    return a + b\n", p)
        assert valid and err is None, f"Valid python should pass: {err}"

        # Invalid python
        invalid, err = validate_syntax("def add(a, b\n    return a + b\n", p)
        assert not invalid, "Invalid python syntax must be rejected"
        assert "SyntaxError" in err, f"Expected SyntaxError in message: {err}"

        # Valid JSON
        jp = Path(tmpdir) / "test.json"
        valid, err = validate_syntax('{"key": 123}', jp)
        assert valid, "Valid JSON should pass"

        # Invalid JSON
        invalid, err = validate_syntax('{"key": 123,}', jp)
        assert not invalid, "Trailing comma in JSON should be rejected"
    print("[PASS] Pre-write Syntax Validation Tests")


def test_fuzzy_patching_and_rollback():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "app.py"
        target.write_text("def hello():\r\n    print('hello world')\r\n", encoding="utf-8")

        task_id = "test_run_01"

        # 1. Level 2 Patch with LF on a CRLF file
        search_block = "def hello():\n    print('hello world')"
        replace_block = "def hello():\n    print('hello BoteX!')"

        res = apply_patch("app.py", search_block, replace_block, root, task_id=task_id)
        assert res["ok"], f"Patch should succeed: {res.get('error')}"
        assert "hello BoteX!" in target.read_text(encoding="utf-8"), "File should contain patched content"

        # 2. Syntax rejection does not corrupt file
        bad_replace = "def hello():\n    print('broken syntax"
        res_bad = apply_patch("app.py", "print('hello BoteX!')", bad_replace, root, task_id=task_id)
        assert not res_bad["ok"], "Broken syntax must be rejected"
        assert "hello BoteX!" in target.read_text(encoding="utf-8"), "File must remain uncorrupted"

        # 3. Rollback restores original file
        restored = snapshot_manager.rollback_task(task_id)
        assert len(restored) > 0, "Should restore backed up files"
        assert "hello world" in target.read_text(encoding="utf-8"), "Original file must be restored"
    print("[PASS] Fuzzy Patching & Rollback Tests")


def test_outline_and_reading():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        code_file = root / "service.py"
        code = """class UserService:
    def __init__(self, db):
        self.db = db

    def get_user(self, user_id):
        return self.db.find(user_id)

def helper_fn():
    return 42
"""
        code_file.write_text(code, encoding="utf-8")

        # 1. Outline
        outline_res = get_file_outline("service.py", root)
        assert outline_res["ok"], "Outline should succeed"
        outline_items = outline_res["outline"]
        assert len(outline_items) == 2, f"Expected 2 top-level items, got {len(outline_items)}"
        cls_item = outline_items[0]
        assert cls_item["name"] == "UserService", "Class name must match"
        assert len(cls_item.get("methods", [])) == 2, "UserService should have 2 methods"

        # 2. Read lines
        lines_res = read_file_lines("service.py", start=5, end=7, workspace_root=root)
        assert lines_res["ok"], "Read lines should succeed"
        assert "get_user" in lines_res["content"], "Should read lines containing get_user"
        assert "5: " in lines_res["content"], "Lines must be numbered"

        # 3. Create file
        new_f_res = create_file("utils/math.py", "def square(x):\n    return x * x\n", root, task_id="test_02")
        assert new_f_res["ok"], "Create file should succeed"
        assert (root / "utils" / "math.py").exists(), "New file must exist on disk"

        # 4. List dir
        ld_res = list_dir(".", root)
        assert ld_res["ok"], "List dir should succeed"
        paths = [e["path"] for e in ld_res["entries"]]
        assert any("service.py" in p for p in paths), "service.py should be in dir listing"
        assert any("utils" in p for p in paths), "utils should be in dir listing"
    print("[PASS] Outline & File Tools Tests")


def test_destructive_ops_and_rollback():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        victim = root / "old.py"
        victim.write_text("x = 1\n", encoding="utf-8")

        task_id = "test_destructive_01"

        # 1. Move file
        res = move_file("old.py", "renamed/new.py", root, task_id=task_id)
        assert res["ok"], f"Move should succeed: {res.get('error')}"
        assert not victim.exists() and (root / "renamed" / "new.py").exists()

        # 2. Delete file
        res = delete_file("renamed/new.py", root, task_id=task_id)
        assert res["ok"], f"Delete should succeed: {res.get('error')}"
        assert not (root / "renamed" / "new.py").exists()

        # 3. Directory deletion is refused
        res_dir = delete_file("renamed", root, task_id=task_id)
        assert not res_dir["ok"], "Directories must not be deletable"

        # 4. Rollback restores deleted content — and must NOT resurrect a
        # file the task itself created (created marker wins over a later
        # snapshot of the same path)
        restored = snapshot_manager.rollback_task(task_id)
        assert restored, "Rollback should restore snapshotted files"
        assert (root / "old.py").exists(), "Move source must be restored"
        assert not (root / "renamed" / "new.py").exists(), \
            "A task-created file must not be resurrected by rollback"
        assert snapshot_manager.verify_rollback(task_id) == [], \
            "Rollback must be verifiably complete"

        # 5. move_file dst is registered as created — a bare move rolls
        # back to: src present, dst gone
        task_id2 = "test_destructive_02"
        res = move_file("old.py", "moved.py", root, task_id=task_id2)
        assert res["ok"] and (root / "moved.py").exists()
        snapshot_manager.rollback_task(task_id2)
        assert (root / "old.py").read_text() == "x = 1\n"
        assert not (root / "moved.py").exists(), \
            "move dst must be removed by rollback"
        assert snapshot_manager.verify_rollback(task_id2) == []
    print("[PASS] Destructive Ops & Rollback Tests")


def test_capability_modes():
    # 1. Presets expose exactly their declared capability sets
    assert resolve_capabilities("readonly") == {CAP_READ}
    assert resolve_capabilities("edit") == {CAP_READ, CAP_WRITE}
    assert resolve_capabilities("destructive") == {
        CAP_READ, CAP_WRITE, CAP_DESTRUCTIVE
    }
    assert resolve_capabilities("full") == {
        CAP_READ, CAP_WRITE, CAP_DESTRUCTIVE, CAP_EXEC
    }
    # 'net' is deliberately never part of a preset
    assert CAP_NET not in resolve_capabilities("full")

    # 2. Empty mode falls back to engine.default_mode (edit in defaults)
    assert resolve_capabilities("") == {CAP_READ, CAP_WRITE}

    # 3. Unknown mode fails closed
    try:
        resolve_capabilities("godmode")
        assert False, "Unknown mode must raise"
    except ValueError:
        pass

    # 4. allow_* flags can only widen, never narrow
    caps = resolve_capabilities("readonly", allow_destructive=True)
    assert CAP_DESTRUCTIVE in caps
    assert resolve_capabilities("edit", allow_net=True) == {
        CAP_READ, CAP_WRITE, CAP_NET
    }

    # 5. tool_allowed: write tools need write, gated tools may be confirmed
    assert tool_allowed("read_file", {CAP_READ})
    assert not tool_allowed("apply_patch", {CAP_READ})
    assert tool_allowed("apply_patch", {CAP_READ, CAP_WRITE})
    assert not tool_allowed("delete_file", {CAP_READ, CAP_WRITE})
    assert tool_allowed("delete_file", {CAP_READ, CAP_WRITE}, can_confirm=True)
    assert not tool_allowed("run_command", {CAP_READ, CAP_WRITE})
    assert tool_allowed("run_command", {CAP_READ, CAP_WRITE}, can_confirm=True)
    # write is NOT confirmable — readonly must stay readonly
    assert not tool_allowed("apply_patch", {CAP_READ}, can_confirm=True)
    print("[PASS] Capability Modes Tests")


def test_exec_policy():
    import asyncio
    import os

    py = sys.executable  # resolved binary; basename -> 'python'

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        POLICY = dict(allowlist=["python"], deny_args=[], timeout_s=15,
                      max_output_bytes=2000)

        # 1. Allowlist enforcement — non-allowlisted binary refused
        r = asyncio.run(run_command("evilbinary --help", root, **POLICY))
        assert not r["ok"] and "allowlist" in r["error"]

        # 2. Deny-args — eval flag refused even for allowlisted binary
        r = asyncio.run(run_command(
            f'"{py}" -c "print(1)"', root,
            **{**POLICY, "deny_args": ["python -c"]}
        ))
        assert not r["ok"] and "denied" in r["error"]

        # 3. Allowed command runs in workspace and returns exit code/output
        r = asyncio.run(run_command(f'"{py}" --version', root, **POLICY))
        assert r["ok"] and r["exit_code"] == 0 and "Python" in r["stdout"]

        # 4. No shell: metacharacters are literal argv, not executed
        r = asyncio.run(run_command(
            f'"{py}" -c "import sys; print(sys.argv)" injected & echo pwned',
            root, **POLICY
        ))
        assert "pwned" in r["stdout"] and r["stdout"].count("pwned") == 1  # argv only

        # 5. Secret-looking env vars are stripped from the child env
        os.environ["BOTEX_FAKE_API_KEY"] = "sk-testsecret1234567890abcdef"
        env = _sanitized_env()
        assert "BOTEX_FAKE_API_KEY" not in env
        r = asyncio.run(run_command(
            f'"{py}" -c "import os; print(os.environ.get(\'BOTEX_FAKE_API_KEY\'))"',
            root, **POLICY
        ))
        assert "None" in r["stdout"]
        del os.environ["BOTEX_FAKE_API_KEY"]

        # 6. Secrets in command output are masked
        r = asyncio.run(run_command(
            f'"{py}" -c "print(\'sk-abcdefghij1234567890abcdefghij\')"',
            root, **POLICY
        ))
        assert "[REDACTED]" in r["stdout"]

        # 7. Timeout kills the process
        r = asyncio.run(run_command(
            f'"{py}" -c "import time; time.sleep(60)"', root,
            **{**POLICY, "timeout_s": 2}
        ))
        assert r["timed_out"] and not r["ok"]
    print("[PASS] Exec Policy Tests")


def test_pricing_guardrail():
    """Pricing cap blocks over-budget models before any API call."""
    fake_catalog = {
        "vendor/cheap-model": {"input": 0.10, "output": 0.50},
        "vendor/expensive-model": {"input": 2.00, "output": 10.00},
        "vendor/router": {"input": None, "output": None},
    }
    orig_loader = pricing.load_catalog
    pricing.load_catalog = lambda *a, **k: fake_catalog
    try:
        pcfg = {"base_url": "https://api.example/v1"}
        caps = {"enabled": True, "max_input_per_mtok": 5.0,
                "max_output_per_mtok": 5.0, "on_unknown": "allow"}

        # 1. Cheap model passes
        ok, _ = pricing.check_model_price("vendor/cheap-model", pcfg, caps)
        assert ok

        # 2. Expensive output ($10 > $5 cap) is rejected locally
        ok, msg = pricing.check_model_price("vendor/expensive-model", pcfg, caps)
        assert not ok and "PRICE BLOCK" in msg and "output" in msg

        # 3. Unknown/dynamic router falls under on_unknown policy
        ok, _ = pricing.check_model_price("vendor/router", pcfg, caps)
        assert ok
        ok, msg = pricing.check_model_price(
            "vendor/router", pcfg, {**caps, "on_unknown": "deny"})
        assert not ok and "PRICE BLOCK" in msg

        # 4. Disabled pricing never blocks
        ok, _ = pricing.check_model_price(
            "vendor/expensive-model", pcfg, {**caps, "enabled": False})
        assert ok

        # 5. Input cap independent of output cap
        ok, msg = pricing.check_model_price(
            "vendor/expensive-model", pcfg,
            {**caps, "max_input_per_mtok": 1.0, "max_output_per_mtok": 99.0})
        assert not ok and "input" in msg
    finally:
        pricing.load_catalog = orig_loader
    print("[PASS] Pricing Guardrail Tests")


def test_net_guards_and_created_file_rollback():
    import tempfile
    from botex.patch_engine import SnapshotManager
    from botex.security import SecurityError

    # URL validation must fail closed before any request is made. DNS is
    # stubbed so policy checks remain offline; the public-IP predicate is still
    # exercised separately by the loopback case below.
    original_public_ips = net_tools._public_ips
    net_tools._public_ips = lambda host, scheme="https": ["93.184.216.34"]
    try:
        blocked = False
        try:
            _validate_url("https://evil.example/", ["docs.openrouter.ai"])
        except ValueError:
            blocked = True
        assert blocked, "Non-allowlisted hosts must be rejected"
        assert _validate_url(
            "https://docs.openrouter.ai/docs", ["openrouter.ai"]
        ).startswith("https://")

        scope = resolve_net_scope(
            {"enabled": True, "policy": "caller", "allowed_hosts": ["openrouter.ai"]},
            ["api.github.com"], []
        )
        assert scope["allowed_hosts"] == ["api.github.com"]
        assert _validate_url(
            "https://api.github.com/repos", scope["allowed_hosts"],
            policy=scope["policy"], allowed_urls=scope["allowed_urls"]
        )

        scope = resolve_net_scope(
            {"enabled": True, "policy": "caller", "allowed_hosts": [], "allowed_urls": []},
            [], ["https://docs.example.com/guide/"]
        )
        assert _validate_url(
            "https://docs.example.com/guide/setup", [],
            policy="caller", allowed_urls=scope["allowed_urls"]
        )
        blocked = False
        try:
            _validate_url(
                "https://docs.example.com/other", [],
                policy="caller", allowed_urls=scope["allowed_urls"]
            )
        except ValueError:
            blocked = True
        assert blocked, "URL subtree authorization must not escape its prefix"

        exact = ["https://docs.example.com/search?q=ok"]
        assert _validate_url(
            "https://docs.example.com/search?q=ok", [],
            policy="caller", allowed_urls=exact
        )
        blocked = False
        try:
            _validate_url(
                "https://docs.example.com/search?q=secret", [],
                policy="caller", allowed_urls=exact
            )
        except ValueError:
            blocked = True
        assert blocked, "Exact URL authorization must not permit query changes"

        scope = resolve_net_scope(
            {"enabled": True, "policy": "public"},
            ["docs.openrouter.ai"], []
        )
        assert _validate_url(
            "https://docs.openrouter.ai/docs", scope["allowed_hosts"],
            policy="public", allowed_urls=scope["allowed_urls"]
        )
        blocked = False
        try:
            _validate_url(
                "https://example.com/", scope["allowed_hosts"],
                policy="public", allowed_urls=scope["allowed_urls"]
            )
        except ValueError:
            blocked = True
        assert blocked, "Per-run hosts must narrow public mode"

        scope = resolve_net_scope(
            {"enabled": True, "policy": "allowlist",
             "allowed_hosts": ["openrouter.ai"]},
            ["docs.openrouter.ai"], []
        )
        assert scope["allowed_hosts"] == ["docs.openrouter.ai"]
        scope = resolve_net_scope(
            {"enabled": True, "policy": "allowlist",
             "allowed_hosts": [],
             "allowed_urls": ["https://docs.example.com/guide/"]},
            [], ["https://docs.example.com/guide/api/"]
        )
        assert scope["allowed_urls"] == ["https://docs.example.com/guide/api/"]
        blocked = False
        try:
            resolve_net_scope(
                {"enabled": True, "policy": "allowlist",
                 "allowed_hosts": ["openrouter.ai"]},
                ["example.com"], []
            )
        except ValueError:
            blocked = True
        assert blocked, "Allowlist mode must not let callers widen config"
    finally:
        net_tools._public_ips = original_public_ips

    blocked = False
    try:
        _validate_url("http://127.0.0.1/admin", ["127.0.0.1"])
    except ValueError:
        blocked = True
    assert blocked, "Loopback targets must be blocked even if allowlisted"

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        snaps = root / ".snapshots"
        manager = SnapshotManager(snaps)
        created = root / "new.txt"
        manager.record_created_file("task1", created)
        created.write_text("new", encoding="utf-8")
        restored = manager.rollback_task("task1")
        assert str(created) in restored and not created.exists()
    print("[PASS] Network Guards & Created-File Rollback Tests")


def test_fetch_url_policy():
    import asyncio

    calls = []
    original_read_url = net_tools.read_url

    async def fake_read_url(url, **kwargs):
        calls.append((url, kwargs))
        return {"ok": True, "url": url, "content": "ok"}

    try:
        net_tools.read_url = fake_read_url
        result = asyncio.run(net_tools.fetch_url(
            "https://docs.new-site.example/reference",
            net_cfg={
                "enabled": True, "policy": "caller", "timeout_s": 20,
                "max_bytes": 1000, "max_redirects": 3,
            },
        ))
        assert result["ok"] and calls[-1][1]["policy"] == "public"

        result = asyncio.run(net_tools.fetch_url(
            "https://docs.openrouter.ai/reference",
            net_cfg={
                "enabled": True, "policy": "allowlist",
                "allowed_hosts": ["openrouter.ai"], "timeout_s": 20,
                "max_bytes": 1000, "max_redirects": 3,
            },
        ))
        assert result["ok"] and calls[-1][1]["policy"] == "allowlist"
        assert calls[-1][1]["allowed_hosts"] == ["openrouter.ai"]
    finally:
        net_tools.read_url = original_read_url

    result = asyncio.run(net_tools.fetch_url(
        "https://example.com/",
        net_cfg={"enabled": False, "policy": "caller"},
    ))
    assert not result["ok"]
    print("[PASS] Fetch URL Policy Tests")


def test_fallback_and_verify_guards():
    import asyncio
    import json as _json
    from types import SimpleNamespace
    import botex.engine as engine

    def response(content=None, finish="stop"):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=None),
                finish_reason=finish)],
            usage=None)

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            return self.responses.pop(0)

    original = {name: getattr(engine, name) for name in (
        "AsyncOpenAI", "resolve_model_candidates", "check_model_price",
        "check_model_tools", "check_budget_limit", "log_run",
        "print_run_summary")}
    engine.check_model_price = lambda *a, **k: (True, "")
    engine.check_model_tools = lambda *a, **k: (True, "")
    engine.check_budget_limit = lambda *a, **k: (False, 0.0)
    engine.log_run = lambda **kw: {"cost_usd": 0.0}
    engine.print_run_summary = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeClient([
                response("STATUS: UNSUPPORTED\nmodel cannot use tools"),
                response("STATUS: DONE\nreviewed"),
                response("STATUS: DONE\nreviewed"),
            ])
            engine.resolve_model_candidates = lambda *a, **k: ["first", "second"]
            engine.AsyncOpenAI = lambda **_: fake
            result = asyncio.run(engine.run_botex_task(
                task="review", workspace_dir=tmpdir, model="",
                api_key="x", mode="readonly"))
            assert result["status"] == "DONE"
            assert [call["model"] for call in fake.calls] == ["first", "second"]

            # verify_command is rejected before an API call unless exec is enabled.
            fake2 = FakeClient([])
            engine.AsyncOpenAI = lambda **_: fake2
            result = asyncio.run(engine.run_botex_task(
                task="verify", workspace_dir=tmpdir, model="explicit",
                api_key="x", mode="edit", verify_command="pytest -q"))
            assert result["status"] == "CONFIG_ERROR" and not fake2.calls
    finally:
        for name, value in original.items():
            setattr(engine, name, value)
    print("[PASS] Fallback & Verify Guard Tests")


def test_async_task_registry():
    import asyncio
    from types import SimpleNamespace
    import server

    original = server.run_subagent
    async def fake_run(**kwargs):
        await asyncio.sleep(0)
        return SimpleNamespace(
            content=[SimpleNamespace(text="fake result")],
            structured_content={"ok": True, "status": "DONE"},
        )

    async def exercise():
        payload = await server.start_task("background test")
        task_id = payload["task_id"]
        assert payload["status"] == "QUEUED"
        await asyncio.sleep(0.02)
        status = await server.get_task_status(task_id)
        assert status["status"] == "DONE" and status["result"] == "fake result"
        assert status["result_data"]["status"] == "DONE"
        return task_id

    try:
        server.run_subagent = fake_run
        task_id = asyncio.run(exercise())
    finally:
        server.run_subagent = original
        server._TASKS.pop(task_id, None) if "task_id" in locals() else None
    print("[PASS] Async Task Registry Tests")


def test_engine_write_guards():
    """
    Engine-loop guards (fake API client, zero network): false DONE without a
    write gets one corrective nudge, truncated tool-call arguments get an
    explicit error, and output_path salvages a text payload to disk.
    """
    import asyncio
    import json as _json
    from types import SimpleNamespace
    import botex.engine as engine

    def resp(content=None, tool_calls=None, finish="stop", reasoning=0):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)],
            usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1,
                prompt_tokens_details=None,
                completion_tokens_details=(
                    SimpleNamespace(reasoning_tokens=reasoning)
                    if reasoning else None)),
        )

    def tcall(name, args):
        return SimpleNamespace(
            id="t1", type="function",
            function=SimpleNamespace(
                name=name,
                arguments=args if isinstance(args, str) else _json.dumps(args)))

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        async def _create(self, **kw):
            self.calls.append(kw)
            return self.responses.pop(0)

    orig = {k: getattr(engine, k) for k in (
        "AsyncOpenAI", "check_model_price", "check_model_tools",
        "check_budget_limit", "log_run", "print_run_summary")}
    engine.check_model_price = lambda *a, **k: (True, "")
    engine.check_model_tools = lambda *a, **k: (True, "")
    engine.check_budget_limit = lambda *a, **k: (False, 0.0)
    engine.log_run = lambda **kw: {"cost_usd": 0.0}
    engine.print_run_summary = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            def run(client, **kw):
                engine.AsyncOpenAI = lambda **_: client
                args = dict(workspace_dir=tmpdir, model="test/model",
                            api_key="x", mode="edit")
                args.update(kw)
                return asyncio.run(engine.run_botex_task(**args))

            # 1. False DONE (payload as text) -> nudge -> real write -> DONE
            fake = FakeClient([
                resp('STATUS: DONE\n{"a": 1}'),
                resp(tool_calls=[tcall("create_file", {
                    "path": "out.json", "content": '{"a": 1}'})]),
                resp("STATUS: DONE\nwritten"),
            ])
            res = run(fake, task="write out.json")
            assert res["ok"] and res["status"] == "DONE" and res["steps"] == 3
            assert (Path(tmpdir) / "out.json").exists()

            # 2. Legit no-write DONE is accepted after a single nudge
            fake = FakeClient([
                resp("STATUS: DONE\nreview clean"),
                resp("STATUS: DONE\nreview clean"),
            ])
            res = run(fake, task="review code")
            assert res["ok"] and res["status"] == "DONE" and res["steps"] == 2

            # 3. Truncated tool-call args -> explicit error sent to the model
            fake = FakeClient([
                resp(tool_calls=[tcall("create_file", '{"path": "x.json", "con')]),
                resp("STATUS: DONE\nnothing to write"),
                resp("STATUS: DONE\nnothing to write"),
            ])
            res = run(fake, task="write x.json")
            assert res["status"] == "DONE"
            sent = fake.calls[1]["messages"]
            assert any(
                m.get("role") == "tool" and "not valid JSON" in m["content"]
                for m in sent)

            # 4. output_path: DONE with JSON in text -> salvaged to disk
            fake = FakeClient([
                resp('STATUS: DONE\n{"hooks": ["a", "b"]}'),
                resp('STATUS: DONE\n{"hooks": ["a", "b"]}'),
            ])
            res = run(fake, task="produce hooks json", output_path="result.json")
            assert res["ok"] and "result.json" in res["files_touched"]
            assert _json.loads(
                (Path(tmpdir) / "result.json").read_text(encoding="utf-8")
            ) == {"hooks": ["a", "b"]}

            # 5. output_path under readonly mode -> CONFIG_ERROR, no API call
            fake = FakeClient([])
            res = run(fake, task="x", mode="readonly", output_path="r.json")
            assert res["status"] == "CONFIG_ERROR" and not fake.calls

            # 6. Thinking exhaustion -> straight TOKEN_LIMIT, no retry
            # (raising the cap would only invite longer thinking)
            fake = FakeClient([resp(finish="length", reasoning=7500)])
            res = run(fake, task="think hard")
            assert res["status"] == "TOKEN_LIMIT" and len(fake.calls) == 1
            assert "Reasoning consumed" in res["summary"]

            # 7. Non-reasoning clipped reply -> one retry at the raised cap
            fake = FakeClient([
                resp(finish="length"),
                resp("STATUS: DONE\nok"),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="plain task")
            assert res["status"] == "DONE" and len(fake.calls) == 3
            assert fake.calls[1]["max_tokens"] == 16000

            # 8. Prose refusal WITHOUT the STATUS marker gets a targeted
            # nudge, not an instant abort — a model mid-analysis may say
            # "cannot find X" and still complete the task.
            fake = FakeClient([
                resp("I cannot determine the data flow from this file alone."),
                resp("STATUS: DONE\nfindings: data flows A -> B"),
                resp("STATUS: DONE\nfindings: data flows A -> B"),
            ])
            res = run(fake, task="advisory analysis of the module")
            assert res["status"] == "DONE"
            sent = fake.calls[1]["messages"]
            assert any(
                m.get("role") == "user" and "STATUS: UNSUPPORTED" in m["content"]
                for m in sent)

            # 9. Explicit STATUS: UNSUPPORTED marker -> instant abort
            fake = FakeClient([resp("STATUS: UNSUPPORTED\nneeds shell access")])
            res = run(fake, task="run migrations")
            assert res["status"] == "UNSUPPORTED" and len(fake.calls) == 1

            # 10. Repeated prose refusals -> INCOMPLETE + model_refusal after
            # 3 strikes (never UNSUPPORTED without the explicit marker)
            fake = FakeClient([
                resp("I cannot access the database"),
                resp("I cannot access the database"),
                resp("I cannot access the database"),
            ])
            res = run(fake, task="query the db")
            assert res["status"] == "INCOMPLETE" and len(fake.calls) == 3
            assert res["failure_kind"] == "model_refusal"

            # 11. Refusal exhaustion still falls back to the next candidate —
            # regression guard: INCOMPLETE+model_refusal must stay
            # fallback-eligible (the DeepSeek-R1 scenario).
            fake = FakeClient([
                resp("I cannot access the database"),
                resp("I cannot access the database"),
                resp("I cannot access the database"),
                resp("STATUS: DONE\nqueried via reports table"),
                resp("STATUS: DONE\nqueried via reports table"),
            ])
            orig_candidates = engine.resolve_model_candidates
            engine.resolve_model_candidates = lambda *a, **k: ["first", "second"]
            try:
                res = run(fake, task="query the db")
            finally:
                engine.resolve_model_candidates = orig_candidates
            assert res["status"] == "DONE"
            assert [c["model"] for c in fake.calls] == [
                "first", "first", "first", "second", "second"]

            # 12. STATUS: UNSUPPORTED after writes is not honored as a
            # marker — the model already did the work; it gets a nudge.
            fake = FakeClient([
                resp(tool_calls=[tcall("create_file", {
                    "path": "x.txt", "content": "hi"})]),
                resp("STATUS: UNSUPPORTED\nno shell access"),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="write x.txt")
            assert res["status"] == "DONE" and len(fake.calls) == 3

            # 13. Turn-limit exhaustion after writes -> ok=False, not DONE
            fake = FakeClient([
                resp(tool_calls=[tcall("create_file", {
                    "path": "x.txt", "content": "hi"})]),
            ])
            res = run(fake, task="write x.txt", max_turns=1)
            assert res["status"] == "MAX_TURNS_REACHED" and res["ok"] is False
            assert res["failure_kind"] == "turns_exhausted"

            # 14. Tool-call preflight block reports a distinct failure kind
            engine.check_model_tools = lambda *a, **k: (False, "no tools")
            res = run(FakeClient([]), task="x")
            assert res["status"] == "UNSUPPORTED"
            assert res["failure_kind"] == "tool_call_unsupported"
            engine.check_model_tools = lambda *a, **k: (True, "")

            # 15. An executed command counts as a workspace mutation for the
            # fallback guard even though files_touched stays empty — a
            # possibly-dirty workspace is never handed to another model.
            orig_load_config = engine.load_config
            engine.load_config = lambda reload=False: {
                **orig_load_config(reload),
                "exec": {
                    **orig_load_config().get("exec", {}), "enabled": True,
                },
            }
            try:
                class ExecThenFail:
                    def __init__(self):
                        self.calls = []
                        self.chat = SimpleNamespace(
                            completions=SimpleNamespace(create=self._create))

                    async def _create(self, **kw):
                        self.calls.append(kw)
                        if len(self.calls) == 1:
                            return resp(tool_calls=[tcall("run_command", {
                                "command": f'"{sys.executable}" --version'})])
                        raise RuntimeError("provider went away")

                fake = ExecThenFail()
                orig_candidates = engine.resolve_model_candidates
                engine.resolve_model_candidates = lambda *a, **k: ["first", "second"]
                try:
                    res = run(fake, task="check version", allow_exec=True)
                finally:
                    engine.resolve_model_candidates = orig_candidates
                assert res["status"] == "API_ERROR"
                assert res["failure_kind"] == "provider_error"
                assert [c["model"] for c in fake.calls] == ["first", "first"]
            finally:
                engine.load_config = orig_load_config

            # 16. Contract: a pre-existing VALID output file is accepted on
            # the first DONE — no nudge, no salvage overwrite (fixes the
            # D1 false DONE_WITHOUT_WRITE / overwrite bug).
            (Path(tmpdir) / "result.json").write_text(
                '{"a": 1}', encoding="utf-8")
            fake = FakeClient([resp("STATUS: DONE\nresult is already correct")])
            res = run(fake, task="produce json", output_path="result.json")
            assert res["status"] == "DONE" and len(fake.calls) == 1
            assert (Path(tmpdir) / "result.json").read_text() == '{"a": 1}'

            # 17. Contract: a pre-existing EMPTY output file is still a
            # contract failure — nudge, then salvage writes the payload.
            (Path(tmpdir) / "result.json").write_text("", encoding="utf-8")
            fake = FakeClient([
                resp("STATUS: DONE\n"),
                resp('STATUS: DONE\n{"b": 2}'),
            ])
            res = run(fake, task="produce json", output_path="result.json")
            assert res["status"] == "DONE"
            assert _json.loads(
                (Path(tmpdir) / "result.json").read_text(encoding="utf-8")
            ) == {"b": 2}

            # 18. Contract: unsafe output_path is rejected before any API call.
            fake = FakeClient([resp("STATUS: DONE\nnever")])
            res = run(fake, task="x", output_path="../evil.json")
            assert res["status"] == "CONFIG_ERROR" and not fake.calls

            # 19. Contract: analysis (readonly) with an empty report is
            # nudged once, then fails as a contract failure — not DONE.
            fake = FakeClient([resp("STATUS: DONE\n"), resp("STATUS: DONE\n")])
            res = run(fake, task="review the code", mode="readonly")
            assert res["status"] == "INCOMPLETE"
            assert res["failure_kind"] == "contract_failed"

            # 20. Contract: analysis with a substantive report completes
            # without any workspace write.
            fake = FakeClient([resp("STATUS: DONE\nno issues found")])
            res = run(fake, task="review the code", mode="readonly")
            assert res["status"] == "DONE" and res["ok"] is True

            # 21. Fallback after a confirmed rollback: a mutated workspace
            # is handed to the next candidate only once every mutation is
            # verifiably undone.
            class WriteThenFail:
                def __init__(self):
                    self.calls = []
                    self.chat = SimpleNamespace(
                        completions=SimpleNamespace(create=self._create))

                async def _create(self, **kw):
                    self.calls.append(kw)
                    if len(self.calls) == 1:
                        return resp(tool_calls=[tcall("create_file", {
                            "path": "dirty.txt", "content": "partial"})])
                    if len(self.calls) == 2:
                        raise RuntimeError("provider went away")
                    return resp("STATUS: DONE\nok")

            fake = WriteThenFail()
            orig_candidates = engine.resolve_model_candidates
            engine.resolve_model_candidates = lambda *a, **k: ["first", "second"]
            try:
                res = run(fake, task="write file")
            finally:
                engine.resolve_model_candidates = orig_candidates
            assert res["status"] == "DONE"
            assert [c["model"] for c in fake.calls] == [
                "first", "first", "second", "second"]
            # rollback removed the partially-created file before the retry
            assert not (Path(tmpdir) / "dirty.txt").exists()
            assert len(res["attempts"]) == 2
            assert res["attempts"][0]["status"] == "API_ERROR"
            assert res["attempts"][0]["rolled_back"] is True
            assert res["fallback_from"] == "first"

            # 22. Provider isolation: the OpenRouter ZDR body is applied
            # only to OpenRouter — NVIDIA gets the empty extra_body.
            fake = FakeClient([
                resp("STATUS: DONE\nok"), resp("STATUS: DONE\nok")])
            res = run(fake, task="zdr check")
            assert res["status"] == "DONE"
            eb = fake.calls[0].get("extra_body") or {}
            assert eb["provider"]["data_collection"] == "deny"
            fake = FakeClient([
                resp("STATUS: DONE\nok"), resp("STATUS: DONE\nok")])
            res = run(fake, task="zdr check", provider="nvidia")
            assert res["status"] == "DONE"
            assert not (fake.calls[0].get("extra_body") or {}).get("provider")

            # 23. Analytics: an API_ERROR still records the attempt with
            # provider + failure_kind — spent tokens are not lost.
            class FailClient:
                def __init__(self):
                    self.chat = SimpleNamespace(
                        completions=SimpleNamespace(create=self._create))

                async def _create(self, **kw):
                    raise RuntimeError("HTTP 500 boom")

            logged = []
            engine.log_run = lambda **kw: (logged.append(kw),
                                           {"cost_usd": 0.0})[1]
            res = run(FailClient(), task="boom")
            engine.log_run = orig["log_run"]
            assert res["status"] == "API_ERROR"
            assert logged and logged[-1]["status"] == "API_ERROR"
            assert logged[-1]["failure_kind"] == "provider_error"
            assert logged[-1]["provider"] == "openrouter"
    finally:
        for k, v in orig.items():
            setattr(engine, k, v)
    print("[PASS] Engine Write-Guard Tests")


def test_provider_adapter():
    """Provider-neutral normalization, header scoping, key resolution,
    error sanitization — the Etap-3 adapter contract."""
    import os
    from types import SimpleNamespace
    from botex import providers

    def _resp(content=None, tool_calls=None, finish="stop", usage=None):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)],
            usage=usage)

    def _tc(name, args):
        return SimpleNamespace(
            id="c1",
            function=SimpleNamespace(name=name, arguments=args))

    # 1. Normalization: tool_calls -> tool_call; usage fields flattened
    n = providers.normalize_response(_resp(
        tool_calls=[_tc("read_file", '{"path": "x.py"}')],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2))))
    assert n.finish_reason == "tool_call"
    assert n.tool_calls[0].name == "read_file"
    assert n.tool_calls[0].arguments == '{"path": "x.py"}'
    assert n.usage.prompt_tokens == 10 and n.usage.cached_tokens == 3
    assert n.usage.reasoning_tokens == 2

    # 2. Finish-reason mapping: stop/length/foreign dialects
    assert providers.normalize_response(_resp("hi")).finish_reason == "completed"
    assert providers.normalize_response(_resp("", finish="length")).finish_reason == "length"
    assert providers.normalize_response(_resp("", finish="tool_use")).finish_reason == "tool_call"
    assert providers.normalize_response(_resp("", finish="end_turn")).finish_reason == "completed"
    assert providers.normalize_response(_resp("", finish="content_filter")).finish_reason == "refusal"
    assert providers.normalize_response(_resp("", finish="weird")).finish_reason == "unknown"
    # No choices at all is a provider protocol violation — classified as
    # provider_error so the engine fails fast instead of idling for turns
    n = providers.normalize_response(SimpleNamespace(choices=[], usage=None))
    assert n.finish_reason == "provider_error" and n.content == ""
    assert n.raw_finish_reason == "no_choices"
    # Dict-shaped responses normalize identically to SDK objects
    n = providers.normalize_response({
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    })
    assert n.finish_reason == "completed" and n.content == "hi"
    assert n.usage.prompt_tokens == 4
    # Content block lists flatten to text; missing tool-call ids get
    # synthesized so the tool reply message stays schema-valid
    n = providers.normalize_response(_resp(
        content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        tool_calls=[SimpleNamespace(
            id="", function=SimpleNamespace(name="list_dir", arguments="{}"))]))
    assert n.content == "ab" and n.tool_calls[0].id == "call_0"

    # 3. Attribution headers: OpenRouter only, explicit opt-in elsewhere
    captured = {}
    def factory(**kw):
        captured.clear()
        captured.update(kw)
        return "client"
    providers.build_async_client(
        "openrouter", {"base_url": "https://openrouter.ai/api/v1"}, "k",
        client_factory=factory)
    assert captured["default_headers"] is not None
    providers.build_async_client(
        "nvidia", {"base_url": "https://integrate.api.nvidia.com/v1"}, "k",
        client_factory=factory)
    assert captured["default_headers"] is None
    providers.build_async_client(
        "other", {"base_url": "https://x", "attribution_headers": True}, "k",
        client_factory=factory)
    assert captured["default_headers"] is not None

    # 4. API-key resolution order: explicit > env var > secrets
    assert providers.resolve_api_key("explicit", "p", {}, {}) == "explicit"
    os.environ["BOTEX_TEST_P_KEY"] = "env-key"
    try:
        assert providers.resolve_api_key(
            None, "p", {"api_key_env": "BOTEX_TEST_P_KEY"}, {}) == "env-key"
    finally:
        del os.environ["BOTEX_TEST_P_KEY"]
    # secrets used only when the env var is unset (env has priority)
    _cfg = {"api_key_env": "BOTEX_TEST_MISSING_KEY"}
    assert providers.resolve_api_key(None, "p", _cfg, {"p_api_key": "sec"}) == "sec"
    assert providers.resolve_api_key(None, "p", _cfg, {}) == ""

    # 5. Provider errors are masked and length-bounded
    err = providers.sanitize_provider_error(
        Exception("HTTP 401 key sk-or-v1-abcdef1234567890abcdef1234567890 "
                  + "x" * 3000), 100)
    assert "sk-or-v1-" not in err and len(err) <= 101

    # 6. Semantic error classification: auth is NOT fallbackable, policy and
    # availability problems are mapped to distinct kinds.
    from botex.contracts import classify_api_error

    def _status_err(code):
        e = Exception(f"HTTP {code}")
        e.status_code = code
        return e

    assert classify_api_error(_status_err(401)) == "provider_auth_error"
    assert classify_api_error(_status_err(403)) == "provider_policy_denied"
    assert classify_api_error(_status_err(404)) == "provider_policy_denied"
    assert classify_api_error(_status_err(429)) == "provider_unavailable"
    assert classify_api_error(_status_err(500)) == "provider_unavailable"
    assert classify_api_error(Exception("weird")) == "provider_error"
    import httpx
    from openai import APITimeoutError, APIConnectionError
    _req = httpx.Request("GET", "https://example.invalid")
    assert classify_api_error(
        APITimeoutError(request=_req)) == "request_timeout"
    assert classify_api_error(
        APIConnectionError(message="down", request=_req)
    ) == "provider_unavailable"

    print("[PASS] Provider Adapter Tests")


def test_analytics_record_fields():
    """Analytics ledger: provider/failure_kind correlation fields, secret
    masking, and status-derived failure_kind defaults."""
    import tempfile
    from pathlib import Path
    from botex import analytics

    with tempfile.TemporaryDirectory() as tmpdir:
        original = analytics.ANALYTICS_FILE
        analytics.ANALYTICS_FILE = Path(tmpdir) / "analytics.json"
        try:
            def _log(**kw):
                args = dict(
                    task_id="t1", model="m/x", duration_s=1.0, steps=2,
                    prompt_tokens=10, completion_tokens=5, cached_tokens=0,
                    files_touched=[], lines_added=0, lines_removed=0,
                    status="DONE", summary="done")
                args.update(kw)
                return analytics.log_run(**args)

            # explicit failure_kind wins over the status-derived default
            e = _log(status="API_ERROR", provider="openrouter",
                     failure_kind="provider_auth_error",
                     summary="key sk-or-v1-abcdef1234567890abcdef1234567890")
            assert e["failure_kind"] == "provider_auth_error"
            assert e["provider"] == "openrouter"
            assert "sk-or-v1" not in e["summary"]

            # status-derived kinds when the caller did not specify one
            assert _log(status="INCOMPLETE")["failure_kind"] == "no_progress"
            assert _log(
                status="TOKEN_LIMIT")["failure_kind"] == "token_exhausted"
            assert _log(status="DONE")["failure_kind"] is None
            assert _log(status="DONE")["provider"] is None

            # persisted records round-trip through load_analytics
            records = analytics.load_analytics()
            assert len(records) == 5
            assert records[0]["failure_kind"] == "provider_auth_error"
        finally:
            analytics.ANALYTICS_FILE = original
    print("[PASS] Analytics Record Field Tests")


def test_mcp_structured_output():
    """MCP surface: published output schemas, TextContent+structuredContent
    dual result, and input validation rejecting bad args before any
    billable engine call."""
    import asyncio
    from unittest.mock import patch
    import server

    async def exercise():
        tools = await server.mcp.list_tools()
        schemas = {t.name: t.output_schema for t in tools}
        # run_subagent publishes the SubagentTaskResult object schema
        rs = schemas["run_subagent"]
        assert rs and rs.get("type") == "object"
        props = rs.get("properties", {})
        for field in ("status", "failure_kind", "ok", "files_touched",
                      "exec_ran", "attempts"):
            assert field in props, field
        # dict-returning tools publish a free-form object schema
        assert schemas["get_task_status"]["type"] == "object"
        # legacy string tools keep the wrapped {result: string} shape
        assert schemas["get_stats"]["properties"]["result"]["type"] == "string"

        async def fake_engine(**kw):
            return {"ok": True, "task_id": "t1", "status": "DONE",
                    "summary": "done", "files_touched": ["a.py"],
                    "steps": 1}
        with patch.object(server, "run_botex_task", fake_engine):
            res = await server.mcp.call_tool("run_subagent", {"task": "x"})
        assert res.content[0].text.startswith("[SUCCESS]")
        assert res.structured_content["status"] == "DONE"
        assert res.structured_content["files_touched"] == ["a.py"]

        # malformed input dies in schema validation — engine never runs
        called = []

        async def sentinel(**kw):
            called.append(kw)
            return {}
        with patch.object(server, "run_botex_task", sentinel):
            try:
                bad = await server.mcp.call_tool(
                    "run_subagent",
                    {"task": "x", "max_turns": "not-an-int"})
            except Exception:
                bad = None
            assert not called
            if bad is not None:
                assert (getattr(bad, "is_error", False)
                        or getattr(bad, "isError", False))

    asyncio.run(exercise())
    print("[PASS] MCP Structured Output Tests")


def test_zdr_guardrail():
    """Zero Data Retention (ZDR) guardrail: pre-flight local rejection of
    free-tier and non-ZDR models before any network call or token spend."""
    import asyncio
    from botex.security import check_model_zdr
    from botex.engine import run_botex_task

    # 1. Direct unit checks for check_model_zdr
    # Pattern rejection when ZDR enabled
    ok, msg = check_model_zdr("meta-llama/llama-3.3-70b-instruct:free", {"zdr": {"enabled": True, "deny_patterns": [":free"]}})
    assert not ok
    assert "[ZDR BLOCK]" in msg

    # Deny pattern: router / free pattern
    ok, msg = check_model_zdr("openrouter/free", {"zdr": {"enabled": True, "deny_patterns": [":free", "openrouter/free"]}})
    assert not ok
    assert "[ZDR BLOCK]" in msg

    # Allowed when model does not match deny pattern
    ok, msg = check_model_zdr("openrouter/auto", {"zdr": {"enabled": True, "deny_patterns": [":free"]}})
    assert ok
    assert msg == ""

    # Fallback to extra_body.provider.data_collection == deny when zdr block omitted
    ok, msg = check_model_zdr("qwen/qwen-2.5-coder-32b-instruct:free", {
        "extra_body": {"provider": {"data_collection": "deny"}}
    })
    assert not ok
    assert "[ZDR BLOCK]" in msg

    # Allowed when explicitly disabled in config
    ok, msg = check_model_zdr("qwen/qwen-2.5-coder-32b-instruct:free", {
        "zdr": {"enabled": False}
    })
    assert ok
    assert msg == ""

    # 2. Engine integration check: run_botex_task pre-flight rejects locally with ZDR_VIOLATION
    res = asyncio.run(run_botex_task(
        task="Test task that must not hit network",
        model="nex-agi/nex-n2.5-mini:free",
        workspace_dir="."
    ))
    assert res["ok"] is False
    assert res["status"] == "ZDR_VIOLATION"
    assert res["failure_kind"] == "security_or_policy_block"
    assert "[ZDR BLOCK]" in res["message"]
    assert res["steps"] == 0
    assert len(res["files_touched"]) == 0

    print("[PASS] ZDR Guardrail Tests")


def test_review_regressions():
    """Regression coverage for the implementation-review fixes: snapshot
    name collisions, strict rollback verification, per-turn stagnation,
    blocked intermediate path segments, deny-list evasion, gitignore
    semantics, clamped turn limits, and provider_error responses."""
    import asyncio
    import json as _json
    import sys
    from types import SimpleNamespace
    from botex.exec_tools import command_policy_error
    from botex.security import is_gitignored, load_gitignore_patterns
    import botex.engine as engine

    # --- Snapshot registry: collision-free names -------------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "a").mkdir()
        p_nested = root / "a" / "b"
        p_flat = root / "a_b"
        p_nested.write_text("nested", encoding="utf-8")
        p_flat.write_text("flat", encoding="utf-8")
        tid = "collision_task"
        snapshot_manager.snapshot_file(tid, p_nested)
        snapshot_manager.snapshot_file(tid, p_flat)
        p_nested.write_text("CHANGED", encoding="utf-8")
        p_flat.write_text("CHANGED", encoding="utf-8")
        snapshot_manager.rollback_task(tid)
        assert snapshot_manager.verify_rollback(tid) == []
        assert p_nested.read_text() == "nested"
        assert p_flat.read_text() == "flat"

    # verify_rollback fails closed when the registry should exist but is gone
    assert snapshot_manager.verify_rollback("nonexistent_task", expect_entries=True)
    assert snapshot_manager.verify_rollback("nonexistent_task") == []

    # --- Blocked intermediate path segments ------------------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        for seg in (".env", ".git", ".snapshots"):
            (root / seg).mkdir()
            (root / seg / "inner.txt").write_text("x", encoding="utf-8")
            for p in (f"{seg}/inner.txt", f"./{seg}/inner.txt"):
                try:
                    resolve_safe_path(root, p)
                    raise AssertionError(f"{p} must be blocked")
                except SecurityError:
                    pass

    # --- exec deny-list: flag-insertion and subcommand evasion -----------
    allowlist = ["py", "python", "node", "git", "pip", "pytest"]
    deny = [
        "py -c", "python -c", "node -e", "node --eval", "node -p",
        "pip install", "git checkout", "git switch",
    ]
    assert command_policy_error('py -3 -c "x=1"', allowlist=allowlist, deny_args=deny)
    assert command_policy_error('node --eval "x=1"', allowlist=allowlist, deny_args=deny)
    assert command_policy_error('node -p "x=1"', allowlist=allowlist, deny_args=deny)
    assert command_policy_error('git switch main', allowlist=allowlist, deny_args=deny)
    assert command_policy_error('python -m pip install x', allowlist=allowlist, deny_args=deny)
    assert command_policy_error('python -m pytest', allowlist=allowlist, deny_args=deny)
    assert not command_policy_error('pytest tests/', allowlist=allowlist, deny_args=deny)

    # --- Fuzzy patches reject ambiguous indentation-only matches ----------
    matched, _, _ = apply_fuzzy_patch(
        "def first():\n    return value\n\n"
        "def second():\n    return value\n",
        "return value",
        "return other",
    )
    assert not matched

    # --- Concurrent rollback is optimistic and fails closed ---------------
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "state.py"
        target.write_text("value = 1\n", encoding="utf-8")
        assert apply_patch(
            "state.py", "value = 1", "value = 2", root, task_id="task_a"
        )["ok"]
        assert apply_patch(
            "state.py", "value = 2", "value = 3", root, task_id="task_b"
        )["ok"]
        snapshot_manager.rollback_task("task_a")
        assert target.read_text(encoding="utf-8") == "value = 3\n"
        assert snapshot_manager.verify_rollback("task_a", expect_entries=True)

    # --- Snapshot markers cannot escape the task workspace ----------------
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        target = root / "safe.txt"
        outside = root.parent / "outside.txt"
        target.write_text("safe", encoding="utf-8")
        outside.write_text("outside", encoding="utf-8")
        task_id = "tampered_task"
        snapshot_manager.snapshot_file(task_id, target, root)
        target.write_text("changed", encoding="utf-8")
        marker = next(snapshot_manager._task_dir(task_id).glob("*.meta"))
        marker.write_text(str(outside), encoding="utf-8")
        snapshot_manager.rollback_task(task_id)
        assert outside.read_text(encoding="utf-8") == "outside"

    # --- gitignore: anchored patterns and negations ----------------------
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / ".gitignore").write_text(
            "/anchored.log\n*.tmp\n!keep.tmp\n", encoding="utf-8")
        pats = load_gitignore_patterns(root)
        assert is_gitignored("anchored.log", pats)
        assert not is_gitignored("sub/anchored.log", pats)
        assert is_gitignored("x.tmp", pats)
        assert not is_gitignored("keep.tmp", pats)

    # --- Secret masking: hashes survive, prefixed tokens are caught ------
    sha = "a" * 64
    assert sha in mask_secrets(f"sha256: {sha}")
    assert "ghp_" not in mask_secrets("token: ghp_abcdefghij0123456789")

    # --- Engine-level regressions ----------------------------------------
    def resp(content=None, tool_calls=None, finish="stop"):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)],
            usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1,
                prompt_tokens_details=None, completion_tokens_details=None))

    def tcall(name, args):
        return SimpleNamespace(
            id="t1", type="function",
            function=SimpleNamespace(name=name, arguments=_json.dumps(args)))

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        async def _create(self, **kw):
            self.calls.append(kw)
            return self.responses.pop(0)

    orig = {k: getattr(engine, k) for k in (
        "AsyncOpenAI", "check_model_price", "check_model_tools",
        "check_budget_limit", "log_run", "print_run_summary")}
    engine.check_model_price = lambda *a, **k: (True, "")
    engine.check_model_tools = lambda *a, **k: (True, "")
    engine.check_budget_limit = lambda *a, **k: (False, 0.0)
    engine.log_run = lambda **kw: {"cost_usd": 0.0}
    engine.print_run_summary = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            def run(client, **kw):
                engine.AsyncOpenAI = lambda **_: client
                args = dict(workspace_dir=tmpdir, model="test/model",
                            api_key="x", mode="edit")
                args.update(kw)
                return asyncio.run(engine.run_botex_task(**args))

            # Non-positive max_turns is clamped — never an UnboundLocalError.
            # One turn means the edit contract's confirm-nudge cannot be
            # answered, so the structured result is MAX_TURNS_REACHED.
            fake = FakeClient([resp("STATUS: DONE\nok")])
            res = run(fake, task="x", max_turns=-5)
            assert res["status"] == "MAX_TURNS_REACHED" and res["steps"] == 1
            assert res["failure_kind"] == "turns_exhausted"

            # Malformed provider response (no choices) -> API_ERROR fast,
            # not three wasted turns of INCOMPLETE/no_progress
            fake = FakeClient([SimpleNamespace(choices=[], usage=None)])
            res = run(fake, task="x")
            assert res["status"] == "API_ERROR"
            assert res["failure_kind"] == "provider_error"
            assert len(fake.calls) == 1

            # Stagnation AFTER a successful write still triggers rollback —
            # the cumulative files_touched check used to disable it forever
            patch_args = {"path": "x.txt", "search_block": "nope",
                          "replace_block": "y"}
            fake = FakeClient(
                [resp(tool_calls=[tcall("create_file", {
                    "path": "x.txt", "content": "hi"})])]
                + [resp(tool_calls=[tcall("apply_patch", patch_args)])
                   for _ in range(4)])
            res = run(fake, task="patch x.txt")
            assert res["status"] == "STAGNANT_ROLLBACK"
            assert res["rollback_verified"] is True
            assert res["files_touched"] == []
            assert not (Path(tmpdir) / "x.txt").exists()

            # verify_command validates a legitimate no-change DONE instead
            # of failing as CONFIG_ERROR
            orig_load_config = engine.load_config
            engine.load_config = lambda reload=False: {
                **orig_load_config(reload),
                "exec": {
                    **orig_load_config().get("exec", {}), "enabled": True,
                },
            }
            try:
                fake = FakeClient([
                    resp("STATUS: DONE\nnothing to change"),
                    resp("STATUS: DONE\nnothing to change"),
                ])
                res = run(fake, task="check version", allow_exec=True,
                          verify_command=f'"{sys.executable}" --version')
                assert res["status"] == "DONE" and res["ok"] is True
            finally:
                engine.load_config = orig_load_config
    finally:
        for k, v in orig.items():
            setattr(engine, k, v)
    print("[PASS] Review Regression Tests")


def test_tool_misuse_and_decisions_guard():
    """Malformed tool calls get schema-echoed feedback; a surrender after
    exclusively failed invocations reports model_tool_misuse (not the
    misleading capability_missing); decisions-only models are rejected
    locally before any API call."""
    import asyncio
    import json as _json
    from types import SimpleNamespace
    import botex.engine as engine
    import botex.config as botex_config

    def resp(content=None, tool_calls=None, finish="stop"):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)],
            usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1,
                prompt_tokens_details=None, completion_tokens_details=None))

    def tcall_raw(name, arguments):
        return SimpleNamespace(
            id="t1", type="function",
            function=SimpleNamespace(name=name, arguments=arguments))

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        async def _create(self, **kw):
            self.calls.append(kw)
            return self.responses.pop(0)

    orig = {k: getattr(engine, k) for k in (
        "AsyncOpenAI", "check_model_price", "check_model_tools",
        "check_budget_limit", "log_run", "print_run_summary")}
    engine.check_model_price = lambda *a, **k: (True, "")
    engine.check_model_tools = lambda *a, **k: (True, "")
    engine.check_budget_limit = lambda *a, **k: (False, 0.0)
    engine.log_run = lambda **kw: {"cost_usd": 0.0}
    engine.print_run_summary = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            def run(client, **kw):
                engine.AsyncOpenAI = lambda **_: client
                args = dict(workspace_dir=tmpdir, model="test/model",
                            api_key="x", mode="edit")
                args.update(kw)
                return asyncio.run(engine.run_botex_task(**args))

            # 1. Bare-string args -> schema-echoed error, run recovers.
            #    (the observed mistral-nemo failure mode)
            fake = FakeClient([
                resp(tool_calls=[tcall_raw("list_dir", '"."')]),
                resp("STATUS: DONE\nnothing found"),
                resp("STATUS: DONE\nnothing found"),
            ])
            res = run(fake, task="list files")
            assert res["status"] == "DONE"
            sent = fake.calls[1]["messages"]
            assert any(
                m.get("role") == "tool"
                and "must be a JSON object" in m["content"]
                and "list_dir(path: string)" in m["content"]
                for m in sent)

            # 2. Unknown tool name -> explicit list of available tools
            fake = FakeClient([
                resp(tool_calls=[tcall_raw("nuke_disk", "{}")]),
                resp("STATUS: DONE\nok"),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="x")
            assert res["status"] == "DONE"
            sent = fake.calls[1]["messages"]
            assert any(
                m.get("role") == "tool" and "Unknown tool" in m["content"]
                and "list_dir" in m["content"]
                for m in sent)

            # 3. Surrender after a failed invocation -> model_tool_misuse
            fake = FakeClient([
                resp(tool_calls=[tcall_raw("list_dir", '"."')]),
                resp("STATUS: UNSUPPORTED\nthe list_dir tool is broken"),
            ])
            res = run(fake, task="list files")
            assert res["status"] == "UNSUPPORTED"
            assert res["failure_kind"] == "model_tool_misuse"

            # 4. Surrender with zero tool calls -> still capability_missing
            fake = FakeClient([
                resp("STATUS: UNSUPPORTED\nneeds shell access"),
            ])
            res = run(fake, task="run a shell command")
            assert res["status"] == "UNSUPPORTED"
            assert res["failure_kind"] == "capability_missing"

            # 5. Idle surrender after a failed call -> INCOMPLETE + misuse
            fake = FakeClient([
                resp(tool_calls=[tcall_raw("list_dir", '"."')]),
                resp("thinking aloud"), resp("still thinking"),
                resp("giving up in prose"),
            ])
            res = run(fake, task="x")
            assert res["status"] == "INCOMPLETE"
            assert res["failure_kind"] == "model_tool_misuse"

            # 6. A successful call before the surrender keeps refusal kinds
            fake = FakeClient([
                resp(tool_calls=[tcall_raw("list_dir", '{"path": "."}')]),
                resp("thinking aloud"), resp("still thinking"),
                resp("giving up in prose"),
            ])
            res = run(fake, task="x")
            assert res["status"] == "INCOMPLETE"
            assert res["failure_kind"] == "no_progress"

            # 7. Declared decisions model -> local UNSUPPORTED, zero API calls
            orig_cfg_load = botex_config.load_config
            cfg = _json.loads(_json.dumps(orig_cfg_load()))
            cfg["providers"]["openrouter"]["decisions_models"] = [
                "typesafe/jev-*"]
            botex_config.load_config = lambda reload=False: cfg
            try:
                fake = FakeClient([])
                res = run(fake, model="typesafe/jev-1.13", task="analyze")
                assert res["status"] == "UNSUPPORTED"
                assert res["failure_kind"] == "capability_missing"
                assert "decisions" in res["message"].lower()
                assert not fake.calls
            finally:
                botex_config.load_config = orig_cfg_load
    finally:
        for k, v in orig.items():
            setattr(engine, k, v)

    # --- config-level matcher --------------------------------------------
    assert botex_config.is_decisions_model(
        "typesafe/jev-1.13", {"decisions_models": ["typesafe/jev-*"]})
    assert botex_config.is_decisions_model(
        "~typesafe/jev-latest", {"decisions_models": ["~typesafe/jev-*"]})
    assert botex_config.is_decisions_model(
        "typesafe/jev-1.13", {"decisions_models": "typesafe/jev-1.13"})
    assert not botex_config.is_decisions_model(
        "openrouter/auto", {"decisions_models": ["typesafe/jev-*"]})
    assert not botex_config.is_decisions_model("x", {})
    assert not botex_config.is_decisions_model("x", None)
    print("[PASS] Tool Misuse & Decisions Guard Tests")


def test_read_before_write_gate():
    """Read-before-write gate: apply_patch and create_file (on existing files)
    require prior inspection via outline/read tools, while new files do not."""
    import asyncio
    import json as _json
    from types import SimpleNamespace
    import botex.engine as engine

    def resp(content=None, tool_calls=None, finish="stop"):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)],
            usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1,
                prompt_tokens_details=None, completion_tokens_details=None))

    def tcall(name, args):
        return SimpleNamespace(
            id="t1", type="function",
            function=SimpleNamespace(name=name, arguments=_json.dumps(args)))

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        async def _create(self, **kw):
            self.calls.append(kw)
            return self.responses.pop(0)

    orig = {k: getattr(engine, k) for k in (
        "AsyncOpenAI", "check_model_price", "check_model_tools",
        "check_budget_limit", "log_run", "print_run_summary")}
    engine.check_model_price = lambda *a, **k: (True, "")
    engine.check_model_tools = lambda *a, **k: (True, "")
    engine.check_budget_limit = lambda *a, **k: (False, 0.0)
    engine.log_run = lambda **kw: {"cost_usd": 0.0}
    engine.print_run_summary = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "existing.py").write_text("def hello():\n    return 1\n", encoding="utf-8")

            def run(client, **kw):
                engine.AsyncOpenAI = lambda **_: client
                args = dict(workspace_dir=tmpdir, model="test/model",
                            api_key="x", mode="edit")
                args.update(kw)
                return asyncio.run(engine.run_botex_task(**args))

            # 1. Blind apply_patch is rejected
            fake = FakeClient([
                resp(tool_calls=[tcall("apply_patch", {
                    "path": "existing.py",
                    "search_block": "return 1",
                    "replace_block": "return 2"
                })]),
                resp("STATUS: DONE\nok"),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="patch existing.py")
            sent = fake.calls[1]["messages"]
            assert any(m.get("role") == "tool" and "rejected — file not read this task" in m["content"] for m in sent)

            # 2. Outline then apply_patch succeeds
            fake = FakeClient([
                resp(tool_calls=[tcall("get_file_outline", {"path": "existing.py"})]),
                resp(tool_calls=[tcall("apply_patch", {
                    "path": "existing.py",
                    "search_block": "return 1",
                    "replace_block": "return 2"
                })]),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="patch existing.py")
            assert res["status"] == "DONE" and res["ok"] is True
            assert "return 2" in (root / "existing.py").read_text()

            # 3. create_file on existing file is rejected without prior read
            fake = FakeClient([
                resp(tool_calls=[tcall("create_file", {
                    "path": "existing.py",
                    "content": "def hello():\n    return 3\n"
                })]),
                resp("STATUS: DONE\nok"),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="overwrite existing.py")
            sent = fake.calls[1]["messages"]
            assert any(m.get("role") == "tool" and "rejected — file not read this task" in m["content"] for m in sent)

            # 4. create_file on a BRAND NEW non-existent file is allowed without read
            fake = FakeClient([
                resp(tool_calls=[tcall("create_file", {
                    "path": "brand_new.py",
                    "content": "def new_fn():\n    pass\n"
                })]),
                resp("STATUS: DONE\ncreated"),
            ])
            res = run(fake, task="create brand_new.py")
            assert res["status"] == "DONE" and res["ok"] is True
            assert (root / "brand_new.py").exists()

            # 5. When require_read_before_write is disabled, blind write succeeds
            fake = FakeClient([
                resp(tool_calls=[tcall("create_file", {
                    "path": "existing.py",
                    "content": "def hello():\n    return 4\n"
                })]),
                resp("STATUS: DONE\noverwritten"),
            ])
            res = run(fake, task="overwrite", require_read_before_write=False)
            assert res["status"] == "DONE" and res["ok"] is True
            assert "return 4" in (root / "existing.py").read_text()

            # 6. Path traversal in read tool does NOT bypass read-before-write gate for workspace files
            fake = FakeClient([
                resp(tool_calls=[tcall("get_file_outline", {"path": "../existing.py"})]),
                resp(tool_calls=[tcall("apply_patch", {
                    "path": "existing.py",
                    "search_block": "return 4",
                    "replace_block": "return 5"
                })]),
                resp("STATUS: DONE\nok"),
                resp("STATUS: DONE\nok"),
            ])
            res = run(fake, task="try bypass")
            sent = fake.calls[2]["messages"]
            assert any(m.get("role") == "tool" and "rejected — file not read this task" in m["content"] for m in sent)
    finally:
        for k, v in orig.items():
            setattr(engine, k, v)
    print("[PASS] Read-Before-Write Gate Tests")


def test_workspace_memory_vault():
    """Workspace Memory Vault: save, search, read, secret masking, gitignore protection, path traversal, size limits, and robust frontmatter."""
    import tempfile
    from pathlib import Path
    from botex import memory

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # 1. Save memory entry
        res = memory.save_memory(
            workspace_dir=tmpdir,
            title="Database Migration Plan: Key: Value [v2]",
            content="Use PostgreSQL partitioned tables. Secret: sk-or-v1-abcdef1234567890abcdef1234567890",
            kind="decision",
            tags=["database:sql", "architecture", "v2"],
        )
        assert res["ok"] is True
        mem_id = res["id"]
        assert mem_id.startswith("mem_")
        assert (root / ".botex" / "memory" / f"{mem_id}.md").exists()
        assert (root / ".botex" / ".gitignore").exists()

        # Check content and frontmatter
        raw_file = (root / ".botex" / "memory" / f"{mem_id}.md").read_text(encoding="utf-8")
        assert "schema: ecc.memory.v1" in raw_file
        assert "kind: decision" in raw_file
        assert "sk-or-v1" not in raw_file
        assert "[REDACTED]" in raw_file

        # 2. Save second memory entry
        res2 = memory.save_memory(
            workspace_dir=tmpdir,
            title="Authentication Notes",
            content="JWT tokens with RSA signatures.",
            kind="context",
            tags=["auth", "security"],
        )
        assert res2["ok"] is True
        mem_id2 = res2["id"]

        # 3. Read memory
        read_res = memory.read_memory(tmpdir, mem_id)
        assert read_res["ok"] is True
        assert read_res["id"] == mem_id
        assert read_res["metadata"]["title"] == "Database Migration Plan: Key: Value [v2]"
        assert read_res["metadata"]["kind"] == "decision"
        assert "PostgreSQL" in read_res["content"]

        # Read non-existent memory
        assert memory.read_memory(tmpdir, "mem_nonexistent")["ok"] is False

        # 4. Search memory
        # By tag
        search_tag = memory.search_memory(tmpdir, tag="architecture")
        assert search_tag["ok"] is True and search_tag["count"] == 1
        assert search_tag["memories"][0]["id"] == mem_id

        # By kind
        search_kind = memory.search_memory(tmpdir, kind="context")
        assert search_kind["ok"] is True and search_kind["count"] == 1
        assert search_kind["memories"][0]["id"] == mem_id2

        # By query
        search_q = memory.search_memory(tmpdir, query="JWT")
        assert search_q["ok"] is True and search_q["count"] == 1
        assert search_q["memories"][0]["id"] == mem_id2

        # 5. Path traversal protection
        bad_save = memory.save_memory(tmpdir, "evil", "content", memory_id="../../evil")
        assert bad_save["ok"] is False
        assert not (root.parent / "evil.md").exists()

        bad_read = memory.read_memory(tmpdir, "../../evil")
        assert bad_read["ok"] is False

        # 6. Size limit protection
        huge_content = "A" * (memory.MAX_MEMORY_BYTES + 100)
        huge_save = memory.save_memory(tmpdir, "Too Large", huge_content)
        assert huge_save["ok"] is False
        assert "exceeds maximum allowed size" in huge_save["error"]
    print("[PASS] Workspace Memory Vault Tests")


def test_recipes_and_rationalization_heuristics():
    """Recipes registry and delivery-gate rationalization heuristics."""
    import asyncio
    import json as _json
    from types import SimpleNamespace
    import botex.engine as engine
    import botex.recipes as recipes

    # 1. Recipes registry
    avail = recipes.list_recipes()
    assert set(avail) == {"build-resolver", "code-explorer", "planner", "reviewer", "security-reviewer", "tdd"}
    for name in avail:
        content = recipes.load_recipe(name)
        assert content and len(content) > 50
    assert recipes.load_recipe("nonexistent_recipe") is None
    assert recipes.load_recipe("../evil") is None

    # 2. Engine integration: valid recipe
    def resp(content=None, tool_calls=None, finish="stop"):
        msg = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=msg, finish_reason=finish)],
            usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1,
                prompt_tokens_details=None, completion_tokens_details=None))

    class FakeClient:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        async def _create(self, **kw):
            self.calls.append(kw)
            return self.responses.pop(0)

    orig = {k: getattr(engine, k) for k in (
        "AsyncOpenAI", "check_model_price", "check_model_tools",
        "check_budget_limit", "log_run", "print_run_summary")}
    engine.check_model_price = lambda *a, **k: (True, "")
    engine.check_model_tools = lambda *a, **k: (True, "")
    engine.check_budget_limit = lambda *a, **k: (False, 0.0)
    engine.log_run = lambda **kw: {"cost_usd": 0.0}
    engine.print_run_summary = lambda *a, **k: None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            def run(client, **kw):
                engine.AsyncOpenAI = lambda **_: client
                args = dict(workspace_dir=tmpdir, model="test/model",
                            api_key="x", mode="readonly")
                args.update(kw)
                return asyncio.run(engine.run_botex_task(**args))

            fake = FakeClient([resp("STATUS: DONE\nplan completed")])
            res = run(fake, task="plan architecture", recipe="planner")
            assert res["status"] == "DONE" and res["ok"] is True
            sys_msg = fake.calls[0]["messages"][0]["content"][0]["text"]
            assert "ACTIVE RECIPE / PERSONA (planner)" in sys_msg
            assert "Planner" in sys_msg

            # 3. Engine rejection on unknown recipe
            fake_err = FakeClient([])
            res_err = run(fake_err, task="plan", recipe="unknown_xyz")
            assert res_err["status"] == "CONFIG_ERROR"
            assert "Unknown recipe 'unknown_xyz'" in res_err["message"]
            assert not fake_err.calls

            # 4. Rationalization rejection: repeated unevidenced claim fails as contract_failed
            def run_edit(client, **kw):
                engine.AsyncOpenAI = lambda **_: client
                args = dict(workspace_dir=tmpdir, model="test/model",
                            api_key="x", mode="edit")
                args.update(kw)
                return asyncio.run(engine.run_botex_task(**args))

            fake_rat = FakeClient([
                resp("STATUS: DONE\nI have modified the file and completed the task."),
                resp("STATUS: DONE\nI have modified the file and completed the task."),
            ])
            res_rat = run_edit(fake_rat, task="modify file")
            assert res_rat["status"] == "INCOMPLETE"
            assert res_rat["failure_kind"] == "contract_failed"
            assert "rationalization contract failure" in res_rat["message"]
            sent_msgs = fake_rat.calls[1]["messages"]
            assert any(
                m.get("role") == "user" and "Do not rationalize or claim success without evidence" in m["content"]
                for m in sent_msgs
            )

            # 5. Legitimate explanation of no-change needed after nudge succeeds
            fake_legit = FakeClient([
                resp("STATUS: DONE\nI checked the files and finished."),
                resp("STATUS: DONE\nThe code in auth.py already handles tokens properly, so no modifications were required."),
            ])
            res_legit = run_edit(fake_legit, task="check auth")
            assert res_legit["status"] == "DONE" and res_legit["ok"] is True
    finally:
        for k, v in orig.items():
            setattr(engine, k, v)
    print("[PASS] Recipes & Rationalization Heuristics Tests")


def test_mcp_memory_and_stats():
    """MCP interface tests for memory tools and stats breakdown."""
    import asyncio
    from unittest.mock import patch
    import server

    async def exercise():
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. MCP tool: save_memory
            save_res = await server.save_memory(
                title="Architecture Note",
                content="Modular harness architecture.",
                workspace_dir=tmpdir,
                kind="context",
                tags=["arch", "mcp"],
            )
            assert save_res["ok"] is True
            mid = save_res["id"]

            # 2. MCP tool: search_memory
            search_res = await server.search_memory(
                workspace_dir=tmpdir,
                query="Modular",
            )
            assert search_res["ok"] is True and search_res["count"] == 1
            assert search_res["memories"][0]["id"] == mid

            # 3. MCP tool: read_memory
            read_res = await server.read_memory(
                memory_id=mid,
                workspace_dir=tmpdir,
            )
            assert read_res["ok"] is True
            assert read_res["metadata"]["title"] == "Architecture Note"

            # 4. MCP tool: get_stats formatting with mocked analytics records
            synthetic_records = [
                {
                    "task_id": "t1",
                    "timestamp": "2099-01-01T00:00:00",
                    "model": "openrouter/auto",
                    "status": "DONE",
                    "cost_usd": 0.001,
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "cached_tokens": 0,
                    "lines_added": 5,
                    "lines_removed": 2,
                }
            ]
            with patch("server.load_analytics", return_value=synthetic_records):
                stats_str = await server.get_stats("month")
                assert "BOTEX ANALYTICS" in stats_str
                assert "openrouter/auto" in stats_str
                assert "DONE rate: 100.0%" in stats_str
    asyncio.run(exercise())
    print("[PASS] MCP Memory & Stats Tests")


def test_mcp_tool_surface():
    """Offline coverage of the remaining MCP tools (health, snapshots,
    outline, fetch_url, recommend_models) plus a guard that the published
    tool set matches the documented one."""
    import asyncio
    import io
    import json as _json
    from unittest.mock import patch
    import server
    from botex.patch_engine import SnapshotManager

    expected = {
        "run_subagent", "start_task", "get_task_status", "fetch_url",
        "recommend_models", "get_outline", "save_memory", "search_memory",
        "read_memory", "get_stats", "check_health", "clean_snapshots",
    }

    catalog = {"data": [
        {"id": "vendor/cheap-coder", "context_length": 128000,
         "pricing": {"prompt": "0.0000001", "completion": "0.0000002"}},
        {"id": "vendor/pricey-coder", "context_length": 128000,
         "pricing": {"prompt": "0.00001", "completion": "0.00002"}},
        {"id": "vendor/zero-coder:free", "context_length": 128000,
         "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "vendor/tiny-coder", "context_length": 4000,
         "pricing": {"prompt": "0", "completion": "0"}},
    ]}

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    async def exercise():
        tools = await server.mcp.list_tools()
        assert {t.name for t in tools} == expected, sorted(t.name for t in tools)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snaps = SnapshotManager(root / ".snapshots")
            with patch.object(server, "snapshot_manager", snaps):
                health = await server.check_health()
                assert "HEALTH CHECK" in health and "openrouter" in health
                cleaned = await server.clean_snapshots(max_age_days=0, max_total_mb=0)
                assert cleaned.startswith("[OK]"), cleaned

            (root / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            outline = await server.get_outline("app.py", workspace_dir=tmpdir)
            assert outline.get("ok") is True and "main" in str(outline), outline

        calls = []

        async def fake_fetch(url, *, net_cfg, max_bytes=None):
            calls.append((url, net_cfg, max_bytes))
            return {"ok": True, "url": url}

        with patch.object(server, "_fetch_url", fake_fetch):
            assert (await server.fetch_url("https://example.com/"))["ok"]
            await server.fetch_url("https://example.com/", max_bytes=500)
        assert calls[0][2] is None and calls[1][2] == 500
        assert isinstance(calls[0][1], dict) and "policy" in calls[0][1]

        payload = _json.dumps(catalog).encode("utf-8")
        with patch("botex.benchmarks.urllib.request.urlopen",
                   lambda *a, **k: FakeResponse(payload)):
            recs = _json.loads(await server.recommend_models("coding", 5))
        ids = [r["id"] for r in recs]
        assert ids == ["vendor/cheap-coder", "vendor/pricey-coder"], ids

        def offline(*a, **k):
            raise OSError("offline")
        with patch("botex.benchmarks.urllib.request.urlopen", offline):
            recs = _json.loads(await server.recommend_models("coding", 5))
        assert "error" in recs[0]

    asyncio.run(exercise())
    print("[PASS] MCP Tool Surface Tests")


if __name__ == "__main__":
    print("Running BoteX Test Suite...")
    test_security()
    test_pre_write_syntax()
    test_fuzzy_patching_and_rollback()
    test_outline_and_reading()
    test_destructive_ops_and_rollback()
    test_capability_modes()
    test_exec_policy()
    test_pricing_guardrail()
    test_zdr_guardrail()
    test_net_guards_and_created_file_rollback()
    test_fetch_url_policy()
    test_fallback_and_verify_guards()
    test_async_task_registry()
    test_engine_write_guards()
    test_provider_adapter()
    test_analytics_record_fields()
    test_mcp_structured_output()
    test_review_regressions()
    test_tool_misuse_and_decisions_guard()
    test_read_before_write_gate()
    test_workspace_memory_vault()
    test_recipes_and_rationalization_heuristics()
    test_mcp_memory_and_stats()
    test_mcp_tool_surface()
    print("\n[SUCCESS] ALL BOTEX ENGINE TESTS PASSED!")
