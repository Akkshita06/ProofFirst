"""
OutreachAgent - Phase 9.

Structural guarantee: this function's signature only accepts an
EvidenceDossier and a ProposedAction whose status is already SUCCESS.
There is no code path that lets it fabricate an action that did not
happen, and no code path that lets it fire on a CONTRADICTED/CLOSE lead -
orchestrator.py never calls this function in those branches at all.

Rate limiting (Track 1 hardening): this is the LAST gate before an
outbound send, run strictly after orchestrator.approve_action() (the
human-approval checkpoint) and preflight_verifier.py have already passed -
it does not replace either check, it adds a final one immediately before
the message is actually generated/recorded. If a lead has already had
OUTREACH_RATE_LIMIT_PER_HOUR sends within the last hour, this raises
OutreachRateLimitExceeded instead of silently dropping the send or
returning a generic success - callers (orchestrator.finalize_outreach's
callers) are expected to catch it and surface the block explicitly.
"""
from __future__ import annotations

import os
import time

from proof_first.models import EvidenceDossier, ProposedAction, ActionStatus, Verdict, ActivityEvent
from proof_first import storage

# Ad hoc env-var config, following the existing pattern in llm_client.py /
# server.py (no dedicated settings module in this codebase).
OUTREACH_RATE_LIMIT_PER_HOUR = int(os.environ.get("OUTREACH_RATE_LIMIT_PER_HOUR", "3"))
OUTREACH_RATE_LIMIT_WINDOW_SECONDS = 3600.0


class OutreachRateLimitExceeded(Exception):
    """Raised when a lead has already hit its outbound-message quota for
    the current rolling window. Never caught silently inside this module -
    it is meant to propagate to a caller that will log/surface it."""

    def __init__(self, lead_id: str, limit: int, current_count: int):
        self.lead_id = lead_id
        self.limit = limit
        self.current_count = current_count
        super().__init__(
            f"Outreach rate limit exceeded for lead {lead_id}: "
            f"{current_count}/{limit} sends already recorded in the last hour."
        )


def generate_outreach(dossier: EvidenceDossier, action: ProposedAction) -> str:
    if action.status != ActionStatus.SUCCESS:
        raise ValueError("generate_outreach() must never be called with a non-SUCCESS action.")

    allowed, count = storage.try_claim_outreach_send(
        dossier.lead_id, OUTREACH_RATE_LIMIT_PER_HOUR, OUTREACH_RATE_LIMIT_WINDOW_SECONDS
    )
    if not allowed:
        storage.log_activity(ActivityEvent(
            lead_id=dossier.lead_id, agent="OutreachAgent",
            message=(
                f"BLOCKED: outreach rate limit exceeded for lead {dossier.lead_id} "
                f"({count}/{OUTREACH_RATE_LIMIT_PER_HOUR} sends in the last hour, "
                f"at {time.time():.0f}) - send withheld, not silently dropped."
            ),
        ))
        raise OutreachRateLimitExceeded(dossier.lead_id, OUTREACH_RATE_LIMIT_PER_HOUR, count)

    target_claim = next(c for c in dossier.claims if c.id == action.defect_claim_id)
    corroborating_sources = ", ".join(sorted({e.source for e in target_claim.supporting_evidence})) or "our research"

    message = (
        f"Hi {dossier.lead_name},\n\n"
        f"We noticed: {target_claim.text}\n\n"
        f"We checked it independently ({corroborating_sources}) and confirmed it.\n\n"
        f"We went ahead and prepared it: {action.description}\n\n"
        f"Here's the proof: {action.artifact_url}\n\n"
        f"If you'd like, we can take care of the remaining work too.\n"
    )

    storage.save_outreach(dossier.lead_id, message)
    storage.log_activity(ActivityEvent(
        lead_id=dossier.lead_id, agent="OutreachAgent",
        message="Generated proof-first outreach message (leads with completed action, not a cold pitch).",
    ))
    return message
