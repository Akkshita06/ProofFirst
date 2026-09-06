"""
DecisionAgent / three-way router.

Plays the same structural role as SalesShortcut's sdr_router.py (a plain
function/class making a control-flow decision outside the LLM, rather than
asking a model to "decide" via free text) - but branches three ways
instead of two, and branches on evidence confidence BEFORE any contact is
made, not on a post-call outcome classification.

This function is what makes the routing real rather than a cosmetic status
field: orchestrator.py literally does not call action_decision_agent or
outreach_agent unless this function returns "ACT", and does not call
outreach_agent unless a later stage independently reports SUCCESS. There
is no path in orchestrator.py that lets a CLOSE-routed lead reach outreach.
"""
from __future__ import annotations

from proof_first.models import EvidenceDossier, RecommendedAction, ActivityEvent
from proof_first import storage


def route(dossier: EvidenceDossier) -> str:
    action = dossier.recommended_action

    if action == RecommendedAction.CLOSE:
        decision = "CLOSE"
        note = "Evidence insufficient or contradicted - do NOT contact this lead."
    elif action == RecommendedAction.HUMAN_REVIEW:
        decision = "HUMAN_REVIEW"
        note = "Confidence is medium / claims are unverifiable - routed to a human with the dossier attached."
    elif action == RecommendedAction.ACT:
        decision = "PROCEED_TO_PROOF_OF_WORK"
        note = "High confidence, corroborated defect available - proceeding to Feature 2."
    else:
        decision = "CLOSE"
        note = "No recommended_action set - failing closed."

    storage.log_activity(ActivityEvent(
        lead_id=dossier.lead_id, agent="DecisionAgent", message=f"{decision}: {note}",
    ))
    return decision
