"""
ReflectionAgent - Track 1's learning loop.

SkepticAgent's verdicts are deterministic and rule-based (see skeptic_agent.py
docstrings) - by design, they don't change on their own. What CAN change is
which secondary_signal values are trusted, once we have evidence the
deterministic rule was too confident (or not confident enough) on some lead.

ReflectionAgent is the piece that produces that evidence. It runs in two
places:

1. After a full pipeline run, comparing the claim's predicted verdict
   against the ACTUAL outcome of acting on it (did the resulting action
   succeed at post_action_verifier, or fail/roll back?). A CORROBORATED
   claim whose action then FAILED is a miss worth learning from.

2. After an explicit human correction (`POST /api/leads/{id}/feedback`),
   which is the fastest, most demo-friendly way to show the loop working
   live: a judge can say "actually that claim was wrong" and watch the
   Skeptic get more cautious about the same signal on the *next* lead.

Every reflection - whether or not it produces a promoted pattern - is
logged via storage.log_reflection so the "self-reflection" trail is
visible on the dashboard, not just its end effect.

Pattern extraction itself stays fully deterministic (a lookup table keyed
on claim_type, not an LLM guessing at generalizations) so a promoted
pattern is exactly as auditable as everything else in this pipeline. An
LLM call is used ONLY to produce a human-readable rationale string, mirroring
the same "LLM narrates, rules decide" split used in skeptic_agent.py.
"""
from __future__ import annotations

import uuid
from typing import Optional

from proof_first.models import Claim, EvidenceDossier, Verdict, ActivityEvent
from proof_first import storage
from proof_first import llm_client
from proof_first.tracing import span
from proof_first.agents.pattern_matching import build_trigger_signal

# Which secondary_signal key each claim type's deterministic check actually
# hinges on - kept as an explicit table (not inferred) so pattern proposals
# stay traceable to the exact _check_* logic in skeptic_agent.py.
_DECISIVE_SIGNAL_BY_CLAIM_TYPE = {
    "booking_claim": "instagram_recent_posts_mention_dm_booking",
    "broken_link_claim": "booking_link_http_status",
    "hours_claim": "actual_hours_from_gbp",
}

_BOUNDED_DELTA = 0.35  # cap on how much a single learned pattern can shift confidence


def _classify_claim_type(claim_text: str) -> Optional[str]:
    text = claim_text.lower()
    if "no online booking" in text:
        return "booking_claim"
    if "broken or unreliable" in text:
        return "broken_link_claim"
    if "listing hours" in text:
        return "hours_claim"
    if "has no website" in text:
        return "no_website_claim"
    return None


def _rationale(claim: Claim, predicted: str, actual: str) -> str:
    prompt = (
        f"A claim was predicted as {predicted} but the real outcome was {actual}. "
        f"Claim: {claim.text}. In one sentence, explain what generalizable lesson "
        "this suggests about trusting this type of secondary signal in future leads."
    )
    try:
        text = llm_client.get_llm().complete(
            prompt, system="You write terse, one-sentence lessons for an audit log. No hedging."
        ).strip()
        # MOCK_MODE's stub client echoes the prompt back for debuggability
        # elsewhere in this codebase, but that makes a poor dashboard
        # rationale here - fall back to a clean, honest label instead.
        if "MOCK_MODE" in text or "system=" in text:
            raise ValueError("mock echo, not a real rationale")
        return text
    except Exception:
        return f"Predicted {predicted}, actual outcome was {actual} - lowering trust in this signal type."


def _propose_pattern(lead: dict, claim: Claim, predicted: Verdict, actual_was_bad: bool) -> Optional[dict]:
    claim_type = _classify_claim_type(claim.text)
    signal_key = _DECISIVE_SIGNAL_BY_CLAIM_TYPE.get(claim_type)
    if not claim_type or not signal_key:
        return None  # no known decisive signal to generalize from (e.g. no_website_claim)

    secondary = lead.get("secondary_signals", {})
    if signal_key not in secondary:
        return None
    observed_value = secondary[signal_key]

    # A miss on a CORROBORATED verdict means: trust this signal value LESS
    # next time (negative delta). A miss on a CONTRADICTED/UNVERIFIABLE
    # verdict that turned out fine means trust it slightly MORE (positive
    # delta) - but we keep this pass conservative and only ever generalize
    # in the direction of caution, since over-correcting toward automatic
    # trust is the riskier failure mode for a system whose whole premise is
    # skepticism.
    delta = -_BOUNDED_DELTA if actual_was_bad else 0.0
    if delta == 0.0:
        return None

    return {
        "claim_type": claim_type,
        "trigger_signal": build_trigger_signal(signal_key, observed_value),
        "confidence_delta": delta,
        "rationale": _rationale(claim, predicted.value, "FAILED" if actual_was_bad else "OK"),
        "source_lead_ids": [lead["id"]],
    }


@span(kind="TOOL", name="reflection_agent.predicted_vs_actual", capture_input=True, capture_output=True)
def _compare_predicted_vs_actual(predicted_verdict: str, actual_outcome: str, decisive_signal: Optional[str]) -> bool:
    """The actual predicted-vs-actual comparison at the heart of the
    learning loop: was the Skeptic's earlier CORROBORATED verdict on this
    claim contradicted by what actually happened once we acted on it?
    Pulled out into its own function (rather than inlined in the loop in
    reflect_on_pipeline_result) purely so it can be traced as a standalone
    Neatlogs span with `predicted_verdict`, `actual_outcome`, and the
    decisive `_DECISIVE_SIGNAL_BY_CLAIM_TYPE` entry captured as span
    attributes (via capture_input) - the comparison logic itself is
    unchanged either way.
    """
    return actual_outcome in ("FAILED", "ROLLED_BACK")


def _log_and_maybe_promote(lead_id: str, claim: Claim, actual_outcome: str, was_miss: bool, lead: dict, actual_was_bad: bool):
    pattern_id = None
    if was_miss:
        proposal = _propose_pattern(lead, claim, claim.verdict, actual_was_bad)
        if proposal:
            pattern_id, promoted = storage.upsert_learned_pattern(proposal)
            storage.log_activity(ActivityEvent(
                lead_id=lead_id, agent="ReflectionAgent",
                message=(
                    f"Miss on \"{claim.text[:50]}...\": predicted {claim.verdict.value}, outcome was bad. "
                    f"{'Promoted' if promoted else 'Recorded candidate'} pattern {pattern_id} "
                    f"({'now active - 2+ corroborating leads' if promoted else 'awaiting a second corroborating lead'})."
                ),
            ))

    storage.log_reflection({
        "id": uuid.uuid4().hex[:12],
        "lead_id": lead_id,
        "claim_id": claim.id,
        "predicted_verdict": claim.verdict.value if claim.verdict else "NONE",
        "actual_outcome": actual_outcome,
        "was_miss": was_miss,
        "pattern_id": pattern_id,
    })
    return pattern_id


def reflect_on_pipeline_result(lead: dict, dossier: EvidenceDossier, action_status: Optional[str]) -> list[str]:
    """Called once a ProposedAction reaches a terminal state (SUCCESS,
    FAILED, or ROLLED_BACK). A CORROBORATED claim whose action then failed
    is exactly the kind of miss the Skeptic's static thresholds cannot see
    on their own - this closes that loop."""
    if action_status not in ("FAILED", "ROLLED_BACK", "SUCCESS"):
        return []

    promoted_ids = []
    for claim in dossier.claims:
        if claim.verdict != Verdict.CORROBORATED:
            continue  # only corroborated claims lead to an action worth reflecting on
        claim_type = _classify_claim_type(claim.text)
        decisive_signal = _DECISIVE_SIGNAL_BY_CLAIM_TYPE.get(claim_type)
        was_bad_outcome = _compare_predicted_vs_actual(
            predicted_verdict=claim.verdict.value,
            actual_outcome=action_status,
            decisive_signal=decisive_signal,
        )
        pattern_id = _log_and_maybe_promote(
            lead["id"], claim, actual_outcome=action_status,
            was_miss=was_bad_outcome, lead=lead, actual_was_bad=was_bad_outcome,
        )
        if pattern_id:
            promoted_ids.append(pattern_id)
    return promoted_ids


def reflect_on_human_feedback(lead: dict, dossier: EvidenceDossier, claim_id: str, human_verdict: str) -> Optional[str]:
    """Called from POST /api/leads/{id}/feedback - a human directly telling
    the system a claim's predicted verdict was wrong. This is the fastest
    demo path: propose a pattern immediately (still gated by the same
    2-corroborating-lead promotion rule in storage.upsert_learned_pattern)."""
    claim = next((c for c in dossier.claims if c.id == claim_id), None)
    if claim is None:
        return None

    was_miss = claim.verdict is not None and claim.verdict.value != human_verdict
    actual_was_bad = was_miss and claim.verdict == Verdict.CORROBORATED
    pattern_id = _log_and_maybe_promote(
        lead["id"], claim, actual_outcome=f"HUMAN_SAYS_{human_verdict}",
        was_miss=was_miss, lead=lead, actual_was_bad=actual_was_bad,
    )
    return pattern_id
