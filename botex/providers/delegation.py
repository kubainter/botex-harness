# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Direct-ACP delegation to the Devin CLI (``devin.exe acp``).

Replaces the BoteX -> devin_mcp -> devin.exe double-hop: the harness owns the
ACP subprocess itself, so events are structured ``session/update`` frames
(not a text tail), kill is process-handle based, and a Windows Job Object
with KILL_ON_JOB_CLOSE prevents orphaned devin.exe trees.

Run metadata is written to the shared ``RUNS_DIR`` (same layout as
devin_mcp) so ``devin_list``/``devin_kill`` in the bridge and this module
can see and terminate each other's runs with PID-reuse protection.
"""

import ctypes
import inspect
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
import uuid

from botex.core.contracts import parse_structured_completion
from botex.core.events import EventKind, emitter_or_none
from botex.tools.security import mask_secrets

RUNS_DIR = os.path.join(tempfile.gettempdir(), "devin-delegate-runs")
DEFAULT_MODEL = "swe-2-max"
PINNED_MODEL = DEFAULT_MODEL  # Backward-compatible default; callers may optionally override
PROMPT_TIMEOUT_S = 1800
_MAX_EVENT_TEXT = 500
# Devin/ACP session ids (e.g. "brisk-otter", uuid hex) — validated before the
# id lands on a `devin -r <id>` argv or a JSON-RPC session/load param.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")

_CREDENTIALS_CANDIDATES = (
    os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "devin", "credentials.toml"),
    os.path.join(os.path.expanduser("~"), ".config", "devin", "credentials.toml"),
)


def _delegation_cfg():
    from botex.server.config import load_config

    cfg = load_config().get("delegation", {}).get("devin", {})
    return cfg if isinstance(cfg, dict) else {}


def resolve_devin_exe() -> str:
    """Validated executable path only — never a free-form command string."""
    cfg_path = str(_delegation_cfg().get("exe_path") or "").strip()
    cand = cfg_path or os.environ.get("DEVIN_ACP_EXE", "").strip()
    if not cand:
        return ""
    cand = os.path.abspath(os.path.expandvars(os.path.expanduser(cand)))
    if not os.path.isfile(cand):
        return ""
    if os.path.basename(cand).lower() not in ("devin.exe", "devin"):
        return ""
    return cand


def _api_key() -> str:
    key = os.environ.get("DEVIN_API_KEY", "").strip()
    if key and key != "PASTE_API_KEY_HERE":
        return key
    import re

    for path in _CREDENTIALS_CANDIDATES:
        try:
            m = re.search(r'windsurf_api_key\s*=\s*"([^"]+)"', open(path, encoding="utf-8").read())
            if m:
                return m.group(1)
        except OSError:
            continue
    return ""


def devin_auth_status() -> dict:
    """Health/probe surface for the PWA executor selector."""
    exe = resolve_devin_exe()
    cred = bool(_api_key())
    enabled = bool(_delegation_cfg().get("enabled", True))
    return {
        "enabled": enabled,
        "available": bool(exe and cred and enabled),
        "exe_resolved": bool(exe),
        "credential_resolved": cred,
        "reason": (
            "ok"
            if exe and cred and enabled
            else (
                "disabled in config"
                if not enabled
                else "exe_path not configured/found"
                if not exe
                else "no credential (run `devin auth login` or set DEVIN_API_KEY)"
            )
        ),
    }


def _attach_kill_on_close(proc) -> object:
    """Windows Job Object: devin.exe tree dies if this harness process dies."""
    if os.name != "nt":
        return None

    class _LARGE(ctypes.Structure):
        _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", _LARGE),
            ("PerJobUserTimeLimit", _LARGE),
            ("LimitFlags", ctypes.c_ulong),
            ("MinWorkSet", ctypes.c_size_t),
            ("MaxWorkSet", ctypes.c_size_t),
            ("ActiveProcLimit", ctypes.c_ulong),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_ulong),
            ("SchedulingClass", ctypes.c_ulong),
        ]

    class _IO(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in ("ReadOp", "WriteOp", "OtherOp", "ReadXfer", "WriteXfer", "OtherXfer")
        ]

    class _EXT(ctypes.Structure):
        _fields_ = [
            ("Basic", _BASIC),
            ("Io", _IO),
            ("ProcMemLimit", ctypes.c_size_t),
            ("JobMemLimit", ctypes.c_size_t),
            ("PeakProcMem", ctypes.c_size_t),
            ("PeakJobMem", ctypes.c_size_t),
        ]

    try:
        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        info = _EXT()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        handle = getattr(proc, "_handle", None)
        opened = False
        if not handle:
            # PROCESS_SET_QUOTA | PROCESS_TERMINATE — enough for job assignment
            handle = k32.OpenProcess(0x0400 | 0x0001, False, int(proc.pid))
            opened = bool(handle)
        try:
            if not handle or not k32.AssignProcessToJobObject(job, handle):
                return None
        finally:
            if opened:
                k32.CloseHandle(handle)
        return job
    except Exception:
        return None


def _fs_read(params: dict) -> str:
    path = params.get("path") or ""
    text = open(path, encoding="utf-8", errors="replace").read()
    line, limit = params.get("line"), params.get("limit")
    if line is not None or limit is not None:
        lines = text.splitlines(keepends=True)
        start = max(int(line or 1) - 1, 0)
        end = None if limit is None else start + int(limit)
        text = "".join(lines[start:end])
    return text


def _fs_write(params: dict):
    path = params.get("path") or ""
    if not path:
        return "missing path"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(params.get("content") or "")
        return None
    except OSError as e:
        return str(e)


def _pick_allow(params: dict) -> str:
    options = params.get("options") or []
    for want in ("allow_once", "allow_always"):
        for o in options:
            if o.get("kind") == want or o.get("optionId") == want:
                return o.get("optionId") or want
    for o in options:
        if (
            "allow" in str(o.get("optionId", "")).lower()
            or "allow" in str(o.get("kind", "")).lower()
        ):
            return o.get("optionId") or "allow_once"
    return options[0].get("optionId", "allow_once") if options else "allow_once"


class DevinRun:
    """One ``devin.exe acp`` process + reader thread + event emission."""

    def __init__(self, run_id: str, agent_type: str, task_id: str = "", model: str = ""):
        self.run_id = run_id
        self.task_id = task_id
        self.model = (model or "").strip() or PINNED_MODEL
        self.dir = os.path.join(RUNS_DIR, run_id)
        os.makedirs(self.dir, exist_ok=True)
        exe = resolve_devin_exe()
        args = [exe, "acp", "--model", self.model]
        if agent_type and agent_type != "default":
            args += ["--agent-type", agent_type]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._job = _attach_kill_on_close(self.proc)
        self.q: "queue.Queue" = queue.Queue()
        self.log_path = os.path.join(self.dir, "updates.jsonl")
        self.done = False
        self.result_text = ""
        self.error = ""
        self.last_update_ts = time.time()
        self._write_meta(agent_type)
        threading.Thread(target=self._reader, daemon=True).start()

    def _write_meta(self, agent_type):
        try:
            with open(os.path.join(self.dir, "run.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "run_id": self.run_id,
                        "pid": self.proc.pid,
                        "exe": "devin.exe",
                        "agent_type": agent_type,
                        "model": self.model,
                        "owner": "botex",
                        "task_id": self.task_id,
                        "started_ts": time.time(),
                    },
                    f,
                )
        except OSError:
            pass

    def _meta_set(self, **fields):
        path = os.path.join(self.dir, "run.json")
        try:
            meta = json.load(open(path, encoding="utf-8"))
            meta.update(fields)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(meta, f)
        except Exception:
            pass

    def _meta_set_session(self, session_id):
        self._meta_set(session_id=session_id)

    def _reader(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self.q.put(line)
        finally:
            self.q.put(None)

    def send(self, obj: dict):
        data = json.dumps(obj, ensure_ascii=False) + "\n"
        self.proc.stdin.write(data.encode("utf-8", "replace"))
        self.proc.stdin.flush()

    def _emit(self, kind: str, **data):
        em = emitter_or_none(self.task_id) if self.task_id else None
        if em:
            try:
                em.emit(kind, **data)
            except Exception:
                pass

    def read_for(self, msg_id: int, timeout_s: float):
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.done:
                return None
            try:
                line = self._next_json(timeout=0.3)
            except queue.Empty:
                continue
            if line is None:
                return None
            if line.get("id") == msg_id:
                return line
        return None

    def _next_json(self, timeout):
        while True:
            line = self.q.get(timeout=timeout)
            if line is None:
                return None
            self._write_raw(line)
            try:
                obj = json.loads(line)
            except Exception:
                return {}
            if "id" in obj and "method" in obj:
                self._handle_agent_request(obj)
                continue
            return obj

    def _handle_agent_request(self, obj: dict):
        rid, method, params = obj["id"], obj["method"], obj.get("params") or {}
        try:
            if method == "fs/read_text_file":
                self.send({"jsonrpc": "2.0", "id": rid, "result": {"content": _fs_read(params)}})
            elif method == "fs/write_text_file":
                err = _fs_write(params)
                if err:
                    self.send(
                        {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": err}}
                    )
                else:
                    self.send({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "session/request_permission":
                self._emit(EventKind.APPROVAL_WAIT, source="devin_acp")
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "outcome": {"outcome": "selected", "optionId": _pick_allow(params)}
                        },
                    }
                )
            else:
                self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32601, "message": f"unsupported: {method}"},
                    }
                )
        except Exception as e:
            try:
                self.send(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}}
                )
            except Exception:
                pass

    def _write_raw(self, line: str):
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def kill(self):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.done = True

    def collect(self, timeout_s: float, idle_timeout_s: float):
        """Stream session/update notifications -> task events; returns (text, error)."""
        chunks = []
        deadline = time.time() + timeout_s
        idle_emitted = False
        while time.time() < deadline and not self.done:
            try:
                obj = self._next_json(timeout=0.5)
            except queue.Empty:
                if not idle_emitted and time.time() - self.last_update_ts > idle_timeout_s:
                    idle_emitted = True
                    self._emit(
                        EventKind.WAITING_FOR_INPUT, idle_s=int(time.time() - self.last_update_ts)
                    )
                continue
            if obj is None:
                break
            idle_emitted = False
            self.last_update_ts = time.time()
            method = obj.get("method")
            if method == "session/update":
                upd = (obj.get("params") or {}).get("update") or {}
                kind = upd.get("sessionUpdate", "")
                if kind == "agent_message_chunk":
                    content = upd.get("content") or {}
                    if content.get("type") == "text":
                        chunks.append(content.get("text", ""))
                    continue
                # tool_call / tool_call_update / plan / agent_thought_chunk ...
                self._emit(
                    EventKind.DEVIN_UPDATE,
                    update_kind=kind,
                    title=mask_secrets(str(upd.get("title") or ""))[:_MAX_EVENT_TEXT],
                    status=str(upd.get("status") or ""),
                )
            elif "id" in obj and obj.get("result") is not None:
                self.done = True
                self.result_text = "".join(chunks)
                return self.result_text, None
            elif "id" in obj and obj.get("error"):
                self.done = True
                self.result_text = "".join(chunks)
                self.error = str(obj["error"])
                return self.result_text, self.error
        self.done = True
        self.result_text = "".join(chunks)
        return self.result_text, "timeout" if time.time() >= deadline else "process ended"


class CliResumeRun:
    """Non-ACP fallback run: ``devin -r <session_id> -p <prompt>`` (print mode).

    Used when the ACP server cannot ``session/load`` an existing session (older
    devin.exe builds or a missing capability bit). The session is resumed by the
    CLI itself, so prompt caching over the stored conversation still applies.
    Registered in ``_ACTIVE_RUNS`` so the in-process cancel path and the
    cross-process ``kill_run`` (run.json pid + start-time validation) both work.
    """

    def __init__(self, run_id, task_id, session_id, cwd, prompt, model: str = ""):
        self.run_id = run_id
        self.task_id = task_id
        self.model = (model or "").strip() or PINNED_MODEL
        self.dir = os.path.join(RUNS_DIR, run_id)
        os.makedirs(self.dir, exist_ok=True)
        exe = resolve_devin_exe()
        env = dict(os.environ)
        key = _api_key()
        if key:
            env.setdefault("WINDSURF_API_KEY", key)
        # `-p` (print) is the non-interactive one-shot mode; combined with `-r`
        # the stored session is resumed, the prompt is answered, and the process
        # exits. `--model` keeps the default/requested model across the resume.
        self.proc = subprocess.Popen(
            [exe, "-r", session_id, "--model", self.model, "-p", prompt],
            cwd=cwd or None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._job = _attach_kill_on_close(self.proc)
        self.log_path = os.path.join(self.dir, "updates.jsonl")
        try:
            with open(os.path.join(self.dir, "run.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "run_id": self.run_id,
                        "pid": self.proc.pid,
                        "exe": "devin.exe",
                        "agent_type": "default",
                        "owner": "botex",
                        "task_id": self.task_id,
                        "session_id": session_id,
                        "resume_mode": "cli",
                        "started_ts": time.time(),
                    },
                    f,
                )
        except OSError:
            pass

    def _emit(self, kind, **data):
        em = emitter_or_none(self.task_id) if self.task_id else None
        if em:
            try:
                em.emit(kind, **data)
            except Exception:
                pass

    def kill(self):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def wait(self, timeout_s):
        """Block until exit; returns (stdout_text, error_or_None)."""
        try:
            out, _ = self.proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self.kill()
            return "", "timeout"
        except Exception as e:
            self.kill()
            return "", str(e)
        text = (out or b"").decode("utf-8", "replace")
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        if self.proc.returncode:
            return text, "devin -r exited with code " + str(self.proc.returncode)
        return text, None


_ACTIVE_RUNS: "dict[str, object]" = {}
_ACTIVE_LOCK = threading.Lock()


def cancel_run_for_task(task_id: str) -> dict:
    """Kill the in-process ACP run owned by `task_id` (cloud cancel path)."""
    with _ACTIVE_LOCK:
        run = _ACTIVE_RUNS.get(task_id)
    if run is None:
        return {"ok": False, "error": "no active run for task"}
    run._emit(EventKind.DEVIN_UPDATE, update_kind="cancelled", run_id=run.run_id)
    run.kill()
    return {"ok": True, "run_id": run.run_id}


def _run_cli_resume(
    task_id: str,
    prompt: str,
    cwd: str,
    session_id: str,
    timeout_s: int,
    run_id: str,
    model: str = "",
) -> dict:
    """Follow-up turn via ``devin -r <session_id> -p <prompt>`` print mode.

    Fallback when the local ACP server cannot ``session/load``; the CLI resumes
    the persisted session so the provider's prompt cache still sees the prior
    conversation. Blocks the calling worker thread like ``delegate_run``.
    """
    import inspect

    sig = inspect.signature(CliResumeRun.__init__)
    if "model" in sig.parameters:
        run = CliResumeRun(run_id, task_id, session_id, cwd, prompt, model=model)
    else:
        run = CliResumeRun(run_id, task_id, session_id, cwd, prompt)
    with _ACTIVE_LOCK:
        _ACTIVE_RUNS[task_id] = run
    try:
        run._emit(
            EventKind.DEVIN_UPDATE,
            update_kind="session_resumed",
            run_id=run_id,
            session_id=session_id,
            resume_mode="cli",
        )
        text, err = run.wait(timeout_s)
        run.kill()
        return {
            "ok": err is None,
            "error": err,
            "run_id": run_id,
            "session_id": session_id,
            "resumed": err is None,
            "resume_mode": "cli",
            "summary": text[-2000:],
            "updates_log": run.log_path,
        }
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_RUNS.pop(task_id, None)


def delegate_run(
    task_id: str,
    prompt: str,
    cwd: str,
    agent_type: str,
    timeout_s: int = PROMPT_TIMEOUT_S,
    session_id: str = "",
    model: str = "",
) -> dict:
    """Spawn + drive one ACP run to completion. Intended for a worker thread.

    ``session_id`` enables multi-turn continuation: the run ``session/load``s the
    persisted ACP session instead of ``session/new``, so the follow-up prompt is
    answered with the whole conversation in cache. When the ACP server lacks the
    ``loadSession`` capability (or the load fails), falls back to the
    ``devin -r <session_id> -p`` print-mode resume so caching is still leveraged.

    Returns a compact receipt dict (never the raw event feed)."""
    status = devin_auth_status()
    if not status["available"]:
        return {"ok": False, "error": f"devin executor unavailable: {status['reason']}"}
    prompt = prompt.encode("utf-8", "replace").decode("utf-8", "replace")
    session_id = (session_id or "").strip()
    if session_id and not _SESSION_ID_RE.fullmatch(session_id):
        return {"ok": False, "error": "invalid session_id format"}
    cfg = _delegation_cfg()
    idle_s = int(cfg.get("idle_timeout_s") or 90)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    run = DevinRun(run_id, agent_type or "default", task_id=task_id, model=model)
    em = emitter_or_none(task_id)
    with _ACTIVE_LOCK:
        _ACTIVE_RUNS[task_id] = run
    try:
        run.send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
                },
            }
        )
        init = run.read_for(0, 60)
        if not init or "error" in init:
            run.kill()
            return {"ok": False, "error": f"ACP initialize failed: {init}", "run_id": run_id}
        load_supported = bool(
            ((init.get("result") or {}).get("agentCapabilities") or {}).get("loadSession")
        )
        if session_id and not load_supported:
            # ACP server cannot session/load — CLI print-mode resume keeps the
            # session's prompt cache instead of silently starting a clean slate.
            run.kill()
            with _ACTIVE_LOCK:
                _ACTIVE_RUNS.pop(task_id, None)
            sig = inspect.signature(_run_cli_resume)
            if "model" in sig.parameters and model:
                return _run_cli_resume(
                    task_id, prompt, cwd, session_id, timeout_s, run_id, model=model
                )
            return _run_cli_resume(task_id, prompt, cwd, session_id, timeout_s, run_id)
        run.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "authenticate",
                "params": {
                    "methodId": "devin-browser",
                    "_meta": {"api_key": _api_key()},
                },
            }
        )
        auth = run.read_for(1, 60)
        if not auth or "error" in auth:
            run.kill()
            return {"ok": False, "error": f"ACP authenticate failed: {auth}", "run_id": run_id}
        resumed = False
        if session_id:
            run.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/load",
                    "params": {
                        "sessionId": session_id,
                        "cwd": cwd,
                        "mcpServers": [],
                    },
                }
            )
            loaded = run.read_for(2, 120)
            if loaded and "error" not in loaded:
                resumed = True
            else:
                # Advertised but failed (stale/foreign session) — CLI resume
                # still leverages the persisted session + prompt cache.
                run.kill()
                with _ACTIVE_LOCK:
                    _ACTIVE_RUNS.pop(task_id, None)
                res = _run_cli_resume(task_id, prompt, cwd, session_id, timeout_s, run_id)
                res["acp_load_error"] = loaded
                return res
        else:
            run.send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "session/new",
                    "params": {
                        "cwd": cwd,
                        "mcpServers": [],
                    },
                }
            )
            sess = run.read_for(2, 120)
            if not sess or "error" in sess:
                run.kill()
                return {"ok": False, "error": f"session/new failed: {sess}", "run_id": run_id}
            session_id = sess["result"]["sessionId"]
        run._meta_set(
            session_id=session_id,
            resumed=resumed,
            resume_mode="acp_load" if resumed else "new",
        )
        if em:
            run._emit(
                EventKind.DEVIN_UPDATE,
                update_kind="session_resumed" if resumed else "session_started",
                run_id=run_id,
                session_id=session_id,
                resume_mode="acp_load" if resumed else "new",
            )
        run.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "session/prompt",
                "params": {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            }
        )
        text, err = run.collect(timeout_s, idle_s)
        run.kill()
        parsed_completion = parse_structured_completion(text)
        status_val = "DONE" if (err is None) else "FAILED"
        if parsed_completion and parsed_completion.get("status"):
            status_val = parsed_completion["status"]

        return {
            "ok": err is None and status_val == "DONE",
            "version": "1.0",
            "status": status_val,
            "error": err,
            "run_id": run_id,
            "session_id": session_id,
            "resumed": resumed,
            "resume_mode": "acp_load" if resumed else "new",
            "summary": parsed_completion.get("summary") if parsed_completion else text[-2000:],
            "artifact_refs": parsed_completion.get("artifact_refs", [])
            if parsed_completion
            else [],
            "memory_keys": parsed_completion.get("memory_keys", []) if parsed_completion else [],
            "updates_log": run.log_path,
        }
    except BrokenPipeError:
        run.kill()
        return {"ok": False, "error": "devin acp process died (broken pipe)", "run_id": run_id}
    except Exception as e:
        run.kill()
        return {"ok": False, "error": str(e), "run_id": run_id}
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_RUNS.pop(task_id, None)


def _proc_start_time(pid: int):
    """(image_name, create_time_epoch) via PowerShell — PID-reuse guard."""
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"$p=Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
                "if($p){Write-Output ($p.ProcessName+'|'+$p.StartTime.ToFileTimeUtc())}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        if "|" in out:
            name, ft = out.rsplit("|", 1)
            return name.strip(), (int(ft) - 116444736000000000) / 10**7
    except Exception:
        pass
    return None, None


def list_runs(limit: int = 20) -> list:
    """Cross-process run listing from shared RUNS_DIR metadata."""
    runs = []
    try:
        dirs = sorted(os.listdir(RUNS_DIR), reverse=True)[:limit]
    except OSError:
        return runs
    for d in dirs:
        run_dir = os.path.join(RUNS_DIR, d)
        meta_path = os.path.join(run_dir, "run.json")
        if not os.path.isfile(meta_path):
            continue  # not a delegation run dir
        meta = {}
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            pass
        pid = meta.get("pid")
        live = False
        if pid:
            name, started = _proc_start_time(pid)
            live = bool(
                name
                and "devin" in name.lower()
                and started
                and abs(started - float(meta.get("started_ts") or 0)) < 60
            )
        runs.append(
            {
                "run_id": d,
                "live": live,
                **{
                    k: meta.get(k)
                    for k in ("agent_type", "owner", "task_id", "session_id", "started_ts")
                },
            }
        )
    return runs


def kill_run(run_id: str) -> dict:
    """Terminate a run by id; validates image name + start time before taskkill."""
    meta = {}
    try:
        meta = json.load(open(os.path.join(RUNS_DIR, run_id, "run.json"), encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "run.json missing/unreadable"}
    pid = meta.get("pid")
    if not pid:
        return {"ok": False, "error": "no pid in run.json"}
    name, started = _proc_start_time(int(pid))
    if not name:
        return {"ok": True, "already_dead": True}
    if "devin" not in name.lower():
        return {"ok": False, "error": f"pid {pid} is '{name}', not devin — refusing (PID reuse)"}
    if started and abs(started - float(meta.get("started_ts") or 0)) > 60:
        return {"ok": False, "error": "process start-time mismatch — refusing (PID reuse)"}
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
        return {"ok": True, "killed_pid": pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}
