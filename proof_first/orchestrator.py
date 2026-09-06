"""
Orchestrator - the actual control flow:

Lead Discovery(fixture) -> Research -> Skeptic -> Evidence Synthesis
    -> Decision Router
         CLOSE          -> stop, log
         HUMAN_REVIEW   -> stop, wait for a human decision (see approve_action)
         ACT            -> Action Decision -> Preflight -> [HUMAN APPROVAL] ->
                            Execute -> Post-Verify -> (SUCCESS -> Outreach)
                                                       (FAILED  -> Rollback, no outreach)

This mirrors the architecture diagram in the design doc exactly. Every
stage writes to storage.py so the dashboard/API can render the live trace.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from proof_first.models import ActionStatus, ProposedAction, EvidenceDossier
from proof_first import storage
from proof_first.tracing import span
from proof_first.agents import (
    research_agent,
    skeptic_agent,
    evidence_synthesis_agent,
    decision_router,
    action_decision_agent,
    preflight_verifier,
    action_executor,
    post_action_verifier,
    outreach_agent,
    reflection_agent,
)

# Neatlogs spans (optional - see proof_first/tracing.py and NEATLOGS_API_KEY
# in the README). These wrap each agent call so a Neatlogs-enabled run shows
# the full Research -> Skeptic -> Evidence Synthesis -> ... trace; they are
# true no-ops when Neatlogs was never initialized.
#
# IMPORTANT: each wrapper below looks up its target via the *module-level
# name* (e.g. `research_agent.research_lead`) at call time rather than
# capturing the function object at import time. test_phase2_genericity.py
# relies on `mock.patch.multiple(orchestrator, research_agent=..., ...)`
# swapping the module attribute out from under a running pipeline, and that
# substitution would silently stop working if we bound the underlying
# function once at import time instead.


@span(kind="AGENT", name="research_agent")
def _research_lead(lead):
    return research_agent.research_lead(lead)


@span(kind="AGENT", name="skeptic_agent")
def _verify_all(lead, claims):
    return skeptic_agent.verify_all(lead, claims)


@span(kind="TOOL", name="evidence_synthesis_agent")
def _synthesize(lead, claims):
    return evidence_synthesis_agent.synthesize(lead, claims)


@span(kind="AGENT", name="action_decision_agent")
def _decide_action(dossier):
    return action_decision_agent.decide_action(dossier)


@span(kind="TOOL", name="preflight_verifier")
def _preflight(action, dossier):
    return preflight_verifier.preflight(action, dossier)


@span(kind="TOOL", name="action_executor")
def _execute(action, lead_name):
    return action_executor.execute(action, lead_name)


@span(kind="TOOL", name="post_action_verifier")
def _verify(action):
    return post_action_verifier.verify(action)


@span(kind="AGENT", name="outreach_agent")
def _generate_outreach(dossier, action):
    return outreach_agent.generate_outreach(dossier, action)


@dataclass
class PipelineResult:
    lead_id: str
    lead_name: str
    dossier: EvidenceDossier
    decision: str
    action: Optional[ProposedAction] = None
    outreach_message: Optional[str] = None
    stopped_reason: Optional[str] = None
    outreach_blocked_reason: Optional[str] = None


@span(kind="WORKFLOW", name="run_research_through_decision")
def run_research_through_decision(lead: dict) -> tuple[EvidenceDossier, str]:
    """Runs everything up to (but not including) any real-world action.
    Used both by the interactive demo (which then waits for a human
    approval click) and by the evaluation harness."""
    claims = _research_lead(lead)
    claims = _verify_all(lead, claims)
    dossier = _synthesize(lead, claims)
    decision = decision_router.route(dossier)
    return dossier, decision


@span(kind="WORKFLOW", name="propose_action_for_lead")
def propose_action_for_lead(lead: dict, dossier: EvidenceDossier) -> Optional[ProposedAction]:
    """Only ever called when decision == PROCEED_TO_PROOF_OF_WORK."""
    action = _decide_action(dossier)
    if action is None:
        return None
    action = _preflight(action, dossier)
    return action  # status is now PENDING_APPROVAL or BLOCKED - waits for approve_action()


def approve_action(action: ProposedAction, lead_name: str, approved: bool) -> ProposedAction:
    """The mandatory human-approval checkpoint (Phase 7). No code path
    reaches action_executor.execute() without this function having been
    called with approved=True first."""
    if action.status == ActionStatus.BLOCKED:
        return action  # cannot approve a blocked action
    if not approved:
        action.status = ActionStatus.REJECTED
        storage.save_action(action)
        return action

    action.status = ActionStatus.APPROVED
    storage.save_action(action)
    action = _execute(action, lead_name)
    action = _verify(action)
    return action


def finalize_outreach(dossier: EvidenceDossier, action: ProposedAction) -> Optional[str]:
    """Note: does NOT catch outreach_agent.OutreachRateLimitExceeded - that
    is intentional. This function's contract (return the message, or None
    only for "no completed action to talk about") predates rate limiting
    and existing tests rely on it, so a rate-limit block is signalled by
    letting the exception propagate rather than by silently returning None
    (which here already means something else: no SUCCESS action). Callers
    that can hit rate limiting (run_full_pipeline, server.py) catch it
    explicitly and record the block instead of swallowing it."""
    if action.status != ActionStatus.SUCCESS:
        return None  # never fabricate outreach for a failed/rolled-back action
    return _generate_outreach(dossier, action)


def run_full_pipeline(lead: dict, auto_approve: bool = True) -> PipelineResult:
    """End-to-end convenience runner for the CLI demo and evaluation harness.
    `auto_approve=True` simulates a human clicking APPROVE immediately, so
    the pipeline can be timed/measured non-interactively; the dashboard's
    interactive mode calls the stage functions above directly instead and
    genuinely waits for a UI click.
    """
    dossier, decision = run_research_through_decision(lead)
    result = PipelineResult(lead_id=lead["id"], lead_name=lead["name"], dossier=dossier, decision=decision)

    if decision == "CLOSE":
        result.stopped_reason = "Closed: evidence contradicted or insufficient."
        return result
    if decision == "HUMAN_REVIEW":
        result.stopped_reason = "Routed to human review: confidence not high enough to act autonomously."
        return result

    action = propose_action_for_lead(lead, dossier)
    if action is None:
        result.stopped_reason = "No safe whitelisted action available for the corroborated defect."
        return result

    if action.status == ActionStatus.BLOCKED:
        result.action = action
        result.stopped_reason = f"Blocked at preflight: {action.block_reason}"
        return result

    action = approve_action(action, lead["name"], approved=auto_approve)
    result.action = action

    if action.status == ActionStatus.SUCCESS:
        try:
            result.outreach_message = finalize_outreach(dossier, action)
        except outreach_agent.OutreachRateLimitExceeded as e:
            result.outreach_blocked_reason = str(e)
            result.stopped_reason = f"Outreach blocked: {e}"
    else:
        result.stopped_reason = f"Action did not succeed (status={action.status.value}) - outreach withheld."

    # Track 1 learning loop: once the action has reached a terminal state we
    # have real ground truth (did acting on the corroborated claim actually
    # work?) to check the Skeptic's earlier verdict against. This is a
    # no-op for BLOCKED/REJECTED/PENDING_APPROVAL, which reflect_on_pipeline_result
    # ignores (see its docstring/guard).
    reflection_agent.reflect_on_pipeline_result(lead, dossier, action.status.value)

    return result
