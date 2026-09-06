"""
ActionExecutor - performs a HUMAN-APPROVED, whitelisted action.

Equivalent role to SalesShortcut's tools/phone_call.py + callbacks.py
(prevent_duplicate_call_callback): a guarded tool-call boundary. Since this
project has no live listing/hosting API wired up, `_run_whitelisted_action`
actually generates and writes a real local HTML artifact to disk and
returns a real (local, inspectable) file path/URL - it does not print a
fake "posted to Google Business Profile" message. Swap `_run_whitelisted_action`
for real API calls (Business Profile API, a hosting provider) to go from
demo mode to production; the guard/verify structure around it does not
need to change.
"""
from __future__ import annotations

from pathlib import Path

from proof_first.models import ProposedAction, ActionStatus, ActivityEvent
from proof_first import storage

ARTIFACT_DIR = Path(__file__).parent.parent / "static" / "artifacts"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)


def before_tool_callback(action: ProposedAction) -> bool:
    """Duplicate-action guard, run immediately before the real tool call -
    mirrors SalesShortcut's before_tool_callback pattern.

    Uses storage.try_claim_action rather than a plain already_actioned()
    check: two concurrent execute() calls for the same (lead_id,
    action_type) would otherwise both see "not yet actioned" and both
    proceed (a classic TOCTOU race - see
    tests/test_concurrency.py::test_concurrent_approvals_only_one_succeeds).
    try_claim_action closes that window atomically at the database level.
    """
    if not storage.try_claim_action(action.lead_id, action.action_type, action.id):
        storage.log_activity(ActivityEvent(
            lead_id=action.lead_id, agent="ActionExecutor",
            message="BLOCKED at tool-call boundary: duplicate action detected (or a concurrent execution is already in flight).",
        ))
        return False
    return True


def _run_whitelisted_action(action: ProposedAction, lead_name: str) -> str:
    """Actually produces a real, inspectable local artifact."""
    filename = f"{action.lead_id}_{action.action_type}.html"
    path = ARTIFACT_DIR / filename
    path.write_text(
        f"<html><body style='font-family:sans-serif;padding:2rem'>"
        f"<h2>Verified fix for {lead_name}</h2>"
        f"<p><b>Action type:</b> {action.action_type}</p>"
        f"<p><b>Description:</b> {action.description}</p>"
        f"<p><i>This is a real, locally-generated artifact standing in for a "
        f"published listing correction / hosted booking preview. In production "
        f"this write would target the Business Profile API or a hosting "
        f"provider instead of a local file.</i></p>"
        f"</body></html>"
    )
    return f"/static/artifacts/{filename}"


def execute(action: ProposedAction, lead_name: str) -> ProposedAction:
    if action.status != ActionStatus.APPROVED:
        raise ValueError("execute() called on an action that was not APPROVED - refusing to run.")

    if not before_tool_callback(action):
        action.status = ActionStatus.BLOCKED
        action.block_reason = "Duplicate action blocked at execution boundary."
        storage.save_action(action)
        return action

    try:
        url = _run_whitelisted_action(action, lead_name)
        action.artifact_url = url
        action.status = ActionStatus.EXECUTED
        storage.log_activity(ActivityEvent(
            lead_id=action.lead_id, agent="ActionExecutor",
            message=f"Executed {action.action_type} -> artifact at {url}",
        ))
    except Exception as e:  # pragma: no cover
        action.status = ActionStatus.FAILED
        action.verification_result = f"Execution raised an exception: {e}"
        storage.log_activity(ActivityEvent(
            lead_id=action.lead_id, agent="ActionExecutor",
            message=f"Execution FAILED: {e}",
        ))
    finally:
        # Safe to release unconditionally here: on EXECUTED, already_actioned()
        # now permanently blocks this (lead_id, action_type) regardless of the
        # claims table; on FAILED, releasing allows a legitimate future retry.
        storage.release_action_claim(action.lead_id, action.action_type)

    storage.save_action(action)
    return action
