"""
Tests for per-lead outreach rate limiting (Track 1 hardening).

Covers:
  1. OutreachAgent allows up to the configured limit of sends per lead
     per rolling hour, then blocks (raises, does not silently drop) any
     send beyond that.
  2. The counter is per-lead, not global.
  3. storage.try_claim_outreach_send is safe under real concurrent
     execution for the same lead (mirrors test_concurrency.py's style
     for the duplicate-action guard).
  4. The block is surfaced to callers (orchestrator.run_full_pipeline and
     the HTTP API) rather than swallowed into a generic success.
  5. The env-var override (OUTREACH_RATE_LIMIT_PER_HOUR) is respected.
  6. The rate limit runs strictly after approval/preflight, never before -
     it never touches actions that haven't already reached SUCCESS.
"""
import importlib
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from proof_first import storage, orchestrator
from proof_first.agents import outreach_agent
from proof_first.data.leads import get_lead
from proof_first.models import ActionStatus
from proof_first.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


def _get_success_dossier_and_action(lead_id: str = "lead_002"):
    """lead_002 (Acme Cafe) is a fixture whose broken-link claim is
    CORROBORATED -> PROCEED_TO_PROOF_OF_WORK -> auto-approve succeeds,
    giving us a real SUCCESS action/dossier pair without hand-building
    dataclasses that could drift from the real shapes."""
    lead = get_lead(lead_id)
    dossier, decision = orchestrator.run_research_through_decision(lead)
    assert decision == "PROCEED_TO_PROOF_OF_WORK"
    action = orchestrator.propose_action_for_lead(lead, dossier)
    action = orchestrator.approve_action(action, lead["name"], approved=True)
    assert action.status == ActionStatus.SUCCESS
    return lead, dossier, action


def test_allows_up_to_limit_then_blocks_not_drops():
    """Directly exercises OutreachAgent's gate: the Nth+1 send for the same
    lead must raise, not return None or a truncated/blank message."""
    lead, dossier, action = _get_success_dossier_and_action()
    limit = outreach_agent.OUTREACH_RATE_LIMIT_PER_HOUR

    for i in range(limit):
        msg = outreach_agent.generate_outreach(dossier, action)
        assert msg is not None and len(msg) > 0

    with pytest.raises(outreach_agent.OutreachRateLimitExceeded) as exc_info:
        outreach_agent.generate_outreach(dossier, action)

    assert exc_info.value.lead_id == lead["id"]
    assert exc_info.value.limit == limit
    assert exc_info.value.current_count == limit

    # The block itself must be visible/debuggable in the activity log,
    # with lead id, count, and limit - not just a silent no-op.
    activity = storage.list_activity(lead["id"])
    blocked_events = [a for a in activity if "BLOCKED" in a["message"] and "rate limit" in a["message"]]
    assert len(blocked_events) == 1
    assert lead["id"] in blocked_events[0]["message"]
    assert str(limit) in blocked_events[0]["message"]


def test_rate_limit_is_per_lead_not_global():
    """Exhausting lead_002's quota must not affect lead_009's quota."""
    lead_a, dossier_a, action_a = _get_success_dossier_and_action("lead_002")
    lead_b, dossier_b, action_b = _get_success_dossier_and_action("lead_009")

    limit = outreach_agent.OUTREACH_RATE_LIMIT_PER_HOUR
    for _ in range(limit):
        outreach_agent.generate_outreach(dossier_a, action_a)

    with pytest.raises(outreach_agent.OutreachRateLimitExceeded):
        outreach_agent.generate_outreach(dossier_a, action_a)

    # lead_b is untouched - it should still get its full quota.
    for _ in range(limit):
        msg = outreach_agent.generate_outreach(dossier_b, action_b)
        assert msg is not None


def test_run_full_pipeline_surfaces_blocked_reason_when_quota_exhausted():
    """The end-to-end orchestrator entrypoint must not swallow a rate-limit
    block into a generic success - outreach_message stays None and a
    dedicated field explains why."""
    lead = get_lead("lead_002")
    limit = outreach_agent.OUTREACH_RATE_LIMIT_PER_HOUR

    # Pre-exhaust the quota for this lead via the storage layer directly,
    # simulating that OutreachAgent already sent `limit` messages earlier
    # in the hour (e.g. from prior pipeline runs).
    for _ in range(limit):
        allowed, _ = storage.try_claim_outreach_send(lead["id"], limit)
        assert allowed

    result = orchestrator.run_full_pipeline(lead, auto_approve=True)

    assert result.action.status == ActionStatus.SUCCESS  # action itself still succeeded
    assert result.outreach_message is None  # but outreach was withheld
    assert result.outreach_blocked_reason is not None
    assert lead["id"] in result.outreach_blocked_reason
    assert result.stopped_reason is not None
    assert "blocked" in result.stopped_reason.lower()

    # And the underlying action guardrails were unaffected: the action is
    # still recorded as SUCCESS, this is purely an outreach-layer block.
    assert storage.already_actioned(lead["id"], result.action.action_type) is True


def test_api_approve_endpoint_surfaces_outreach_blocked_reason():
    """Same guarantee, exercised through the real HTTP surface."""
    lead_id = "lead_002"
    limit = outreach_agent.OUTREACH_RATE_LIMIT_PER_HOUR

    run_resp = client.post(f"/api/leads/{lead_id}/run")
    assert run_resp.status_code == 200
    action_id = run_resp.json()["proposed_action"]["id"]

    # Pre-exhaust the quota before the human clicks approve.
    for _ in range(limit):
        allowed, _ = storage.try_claim_outreach_send(lead_id, limit)
        assert allowed

    approve_resp = client.post(
        f"/api/leads/{lead_id}/approve", json={"action_id": action_id, "approved": True}
    )
    assert approve_resp.status_code == 200
    body = approve_resp.json()
    assert body["action"]["status"] == "SUCCESS"
    assert body["outreach_message"] is None
    assert body["outreach_blocked_reason"] is not None
    assert lead_id in body["outreach_blocked_reason"]


def test_rejected_or_non_success_action_never_hits_the_rate_limiter():
    """Rate limiting is the LAST gate before a send - it must never be
    consulted at all for an action that never reached SUCCESS (i.e. it
    does not replace or precede the approval/preflight guardrails)."""
    lead = get_lead("lead_003")
    dossier, decision = orchestrator.run_research_through_decision(lead)
    action = orchestrator.propose_action_for_lead(lead, dossier)
    action = orchestrator.approve_action(action, lead["name"], approved=False)
    assert action.status == ActionStatus.REJECTED

    outreach = orchestrator.finalize_outreach(dossier, action)
    assert outreach is None
    # No send was ever attempted, so nothing should be recorded against
    # the rate-limit counter for this lead.
    assert storage.count_recent_outreach_sends(lead["id"], 3600.0) == 0


def test_window_expiry_allows_sends_again():
    """A send recorded outside the rolling window must not count against
    the current quota - simulate this by inserting a claim timestamped
    over an hour ago directly, bypassing the real clock."""
    lead, dossier, action = _get_success_dossier_and_action()
    limit = outreach_agent.OUTREACH_RATE_LIMIT_PER_HOUR

    # Manually seed `limit` sends, but timestamped 2 hours in the past.
    conn = storage._conn()
    old_ts = time.time() - 7200
    for _ in range(limit):
        conn.execute("INSERT INTO outreach_sends (lead_id, ts) VALUES (?, ?)", (lead["id"], old_ts))
    conn.commit()
    conn.close()

    # Those old sends are outside the 1-hour window, so a fresh send now
    # should still be allowed.
    msg = outreach_agent.generate_outreach(dossier, action)
    assert msg is not None


def test_env_var_override_changes_the_limit():
    """OUTREACH_RATE_LIMIT_PER_HOUR must be respected via the same ad hoc
    os.environ.get(...) config pattern used elsewhere in this codebase."""
    old_value = os.environ.get("OUTREACH_RATE_LIMIT_PER_HOUR")
    try:
        os.environ["OUTREACH_RATE_LIMIT_PER_HOUR"] = "1"
        reloaded = importlib.reload(outreach_agent)
        assert reloaded.OUTREACH_RATE_LIMIT_PER_HOUR == 1

        lead, dossier, action = _get_success_dossier_and_action()
        msg = reloaded.generate_outreach(dossier, action)
        assert msg is not None
        with pytest.raises(reloaded.OutreachRateLimitExceeded):
            reloaded.generate_outreach(dossier, action)
    finally:
        if old_value is None:
            os.environ.pop("OUTREACH_RATE_LIMIT_PER_HOUR", None)
        else:
            os.environ["OUTREACH_RATE_LIMIT_PER_HOUR"] = old_value
        importlib.reload(outreach_agent)  # restore the default for later tests


def test_concurrent_sends_for_same_lead_never_exceed_the_limit():
    """The atomic BEGIN IMMEDIATE check-and-record in
    try_claim_outreach_send must hold under a genuine race, exactly like
    try_claim_action does for the duplicate-action guard - not "usually
    correct", but exactly `limit` allowed, deterministically, every run."""
    lead_id = "lead_002"
    limit = 3
    window_seconds = 3600.0

    results = []
    lock = threading.Lock()

    def attempt():
        allowed, count = storage.try_claim_outreach_send(lead_id, limit, window_seconds)
        with lock:
            results.append(allowed)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    allowed_count = sum(1 for r in results if r)
    blocked_count = sum(1 for r in results if not r)
    assert allowed_count == limit, f"expected exactly {limit} allowed under a real race, got {allowed_count}"
    assert blocked_count == 12 - limit
    assert storage.count_recent_outreach_sends(lead_id, window_seconds) == limit
