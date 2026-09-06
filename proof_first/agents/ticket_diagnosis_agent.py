"""
TicketDiagnosisAgent - Phase 2 (Prove Genericity).

This is the "one new thin agent adapter" for the SECOND task type:
verifying a support-ticket ROOT-CAUSE claim ("this deploy caused the
incident") instead of a local-business lead claim ("this listing has no
booking link"). It exists purely to demonstrate that
proof_first.orchestrator's control-flow functions
(`run_research_through_decision`, `propose_action_for_lead`) and the
shared dataclasses in models.py (`Claim`, `Evidence`, `EvidenceDossier`,
`ProposedAction`) do not encode any local-business-specific assumptions -
see demo_phase2.py for how this module is swapped in.

It intentionally exposes the SAME three function signatures the
orchestrator already calls by name on the lead-domain modules:

    research_agent.research_lead(lead)        -> ticket_diagnosis_agent.research_lead(ticket)
    skeptic_agent.verify_all(lead, claims)     -> ticket_diagnosis_agent.verify_all(ticket, claims)
    action_decision_agent.decide_action(d)     -> ticket_diagnosis_agent.decide_action(dossier)

Nothing downstream of these three calls needed a single line changed:
EvidenceSynthesisAgent, DecisionAgent, PreflightVerifierAgent,
ActionExecutor, PostActionVerifier, and OutreachAgent are already
domain-neutral as written - they only ever reason over Verdict,
confidence, reversible/whitelist flags, and dossier/action status, never
over lead- or business-specific text. That is the actual proof: 5 of the
8 pipeline stages required ZERO adapter code, and the 3 that did
(research/skeptic/action-decision - the "trust, then challenge" layer)
only needed a same-shaped drop-in, not a change to how they're called.

Kept deliberately thin and deterministic (no LLM calls) - the point here
is proving the pipeline's *shape* is domain-neutral, not re-demonstrating
Phase 1's live-LLM wording behavior a second time.
"""
from __future__ import annotations

from proof_first.models import (
    Claim,
    Evidence,
    Verdict,
    EvidenceDossier,
    ProposedAction,
    ActivityEvent,
    ACTION_WHITELIST,
)
from proof_first import storage


def research_lead(ticket: dict) -> list[Claim]:
    """Single, untrusted pass: turn primary_signals into ONE candidate
    root-cause claim. Mirrors research_agent.research_lead()'s contract
    exactly (dict in, list[Claim] out) but the ticket dict never contains
    a `has_website`/`site_has_booking_link` key - proving the orchestrator
    and downstream agents never needed those keys to exist.
    """
    sig = ticket["primary_signals"]
    system = ticket.get("system", ticket["name"])

    if sig.get("deploy_time_correlates_with_spike"):
        text = (
            f"The {sig['recent_deploy']} deploy is the likely root cause of "
            f"\"{sig['error_signature']}\" on {system}'s {sig['affected_endpoint']}."
        )
        reasoning = "Primary pass found a recent deploy whose timing correlates with the error spike."
    else:
        text = (
            f"No recent deploy correlates with \"{sig['error_signature']}\" on "
            f"{system}'s {sig['affected_endpoint']}; root cause is not yet identified from deploy history alone."
        )
        reasoning = "Primary pass found no deploy whose timing correlates with the regression."

    claim = Claim(text=text, origin_agent="TicketDiagnosisAgent", reasoning=reasoning)

    storage.log_activity(ActivityEvent(
        lead_id=ticket["id"], agent="TicketDiagnosisAgent",
        message="Generated 1 candidate root-cause claim from a single deploy-log/ticket pass.",
    ))
    return [claim]


def verify_all(ticket: dict, claims: list[Claim]) -> list[Claim]:
    """Independent second pass: try to disprove each root-cause claim using
    a SECOND signal set the first pass never looked at (rollback outcome,
    or a shared-dependency incident affecting services that never
    deployed). Mirrors skeptic_agent.verify_all()'s contract exactly.
    """
    secondary = ticket["secondary_signals"]
    storage.log_activity(ActivityEvent(
        lead_id=ticket["id"], agent="TicketDiagnosisAgent",
        message="Independently checking rollback outcome / shared-dependency incidents...",
    ))

    for claim in claims:
        if secondary.get("rollback_performed") and secondary.get("error_rate_after_rollback"):
            claim.supporting_evidence.append(Evidence(
                text=f"Rolling back the deploy independently confirmed the fix: {secondary['error_rate_after_rollback']}.",
                source="rollback_outcome",
                supports=True,
            ))
            claim.verdict = Verdict.CORROBORATED
            claim.confidence = 0.93
            claim.reasoning = "An independent rollback confirmed the error was tied to the deploy, not just correlated with it."
        elif secondary.get("shared_dependency_incident_id") and secondary.get("other_undeployed_services_also_errored"):
            claim.contradicting_evidence.append(Evidence(
                text=(
                    f"Shared-dependency incident {secondary['shared_dependency_incident_id']} also hit "
                    "other services that did not deploy anything - the deploy is not the root cause."
                ),
                source="incident_timeline",
                supports=False,
            ))
            claim.verdict = Verdict.CONTRADICTED
            claim.confidence = 0.15
            claim.reasoning = "An independent incident-timeline check found the same error on undeployed services, ruling out the deploy."
        else:
            claim.verdict = Verdict.UNVERIFIABLE
            claim.confidence = 0.4
            claim.reasoning = "No rollback was attempted and no shared-dependency incident is on record - nothing independently confirms or rules out the deploy."

        storage.log_activity(ActivityEvent(
            lead_id=ticket["id"], agent="TicketDiagnosisAgent",
            message=f"Verdict on \"{claim.text[:60]}...\" -> {claim.verdict.value} (confidence {claim.confidence:.2f})",
        ))

    return claims


def decide_action(dossier: EvidenceDossier) -> ProposedAction | None:
    """Mirrors action_decision_agent.decide_action()'s contract exactly:
    pick the smallest safe, reversible, whitelisted action for the
    highest-confidence CORROBORATED claim. Reuses the SAME
    models.ACTION_WHITELIST - "generate_corrected_artifact" is generic
    enough to mean "a corrected hours snippet" for a lead OR "a root-cause
    postmortem artifact" for a ticket; no new whitelist entry, and no
    change to models.py, was needed.
    """
    corroborated = [c for c in dossier.claims if c.verdict == Verdict.CORROBORATED]
    if not corroborated:
        return None

    target = max(corroborated, key=lambda c: c.confidence)
    action_type = "generate_corrected_artifact"
    if action_type not in ACTION_WHITELIST:  # pragma: no cover - defensive, mirrors action_decision_agent
        return None

    action = ProposedAction(
        lead_id=dossier.lead_id,
        defect_claim_id=target.id,
        description=f"Generate a root-cause postmortem artifact confirming and documenting: \"{target.text}\"",
        action_type=action_type,
        reversible=True,
        risk="LOW",
    )

    storage.log_activity(ActivityEvent(
        lead_id=dossier.lead_id, agent="TicketDiagnosisAgent",
        message=(
            f"Confirmed root cause: {target.text}\n"
            f"Proposed action: {action.description}\n"
            f"Why safe: reversible={action.reversible}, risk={action.risk}, whitelisted type={action.action_type}"
        ),
    ))
    storage.save_action(action)
    return action
