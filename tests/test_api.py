"""
HTTP-level regression tests against the live FastAPI app (server.py).

These exist to catch drift between what README.md documents as the API
contract and what the server actually enforces - see
test_approve_endpoint_matches_readme_schema below, which is a regression
test for the exact bug reported in the brief: README §5 showed
`POST /api/leads/{id}/approve` with body `{"approved": true}`, but
`ApprovalRequest` requires `action_id` too.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi.testclient import TestClient

from proof_first import storage
from proof_first.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


def test_approve_endpoint_matches_readme_schema():
    """Reproduces README.md §5's documented curl flow exactly:
      1. POST /api/leads/lead_002/run
      2. POST /api/leads/lead_002/approve with the returned action_id and
         "approved": true

    A body of bare `{"approved": true}` (the old, now-fixed README example)
    must fail with a 422 so this regression cannot silently reappear -
    the real fix is documenting the true required schema, not loosening
    the endpoint.
    """
    run_resp = client.post("/api/leads/lead_002/run")
    assert run_resp.status_code == 200
    proposed = run_resp.json()["proposed_action"]
    assert proposed is not None, "lead_002 is expected to reach PROCEED_TO_PROOF_OF_WORK"
    action_id = proposed["id"]

    # The old README example - bare {"approved": true} - must 422, proving
    # the documented bug is real and would be caught if it recurred.
    bad_resp = client.post(f"/api/leads/lead_002/approve", json={"approved": True})
    assert bad_resp.status_code == 422

    # The corrected, now-documented schema must succeed end-to-end.
    good_resp = client.post(
        "/api/leads/lead_002/approve",
        json={"action_id": action_id, "approved": True},
    )
    assert good_resp.status_code == 200
    body = good_resp.json()
    assert body["action"]["status"] == "SUCCESS"
    assert body["outreach_message"] is not None


def test_run_endpoint_unknown_lead_returns_404():
    resp = client.post("/api/leads/does_not_exist/run")
    assert resp.status_code == 404


def test_leads_endpoint_reports_mock_mode_and_all_leads():
    from proof_first.data.leads import LEADS
    resp = client.get("/api/leads")
    assert resp.status_code == 200
    body = resp.json()
    assert "mock_mode" in body
    assert len(body["leads"]) == len(LEADS)


def test_get_action_endpoint_is_pure_read_and_does_not_duplicate_rows():
    """Regression test for Bug 2 (frontend UI pass): the approval panel used
    to call POST /run just to view an existing action, which silently
    re-ran the whole pipeline and inserted a new PENDING_APPROVAL row on
    every click. GET /api/leads/{id}/action must return the same action_id
    every time and must never grow the activity log."""
    run_resp = client.post("/api/leads/lead_002/run")
    assert run_resp.status_code == 200
    proposed = run_resp.json()["proposed_action"]
    assert proposed is not None

    activity_before = client.get("/api/leads/lead_002/activity").json()["activity"]

    seen_ids = set()
    for _ in range(5):
        resp = client.get("/api/leads/lead_002/action")
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] is not None
        assert body["dossier"] is not None
        seen_ids.add(body["action"]["id"])

    assert seen_ids == {proposed["id"]}, "action_id must not change across repeated reads"

    activity_after = client.get("/api/leads/lead_002/activity").json()["activity"]
    assert len(activity_after) == len(activity_before), (
        "GET /action must be read-only - the activity log grew from "
        f"{len(activity_before)} to {len(activity_after)} rows"
    )


def test_get_action_endpoint_unknown_lead_returns_empty_not_error():
    """A lead with no run yet should report action=None, not 404/500 -
    the leads table always exists (fixtures), only the dossier/action
    state is optional."""
    resp = client.get("/api/leads/lead_020/action")
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] is None
