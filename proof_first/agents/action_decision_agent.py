"""
ActionDecisionAgent - Feature 2's entry point.

Objective: select the SMALLEST safe, reversible, verifiable improvement
supported by the evidence dossier's highest-confidence CORROBORATED claim.
Explicitly refuses to propose a large rebuild - only whitelisted action
types (models.ACTION_WHITELIST) are ever proposed.
"""
from __future__ import annotations

from proof_first.models import EvidenceDossier, ProposedAction, Verdict, ActivityEvent
from proof_first import storage


def decide_action(dossier: EvidenceDossier) -> ProposedAction | None:
    corroborated = [c for c in dossier.claims if c.verdict == Verdict.CORROBORATED]
    if not corroborated:
        return None

    # Prefer the highest-confidence corroborated claim as the target defect.
    target = max(corroborated, key=lambda c: c.confidence)
    text = target.text.lower()

    if "broken" in text or "unreliable" in text:
        action = ProposedAction(
            lead_id=dossier.lead_id,
            defect_claim_id=target.id,
            description=f"Generate a working, hosted booking-flow preview page proving the fix, referencing: \"{target.text}\"",
            action_type="generate_booking_preview",
            reversible=True,
            risk="LOW",
        )
    elif "listing hours" in text:
        action = ProposedAction(
            lead_id=dossier.lead_id,
            defect_claim_id=target.id,
            description=f"Draft a corrected public-listing hours detail based on: \"{target.text}\"",
            action_type="propose_listing_correction",
            reversible=True,
            risk="LOW",
        )
    elif "no website" in text or "no online booking" in text:
        action = ProposedAction(
            lead_id=dossier.lead_id,
            defect_claim_id=target.id,
            description=f"Generate a minimal, corrected artifact (one-page booking preview) demonstrating the fix for: \"{target.text}\"",
            action_type="generate_corrected_artifact",
            reversible=True,
            risk="LOW",
        )
    else:
        # No whitelisted action fits this defect type - do nothing rather
        # than force an unsafe/unsupported action.
        storage.log_activity(ActivityEvent(
            lead_id=dossier.lead_id, agent="ActionDecisionAgent",
            message=f"No whitelisted action type fits defect \"{target.text[:50]}...\" - no action proposed.",
        ))
        return None

    storage.log_activity(ActivityEvent(
        lead_id=dossier.lead_id, agent="ActionDecisionAgent",
        message=(
            f"Detected defect: {target.text}\n"
            f"Proposed action: {action.description}\n"
            f"Why safe: reversible={action.reversible}, risk={action.risk}, whitelisted type={action.action_type}\n"
            f"Why smallest: single narrowly-scoped artifact, not a full rebuild."
        ),
    ))
    storage.save_action(action)
    return action
