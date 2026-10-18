# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jakub Grzesiak (jg-webtech.pl)
"""Unit and integration tests for BoteX Human-in-the-Loop Approval System."""

import asyncio
from unittest.mock import patch

import pytest

from botex.approval import (
    ActionStatus,
    ApprovalManager,
    ApprovalPolicy,
    PendingAction,
    POLICY_TOOL_MAP,
    TaskAbortedError,
    format_action_preview,
)
from botex.capabilities import (
    CAP_DESTRUCTIVE,
    CAP_EXEC,
    CAP_NET,
    CAP_READ,
    CAP_WRITE,
    async_check_agency_policy,
)
from botex.contracts import STATUS_FAILURE_KIND
from server import _interactive_confirm


# ---------------------------------------------------------------------------
# 1. ActionStatus & ApprovalPolicy Taxonomy Tests
# ---------------------------------------------------------------------------
def test_action_status_enum():
    assert ActionStatus.PENDING == "pending"
    assert ActionStatus.APPROVED == "approved"
    assert ActionStatus.REJECTED == "rejected"
    assert ActionStatus.ABORTED == "aborted"


def test_approval_policy_tool_map():
    assert POLICY_TOOL_MAP[ApprovalPolicy.NONE] == set()
    assert POLICY_TOOL_MAP[ApprovalPolicy.HIGH_RISK] == {
        "delete_file",
        "move_file",
        "run_command",
    }
    assert POLICY_TOOL_MAP[ApprovalPolicy.MUTATIONS] == {
        "create_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "run_command",
    }
    assert POLICY_TOOL_MAP[ApprovalPolicy.ALL_RISKY] == {
        "create_file",
        "apply_patch",
        "delete_file",
        "move_file",
        "run_command",
        "read_url",
    }
    # Read-only tools should never be gated by approval policies
    for policy, tools in POLICY_TOOL_MAP.items():
        assert "get_file_outline" not in tools
        assert "read_file_lines" not in tools
        assert "read_file" not in tools
        assert "list_dir" not in tools


def test_status_failure_kind_aborted():
    assert STATUS_FAILURE_KIND.get("ABORTED") == "task_aborted"


# ---------------------------------------------------------------------------
# 2. PendingAction & Secret Masking
# ---------------------------------------------------------------------------
def test_pending_action_to_dict_and_masking():
    secret_key = "sk-or-v1-abcdef1234567890abcdef1234567890"
    action = PendingAction(
        action_id="act-1234",
        task_id="task-9999",
        tool_name="run_command",
        params={"command": f"echo {secret_key}"},
        preview=f"EXEC: echo {secret_key}",
    )
    d = action.to_dict()
    assert d["action_id"] == "act-1234"
    assert d["task_id"] == "task-9999"
    assert d["tool_name"] == "run_command"
    assert d["status"] == "pending"
    assert secret_key not in d["preview"]
    assert "[REDACTED]" in d["preview"] or "***" in d["preview"] or "sk-or-" not in d["preview"]


def test_format_action_preview():
    # apply_patch preview
    patch_preview = format_action_preview(
        "apply_patch",
        {
            "path": "app.py",
            "search_block": "print('hello')",
            "replace_block": "print('world')",
        },
    )
    assert "app.py" in patch_preview
    assert "- print('hello')" in patch_preview
    assert "+ print('world')" in patch_preview

    # create_file preview
    create_preview = format_action_preview(
        "create_file",
        {"path": "test.txt", "content": "line1\nline2\n"},
    )
    assert "Create File: test.txt" in create_preview

    # delete_file preview
    del_preview = format_action_preview("delete_file", {"path": "old.py"})
    assert "DELETE File: old.py" in del_preview

    # move_file preview
    move_preview = format_action_preview("move_file", {"src_path": "a.txt", "dst_path": "b.txt"})
    assert "MOVE File: a.txt -> b.txt" in move_preview

    # run_command preview
    cmd_preview = format_action_preview("run_command", {"command": "pytest tests/"})
    assert "EXEC Command: pytest tests/" in cmd_preview

    # read_url preview
    net_preview = format_action_preview("read_url", {"url": "https://docs.openrouter.ai"})
    assert "FETCH URL: https://docs.openrouter.ai" in net_preview


# ---------------------------------------------------------------------------
# 3. ApprovalManager Unit Tests
# ---------------------------------------------------------------------------
def test_approval_manager_policy_checks():
    mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)

    # Read operations never require approval
    assert not mgr.is_action_requiring_approval("get_file_outline")
    assert not mgr.is_action_requiring_approval("read_file")

    # High risk operations require approval
    assert mgr.is_action_requiring_approval("delete_file")
    assert mgr.is_action_requiring_approval("run_command")

    # Mutations not in HIGH_RISK do not require approval under HIGH_RISK
    assert not mgr.is_action_requiring_approval("create_file")
    assert not mgr.is_action_requiring_approval("apply_patch")

    # But under MUTATIONS policy they do
    assert mgr.is_action_requiring_approval("create_file", policy=ApprovalPolicy.MUTATIONS)
    assert mgr.is_action_requiring_approval("apply_patch", policy=ApprovalPolicy.MUTATIONS)

    # Under NONE policy, nothing requires approval
    assert not mgr.is_action_requiring_approval("delete_file", policy=ApprovalPolicy.NONE)
    assert not mgr.is_action_requiring_approval("run_command", policy=ApprovalPolicy.NONE)


def test_approval_manager_session_preapproval():
    mgr = ApprovalManager(default_policy=ApprovalPolicy.MUTATIONS)
    task_id = "task-session-1"

    assert mgr.is_action_requiring_approval("apply_patch", task_id=task_id)

    # Pre-approve apply_patch for this task
    mgr.preapprove_tool(task_id, "apply_patch")
    assert not mgr.is_action_requiring_approval("apply_patch", task_id=task_id)

    # Other tools still require approval
    assert mgr.is_action_requiring_approval("create_file", task_id=task_id)

    # Other tasks still require approval for apply_patch
    assert mgr.is_action_requiring_approval("apply_patch", task_id="task-session-2")

    # Cleanup task
    mgr.clear_task(task_id)
    assert mgr.is_action_requiring_approval("apply_patch", task_id=task_id)


def test_approval_manager_auto_allow_unrestricted():
    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)
        res = await mgr.request_approval(
            task_id="t1",
            tool_name="create_file",
            params={"path": "demo.txt", "content": "ok"},
        )
        assert res is True
        assert len(mgr.get_pending_actions()) == 0

    asyncio.run(run())


def test_approval_manager_async_resolution_approve_and_reject():
    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)

        # 1. Approval flow
        req_task = asyncio.create_task(
            mgr.request_approval(
                task_id="t1",
                tool_name="run_command",
                params={"command": "pytest"},
            )
        )
        await asyncio.sleep(0.01)

        pending = mgr.get_pending_actions("t1")
        assert len(pending) == 1
        action_id = pending[0]["action_id"]

        resolved = mgr.resolve(action_id, approved=True, reason="Looks safe")
        assert resolved is True

        result = await req_task
        assert result is True

        action_record = mgr.get_action(action_id)
        assert action_record["status"] == "approved"
        assert action_record["decision_reason"] == "Looks safe"

        # 2. Rejection flow
        req_task2 = asyncio.create_task(
            mgr.request_approval(
                task_id="t1",
                tool_name="delete_file",
                params={"path": "important.py"},
            )
        )
        await asyncio.sleep(0.01)

        pending2 = mgr.get_pending_actions("t1")
        assert len(pending2) == 1
        action_id2 = pending2[0]["action_id"]

        mgr.resolve(action_id2, approved=False, reason="Too dangerous")
        result2 = await req_task2
        assert result2 is False

        action_record2 = mgr.get_action(action_id2)
        assert action_record2["status"] == "rejected"
        assert action_record2["decision_reason"] == "Too dangerous"

    asyncio.run(run())


def test_approval_manager_resolve_with_remember():
    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)
        task_id = "task-remember-1"

        req_task = asyncio.create_task(
            mgr.request_approval(
                task_id=task_id,
                tool_name="run_command",
                params={"command": "pytest"},
            )
        )
        await asyncio.sleep(0.01)

        action_id = mgr.get_pending_actions(task_id)[0]["action_id"]
        mgr.resolve(action_id, approved=True, remember=True)
        await req_task

        # Next run_command for this task should now be pre-approved
        assert not mgr.is_action_requiring_approval("run_command", task_id=task_id)

    asyncio.run(run())


def test_approval_manager_abort_task():
    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)
        task_id = "task-to-abort"

        req_task = asyncio.create_task(
            mgr.request_approval(
                task_id=task_id,
                tool_name="run_command",
                params={"command": "rm -rf *"},
            )
        )
        await asyncio.sleep(0.01)

        assert len(mgr.get_pending_actions(task_id)) == 1

        # Abort the task
        count = mgr.abort_task(task_id, reason="Operator clicked Abort Task")
        assert count == 1

        # The waiting future should raise TaskAbortedError
        with pytest.raises(TaskAbortedError) as exc_info:
            await req_task
        assert "Operator clicked Abort Task" in str(exc_info.value)

        assert len(mgr.get_pending_actions(task_id)) == 0

    asyncio.run(run())


def test_approval_manager_listener_notification():
    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)
        notified_actions = []

        def on_action(action: PendingAction):
            notified_actions.append(action)

        mgr.add_listener(on_action)

        req_task = asyncio.create_task(
            mgr.request_approval(
                task_id="t_listen",
                tool_name="run_command",
                params={"command": "git status"},
            )
        )
        await asyncio.sleep(0.01)

        assert len(notified_actions) == 1
        assert notified_actions[0].tool_name == "run_command"

        # Clean up
        mgr.resolve(notified_actions[0].action_id, approved=True)
        await req_task
        mgr.remove_listener(on_action)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. async_check_agency_policy Integration Tests
# ---------------------------------------------------------------------------
def test_async_check_agency_policy_allowed():
    async def run():
        caps = {CAP_READ, CAP_WRITE, CAP_EXEC, CAP_DESTRUCTIVE, CAP_NET}
        err = await async_check_agency_policy(
            "get_file_outline",
            caps,
            op_path="test.py",
            approval_policy=ApprovalPolicy.ALL_RISKY,
        )
        assert err == ""

    asyncio.run(run())


def test_async_check_agency_policy_missing_scopes():
    async def run():
        caps = {CAP_READ}
        err = await async_check_agency_policy(
            "create_file",
            caps,
            op_path="test.py",
        )
        assert "Missing required scopes for create_file: write" in err

    asyncio.run(run())


def test_async_check_agency_policy_with_approval_manager():
    async def run():
        caps = {CAP_READ, CAP_WRITE, CAP_EXEC, CAP_DESTRUCTIVE}
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)

        check_task = asyncio.create_task(
            async_check_agency_policy(
                "run_command",
                caps,
                confirm_fn=mgr,
                op_path="pytest",
                params={"command": "pytest"},
                task_id="task_agency_1",
                approval_policy=ApprovalPolicy.HIGH_RISK,
            )
        )
        await asyncio.sleep(0.01)

        pending = mgr.get_pending_actions("task_agency_1")
        assert len(pending) == 1
        mgr.resolve(pending[0]["action_id"], approved=True)

        err = await check_task
        assert err == ""

    asyncio.run(run())


def test_async_check_agency_policy_rejection():
    async def run():
        caps = {CAP_READ, CAP_WRITE, CAP_EXEC, CAP_DESTRUCTIVE}
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)

        check_task = asyncio.create_task(
            async_check_agency_policy(
                "delete_file",
                caps,
                confirm_fn=mgr,
                op_path="important.py",
                params={"path": "important.py"},
                task_id="task_agency_2",
                approval_policy=ApprovalPolicy.HIGH_RISK,
            )
        )
        await asyncio.sleep(0.01)

        pending = mgr.get_pending_actions("task_agency_2")
        mgr.resolve(pending[0]["action_id"], approved=False)

        err = await check_task
        assert "was not confirmed by the operator" in err

    asyncio.run(run())


def test_async_check_agency_policy_aborted():
    async def run():
        caps = {CAP_READ, CAP_WRITE, CAP_EXEC, CAP_DESTRUCTIVE}
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)

        check_task = asyncio.create_task(
            async_check_agency_policy(
                "run_command",
                caps,
                confirm_fn=mgr,
                op_path="rm -rf /",
                params={"command": "rm -rf /"},
                task_id="task_agency_3",
                approval_policy=ApprovalPolicy.HIGH_RISK,
            )
        )
        await asyncio.sleep(0.01)

        mgr.abort_task("task_agency_3", reason="Emergency abort")

        err = await check_task
        assert "aborted by the operator" in err
        assert "Emergency abort" in err

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 5. CLI _interactive_confirm Tests
# ---------------------------------------------------------------------------
def test_interactive_confirm_approve():
    with patch("builtins.input", return_value="y"):
        assert _interactive_confirm("run_command", "pytest", {"command": "pytest"}) is True


def test_interactive_confirm_reject():
    with patch("builtins.input", return_value="n"):
        assert _interactive_confirm("delete_file", "old.py", {"path": "old.py"}) is False


def test_interactive_confirm_always_allow():
    from botex.approval import default_approval_manager

    task_id = "cli-task-preapprove"
    with patch("builtins.input", return_value="a"):
        res = _interactive_confirm("create_file", "a.txt", {"path": "a.txt"}, task_id=task_id)
        assert res is True
        assert not default_approval_manager.is_action_requiring_approval(
            "create_file", task_id=task_id, policy=ApprovalPolicy.MUTATIONS
        )
    default_approval_manager.clear_task(task_id)


def test_interactive_confirm_abort():
    with patch("builtins.input", return_value="c"):
        with pytest.raises(TaskAbortedError) as exc_info:
            _interactive_confirm("run_command", "git reset", {"command": "git reset"})
        assert "cancelled by operator" in str(exc_info.value)


def test_interactive_confirm_interrupt():
    with patch("builtins.input", side_effect=KeyboardInterrupt):
        with pytest.raises(TaskAbortedError) as exc_info:
            _interactive_confirm("delete_file", "old.py", {"path": "old.py"})
        assert "interrupted by operator" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 6. Hardening Tests: C1, M1, M3 Findings
# ---------------------------------------------------------------------------
def test_async_check_agency_policy_headless_fails_closed():
    """C1: Verify headless execution (confirm_fn=None) fails closed on risky tools."""

    async def run():
        caps = {CAP_READ, CAP_WRITE, CAP_DESTRUCTIVE, CAP_EXEC}
        # Without pre-approval and with HIGH_RISK policy, delete_file must be blocked in headless mode
        err = await async_check_agency_policy(
            "delete_file",
            caps,
            confirm_fn=None,
            op_path="critical.py",
            params={"path": "critical.py"},
            task_id="headless_t1",
            approval_policy=ApprovalPolicy.HIGH_RISK,
        )
        assert "requires operator confirmation under policy 'high_risk'" in err
        assert "none is available" in err

        # If policy is explicitly NONE, it should proceed
        ok_err = await async_check_agency_policy(
            "delete_file",
            caps,
            confirm_fn=None,
            op_path="critical.py",
            params={"path": "critical.py"},
            task_id="headless_t2",
            approval_policy=ApprovalPolicy.NONE,
        )
        assert ok_err == ""

    asyncio.run(run())


def test_approval_manager_timeout():
    """M3: Verify request_approval with timeout rejects when operator does not decide."""

    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)
        task_id = "task_timeout_1"
        res = await mgr.request_approval(
            task_id=task_id,
            tool_name="delete_file",
            params={"path": "auto.py"},
            timeout=0.05,
        )
        assert res is False
        actions = mgr.get_actions_for_task(task_id)
        assert len(actions) == 1
        assert actions[0]["status"] == "rejected"
        assert "timed out" in actions[0]["decision_reason"].lower()

    asyncio.run(run())


def test_approval_manager_cross_thread_resolve():
    """M3: Verify future resolution from another thread works safely."""
    import threading

    async def run():
        mgr = ApprovalManager(default_policy=ApprovalPolicy.HIGH_RISK)
        task_id = "task_thread_1"

        req_task = asyncio.create_task(
            mgr.request_approval(
                task_id=task_id,
                tool_name="run_command",
                params={"command": "dir"},
            )
        )
        await asyncio.sleep(0.02)
        pending = mgr.get_pending_actions(task_id)
        assert len(pending) == 1
        action_id = pending[0]["action_id"]

        def thread_target():
            mgr.resolve(action_id, approved=True)

        t = threading.Thread(target=thread_target)
        t.start()
        t.join()

        res = await req_task
        assert res is True

    asyncio.run(run())


def test_server_recursive_masking():
    """M1: Verify mask_secrets_recursive redacts tokens in nested structures."""
    from server import mask_secrets_recursive

    fake_sk = "sk-" + "dummy-mock-secret-token-key-1234567890"
    nested = {
        "status": "DONE",
        "nested": {
            "token": fake_sk,
            "list": [f"Bearer {fake_sk}", "clean value"],
        },
    }
    cleaned = mask_secrets_recursive(nested)
    assert fake_sk not in str(cleaned)
    assert "[REDACTED]" in str(cleaned)
    assert cleaned["nested"]["list"][1] == "clean value"
