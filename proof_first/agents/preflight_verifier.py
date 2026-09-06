"""
PreflightVerifierAgent - the last real check before anything touches the
outside world. Implements Phase 6 exactly:
  1. factual basis still valid
  2. action matches the evidence
  3. action is reversible
  4. action is in the whitelist
  5. same action not already performed for this lead

Any failed condition -> BLOCK ACTION, with a stored, inspectable reason.
This is a plain function, not an LLM call, on purpose: preflight safety
checks should be deterministic and auditable, not subject to model
sampling variance.
"""
from __future__ import annotations

from proof_first.models import ProposedAction, EvidenceDossier, Verdict, ActionStatus, ACTION_WHITELIST, ActivityEvent
from proof_first import storage


def preflight(action: ProposedAction, dossier: EvidenceDossier) -> ProposedAction:
    reasons = []

    target_claim = next((c for c in dossier.claims if c.id == action.defect_claim_id), None)
    if target_claim is None or target_claim.verdict != Verdict.CORROBORATED:
        reasons.append("Factual basis is no longer CORROBORATED.")

    if action.action_type not in ACTION_WHITELIST:
        reasons.append(f"Action type '{action.action_type}' is not in the whitelist.")

    if not action.reversible:
        reasons.append("Action is not marked reversible.")

    if storage.already_actioned(action.lead_id, action.action_type):
        reasons.append("An identical action has already succeeded for this lead (duplicate-action guard).")

    if reasons:
        action.status = ActionStatus.BLOCKED
        action.block_reason = "; ".join(reasons)
    else:
        action.status = ActionStatus.PENDING_APPROVAL

    storage.log_activity(ActivityEvent(
        lead_id=action.lead_id, agent="PreflightVerifierAgent",
        message=f"Preflight -> {action.status.value}" + (f" ({action.block_reason})" if action.block_reason else ""),
    ))
    storage.save_action(action)
    return action
