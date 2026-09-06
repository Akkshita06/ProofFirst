"""
Phase 2 demo - "Prove Genericity".

Runs TWO different task types - a local-business LEAD claim and a
support-TICKET root-cause claim - through the exact same, byte-for-byte
unmodified orchestrator functions:

    orchestrator.run_full_pipeline()
        -> orchestrator.run_research_through_decision()
        -> orchestrator.propose_action_for_lead()
        -> orchestrator.approve_action()
        -> orchestrator.finalize_outreach()

orchestrator.py is not edited at all for this. Its four agent stages
(research_agent, skeptic_agent, evidence_synthesis_agent, decision_router,
action_decision_agent, preflight_verifier, action_executor,
post_action_verifier, outreach_agent) are looked up as plain module
attributes at call time, so for the ticket run we substitute
`ticket_diagnosis_agent` in place of `research_agent` / `skeptic_agent` /
`action_decision_agent` for the duration of the call using
unittest.mock.patch.multiple - a standard dependency-substitution
technique, not a source change. evidence_synthesis_agent, decision_router,
preflight_verifier, action_executor, post_action_verifier, and
outreach_agent are NEVER swapped - they run completely unmodified for
both domains, because they were already domain-neutral.

Run with:
    python -m proof_first.demo_phase2
"""
from __future__ import annotations

from unittest import mock

from proof_first import storage, orchestrator
from proof_first.data.leads import get_lead
from proof_first.agents import ticket_diagnosis_agent
from proof_first.data.tickets import get_ticket


def _print_result(domain: str, result) -> None:
    dossier = result.dossier
    print(f"\n=== [{domain}] {result.lead_name} ({result.lead_id}) ===")
    for c in dossier.claims:
        print(f"  claim: {c.text}")
        print(f"    verdict={c.verdict.value if c.verdict else None} confidence={c.confidence:.2f}")
    print(f"  overall_confidence={dossier.overall_confidence:.2f} decision={result.decision}")
    if result.action:
        print(f"  action: {result.action.action_type} -> status={result.action.status.value}")
        if result.action.artifact_url:
            print(f"  artifact: {result.action.artifact_url}")
    if result.outreach_message:
        print(f"  outreach:\n{result.outreach_message}")
    if result.stopped_reason:
        print(f"  stopped_reason: {result.stopped_reason}")


def run_lead_domain(lead_id: str = "lead_002"):
    """Task type #1 (Phase 1): local-business lead claim verification.
    Uses orchestrator's DEFAULT agent wiring - no substitution needed."""
    lead = get_lead(lead_id)
    return orchestrator.run_full_pipeline(lead, auto_approve=True)


def run_ticket_domain(ticket_id: str = "ticket_001"):
    """Task type #2 (Phase 2): support-ticket root-cause claim
    verification. Same orchestrator.run_full_pipeline() call, same
    run_research_through_decision()/propose_action_for_lead() code paths -
    only the research/skeptic/action-decision layer is substituted, and
    only for the duration of this call."""
    ticket = get_ticket(ticket_id)
    with mock.patch.multiple(
        orchestrator,
        research_agent=ticket_diagnosis_agent,
        skeptic_agent=ticket_diagnosis_agent,
        action_decision_agent=ticket_diagnosis_agent,
    ):
        return orchestrator.run_full_pipeline(ticket, auto_approve=True)


def main() -> None:
    storage.init_db(reset=True)

    lead_result = run_lead_domain("lead_002")       # -> PROCEED_TO_PROOF_OF_WORK, ACT
    ticket_result_act = run_ticket_domain("ticket_001")   # -> PROCEED_TO_PROOF_OF_WORK, ACT
    ticket_result_close = run_ticket_domain("ticket_002")  # -> CLOSE
    ticket_result_review = run_ticket_domain("ticket_003")  # -> HUMAN_REVIEW

    _print_result("lead", lead_result)
    _print_result("ticket", ticket_result_act)
    _print_result("ticket", ticket_result_close)
    _print_result("ticket", ticket_result_review)

    # Proof that nothing leaked: orchestrator's default wiring is back to
    # the lead-domain agents after every `with` block above exits.
    from proof_first.agents import research_agent as lead_research_agent
    assert orchestrator.research_agent is lead_research_agent
    print("\norchestrator.py's default agent wiring is intact after both domains ran through it.")


if __name__ == "__main__":
    main()
