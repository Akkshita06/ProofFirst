"""
site_scan integration - the ONE fixture data source this codebase swaps
for a real HTTP call (per README §6 limitation #1), gated behind the
PROOFFIRST_REAL_SITE_SCAN environment variable.

This is deliberately the simplest of the three fixture sources
(Maps / business-profile / site-scan) to swap first: it's a single
outbound GET request with a status code, no auth, no pagination.

Contract with callers (skeptic_agent.py):
    result = check_booking_link(url_or_none, fixture_status)
    result.status        -> int | None  (HTTP status code, or None if unknown/unreachable)
    result.outcome       -> SiteScanOutcome enum describing WHY status may be None
    result.detail        -> human-readable string for activity logs / reasoning text

Failure modes are surfaced as distinct SiteScanOutcome values rather than
silently coerced into "site is fine" or "site is broken" - this matters
because SkepticAgent's whole job is to be an honest independent check, and
a 429 or a timeout is not evidence of anything about the booking link
itself. Callers (skeptic_agent.py) are responsible for mapping
non-DEFINITIVE outcomes to Verdict.UNVERIFIABLE rather than guessing.

MOCK_MODE (PROOFFIRST_REAL_SITE_SCAN unset/false, the default and what
grading/demo/tests run under): returns the fixture's
`booking_link_http_status` value verbatim, wrapped as a DEFINITIVE
outcome, exactly matching the pre-existing behavior in skeptic_agent.py.

REAL mode (PROOFFIRST_REAL_SITE_SCAN=1 and a real URL is present): makes
an actual `requests.get(url, timeout=...)` call and classifies the
result. This proves the README's claim that "swap in a real API call,
agent logic doesn't change" rather than just asserting it - the calling
code in skeptic_agent.py is unchanged either way; only this module's
internals differ.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

REAL_SITE_SCAN_ENV_VAR = "PROOFFIRST_REAL_SITE_SCAN"
DEFAULT_TIMEOUT_SECONDS = 5


def real_site_scan_enabled() -> bool:
    return os.environ.get(REAL_SITE_SCAN_ENV_VAR, "").lower() in ("1", "true", "yes")


class SiteScanOutcome(str, Enum):
    DEFINITIVE = "DEFINITIVE"              # we got a real, trustworthy status code
    TIMEOUT = "TIMEOUT"                    # the request timed out
    CONNECTION_ERROR = "CONNECTION_ERROR"  # DNS failure, refused connection, etc.
    RATE_LIMITED = "RATE_LIMITED"          # got a 429 - NOT evidence the link works
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"  # response didn't look like a normal HTTP response
    NO_URL = "NO_URL"                      # nothing to check
    MOCK = "MOCK"                          # fixture-backed result, not a live call


@dataclass
class SiteScanResult:
    status: Optional[int]
    outcome: SiteScanOutcome
    detail: str

    @property
    def is_definitive(self) -> bool:
        """True only when `status` reflects a real, trustworthy check -
        i.e. safe for skeptic_agent to reason about as CORROBORATING or
        CONTRADICTING evidence. Every other outcome must be treated as
        inconclusive (UNVERIFIABLE), never as silent evidence either way."""
        return self.outcome in (SiteScanOutcome.DEFINITIVE, SiteScanOutcome.MOCK) and self.status is not None


def check_booking_link(url: Optional[str], fixture_status: Optional[int]) -> SiteScanResult:
    """Returns the booking-link HTTP status via a real HTTP call when
    PROOFFIRST_REAL_SITE_SCAN is enabled and a URL is available, otherwise
    falls back to the fixture value (`booking_link_http_status` in
    data/leads.py) exactly as before.
    """
    if not real_site_scan_enabled() or not url:
        if fixture_status is None:
            return SiteScanResult(status=None, outcome=SiteScanOutcome.NO_URL,
                                   detail="No fixture status and real site-scan is disabled/no URL - nothing to check.")
        return SiteScanResult(status=fixture_status, outcome=SiteScanOutcome.MOCK,
                               detail=f"MOCK_MODE fixture status {fixture_status} (real site scan disabled).")

    return _real_check_booking_link(url)


def _real_check_booking_link(url: str) -> SiteScanResult:
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is in requirements.txt
        return SiteScanResult(status=None, outcome=SiteScanOutcome.CONNECTION_ERROR,
                               detail="requests library not available - cannot perform a real site scan.")

    try:
        resp = requests.get(url, timeout=DEFAULT_TIMEOUT_SECONDS)
    except requests.exceptions.Timeout:
        return SiteScanResult(status=None, outcome=SiteScanOutcome.TIMEOUT,
                               detail=f"Timed out after {DEFAULT_TIMEOUT_SECONDS}s independently re-checking {url}.")
    except requests.exceptions.ConnectionError as e:
        return SiteScanResult(status=None, outcome=SiteScanOutcome.CONNECTION_ERROR,
                               detail=f"Connection error independently re-checking {url}: {e}")
    except requests.exceptions.RequestException as e:
        return SiteScanResult(status=None, outcome=SiteScanOutcome.CONNECTION_ERROR,
                               detail=f"Request error independently re-checking {url}: {e}")

    status = getattr(resp, "status_code", None)
    if not isinstance(status, int):
        return SiteScanResult(status=None, outcome=SiteScanOutcome.MALFORMED_RESPONSE,
                               detail=f"Response from {url} had no usable status code.")

    if status == 429:
        # Explicitly NOT treated as "no contradicting evidence found" -
        # a rate limit tells us nothing about whether the booking link
        # itself works.
        return SiteScanResult(status=status, outcome=SiteScanOutcome.RATE_LIMITED,
                               detail=f"Independent re-check of {url} was rate-limited (HTTP 429) - inconclusive, not evidence of a working link.")

    return SiteScanResult(status=status, outcome=SiteScanOutcome.DEFINITIVE,
                           detail=f"Independent re-check of {url} returned HTTP {status}.")
