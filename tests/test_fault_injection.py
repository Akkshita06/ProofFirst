"""
Fault-injection tests for realistic failure modes that will appear once
Maps / business-profile / site-scan calls are real HTTP calls instead of
fixtures (see proof_first/integrations/site_scan.py and README §6).

These tests simulate, at the site_scan integration boundary and at the
SkepticAgent level:
  1. Malformed/partial API responses (missing fields, unexpected types)
  2. Timeouts / connection errors during independent evidence checks
  3. Rate-limit responses (429) - must degrade gracefully, never be
     silently read as "no contradicting evidence found"
  4. Two independent evidence sources that disagree with each other
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import types
from unittest.mock import patch, MagicMock

import pytest

from proof_first import storage
from proof_first.models import Claim, Verdict
from proof_first.agents import skeptic_agent
from proof_first.integrations import site_scan


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


LEAD_WITH_REAL_URL = {
    "id": "lead_fault_test",
    "name": "Fault Test Cafe",
    "vertical": "cafe",
    "primary_signals": {
        "has_website": True,
        "site_has_booking_link": True,
        "site_booking_link_url": "https://faulttestcafe.example/book",
        "recent_review_snippets": ["The booking page never works."],
    },
    "secondary_signals": {},
}


def _broken_link_claim() -> Claim:
    return Claim(text="Fault Test Cafe's online booking link is broken or unreliable.")


# --- 1. Malformed / partial responses ------------------------------------

def test_malformed_response_missing_status_code_is_unverifiable():
    """A response object with no usable status_code must not crash and
    must not be silently treated as evidence either way."""
    fake_response = MagicMock()
    del fake_response.status_code  # simulate a field that's just missing
    fake_response.status_code = "not-an-int"  # unexpected type instead

    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", return_value=fake_response):
        result = site_scan.check_booking_link(LEAD_WITH_REAL_URL["primary_signals"]["site_booking_link_url"], None)

    assert result.outcome == site_scan.SiteScanOutcome.MALFORMED_RESPONSE
    assert not result.is_definitive


def test_skeptic_agent_handles_malformed_site_scan_response_gracefully():
    claim = _broken_link_claim()
    fake_response = MagicMock()
    fake_response.status_code = None

    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", return_value=fake_response):
        skeptic_agent.verify_claim(LEAD_WITH_REAL_URL, claim)

    assert claim.verdict == Verdict.UNVERIFIABLE
    assert "MALFORMED_RESPONSE" in claim.reasoning


# --- 2. Timeouts / connection errors --------------------------------------

def test_timeout_during_site_scan_is_unverifiable_not_a_crash():
    import requests as requests_lib

    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", side_effect=requests_lib.exceptions.Timeout("simulated timeout")):
        result = site_scan.check_booking_link(LEAD_WITH_REAL_URL["primary_signals"]["site_booking_link_url"], None)

    assert result.outcome == site_scan.SiteScanOutcome.TIMEOUT
    assert not result.is_definitive


def test_connection_error_during_site_scan_is_unverifiable_not_a_crash():
    import requests as requests_lib

    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", side_effect=requests_lib.exceptions.ConnectionError("simulated DNS failure")):
        result = site_scan.check_booking_link(LEAD_WITH_REAL_URL["primary_signals"]["site_booking_link_url"], None)

    assert result.outcome == site_scan.SiteScanOutcome.CONNECTION_ERROR
    assert not result.is_definitive


def test_skeptic_agent_survives_connection_error_during_research_or_skeptic_check():
    """Simulates a ResearchAgent/SkepticAgent-stage network failure:
    verify_claim must not raise, and must degrade to UNVERIFIABLE."""
    import requests as requests_lib

    claim = _broken_link_claim()
    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", side_effect=requests_lib.exceptions.ConnectionError("simulated")):
        skeptic_agent.verify_claim(LEAD_WITH_REAL_URL, claim)

    assert claim.verdict == Verdict.UNVERIFIABLE
    assert claim.confidence < 0.5  # low confidence, not confidently corroborated


# --- 3. Rate limiting (429) ------------------------------------------------

def test_rate_limit_response_is_not_treated_as_no_contradicting_evidence():
    """A 429 must never be silently read as 'independent check found
    nothing wrong' (which would wrongly corroborate the broken-link claim
    and let a contradicted-in-reality lead sail through to outreach)."""
    fake_response = MagicMock()
    fake_response.status_code = 429

    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", return_value=fake_response):
        result = site_scan.check_booking_link(LEAD_WITH_REAL_URL["primary_signals"]["site_booking_link_url"], None)

    assert result.outcome == site_scan.SiteScanOutcome.RATE_LIMITED
    assert not result.is_definitive, "a 429 must never be treated as a definitive, trustworthy check"


def test_skeptic_agent_marks_rate_limited_claim_unverifiable_not_corroborated():
    claim = _broken_link_claim()
    fake_response = MagicMock()
    fake_response.status_code = 429

    with patch.object(site_scan, "real_site_scan_enabled", return_value=True), \
         patch("requests.get", return_value=fake_response):
        skeptic_agent.verify_claim(LEAD_WITH_REAL_URL, claim)

    assert claim.verdict == Verdict.UNVERIFIABLE, (
        "a rate-limited independent check must never resolve to CORROBORATED - "
        "that would incorrectly clear a defect that was never actually re-checked"
    )


# --- 4. Conflicting independent evidence -----------------------------------

def test_two_disagreeing_independent_sources_yield_unverifiable_not_a_pick():
    lead = {
        "id": "lead_conflict_test",
        "name": "Conflict Test Diner",
        "primary_signals": {"has_website": True, "site_has_booking_link": True,
                             "recent_review_snippets": ["Booking is broken for me."]},
        "secondary_signals": {
            "booking_link_http_status": 500,                     # first monitor: broken
            "booking_link_http_status_secondary_check": 200,      # second monitor: working
        },
    }
    claim = Claim(text="Conflict Test Diner's online booking link is broken or unreliable.")
    skeptic_agent.verify_claim(lead, claim)

    assert claim.verdict == Verdict.UNVERIFIABLE
    assert claim.confidence < 0.5
    assert len(claim.supporting_evidence) >= 1
    assert len(claim.contradicting_evidence) >= 1
    assert "disagree" in claim.reasoning.lower()


def test_agreeing_independent_sources_still_resolve_normally():
    """Sanity check: when a secondary confirmation source is present but
    AGREES with the first, existing CORROBORATED behavior is unaffected."""
    lead = {
        "id": "lead_agree_test",
        "name": "Agree Test Diner",
        "primary_signals": {"has_website": True, "site_has_booking_link": True,
                             "recent_review_snippets": ["Booking is broken for me."]},
        "secondary_signals": {
            "booking_link_http_status": 500,
            "booking_link_http_status_secondary_check": 503,
        },
    }
    claim = Claim(text="Agree Test Diner's online booking link is broken or unreliable.")
    skeptic_agent.verify_claim(lead, claim)

    assert claim.verdict == Verdict.CORROBORATED
    assert claim.confidence == 0.94


def test_mock_mode_unaffected_by_real_site_scan_changes():
    """Existing fixture-driven leads (real_site_scan disabled, the
    default) must behave exactly as before these changes."""
    from proof_first.data.leads import get_lead
    from proof_first.agents import research_agent

    lead = get_lead("lead_002")  # Acme Cafe - broken link CORROBORATED
    claims = research_agent.research_lead(lead)
    claims = skeptic_agent.verify_all(lead, claims)
    broken_link_claim = next(c for c in claims if "broken or unreliable" in c.text)
    assert broken_link_claim.verdict == Verdict.CORROBORATED
    assert broken_link_claim.confidence == 0.94
