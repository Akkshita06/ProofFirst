"""
Proves the README's claim that "swap in a real API call, agent logic
doesn't change" for real, rather than just asserting it in prose: spins
up a real local HTTP server standing in for a business's booking page,
points SkepticAgent at it with PROOFFIRST_REAL_SITE_SCAN=1, and confirms
the exact same CORROBORATED/CONTRADICTED verdict logic in skeptic_agent.py
fires off a genuine `requests.get` response - no fixture involved.
"""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from proof_first import storage
from proof_first.models import Claim, Verdict
from proof_first.agents import skeptic_agent
from proof_first.integrations import site_scan


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


@pytest.fixture()
def real_site_scan_enabled(monkeypatch):
    monkeypatch.setattr(site_scan, "real_site_scan_enabled", lambda: True)
    yield


def _run_local_server(status_code: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status_code)
            self.end_headers()

        def log_message(self, *args):
            pass  # keep test output quiet

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    return server, port


def test_real_http_500_corroborates_broken_link_claim(real_site_scan_enabled):
    server, port = _run_local_server(500)
    try:
        lead = {
            "id": "lead_real_scan_broken",
            "name": "Real Scan Test Cafe",
            "primary_signals": {
                "has_website": True, "site_has_booking_link": True,
                "site_booking_link_url": f"http://127.0.0.1:{port}/book",
                "recent_review_snippets": ["The booking page never works."],
            },
            "secondary_signals": {},  # deliberately empty - the real call is the only source
        }
        claim = Claim(text="Real Scan Test Cafe's online booking link is broken or unreliable.")
        skeptic_agent.verify_claim(lead, claim)

        assert claim.verdict == Verdict.CORROBORATED
        assert claim.confidence == 0.94
        assert any(e.source == "site_scan" for e in claim.supporting_evidence)
    finally:
        server.shutdown()


def test_real_http_200_contradicts_broken_link_claim(real_site_scan_enabled):
    server, port = _run_local_server(200)
    try:
        lead = {
            "id": "lead_real_scan_working",
            "name": "Real Scan Test Diner",
            "primary_signals": {
                "has_website": True, "site_has_booking_link": True,
                "site_booking_link_url": f"http://127.0.0.1:{port}/book",
                "recent_review_snippets": ["Heard booking used to be flaky."],
            },
            "secondary_signals": {},
        }
        claim = Claim(text="Real Scan Test Diner's online booking link is broken or unreliable.")
        skeptic_agent.verify_claim(lead, claim)

        assert claim.verdict == Verdict.CONTRADICTED
        assert claim.confidence == 0.2
    finally:
        server.shutdown()


def test_real_http_429_from_live_server_is_unverifiable(real_site_scan_enabled):
    server, port = _run_local_server(429)
    try:
        lead = {
            "id": "lead_real_scan_ratelimited",
            "name": "Real Scan Rate Limited Bistro",
            "primary_signals": {
                "has_website": True, "site_has_booking_link": True,
                "site_booking_link_url": f"http://127.0.0.1:{port}/book",
                "recent_review_snippets": ["Booking form seems broken."],
            },
            "secondary_signals": {},
        }
        claim = Claim(text="Real Scan Rate Limited Bistro's online booking link is broken or unreliable.")
        skeptic_agent.verify_claim(lead, claim)

        assert claim.verdict == Verdict.UNVERIFIABLE
    finally:
        server.shutdown()


def test_unreachable_real_url_is_unverifiable_not_a_crash(real_site_scan_enabled):
    lead = {
        "id": "lead_real_scan_unreachable",
        "name": "Unreachable Test Shop",
        "primary_signals": {
            "has_website": True, "site_has_booking_link": True,
            "site_booking_link_url": "http://127.0.0.1:1/book",  # nothing listens here
            "recent_review_snippets": ["Booking form seems broken."],
        },
        "secondary_signals": {},
    }
    claim = Claim(text="Unreachable Test Shop's online booking link is broken or unreliable.")
    skeptic_agent.verify_claim(lead, claim)
    assert claim.verdict == Verdict.UNVERIFIABLE


def test_real_site_scan_disabled_by_default():
    """Without PROOFFIRST_REAL_SITE_SCAN set, MOCK_MODE fixtures are used -
    the default the whole rest of the test suite and the grading demo
    depend on."""
    assert site_scan.real_site_scan_enabled() is False
