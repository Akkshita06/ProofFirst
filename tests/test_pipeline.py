"""
Automated tests for the safety-critical claims made in the design doc:
  1. A contradicted claim must never reach outreach.
  2. A failed/rejected action must never be presented as successful.
  3. A duplicate action must be blocked, not silently repeated.

Run with:  python -m pytest tests/ -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch

from proof_first import storage, orchestrator
from proof_first.data.leads import get_lead, LEADS
from proof_first.models import ActionStatus


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


# Expected decision for every fixture lead in data/leads.py, derived from
# the deterministic rules in evidence_synthesis_agent.py (see the comment
# block above lead_004 in data/leads.py for why these are fully
# predictable from the claim verdicts). This is itself a regression test:
# if a fixture is edited in a way that silently changes its branch, this
# fails loudly instead of the drift going unnoticed.
EXPECTED_DECISIONS = {
    "lead_001": "CLOSE",
    "lead_002": "PROCEED_TO_PROOF_OF_WORK",
    "lead_003": "PROCEED_TO_PROOF_OF_WORK",
    "lead_004": "CLOSE",
    "lead_005": "CLOSE",
    "lead_006": "CLOSE",
    "lead_007": "PROCEED_TO_PROOF_OF_WORK",
    "lead_008": "PROCEED_TO_PROOF_OF_WORK",
    "lead_009": "PROCEED_TO_PROOF_OF_WORK",
    "lead_010": "PROCEED_TO_PROOF_OF_WORK",
    "lead_011": "PROCEED_TO_PROOF_OF_WORK",
    "lead_012": "PROCEED_TO_PROOF_OF_WORK",
    "lead_013": "HUMAN_REVIEW",
    "lead_014": "HUMAN_REVIEW",
    "lead_015": "HUMAN_REVIEW",
    "lead_016": "HUMAN_REVIEW",
    "lead_017": "PROCEED_TO_PROOF_OF_WORK",
    "lead_018": "CLOSE",
    "lead_019": "PROCEED_TO_PROOF_OF_WORK",
    "lead_020": "HUMAN_REVIEW",
}


def test_fixture_set_covers_every_branch_and_is_a_real_sample():
    """evaluation.py's numbers are only meaningful if the fixture set
    actually exercises all three routing branches and is large enough to
    be a (small but real) statistical sample, not just 2-3 illustrative
    leads."""
    assert len(LEADS) >= 15, "fixture set should be a real sample, not just a few illustrative leads"
    assert set(l["id"] for l in LEADS) == set(EXPECTED_DECISIONS.keys())

    decisions_seen = set()
    for lead in LEADS:
        _, decision = orchestrator.run_research_through_decision(lead)
        decisions_seen.add(decision)
    assert decisions_seen == {"CLOSE", "PROCEED_TO_PROOF_OF_WORK", "HUMAN_REVIEW"}


@pytest.mark.parametrize("lead_id,expected", sorted(EXPECTED_DECISIONS.items()))
def test_expected_decision_for_every_fixture_lead(lead_id, expected):
    lead = get_lead(lead_id)
    _, decision = orchestrator.run_research_through_decision(lead)
    assert decision == expected


def test_human_review_lead_stops_pipeline_before_action_decision_agent():
    """HUMAN_REVIEW leads must stop the pipeline entirely and never reach
    ActionDecisionAgent - there was previously zero fixture/test coverage
    of this branch at all."""
    lead = get_lead("lead_013")  # single UNVERIFIABLE claim -> HUMAN_REVIEW
    with patch(
        "proof_first.orchestrator.action_decision_agent.decide_action"
    ) as mock_decide:
        result = orchestrator.run_full_pipeline(lead, auto_approve=True)

    assert result.decision == "HUMAN_REVIEW"
    assert result.action is None
    assert result.outreach_message is None
    assert result.stopped_reason is not None
    assert "human review" in result.stopped_reason.lower()
    mock_decide.assert_not_called()


def test_human_review_lead_never_reaches_outreach_or_storage_actions():
    lead = get_lead("lead_014")
    result = orchestrator.run_full_pipeline(lead, auto_approve=True)
    assert result.decision == "HUMAN_REVIEW"
    assert storage.get_outreach(lead["id"]) is None
    assert storage.list_actions(lead["id"]) == []


def test_contradicted_claim_never_reaches_outreach():
    lead = get_lead("lead_001")  # Acme Dental - Skeptic contradicts the booking claim
    result = orchestrator.run_full_pipeline(lead, auto_approve=True)
    assert result.decision == "CLOSE"
    assert result.outreach_message is None
    assert storage.get_outreach(lead["id"]) is None


def test_high_confidence_lead_completes_with_verified_outreach():
    lead = get_lead("lead_002")  # Acme Cafe - broken link is CORROBORATED
    result = orchestrator.run_full_pipeline(lead, auto_approve=True)
    assert result.decision == "PROCEED_TO_PROOF_OF_WORK"
    assert result.action.status == ActionStatus.SUCCESS
    assert result.outreach_message is not None
    assert result.action.artifact_url in result.outreach_message


def test_rejected_action_never_produces_outreach():
    lead = get_lead("lead_003")
    dossier, decision = orchestrator.run_research_through_decision(lead)
    assert decision == "PROCEED_TO_PROOF_OF_WORK"
    action = orchestrator.propose_action_for_lead(lead, dossier)
    action = orchestrator.approve_action(action, lead["name"], approved=False)
    assert action.status == ActionStatus.REJECTED
    outreach = orchestrator.finalize_outreach(dossier, action)
    assert outreach is None


def test_duplicate_action_is_blocked_not_repeated():
    lead = get_lead("lead_002")
    r1 = orchestrator.run_full_pipeline(lead, auto_approve=True)
    assert r1.action.status == ActionStatus.SUCCESS

    # Re-run the whole pipeline against the same lead a second time.
    dossier2, decision2 = orchestrator.run_research_through_decision(lead)
    action2 = orchestrator.propose_action_for_lead(lead, dossier2)
    assert action2.status == ActionStatus.BLOCKED
    assert "duplicate" in action2.block_reason.lower()


def test_failed_action_triggers_rollback_and_withholds_outreach(monkeypatch):
    from proof_first.agents import action_executor
    lead = get_lead("lead_002")
    dossier, decision = orchestrator.run_research_through_decision(lead)
    action = orchestrator.propose_action_for_lead(lead, dossier)

    # Force the executor to "succeed" but at a path that will fail
    # post-action verification, simulating a real-world failure.
    def broken_run(action, lead_name):
        action.artifact_url = "/static/artifacts/does_not_exist.html"
        return "/static/artifacts/does_not_exist.html"

    monkeypatch.setattr(action_executor, "_run_whitelisted_action", broken_run)

    action.status = ActionStatus.APPROVED
    storage.save_action(action)
    action = action_executor.execute(action, lead["name"])
    from proof_first.agents import post_action_verifier
    action = post_action_verifier.verify(action)

    assert action.status == ActionStatus.ROLLED_BACK
    outreach = orchestrator.finalize_outreach(dossier, action)
    assert outreach is None
