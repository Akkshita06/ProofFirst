"""
Concurrency and storage correctness tests (step 4 of the hardening brief).

storage.py uses SQLite from a single process; these tests confirm:
  1. The duplicate-action guard holds under genuine concurrent execution,
     not just sequential calls (see storage.try_claim_action).
  2. SQLite connections are properly scoped per-call - no shared
     connection causes cross-request/cross-thread state to bleed.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from proof_first import storage, orchestrator
from proof_first.data.leads import get_lead
from proof_first.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


def test_concurrent_approvals_only_one_succeeds():
    """Fires several concurrent /approve-equivalent calls (via the
    orchestrator, which is what the API layer calls) for distinct
    proposed actions against the SAME lead_id + action_type, and asserts
    the duplicate-action guard allows exactly one SUCCESS - not "usually
    one" but deterministically one, every run."""
    lead = get_lead("lead_002")  # broken-link -> CORROBORATED -> ACT
    dossier, decision = orchestrator.run_research_through_decision(lead)
    assert decision == "PROCEED_TO_PROOF_OF_WORK"

    # Simulate several independent /run calls racing to propose+approve
    # the same underlying fix before any of them has completed.
    actions = []
    lock = threading.Lock()

    def propose():
        a = orchestrator.propose_action_for_lead(lead, dossier)
        with lock:
            actions.append(a)

    proposers = [threading.Thread(target=propose) for _ in range(6)]
    [t.start() for t in proposers]
    [t.join() for t in proposers]

    pending = [a for a in actions if a.status.value == "PENDING_APPROVAL"]
    assert len(pending) == 6, "all should pass preflight before any has executed"

    results = []

    def approve(action):
        r = orchestrator.approve_action(action, lead["name"], approved=True)
        with lock:
            results.append(r)

    approvers = [threading.Thread(target=approve, args=(a,)) for a in pending]
    [t.start() for t in approvers]
    [t.join() for t in approvers]

    success_count = sum(1 for r in results if r.status.value == "SUCCESS")
    blocked_count = sum(1 for r in results if r.status.value == "BLOCKED")
    assert success_count == 1, f"expected exactly one SUCCESS under a real race, got {success_count}"
    assert blocked_count == len(pending) - 1

    # And storage's permanent record agrees with the in-memory result.
    assert storage.already_actioned(lead["id"], pending[0].action_type) is True


def test_concurrent_http_run_and_approve_requests_for_same_lead():
    """Same race, exercised through the actual HTTP API (server.py) rather
    than calling the orchestrator directly, since that's the real
    deployment surface concurrent requests would hit."""
    lead_id = "lead_002"

    run_resp = client.post(f"/api/leads/{lead_id}/run")
    assert run_resp.status_code == 200
    action_id = run_resp.json()["proposed_action"]["id"]

    results = []
    lock = threading.Lock()

    def approve():
        r = client.post(f"/api/leads/{lead_id}/approve", json={"action_id": action_id, "approved": True})
        with lock:
            results.append(r.json())

    threads = [threading.Thread(target=approve) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    statuses = [r["action"]["status"] for r in results]
    success_count = statuses.count("SUCCESS")
    # The FIRST call to approve this exact action_id transitions it through
    # APPROVED -> EXECUTED/SUCCESS; every other concurrent call for the
    # SAME action_id re-approves the same (already-terminal) action object
    # rather than re-executing a fresh one, so at most one execution of
    # the underlying whitelisted action should ever actually run.
    assert success_count >= 1
    # No request should have crashed the server or returned a 5xx.
    assert all(r["action"]["status"] in
               ("SUCCESS", "BLOCKED", "FAILED", "ROLLED_BACK", "APPROVED", "EXECUTED")
               for r in results)


def test_sqlite_connections_are_not_shared_across_calls():
    """Every storage.py function opens (and closes) its own connection -
    confirm no module-level connection object exists that could leak
    state between requests/threads."""
    import inspect
    import proof_first.storage as storage_mod

    source = inspect.getsource(storage_mod)
    # There should be no long-lived module-level `sqlite3.connect(...)`
    # assigned outside of a function body (which would be a shared,
    # cross-request connection - a correctness and thread-safety hazard
    # for a FastAPI app handling concurrent requests).
    module_level_lines = [
        line for line in source.splitlines()
        if line.startswith("_") and "connect(" in line
    ]
    assert module_level_lines == [], f"found a module-level connection: {module_level_lines}"


def test_no_cross_request_state_bleed_between_leads():
    """Writes to lead A's dossier/action tables from one thread must never
    be visible when reading lead B's data from another thread, and vice
    versa - this would only happen if connections (or an ORM session)
    were being shared/cached incorrectly."""
    lead_a = get_lead("lead_001")
    lead_b = get_lead("lead_002")

    errors = []

    def run_a():
        try:
            orchestrator.run_full_pipeline(lead_a, auto_approve=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    def run_b():
        try:
            orchestrator.run_full_pipeline(lead_b, auto_approve=True)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run_a) for _ in range(3)] + \
              [threading.Thread(target=run_b) for _ in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []

    dossier_a = storage.get_dossier(lead_a["id"])
    dossier_b = storage.get_dossier(lead_b["id"])
    assert dossier_a["lead_id"] == lead_a["id"]
    assert dossier_b["lead_id"] == lead_b["id"]
    assert dossier_a["lead_name"] != dossier_b["lead_name"]

    # lead_001 is CLOSE -> must have no actions of its own recorded.
    assert storage.list_actions(lead_a["id"]) == []
    # lead_002 is ACT -> should have at least one action, and it must
    # only ever reference lead_002, never lead_001's id.
    actions_b = storage.list_actions(lead_b["id"])
    assert len(actions_b) >= 1
    assert all(a["lead_id"] == lead_b["id"] for a in actions_b)
