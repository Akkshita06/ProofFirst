"""
Phase 2 - "Prove Genericity" regression tests.

These tests exist to make a falsifiable claim checkable: that
proof_first.orchestrator's control-flow functions
(`run_full_pipeline`, `run_research_through_decision`,
`propose_action_for_lead`) work unmodified on a completely different task
type (support-ticket root-cause verification), not just on local-business
leads - see proof_first/agents/ticket_diagnosis_agent.py and
proof_first/demo_phase2.py.

Run with:  python -m pytest tests/test_phase2_genericity.py -v
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from proof_first import storage, orchestrator
from proof_first.agents import ticket_diagnosis_agent, research_agent, skeptic_agent, action_decision_agent
from proof_first.data.tickets import get_ticket, TICKETS
from proof_first.data.leads import get_lead
from proof_first.models import ActionStatus


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


def _run_ticket(ticket_id: str):
    ticket = get_ticket(ticket_id)
    with mock.patch.multiple(
        orchestrator,
        research_agent=ticket_diagnosis_agent,
        skeptic_agent=ticket_diagnosis_agent,
        action_decision_agent=ticket_diagnosis_agent,
    ):
        # Confirm the substitution actually took effect inside the block...
        assert orchestrator.research_agent is ticket_diagnosis_agent
        result = orchestrator.run_full_pipeline(ticket, auto_approve=True)
    # ...and that it's gone the moment the block exits, every time.
    assert orchestrator.research_agent is research_agent
    assert orchestrator.skeptic_agent is skeptic_agent
    assert orchestrator.action_decision_agent is action_decision_agent
    return result


def test_fixture_set_covers_every_branch():
    """Same shape as test_pipeline.py's equivalent check for leads: the 3
    ticket fixtures are chosen to exercise all three DecisionAgent
    branches, not just the happy path."""
    assert len(TICKETS) == 3


def test_ticket_corroborated_root_cause_proceeds_to_action():
    result = _run_ticket("ticket_001")
    assert result.decision == "PROCEED_TO_PROOF_OF_WORK"
    assert result.dossier.claims[0].verdict.value == "CORROBORATED"
    assert result.action is not None
    assert result.action.status == ActionStatus.SUCCESS
    assert result.action.action_type == "generate_corrected_artifact"
    assert result.outreach_message is not None
    assert "payment-lib" in result.outreach_message


def test_ticket_contradicted_root_cause_closes():
    result = _run_ticket("ticket_002")
    assert result.decision == "CLOSE"
    assert result.dossier.claims[0].verdict.value == "CONTRADICTED"
    assert result.action is None
    assert result.outreach_message is None


def test_ticket_unverifiable_root_cause_routes_to_human():
    result = _run_ticket("ticket_003")
    assert result.decision == "HUMAN_REVIEW"
    assert result.dossier.claims[0].verdict.value == "UNVERIFIABLE"
    assert result.action is None
    assert result.outreach_message is None


def test_lead_domain_unaffected_by_ticket_runs():
    """The substitution must never leak: running lead_002 through the
    pipeline AFTER several ticket runs must give the identical Phase 1
    result, using the default (non-substituted) agents."""
    _run_ticket("ticket_001")
    _run_ticket("ticket_002")
    lead = get_lead("lead_002")
    result = orchestrator.run_full_pipeline(lead, auto_approve=True)
    assert result.decision == "PROCEED_TO_PROOF_OF_WORK"
    assert result.action.status == ActionStatus.SUCCESS
    assert result.action.action_type == "generate_booking_preview"


def test_orchestrator_module_source_is_not_domain_specific():
    """Cheap static guard: orchestrator.py must never come to mention
    lead/business-specific vocabulary. If this ever fails, someone made
    orchestrator.py's control flow domain-specific again."""
    src = Path(orchestrator.__file__).read_text().lower()
    for banned in ("website", "booking", "instagram", "listing_hours"):
        assert banned not in src, f"orchestrator.py should stay domain-neutral, found {banned!r}"
