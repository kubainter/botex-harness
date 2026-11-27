# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Multi-Host OS driver: process job guards, single-instance lock, epoch git worktrees."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from botex.tools.security import coerce_text, mask_secrets, sanitize_identifier
except (ImportError, ValueError):
    from security import coerce_text, mask_secrets, sanitize_identifier

__all__ = [
    "SingleInstanceLockError",
    "SingleInstanceLock",
    "WindowsJobGuard",
    "HostOSDriver",
    "kill_posix_process_group",
    "pdeathsig_preexec",
    "cancel_process_tree",
    "pause_process_tree",
    "resume_process_tree",
    "prepare_epoch_worktree",
    "fence_local_epoch",
    "check_runtime_blocklist",
    "default_swarm_home",
]

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_SUSPEND_RESUME = 0x0800


def default_swarm_home() -> Path:
    """Return the host-local ``~/.ai/.swarm`` state directory."""
    env_dir = os.environ.get("BOTEX_SWARM_HOME", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()
    return Path.home().resolve() / ".ai" / ".swarm"


def _default_lock_path() -> Path:
    env_path = os.environ.get("BOTEX_SWARM_LOCK_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return default_swarm_home() / "night_pulse.lock"


def _default_blocklist_path() -> Path:
    env_path = os.environ.get("BOTEX_RUNTIME_BLOCKLIST_PATH", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve()
    return default_swarm_home() / "runtime_blocklist.json"


# ============================================================================
# Single-instance OS file lock
# ============================================================================


class SingleInstanceLockError(RuntimeError):
    """Raised when a single-instance lock is already held by another process."""


class SingleInstanceLock:
    """Cross-platform non-blocking exclusive file lock (msvcrt on Win32, flock on POSIX)."""

    def __init__(self, lock_path: Optional[Path | str] = None) -> None:
        self.lock_path: Path = (
            Path(lock_path).expanduser().resolve()
            if lock_path is not None
            else _default_lock_path()
        )
        self._fh: Any = None

    @property
    def locked(self) -> bool:
        return self._fh is not None

    def acquire(self) -> bool:
        if self._fh is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fh = fh
            return True
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def __enter__(self) -> SingleInstanceLock:
        if not self.acquire():
            raise SingleInstanceLockError(f"Lock already held: {self.lock_path}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ============================================================================
# Win64 Job Object / POSIX Process-Group Guard
# ============================================================================

if sys.platform == "win32":
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _build_kernel32() -> Any:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = [ctypes.c_void_p, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = wintypes.BOOL
        return k32

    def _build_ntdll() -> Any:
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtSuspendProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtSuspendProcess.restype = ctypes.c_long
        ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        return ntdll

    _KERNEL32: Any = _build_kernel32()
    _NTDLL: Any = _build_ntdll()
else:
    _KERNEL32 = None
    _NTDLL = None


def kill_posix_process_group(pgid: int, sig: Optional[int] = None) -> bool:
    """Terminate a POSIX process group safely; returns False when unavailable or already gone."""
    killpg_fn = getattr(os, "killpg", None)
    if not callable(killpg_fn):
        return False
    if isinstance(pgid, bool) or int(pgid) <= 0:
        return False
    target_sig = getattr(signal, "SIGKILL", signal.SIGTERM) if sig is None else int(sig)
    try:
        killpg_fn(int(pgid), target_sig)
        return True
    except ProcessLookupError:
        return False


def pdeathsig_preexec() -> Optional[Callable[[], None]]:
    """Return a fork-safe Linux ``preexec_fn`` setting ``PR_SET_PDEATHSIG=SIGKILL``.

    ``libc`` and ``prctl`` signatures are bound before ``fork()`` so the child
    only invokes the raw C syscall prior to ``exec``. Fails closed if ``prctl``
    is unavailable or returns non-zero. Note: Linux tracks parent-death by the
    spawning OS thread, so callers should spawn from a persistent loop thread.
    Returns ``None`` on non-Linux hosts.
    """
    if not sys.platform.startswith("linux") or not hasattr(ctypes, "CDLL"):
        return None
    sig = int(getattr(signal, "SIGKILL", 9))
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        raise OSError("Linux libc prctl symbol unavailable for PR_SET_PDEATHSIG")
    try:
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
    except AttributeError:
        pass

    def _set_pdeathsig() -> None:
        ctypes.set_errno(0)
        rc = int(prctl(1, sig, 0, 0, 0))
        if rc != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")

    return _set_pdeathsig


class WindowsJobGuard:
    """Process-tree isolation guard (Win64 Job Object + POSIX killpg + daemon cancel hooks)."""

    def __init__(
        self,
        external_cancel_cb: Optional[Callable[[], Any]] = None,
        *,
        use_win_job: Optional[bool] = None,
    ) -> None:
        self._use_win_job = (sys.platform == "win32") if use_win_job is None else bool(use_win_job)
        self.h_job: Optional[int] = None
        self.assigned_pids: list[int] = []
        self._cancel_callbacks: list[Callable[[], Any]] = []
        if external_cancel_cb is not None:
            self._cancel_callbacks.append(external_cancel_cb)
        self._closed = False

        if self._use_win_job:
            if _KERNEL32 is None:
                raise RuntimeError("Windows Job Objects are only supported on win32")
            h_job = _KERNEL32.CreateJobObjectW(None, None)
            if not h_job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _KERNEL32.SetInformationJobObject(
                h_job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not ok:
                err_code = ctypes.get_last_error()
                _KERNEL32.CloseHandle(h_job)
                raise ctypes.WinError(err_code)
            self.h_job = int(h_job)

    def register_cancel_callback(self, cb: Callable[[], Any]) -> None:
        """Register an external daemon cancel callback (e.g., devin_cancel/devin_kill)."""
        if self._closed:
            raise RuntimeError("WindowsJobGuard is closed")
        self._cancel_callbacks.append(cb)

    def assign(self, pid: int) -> None:
        """Assign ``pid`` to this guard; raises RuntimeError once closed."""
        if self._closed or (self._use_win_job and self.h_job is None):
            raise RuntimeError("WindowsJobGuard is closed")
        if isinstance(pid, bool) or int(pid) <= 0:
            raise ValueError(f"Invalid PID: {pid!r}")
        pid_int = int(pid)
        if pid_int == os.getpid():
            raise ValueError("Cannot assign current supervisor process PID to WindowsJobGuard")
        if self._use_win_job:
            assert _KERNEL32 is not None
            h_proc = _KERNEL32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                False,
                pid_int,
            )
            if not h_proc:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                ok = _KERNEL32.AssignProcessToJobObject(self.h_job, h_proc)
                if not ok:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                _KERNEL32.CloseHandle(h_proc)
        self.assigned_pids.append(pid_int)

    assign_process = assign

    def kill_all(self, exit_code: int = 1) -> dict[str, Any]:
        """Terminate all assigned processes/groups and run daemon cancel callbacks."""
        if self._closed:
            return {
                "ok": True,
                "terminated_win_job": False,
                "killed_pgids": [],
                "kill_errors": [],
                "callbacks_invoked": 0,
                "callback_errors": [],
            }
        terminated_win_job = False
        killed_pgids: list[int] = []
        kill_errors: list[str] = []
        callback_errors: list[str] = []
        callbacks_invoked = 0

        try:
            if self.h_job is not None and _KERNEL32 is not None:
                ok = _KERNEL32.TerminateJobObject(self.h_job, int(exit_code))
                terminated_win_job = bool(ok)
                _KERNEL32.CloseHandle(self.h_job)
                self.h_job = None

            if not self._use_win_job:
                getpgid_fn = getattr(os, "getpgid", None)
                getpgrp_fn = getattr(os, "getpgrp", None)
                sig = getattr(signal, "SIGKILL", signal.SIGTERM)
                for pid in self.assigned_pids:
                    pgid = pid
                    same_group = False
                    if callable(getpgid_fn):
                        try:
                            resolved_pgid = getpgid_fn(pid)
                            if callable(getpgrp_fn) and resolved_pgid == getpgrp_fn():
                                same_group = True
                            else:
                                pgid = resolved_pgid
                        except OSError:
                            pgid = pid
                    if same_group:
                        try:
                            os.kill(pid, sig)
                            killed_pgids.append(pid)
                        except ProcessLookupError:
                            pass
                        except OSError as exc:
                            kill_errors.append(coerce_text(mask_secrets(str(exc))))
                    else:
                        try:
                            if kill_posix_process_group(pgid, sig):
                                killed_pgids.append(pgid)
                        except ProcessLookupError:
                            pass
                        except OSError as exc:
                            kill_errors.append(coerce_text(mask_secrets(str(exc))))
        finally:
            self.assigned_pids.clear()
            self._closed = True

        for cb in self._cancel_callbacks:
            try:
                cb()
                callbacks_invoked += 1
            except Exception as exc:
                callback_errors.append(coerce_text(mask_secrets(str(exc))))

        return {
            "ok": not callback_errors and not kill_errors,
            "terminated_win_job": terminated_win_job,
            "killed_pgids": killed_pgids,
            "kill_errors": kill_errors,
            "callbacks_invoked": callbacks_invoked,
            "callback_errors": callback_errors,
        }

    def __enter__(self) -> WindowsJobGuard:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self._closed:
            self.kill_all()


# ============================================================================
# Typed process-tree control (pause / resume / cancel)
# ============================================================================


def _coerce_target_pid(pid: Any) -> tuple[Optional[int], str]:
    """Normalize a control target pid; returns ``(pid, "")`` or ``(None, refusal reason)``."""
    if isinstance(pid, bool):
        return None, f"invalid pid: {pid!r}"
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None, f"invalid pid: {pid!r}"
    if pid_int <= 0:
        return None, f"invalid pid: {pid_int}"
    if pid_int == os.getpid():
        return None, f"refusing supervisor self-pid {pid_int}"
    return pid_int, ""


def _process_control_refusal(action: str, pid: Any, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "action": action, "pid": pid, "detail": reason}
    if action != "cancel":
        result["affected_pids"] = []
    return result


def _process_tree_targets(pid: int, job_guard: Optional[WindowsJobGuard]) -> list[int]:
    if job_guard is not None and pid in job_guard.assigned_pids:
        targets = list(job_guard.assigned_pids)
        if pid not in targets:
            targets.append(pid)
        return targets
    return [pid]


def _win_suspend_resume_process(pid: int, *, suspend: bool) -> Optional[str]:
    """Suspend/resume one process via ``NtSuspend``/``NtResume``Process; returns error detail or None."""
    if _KERNEL32 is None or _NTDLL is None:
        return "NtSuspendProcess/NtResumeProcess unavailable on this platform"
    handle = _KERNEL32.OpenProcess(_PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        return f"OpenProcess({pid}) failed with Win32 error {ctypes.get_last_error()}"
    try:
        if suspend:
            status = int(_NTDLL.NtSuspendProcess(handle))
            name = "NtSuspendProcess"
        else:
            status = int(_NTDLL.NtResumeProcess(handle))
            name = "NtResumeProcess"
        if status < 0:
            return f"{name}({pid}) failed with NTSTATUS {status & 0xFFFFFFFF:#010x}"
        return None
    finally:
        _KERNEL32.CloseHandle(handle)


def _win_terminate_process(pid: int, exit_code: int) -> Optional[str]:
    """Terminate one process via TerminateProcess; returns error detail or None."""
    if _KERNEL32 is None:
        return "TerminateProcess unavailable on this platform"
    handle = _KERNEL32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return f"OpenProcess({pid}) failed with Win32 error {ctypes.get_last_error()}"
    try:
        if not _KERNEL32.TerminateProcess(handle, int(exit_code)):
            return f"TerminateProcess({pid}) failed with Win32 error {ctypes.get_last_error()}"
        return None
    finally:
        _KERNEL32.CloseHandle(handle)


def _signal_posix_tree(pid: int, sig: int) -> tuple[bool, str, bool]:
    """Send ``sig`` to the process group of ``pid``; never signals the supervisor's
    own group (degrades to a single-pid signal instead).
    Returns ``(ok, detail, single_pid_only)``."""
    killpg_fn = getattr(os, "killpg", None)
    getpgid_fn = getattr(os, "getpgid", None)
    if not callable(killpg_fn) or not callable(getpgid_fn):
        return False, "process groups unavailable on this platform", True
    try:
        pgid = int(getpgid_fn(pid))
    except ProcessLookupError:
        return False, f"process {pid} not found", True
    except OSError as exc:
        return False, coerce_text(mask_secrets(f"getpgid failed: {exc}")), True
    getpgrp_fn = getattr(os, "getpgrp", None)
    if callable(getpgrp_fn) and pgid == int(getpgrp_fn()):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return False, f"process {pid} not found", True
        except OSError as exc:
            return False, coerce_text(mask_secrets(f"os.kill failed: {exc}")), True
        return (
            True,
            f"target shares the supervisor process group; sent signal {int(sig)} to pid {pid} only",
            True,
        )
    try:
        killpg_fn(pgid, sig)
    except ProcessLookupError:
        return False, f"process group {pgid} not found", True
    except OSError as exc:
        return False, coerce_text(mask_secrets(f"killpg failed: {exc}")), True
    return True, f"sent signal {int(sig)} to process group {pgid}", False


def pause_process_tree(pid: int, *, job_guard: Optional[WindowsJobGuard] = None) -> dict[str, Any]:
    """Suspend a harness process tree: POSIX SIGSTOP on the process group,
    Win32 ``NtSuspendProcess`` on guard-tracked PIDs."""
    pid_int, refusal = _coerce_target_pid(pid)
    if pid_int is None:
        return _process_control_refusal("pause", pid, refusal)
    targets = _process_tree_targets(pid_int, job_guard)
    if callable(getattr(os, "killpg", None)) and callable(getattr(os, "getpgid", None)):
        sig = getattr(signal, "SIGSTOP", None)
        if sig is None:
            return {
                "ok": False,
                "action": "pause",
                "pid": pid_int,
                "affected_pids": [],
                "detail": "SIGSTOP unavailable on this platform",
            }
        ok, detail, single_pid = _signal_posix_tree(pid_int, int(sig))
        return {
            "ok": ok,
            "action": "pause",
            "pid": pid_int,
            "affected_pids": [pid_int] if single_pid else targets,
            "detail": detail,
        }
    if _KERNEL32 is not None and _NTDLL is not None:
        affected: list[int] = []
        errors: list[str] = []
        for target in targets:
            error = _win_suspend_resume_process(target, suspend=True)
            if error is None:
                affected.append(target)
            else:
                errors.append(error)
        ok = pid_int in affected
        if ok and not errors:
            detail = f"suspended {len(affected)} process(es) via NtSuspendProcess"
        else:
            detail = "; ".join(errors) or "pause failed"
        return {
            "ok": ok,
            "action": "pause",
            "pid": pid_int,
            "affected_pids": affected,
            "detail": detail,
        }
    return {
        "ok": False,
        "action": "pause",
        "pid": pid_int,
        "affected_pids": [],
        "detail": "no process-tree pause mechanism available on this platform",
    }


def resume_process_tree(pid: int, *, job_guard: Optional[WindowsJobGuard] = None) -> dict[str, Any]:
    """Resume a suspended harness process tree: POSIX SIGCONT on the process
    group, Win32 ``NtResumeProcess`` on guard-tracked PIDs."""
    pid_int, refusal = _coerce_target_pid(pid)
    if pid_int is None:
        return _process_control_refusal("resume", pid, refusal)
    targets = _process_tree_targets(pid_int, job_guard)
    if callable(getattr(os, "killpg", None)) and callable(getattr(os, "getpgid", None)):
        sig = getattr(signal, "SIGCONT", None)
        if sig is None:
            return {
                "ok": False,
                "action": "resume",
                "pid": pid_int,
                "affected_pids": [],
                "detail": "SIGCONT unavailable on this platform",
            }
        ok, detail, single_pid = _signal_posix_tree(pid_int, int(sig))
        return {
            "ok": ok,
            "action": "resume",
            "pid": pid_int,
            "affected_pids": [pid_int] if single_pid else targets,
            "detail": detail,
        }
    if _KERNEL32 is not None and _NTDLL is not None:
        affected: list[int] = []
        errors: list[str] = []
        for target in targets:
            error = _win_suspend_resume_process(target, suspend=False)
            if error is None:
                affected.append(target)
            else:
                errors.append(error)
        ok = pid_int in affected
        if ok and not errors:
            detail = f"resumed {len(affected)} process(es) via NtResumeProcess"
        else:
            detail = "; ".join(errors) or "resume failed"
        return {
            "ok": ok,
            "action": "resume",
            "pid": pid_int,
            "affected_pids": affected,
            "detail": detail,
        }
    return {
        "ok": False,
        "action": "resume",
        "pid": pid_int,
        "affected_pids": [],
        "detail": "no process-tree resume mechanism available on this platform",
    }


def cancel_process_tree(
    pid: int,
    *,
    job_guard: Optional[WindowsJobGuard] = None,
    exit_code: int = 1,
) -> dict[str, Any]:
    """Cancel a harness process tree via ``WindowsJobGuard.kill_all``, POSIX
    process-group termination, or a safe single-process TerminateProcess."""
    pid_int, refusal = _coerce_target_pid(pid)
    if pid_int is None:
        return _process_control_refusal("cancel", pid, refusal)
    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = 1
    details: list[str] = []
    ok = False
    if job_guard is not None and (
        pid_int in job_guard.assigned_pids or not job_guard.assigned_pids
    ):
        summary = job_guard.kill_all(exit_code=code)
        ok = bool(summary.get("ok"))
        if summary.get("terminated_win_job"):
            details.append("terminated win job object")
        pgids = summary.get("killed_pgids") or []
        if pgids:
            details.append(f"killed posix groups {pgids}")
        kill_errors = summary.get("kill_errors") or []
        if kill_errors:
            details.append("guard kill errors: " + "; ".join(kill_errors))
        callback_errors = summary.get("callback_errors") or []
        if callback_errors:
            details.append("guard callback errors: " + "; ".join(callback_errors))
    elif job_guard is not None:
        details.append(
            f"pid {pid_int} not tracked by provided job_guard; falling back to direct termination"
        )
    if not ok:
        killpg_fn = getattr(os, "killpg", None)
        getpgid_fn = getattr(os, "getpgid", None)
        if callable(killpg_fn) and callable(getpgid_fn):
            try:
                pgid = int(getpgid_fn(pid_int))
                same_group = False
                getpgrp_fn = getattr(os, "getpgrp", None)
                if callable(getpgrp_fn):
                    same_group = pgid == int(getpgrp_fn())
                sig = int(getattr(signal, "SIGKILL", signal.SIGTERM))
                if same_group:
                    os.kill(pid_int, sig)
                    details.append(f"sent signal {sig} to pid {pid_int}")
                else:
                    killpg_fn(pgid, sig)
                    details.append(f"sent signal {sig} to process group {pgid}")
                ok = True
            except ProcessLookupError:
                ok = True
                details.append(f"process {pid_int} already gone")
            except OSError as exc:
                details.append(coerce_text(mask_secrets(f"posix termination failed: {exc}")))
        elif _KERNEL32 is not None:
            error = _win_terminate_process(pid_int, code)
            if error is None:
                ok = True
                details.append(f"terminated pid {pid_int} via TerminateProcess")
            else:
                details.append(error)
        else:
            details.append("no process-tree termination mechanism available on this platform")
    return {"ok": ok, "action": "cancel", "pid": pid_int, "detail": "; ".join(details)}


# ============================================================================
# Epoch-isolated Git worktree lifecycle
# ============================================================================


def _remove_readonly(func: Callable[..., Any], path: str, exc: BaseException) -> None:
    """Python 3.12+/3.14 ``shutil.rmtree(..., onexc=...)`` readonly cleanup handler."""
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    func(path)


def _rmtree_with_retries(target: Path, retries: int = 3) -> None:
    """Remove ``target`` using ``onexc=`` with retries and stale rename fallback."""
    if not target.exists():
        return
    last_exc: Optional[BaseException] = None
    for attempt in range(max(1, retries)):
        try:
            shutil.rmtree(target, onexc=_remove_readonly)
        except OSError as exc:
            last_exc = exc
        if not target.exists():
            return
        time.sleep(0.05 * (attempt + 1))

    stale_target = target.with_name(f"{target.name}.stale.{int(time.time() * 1000)}")
    try:
        target.rename(stale_target)
        try:
            shutil.rmtree(stale_target, onexc=_remove_readonly)
        except OSError:
            pass
    except OSError as exc:
        detail = f": {last_exc or exc}"
        raise RuntimeError(f"Failed to remove stale worktree directory {target}{detail}") from exc

    if target.exists():
        raise RuntimeError(f"Worktree directory still present after cleanup: {target}")


def _validate_order_and_epoch(order_id: str, epoch: int) -> tuple[str, int]:
    raw_order = str(order_id or "").strip()
    clean_order = sanitize_identifier(raw_order)
    if not clean_order or clean_order != raw_order:
        raise ValueError(f"Invalid order_id: {order_id!r}")
    if isinstance(epoch, bool):
        raise ValueError("epoch must be an integer, not bool")
    try:
        epoch_int = int(epoch)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid epoch: {epoch!r}") from exc
    if epoch_int < 0:
        raise ValueError(f"epoch must be >= 0, got {epoch_int}")
    return clean_order, epoch_int


def _validate_git_ref(ref: str, name: str) -> str:
    clean = str(ref or "").strip()
    forbidden = (":", "..", "~", "^", "@{", "\\")
    if (
        not clean
        or clean.startswith("-")
        or any(ch.isspace() or ord(ch) < 32 for ch in clean)
        or any(token in clean for token in forbidden)
    ):
        raise ValueError(f"Invalid {name}: {ref!r}")
    return clean


def _run_git(
    repo_dir: Path,
    args: list[str],
    *,
    check: bool = True,
    force_lf: bool = False,
    timeout_s: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", str(repo_dir)]
    if force_lf:
        cmd.extend(["-c", "core.autocrlf=false", "-c", "core.eol=lf"])
    cmd.extend(args)
    git_env = dict(os.environ)
    git_env.pop("GIT_DIR", None)
    git_env.pop("GIT_WORK_TREE", None)
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    git_env["GIT_ASKPASS"] = "true"
    git_env["SSH_ASKPASS"] = "true"
    git_env["GCM_INTERACTIVE"] = "never"
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=git_env,
        )
    except subprocess.TimeoutExpired as exc:
        if check:
            raise RuntimeError(f"git {' '.join(args)} timed out after {timeout_s}s") from exc
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr="timeout")
    if check and res.returncode != 0:
        detail = coerce_text(mask_secrets((res.stderr or res.stdout or "").strip()))
        raise RuntimeError(f"git {' '.join(args)} failed ({res.returncode}): {detail}")
    return res


def _git_ref_exists(repo_dir: Path, ref: str) -> bool:
    res = _run_git(repo_dir, ["rev-parse", "--verify", "--quiet", ref], check=False)
    return res.returncode == 0


def _list_git_worktrees(repo_dir: Path) -> list[dict[str, str]]:
    res = _run_git(repo_dir, ["worktree", "list", "--porcelain"], check=False)
    if res.returncode != 0:
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if " " in line:
            k, v = line.split(" ", 1)
            current[k] = v.strip()
        else:
            current[line.strip()] = "1"
    if current:
        entries.append(current)
    return entries


def prepare_epoch_worktree(
    origin_repo: Path | str,
    worktree_root: Path | str,
    order_id: str,
    epoch: int,
    base_ref: str = "HEAD",
    swarm_remote: str = "swarm",
) -> Path:
    """Prepare an isolated LF-normalized git worktree on ``swarm/wip/<order_id>/e<epoch>``."""
    clean_order, epoch_int = _validate_order_and_epoch(order_id, epoch)
    clean_base = _validate_git_ref(base_ref or "HEAD", "base_ref")
    clean_remote = str(swarm_remote or "").strip()
    if clean_remote and (clean_remote.startswith("-") or any(ch.isspace() for ch in clean_remote)):
        raise ValueError(f"Invalid swarm_remote: {swarm_remote!r}")

    origin_path = Path(origin_repo).expanduser().resolve()
    if not origin_path.exists():
        raise FileNotFoundError(f"origin_repo does not exist: {origin_path}")
    wt_root = Path(worktree_root).expanduser().resolve()
    wt_path = wt_root / f"{clean_order}_e{epoch_int}"
    branch = f"swarm/wip/{clean_order}/e{epoch_int}"

    start_ref = clean_base
    if clean_remote and clean_remote != "origin" and clean_base != "HEAD":
        remotes_res = _run_git(origin_path, ["remote"], check=False)
        remotes = {r.strip() for r in remotes_res.stdout.splitlines() if r.strip()}
        if clean_remote in remotes:
            fetch_ref = (
                clean_base[len(clean_remote) + 1 :]
                if clean_base.startswith(f"{clean_remote}/")
                else clean_base
            )
            _run_git(
                origin_path,
                ["fetch", clean_remote, fetch_ref],
                check=True,
                force_lf=True,
            )
            if fetch_ref != "HEAD":
                remote_tracking = f"refs/remotes/{clean_remote}/{fetch_ref}"
                if _git_ref_exists(origin_path, remote_tracking):
                    start_ref = f"{clean_remote}/{fetch_ref}"
                elif _git_ref_exists(origin_path, "FETCH_HEAD"):
                    start_ref = "FETCH_HEAD"

    target_branch_ref = f"refs/heads/{branch}"
    for entry in _list_git_worktrees(origin_path):
        entry_raw = entry.get("worktree", "")
        if not entry_raw:
            continue
        entry_path = Path(entry_raw).resolve()
        if entry_path == origin_path:
            continue
        if entry_path == wt_path or entry.get("branch") == target_branch_ref:
            _run_git(origin_path, ["worktree", "remove", "--force", str(entry_path)], check=False)
            if entry_path.exists():
                _rmtree_with_retries(entry_path)

    if wt_path.exists():
        _rmtree_with_retries(wt_path)

    _run_git(origin_path, ["worktree", "prune"], check=True)
    wt_root.mkdir(parents=True, exist_ok=True)
    _run_git(
        origin_path,
        ["worktree", "add", "-B", branch, str(wt_path), start_ref],
        check=True,
        force_lf=True,
    )

    ext_res = _run_git(origin_path, ["config", "extensions.worktreeConfig", "true"], check=False)
    if ext_res.returncode == 0:
        _run_git(wt_path, ["config", "--worktree", "core.autocrlf", "false"], check=False)
        _run_git(wt_path, ["config", "--worktree", "core.eol", "lf"], check=False)
    return wt_path


def fence_local_epoch(
    order_id: str,
    epoch: int,
    origin_repo: Path | str,
    wt_path: Path | str,
    job_guard: Optional[WindowsJobGuard] = None,
) -> dict[str, Any]:
    """Kill active workers for ``order_id``/``epoch``, tag local WIP, and prune worktree."""
    clean_order, epoch_int = _validate_order_and_epoch(order_id, epoch)
    kill_summary = job_guard.kill_all() if job_guard is not None else None

    origin_path = Path(origin_repo).expanduser().resolve()
    target_wt = Path(wt_path).expanduser().resolve()
    branch_ref = f"refs/heads/swarm/wip/{clean_order}/e{epoch_int}"
    fenced_tag = f"local/fenced/{clean_order}/e{epoch_int}"

    tagged_commit = ""
    for candidate in (branch_ref,):
        res = _run_git(origin_path, ["rev-parse", "--verify", "--quiet", candidate], check=False)
        if res.returncode == 0 and res.stdout.strip():
            tagged_commit = res.stdout.strip()
            break
    if not tagged_commit and target_wt.exists():
        res = _run_git(target_wt, ["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            tagged_commit = res.stdout.strip()
    if not tagged_commit:
        res = _run_git(origin_path, ["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        if res.returncode == 0 and res.stdout.strip():
            tagged_commit = res.stdout.strip()

    if tagged_commit:
        _run_git(origin_path, ["tag", "-f", fenced_tag, tagged_commit], check=True)

    _run_git(origin_path, ["worktree", "remove", "--force", str(target_wt)], check=False)
    if target_wt.exists():
        _rmtree_with_retries(target_wt)
    _run_git(origin_path, ["worktree", "prune"], check=True)

    return {
        "ok": True,
        "order_id": clean_order,
        "epoch": epoch_int,
        "fenced_tag": fenced_tag if tagged_commit else "",
        "tagged_commit": tagged_commit,
        "worktree_removed": not target_wt.exists(),
        "kill_summary": kill_summary,
    }


# ============================================================================
# Runtime blocklist & SHA-256 pre-flight
# ============================================================================


def _compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def _extract_blocklist_rules(data: Any, cli_key: str) -> list[dict[str, str]]:
    rules: list[dict[str, str]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("cli") or "").strip().lower() == cli_key:
                rules.append(
                    {
                        "version": str(item.get("version") or "").strip(),
                        "sha256": str(item.get("sha256") or "").strip().lower(),
                        "reason": str(item.get("reason") or "blocked runtime").strip(),
                    }
                )
        return rules

    if not isinstance(data, dict):
        return rules

    if isinstance(data.get("blocked"), list):
        rules.extend(_extract_blocklist_rules(data["blocked"], cli_key))

    runtimes = data.get("runtimes") if isinstance(data.get("runtimes"), dict) else data
    entry = None
    if isinstance(runtimes, dict):
        for runtime_key, runtime_val in runtimes.items():
            if str(runtime_key).strip().lower() == cli_key:
                entry = runtime_val
                break
    if isinstance(entry, dict):
        reason = str(entry.get("reason") or "blocked runtime").strip()
        for ver_key in ("versions", "blocked_versions"):
            vals = entry.get(ver_key)
            if isinstance(vals, list):
                for v in vals:
                    rules.append({"version": str(v or "").strip(), "sha256": "", "reason": reason})
        for sha_key in ("sha256", "blocked_sha256", "hashes"):
            vals = entry.get(sha_key)
            if isinstance(vals, list):
                for s in vals:
                    rules.append(
                        {"version": "", "sha256": str(s or "").strip().lower(), "reason": reason}
                    )
    return rules


def check_runtime_blocklist(
    cli_name: str,
    executable_path: Optional[Path | str] = None,
    *,
    version: Optional[str] = None,
    blocklist_path: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Check an external CLI version / SHA-256 against ``runtime_blocklist.json``."""
    cli_key = str(cli_name or "").strip().lower()
    ver_str = str(version or "").strip()
    sha256_hex = ""
    if executable_path is not None and str(executable_path).strip():
        exe_p = Path(executable_path).expanduser()
        if exe_p.is_file():
            sha256_hex = _compute_file_sha256(exe_p)

    bl_path = (
        Path(blocklist_path).expanduser().resolve()
        if blocklist_path is not None
        else _default_blocklist_path()
    )
    if not bl_path.is_file():
        return {
            "ok": True,
            "status": "allowed",
            "cli": cli_key,
            "version": ver_str,
            "sha256": sha256_hex,
        }

    try:
        raw_text = bl_path.read_text(encoding="utf-8")
        data = json.loads(raw_text) if raw_text.strip() else {}
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "status": "infra_blocked_runtime",
            "error": "infra_blocked_runtime",
            "cli": cli_key,
            "version": ver_str,
            "sha256": sha256_hex,
            "reason": f"Malformed runtime blocklist: {coerce_text(mask_secrets(str(exc)))}",
        }

    if not isinstance(data, (dict, list)):
        return {
            "ok": False,
            "status": "infra_blocked_runtime",
            "error": "infra_blocked_runtime",
            "cli": cli_key,
            "version": ver_str,
            "sha256": sha256_hex,
            "reason": "Malformed runtime blocklist: root must be object or array",
        }

    for rule in _extract_blocklist_rules(data, cli_key):
        rule_ver = rule["version"]
        rule_sha = rule["sha256"]
        ver_match = bool(rule_ver and ver_str and rule_ver == ver_str)
        sha_match = bool(rule_sha and sha256_hex and rule_sha == sha256_hex)
        if ver_match or sha_match:
            return {
                "ok": False,
                "status": "infra_blocked_runtime",
                "error": "infra_blocked_runtime",
                "cli": cli_key,
                "version": ver_str,
                "sha256": sha256_hex,
                "reason": rule["reason"],
            }

    return {
        "ok": True,
        "status": "allowed",
        "cli": cli_key,
        "version": ver_str,
        "sha256": sha256_hex,
    }


class HostOSDriver:
    """Facade combining OS lock, process job guard, worktree lifecycle, and runtime preflight."""

    def __init__(
        self,
        *,
        worktree_root: Optional[Path | str] = None,
        lock_path: Optional[Path | str] = None,
        blocklist_path: Optional[Path | str] = None,
    ) -> None:
        self.worktree_root = (
            Path(worktree_root).expanduser().resolve()
            if worktree_root is not None
            else (default_swarm_home() / "worktrees")
        )
        self.lock_path = (
            Path(lock_path).expanduser().resolve()
            if lock_path is not None
            else _default_lock_path()
        )
        self.blocklist_path = (
            Path(blocklist_path).expanduser().resolve()
            if blocklist_path is not None
            else _default_blocklist_path()
        )

    def acquire_lock(self) -> SingleInstanceLock:
        lock = SingleInstanceLock(self.lock_path)
        if not lock.acquire():
            raise SingleInstanceLockError(f"Lock already held: {self.lock_path}")
        return lock

    def create_job_guard(
        self,
        external_cancel_cb: Optional[Callable[[], Any]] = None,
        *,
        use_win_job: Optional[bool] = None,
    ) -> WindowsJobGuard:
        return WindowsJobGuard(external_cancel_cb=external_cancel_cb, use_win_job=use_win_job)

    def preflight_runtime(
        self,
        cli_name: str,
        executable_path: Optional[Path | str] = None,
        *,
        version: Optional[str] = None,
    ) -> dict[str, Any]:
        return check_runtime_blocklist(
            cli_name,
            executable_path=executable_path,
            version=version,
            blocklist_path=self.blocklist_path,
        )

    def prepare_epoch_worktree(
        self,
        origin_repo: Path | str,
        order_id: str,
        epoch: int,
        base_ref: str = "HEAD",
        swarm_remote: str = "swarm",
        *,
        worktree_root: Optional[Path | str] = None,
    ) -> Path:
        root = Path(worktree_root) if worktree_root is not None else self.worktree_root
        return prepare_epoch_worktree(
            origin_repo,
            root,
            order_id,
            epoch,
            base_ref=base_ref,
            swarm_remote=swarm_remote,
        )

    def fence_local_epoch(
        self,
        order_id: str,
        epoch: int,
        origin_repo: Path | str,
        wt_path: Path | str,
        job_guard: Optional[WindowsJobGuard] = None,
    ) -> dict[str, Any]:
        return fence_local_epoch(
            order_id,
            epoch,
            origin_repo,
            wt_path,
            job_guard=job_guard,
        )

    def pause_process_tree(
        self,
        pid: int,
        *,
        job_guard: Optional[WindowsJobGuard] = None,
    ) -> dict[str, Any]:
        return pause_process_tree(pid, job_guard=job_guard)

    def resume_process_tree(
        self,
        pid: int,
        *,
        job_guard: Optional[WindowsJobGuard] = None,
    ) -> dict[str, Any]:
        return resume_process_tree(pid, job_guard=job_guard)

    def cancel_process_tree(
        self,
        pid: int,
        *,
        job_guard: Optional[WindowsJobGuard] = None,
        exit_code: int = 1,
    ) -> dict[str, Any]:
        return cancel_process_tree(pid, job_guard=job_guard, exit_code=exit_code)
