# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""
BoteX Live OpenRouter Integration & E2E Test Suite
==================================================
Runs real live API requests against OpenRouter using the dedicated test key:
    `OPENROUTER_API_TESTS_KEY`

Safety & Isolation:
- Strictly ignores and does NOT use the production `OPENROUTER_API_KEY`.
- Uses a dedicated in-memory test provider (`openrouter_test`) with ZDR disabled
  specifically to allow free-tier models (e.g. `:free`) without cost.
- Automatically skipped if `OPENROUTER_API_TESTS_KEY` is not set.
- All file operations run in isolated temporary workspace directories.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from dotenv import load_dotenv

# Ensure root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env if present
load_dotenv()
_project_env = Path(__file__).resolve().parent.parent / ".env"
if _project_env.exists():
    load_dotenv(dotenv_path=_project_env)

TEST_KEY_ENV = "OPENROUTER_API_TESTS_KEY"
TEST_API_KEY = os.environ.get(TEST_KEY_ENV, "").strip()

# Default test model (free-tier or user-configured test model).
# `openrouter/free` is a meta-router that picks an available free model,
# so it survives individual :free models leaving the free tier.
_explicit_model = os.environ.get("BOTEX_TEST_MODEL", "").strip()
DEFAULT_TEST_MODEL = _explicit_model or "openrouter/free"

# Candidate models tried in order when BOTEX_TEST_MODEL is not set explicitly.
# All are $0.00/M tokens with tool-calling support per the OpenRouter catalog.
MODEL_CANDIDATES = (
    [_explicit_model] if _explicit_model else [
        "openrouter/free",
        "qwen/qwen3.8-27b:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "google/gemma-4-31b-it:free",
    ]
)

ACTIVE_MODEL = DEFAULT_TEST_MODEL

TEST_PROVIDER_NAME = "openrouter_test"
TEST_PROVIDER_CFG = {
    "default": False,
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": TEST_KEY_ENV,
    "extra_body": {},
    "zdr": {
        "enabled": False,
        "deny_patterns": [],
    },
    "models": {
        "default": "openrouter/free",
        "coding": "openrouter/free",
        "fast": "openrouter/free",
    },
    "decisions_models": [],
}


def _setup_test_config():
    """Inject test provider configuration into botex.config."""
    import botex.config as cfg_module
    import botex.engine as engine_module

    orig_load_config = cfg_module.load_config

    def patched_load_config(reload: bool = False):
        cfg = orig_load_config(reload)
        cfg_copy = dict(cfg)
        providers = dict(cfg_copy.get("providers", {}))
        providers[TEST_PROVIDER_NAME] = TEST_PROVIDER_CFG
        cfg_copy["providers"] = providers
        pricing = dict(cfg_copy.get("pricing", {}))
        pricing["enabled"] = False  # disable pricing guard for test provider
        cfg_copy["pricing"] = pricing
        # exec.enabled is a config master switch — enable it ONLY inside this
        # test process so the live suite can exercise run_command. The real
        # botex.config.json is never touched.
        exec_cfg = dict(cfg_copy.get("exec", {}))
        exec_cfg["enabled"] = True
        cfg_copy["exec"] = exec_cfg
        return cfg_copy

    cfg_module.load_config = patched_load_config
    engine_module.load_config = patched_load_config
    # server.py binds `load_config` via `from ... import` — patch its module
    # reference too so MCP tools see the same test config.
    try:
        import server as server_module
        server_module.load_config = patched_load_config
    except ImportError:
        pass
    return orig_load_config


async def run_live_readonly_recon():
    """Test 1: Read-only codebase reconnaissance (get_file_outline / read_file_lines -> STATUS: DONE)."""
    from botex.engine import run_botex_task

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        sample_file = root / "calculator.py"
        sample_file.write_text(
            "class Calculator:\n"
            "    def add(self, a, b):\n"
            "        return a + b\n\n"
            "    def subtract(self, a, b):\n"
            "        return a - b\n",
            encoding="utf-8",
        )

        print(f"\n[E2E] Running Read-Only Reconnaissance with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task="Inspect calculator.py and explain what methods the Calculator class provides.",
            files=["calculator.py"],
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="readonly",
            max_turns=6,
        )

        print(f"[E2E] Recon Result Status: {res.get('status')}")
        if not res.get("ok"):
            print(f"[E2E] Warning / Error detail: {res.get('message') or res.get('summary')}")
        return res


def _assert_recon(res: dict):
    assert res["ok"] is True, f"Recon task failed: {res}"
    assert res["status"] == "DONE"
    assert len(res.get("files_touched", [])) == 0, "Readonly mode must not modify files"
    summary = (res.get("summary") or "").lower()
    assert "add" in summary or "subtract" in summary or "calculator" in summary
    print("  -> Read-only reconnaissance verified successfully!")


def _is_model_unavailable(res: dict) -> bool:
    """True when the failure is provider-side model unavailability (404 / no endpoints),
    so the suite can fall back to the next candidate model."""
    if res.get("ok"):
        return False
    if res.get("status") != "API_ERROR":
        return False
    detail = str(res.get("message") or res.get("summary") or "").lower()
    return any(tok in detail for tok in ("404", "unavailable", "no endpoints", "not found"))


# Scenarios skipped because the provider rate limit was hit (free-tier daily
# quota is account-wide — retrying other :free candidates cannot help).
RATE_LIMITED: list[str] = []


def _is_rate_limited_text(detail: str) -> bool:
    d = str(detail).lower()
    return "429" in d or "rate limit" in d or "too many requests" in d


def _is_rate_limited(res: dict) -> bool:
    if res.get("ok"):
        return False
    return _is_rate_limited_text(res.get("message") or res.get("summary") or "")


def _drive(fn, label: str):
    """Run one scenario against MODEL_CANDIDATES until one succeeds.

    Free-tier models vary in tool-calling reliability — a model that handles
    recon may still fumble exec/net prompts. Any scenario failure (assertion
    or engine error) falls back to the next candidate; the last error is
    re-raised when every candidate fails. A provider rate limit (429) is an
    environmental condition, not a harness failure — the scenario is skipped
    and reported instead of failing the suite.
    """
    global ACTIVE_MODEL
    last_exc: Exception | None = None
    for candidate in MODEL_CANDIDATES:
        ACTIVE_MODEL = candidate
        try:
            asyncio.run(fn())
            return
        except Exception as exc:  # AssertionError or engine-level failure
            if _is_rate_limited_text(exc):
                print(f"[SKIP] {label}: provider rate limit reached (free daily quota).")
                RATE_LIMITED.append(label)
                return
            last_exc = exc
            print(f"[WARN] {label} failed on '{candidate}': {str(exc)[:200]}")
            if len(MODEL_CANDIDATES) > 1:
                print("       trying next candidate model...")
    raise last_exc


async def run_live_read_before_write_patch():
    """Test 2: Read-Before-Write gate + apply_patch modification."""
    from botex.engine import run_botex_task

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        math_file = root / "math_utils.py"
        math_file.write_text(
            "def multiply(a, b):\n"
            "    # BUG: addition instead of multiplication\n"
            "    return a + b\n",
            encoding="utf-8",
        )

        print(f"\n[E2E] Running Read-Before-Write Patch with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task="Fix the bug in math_utils.py so that multiply(a, b) performs multiplication (a * b).",
            files=["math_utils.py"],
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="edit",
            max_turns=8,
        )

        print(f"[E2E] Patch Result Status: {res.get('status')}")
        if not res.get("ok"):
            print(f"[E2E] Warning / Error detail: {res.get('message') or res.get('summary')}")
        assert res["ok"] is True, f"Patch task failed: {res}"
        assert res["status"] == "DONE"

        # Verify on-disk file was actually modified and syntax is valid
        content = math_file.read_text(encoding="utf-8")
        assert "a * b" in content or "a*b" in content, f"Expected fix 'a * b' in file, got: {content}"
        print("  -> Read-before-write patch verified successfully on disk!")


async def run_live_recipe_explorer():
    """Test 3: Live execution with recipe persona injection (recipe='code-explorer')."""
    from botex.engine import run_botex_task

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        mod_file = root / "config_loader.py"
        mod_file.write_text(
            "import os\n\n"
            "def load_app_config():\n"
            "    return {'host': os.getenv('APP_HOST', 'localhost'), 'port': 8080}\n",
            encoding="utf-8",
        )

        print(f"\n[E2E] Running Recipe 'code-explorer' with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task="Analyze config_loader.py architecture and configuration keys.",
            files=["config_loader.py"],
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="readonly",
            recipe="code-explorer",
            max_turns=6,
        )

        print(f"[E2E] Recipe Result Status: {res.get('status')}")
        assert res["ok"] is True, f"Recipe explorer failed: {res}"
        assert res["status"] == "DONE"
        print("  -> Recipe persona execution verified successfully!")


async def run_live_create_file():
    """Test 4: list_dir + create_file — new files need no prior read."""
    from botex.engine import run_botex_task

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\n[E2E] Running list_dir + create_file with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task=(
                "Use list_dir to inspect the workspace root, then use create_file "
                "to create greeting.py containing a function greet(name) that "
                "returns the string 'Hello, <name>!'."
            ),
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="edit",
            max_turns=8,
        )

        print(f"[E2E] create_file Result Status: {res.get('status')}")
        if not res.get("ok"):
            print(f"[E2E] Warning / Error detail: {res.get('message') or res.get('summary')}")
        assert res["ok"] is True, f"create_file task failed: {res}"
        assert res["status"] == "DONE"
        created = Path(tmpdir) / "greeting.py"
        assert created.exists(), "greeting.py was not created on disk"
        content = created.read_text(encoding="utf-8")
        assert "def greet" in content, f"Expected greet() in greeting.py, got: {content}"
        print("  -> list_dir + create_file verified on disk!")


async def run_live_destructive_ops():
    """Test 5: destructive mode — move_file + delete_file."""
    from botex.engine import run_botex_task

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "draft.txt").write_text("Draft notes\n", encoding="utf-8")
        (root / "obsolete.tmp").write_text("junk\n", encoding="utf-8")

        print(f"\n[E2E] Running destructive ops (move_file + delete_file) with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task=(
                "Rename draft.txt to final.txt using move_file, then delete "
                "obsolete.tmp using delete_file."
            ),
            files=["draft.txt", "obsolete.tmp"],
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="destructive",
            allow_destructive=True,
            max_turns=10,
        )

        print(f"[E2E] Destructive Result Status: {res.get('status')}")
        if not res.get("ok"):
            print(f"[E2E] Warning / Error detail: {res.get('message') or res.get('summary')}")
        assert res["ok"] is True, f"Destructive task failed: {res}"
        assert res["status"] == "DONE"
        assert (root / "final.txt").exists(), "final.txt was not created by move_file"
        assert not (root / "draft.txt").exists(), "draft.txt still exists after move_file"
        assert not (root / "obsolete.tmp").exists(), "obsolete.tmp still exists after delete_file"
        print("  -> move_file + delete_file verified on disk!")


async def run_live_exec_command():
    """Test 6: run_command in full mode (exec.enabled patched True for this process)."""
    from botex.engine import run_botex_task

    py_bin = "python" if shutil.which("python") else "py"
    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\n[E2E] Running run_command (exec) with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task=(
                f"Use run_command to execute `{py_bin} --version` and report "
                "the version string it prints."
            ),
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="full",
            allow_exec=True,
            max_turns=6,
        )

        print(f"[E2E] Exec Result Status: {res.get('status')}")
        if not res.get("ok"):
            print(f"[E2E] Warning / Error detail: {res.get('message') or res.get('summary')}")
        assert res["ok"] is True, f"run_command task failed: {res}"
        assert res["status"] == "DONE"
        assert res.get("exec_ran") is True, "exec_ran should be True after run_command"
        summary = (res.get("summary") or "").lower()
        assert "python" in summary or any(c.isdigit() for c in summary), (
            f"Expected version output in summary, got: {summary}"
        )
        print("  -> run_command executed and reported real output!")


async def run_live_read_url():
    """Test 7: read_url — per-run net authorization (policy 'caller')."""
    from botex.engine import run_botex_task

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\n[E2E] Running read_url (net) with model: {ACTIVE_MODEL}...")
        res = await run_botex_task(
            task=(
                "Use read_url to fetch https://example.com/ and report the "
                "page's <title> content."
            ),
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="readonly",
            allow_net=True,
            net_allowed_hosts=["example.com"],
            max_turns=6,
        )

        print(f"[E2E] read_url Result Status: {res.get('status')}")
        if not res.get("ok"):
            print(f"[E2E] Warning / Error detail: {res.get('message') or res.get('summary')}")
        assert res["ok"] is True, f"read_url task failed: {res}"
        assert res["status"] == "DONE"
        summary = (res.get("summary") or "").lower()
        assert "example" in summary, f"Expected 'Example Domain' title in summary, got: {summary}"
        print("  -> read_url fetched a live URL with per-run net authorization!")


async def run_mcp_surface_test():
    """Test 8: exercise every tool exposed on the MCP server surface.

    Covers all 12 MCP tools end-to-end: deterministic ones directly, and
    start_task/get_task_status/run_subagent as real live engine runs through
    the MCP boundary.
    """
    import server

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "app.py").write_text(
            "def main():\n    print('mcp surface')\n",
            encoding="utf-8",
        )

        print("\n[MCP] Exercising full MCP tool surface...")

        # --- deterministic tools ---
        health = await server.check_health()
        assert "BOTEX" in health.upper(), f"check_health: {health}"
        print("  [OK] check_health")

        outline = await server.get_outline("app.py", workspace_dir=tmpdir)
        assert outline.get("ok") is True, f"get_outline: {outline}"
        print("  [OK] get_outline")

        saved = await server.save_memory(
            title="E2E live note",
            content="Live MCP memory round-trip entry.",
            workspace_dir=tmpdir,
            kind="context",
            tags=["e2e", "live"],
        )
        assert saved.get("ok") is True, f"save_memory: {saved}"
        mid = saved["id"]
        print("  [OK] save_memory")

        hits = await server.search_memory(workspace_dir=tmpdir, query="round-trip")
        assert hits.get("ok") is True and hits.get("count", 0) >= 1, f"search_memory: {hits}"
        print("  [OK] search_memory")

        entry = await server.read_memory(memory_id=mid, workspace_dir=tmpdir)
        assert entry.get("ok") is True and "round-trip" in (entry.get("content") or ""), (
            f"read_memory: {entry}"
        )
        print("  [OK] read_memory")

        stats = await server.get_stats("week")
        assert isinstance(stats, str) and stats.strip(), f"get_stats: {stats}"
        print("  [OK] get_stats")

        rec_raw = await server.recommend_models("coding", 3)
        parsed = json.loads(rec_raw)
        assert parsed, f"recommend_models returned empty: {rec_raw}"
        print("  [OK] recommend_models")

        snap = await server.clean_snapshots()
        assert "[OK]" in snap, f"clean_snapshots: {snap}"
        print("  [OK] clean_snapshots")

        # fetch_url: net.policy='caller' gives the MCP fetch public scope
        fr = await server.fetch_url("https://example.com/", max_bytes=4000)
        assert fr.get("ok") is True, f"fetch_url: {fr}"
        print("  [OK] fetch_url")

        # --- live engine runs through the MCP boundary ---
        started = await server.start_task(
            task="Read app.py and report what calling main() prints.",
            files=["app.py"],
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="readonly",
            max_turns=6,
        )
        assert started.get("task_id"), f"start_task: {started}"
        tid = started["task_id"]
        print(f"  [OK] start_task (task_id={tid})")

        st = {}
        for _ in range(120):
            await asyncio.sleep(5)
            st = await server.get_task_status(tid)
            if st.get("status") in {"DONE", "FAILED", "CANCELLED"}:
                break
        assert st.get("status") == "DONE", f"get_task_status: {st}"
        rd = st.get("result_data") or {}
        if _is_rate_limited(rd):
            print("  [SKIP] get_task_status result: provider rate limit reached.")
            RATE_LIMITED.append("mcp start_task/get_task_status")
            return
        assert rd.get("ok") is True and rd.get("status") == "DONE", f"task result: {rd}"
        print("  [OK] get_task_status (live task DONE)")

        sub = await server.run_subagent(
            task="Create note.txt containing exactly the text 'mcp ok'.",
            workspace_dir=tmpdir,
            model=ACTIVE_MODEL,
            provider=TEST_PROVIDER_NAME,
            api_key=TEST_API_KEY,
            mode="edit",
            max_turns=8,
        )
        sc = sub.structured_content or {}
        if _is_rate_limited(sc):
            print("  [SKIP] run_subagent: provider rate limit reached.")
            RATE_LIMITED.append("mcp run_subagent")
            return
        assert sc.get("ok") is True, f"run_subagent: {sc}"
        assert (root / "note.txt").exists(), "note.txt not created via run_subagent"
        print("  [OK] run_subagent (live task DONE)")

        print("  -> Full MCP surface (12/12 tools) verified!")


def main():
    print("=" * 60)
    print("BoteX Live OpenRouter Integration & E2E Test Suite")
    print("=" * 60)

    if not TEST_API_KEY:
        print(f"\n[SKIP] Environment variable '{TEST_KEY_ENV}' is not set or empty.")
        print("To run live E2E tests with free/low-cost models on OpenRouter:")
        print(f"  1. Set {TEST_KEY_ENV}=sk-or-v1-... in your environment or .env")
        print("  2. (Optional) Set BOTEX_TEST_MODEL=qwen/qwen3.8-27b:free")
        print("  3. Run: py tests/test_live_e2e.py\n")
        sys.exit(0)

    print(f"[INFO] Using test key from '{TEST_KEY_ENV}'")
    print(f"[INFO] Candidate models: {', '.join(MODEL_CANDIDATES)}")
    print("[INFO] Zero Data Retention (ZDR) disabled specifically for test provider to allow free-tier models.")

    _setup_test_config()

    try:
        # Probe candidates with the recon test; first available model is reused
        # for the remaining scenarios.
        global ACTIVE_MODEL
        res = None
        for candidate in MODEL_CANDIDATES:
            ACTIVE_MODEL = candidate
            res = asyncio.run(run_live_readonly_recon())
            if res.get("ok"):
                break
            if _is_rate_limited(res):
                print("\n[SKIP] Provider rate limit reached (free daily quota) — "
                      "live suite cannot run right now.")
                print("       Wait for the daily reset or set BOTEX_TEST_MODEL to a paid model.")
                sys.exit(0)
            if not _is_model_unavailable(res):
                break  # real failure, not model availability — report it
            print(f"[WARN] Model '{candidate}' unavailable on free tier; trying next candidate...")
        _assert_recon(res)

        # The model that passed the probe goes first for every later scenario.
        MODEL_CANDIDATES[:] = (
            [ACTIVE_MODEL] + [m for m in MODEL_CANDIDATES if m != ACTIVE_MODEL]
        )

        _drive(run_live_read_before_write_patch, "read-before-write patch")
        _drive(run_live_recipe_explorer, "recipe code-explorer")
        _drive(run_live_create_file, "list_dir + create_file")
        _drive(run_live_destructive_ops, "move_file + delete_file")
        _drive(run_live_exec_command, "run_command exec")
        _drive(run_live_read_url, "read_url net")
        asyncio.run(run_mcp_surface_test())
        print("\n" + "=" * 60)
        print("[SUCCESS] ALL LIVE E2E TESTS PASSED!")
        if RATE_LIMITED:
            print(f"[INFO] {len(RATE_LIMITED)} scenario(s) skipped — provider rate "
                  f"limit: {', '.join(RATE_LIMITED)}")
        print("=" * 60)
    except Exception as exc:
        print(f"\n[FAILURE] E2E test execution error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
