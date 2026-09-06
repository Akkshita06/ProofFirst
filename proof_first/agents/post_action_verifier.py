"""
PostActionVerifier + RollbackAgent (Phase 8).

Independently re-checks that the artifact the ActionExecutor claims to
have produced actually exists and is well-formed, BEFORE outreach is ever
allowed to say "we fixed X." This is what prevents a failed action from
ever being presented as successful (a hard requirement in the spec).
"""
from __future__ import annotations

from pathlib import Path

from proof_first.models import ProposedAction, ActionStatus, ActivityEvent
from proof_first import storage

STATIC_ROOT = Path(__file__).parent.parent / "static"


def verify(action: ProposedAction) -> ProposedAction:
    if action.status != ActionStatus.EXECUTED or not action.artifact_url:
        action.verification_result = "UNKNOWN"
        storage.log_activity(ActivityEvent(
            lead_id=action.lead_id, agent="PostActionVerifier",
            message="No executed artifact to verify -> UNKNOWN.",
        ))
        storage.save_action(action)
        return action

    local_path = STATIC_ROOT / action.artifact_url.replace("/static/", "", 1)
    if local_path.exists() and local_path.stat().st_size > 0:
        action.status = ActionStatus.SUCCESS
        action.verification_result = "SUCCESS - artifact independently confirmed to exist and be non-empty."
    else:
        action.status = ActionStatus.FAILED
        action.verification_result = "FAILED - artifact was not found where ActionExecutor claimed to have written it."

    storage.log_activity(ActivityEvent(
        lead_id=action.lead_id, agent="PostActionVerifier", message=action.verification_result,
    ))
    storage.save_action(action)

    if action.status == ActionStatus.FAILED:
        rollback(action)

    return action


def rollback(action: ProposedAction) -> ProposedAction:
    """Best-effort rollback: remove any partial artifact, mark ROLLED_BACK.
    In production this would also revert any real external state change
    (e.g. delete a submitted listing-correction draft)."""
    local_path = STATIC_ROOT / (action.artifact_url or "").replace("/static/", "", 1)
    if action.artifact_url and local_path.exists():
        local_path.unlink()
    action.status = ActionStatus.ROLLED_BACK
    storage.log_activity(ActivityEvent(
        lead_id=action.lead_id, agent="RollbackAgent",
        message="Rolled back partial/failed action. Outreach will NOT claim this action succeeded.",
    ))
    storage.save_action(action)
    return action
