"""
EvidenceSynthesisAgent - produces the single authoritative EvidenceDossier.

Per the spec (Phase 3): "Do not allow the final outreach agent to receive
the raw research alone. The evidence dossier must become the authoritative
input." Enforced structurally: outreach_agent.py (later in the pipeline)
only ever accepts an EvidenceDossier + ProposedAction, never a raw Claim
list or raw lead dict - there is no code path around this.
"""
from __future__ import annotations

from proof_first.models import EvidenceDossier, RecommendedAction, Verdict, ActivityEvent, Claim
from proof_first import storage
from proof_first.agents.confidence import compute_confidence

CONTRADICTED_PENALTY = 0.0   # a single contradicted claim caps overall confidence hard
HIGH_CONFIDENCE_THRESHOLD = 0.75
LOW_CONFIDENCE_THRESHOLD = 0.4
# Confidence used when every claim in the dossier is UNVERIFIABLE: no
# evidence points either way, so this is the neutral-direction formula
# at a middling evidence quality (nothing actively conflicts, but
# nothing strongly independent, corroborated, or fresh backs it either).
_ALL_UNVERIFIABLE_CONFIDENCE = compute_confidence(
    direction=0, source_independence=0.5, corroboration_count=0.0,
    contradiction_present=False, freshness=0.75,
)


def synthesize(lead: dict, claims: list[Claim]) -> EvidenceDossier:
    dossier = EvidenceDossier(lead_id=lead["id"], lead_name=lead["name"], claims=claims)

    if not claims:
        dossier.overall_confidence = 0.0
        dossier.recommended_action = RecommendedAction.CLOSE
    else:
        contradicted = [c for c in claims if c.verdict == Verdict.CONTRADICTED]
        corroborated = [c for c in claims if c.verdict == Verdict.CORROBORATED]

        if contradicted:
            # A contradicted core claim caps the whole dossier's confidence,
            # regardless of how confident other unrelated claims are - this
            # is the literal mechanism that makes "the system catches
            # itself" true rather than just a UI label.
            dossier.overall_confidence = min(c.confidence for c in contradicted)
        elif corroborated:
            dossier.overall_confidence = sum(c.confidence for c in corroborated) / len(corroborated)
        else:
            dossier.overall_confidence = _ALL_UNVERIFIABLE_CONFIDENCE  # everything unverifiable

        if dossier.overall_confidence < LOW_CONFIDENCE_THRESHOLD or contradicted and not corroborated:
            dossier.recommended_action = RecommendedAction.CLOSE
        elif dossier.overall_confidence >= HIGH_CONFIDENCE_THRESHOLD and corroborated:
            dossier.recommended_action = RecommendedAction.ACT
        else:
            dossier.recommended_action = RecommendedAction.HUMAN_REVIEW

    storage.log_activity(ActivityEvent(
        lead_id=lead["id"], agent="EvidenceSynthesisAgent",
        message=f"Overall confidence {dossier.overall_confidence:.2f} -> recommended_action={dossier.recommended_action.value}",
    ))
    storage.save_dossier(dossier)
    return dossier
