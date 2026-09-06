"""
Phase 1 tests: research_agent and skeptic_agent now genuinely call
llm_client.complete() instead of doing boolean-flag-to-template /
substring-matching "reasoning". These tests mock the LLM call itself
(so they stay fast and deterministic) and assert that the *live* code
path produces the same output SHAPE - and, critically, the same
routing/verdict behavior - as MOCK_MODE, on the existing fixtures.

MOCK_MODE itself is exercised by every other test in this suite (no
GOOGLE_API_KEY is set in the test environment), so it isn't retested
here beyond one baseline sanity check.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import patch

import pytest

from proof_first import storage
from proof_first.agents import research_agent, skeptic_agent
from proof_first.data.leads import get_lead
from proof_first.models import Claim, Verdict


@pytest.fixture(autouse=True)
def fresh_db():
    storage.init_db(reset=True)
    yield


class _FakeLiveLLM:
    """Stands in for LLMClient when GOOGLE_API_KEY would be set. Returns
    deterministic-but-clearly-not-mock text, so tests can tell the real
    code path ran without hitting a network API."""

    def is_mock(self) -> bool:
        return False

    def complete(self, prompt: str, system: str = "") -> str:
        return f"[LIVE-LLM-STANDIN] rewritten claim based on: {prompt[:40]}"


def test_research_lead_calls_llm_and_keeps_claim_shape():
    """With the LLM path active, research_lead() must still return a
    list[Claim] of the same length/shape as MOCK_MODE produces for the
    same fixture."""
    lead = get_lead("lead_002")  # Acme Cafe: booking-link + review claim
    mock_claims = research_agent.research_lead(lead)

    with patch("proof_first.llm_client.is_mock", return_value=False), \
         patch("proof_first.llm_client.get_llm", return_value=_FakeLiveLLM()):
        live_claims = research_agent.research_lead(lead)

    assert len(live_claims) == len(mock_claims)
    assert all(isinstance(c, Claim) for c in live_claims)
    # Anchor phrase must survive so SkepticAgent can still route it.
    assert any("broken or unreliable" in c.text.lower() for c in live_claims)


def test_research_lead_falls_back_to_template_if_llm_drops_anchor_phrase():
    """If the model's rewrite drops the required anchor phrase, we must
    fall back to the deterministic template rather than silently breaking
    SkepticAgent's downstream routing."""
    lead = get_lead("lead_003")  # Acme Hardware: has_website is False

    class _DriftingLLM:
        def is_mock(self):
            return False

        def complete(self, prompt, system=""):
            return "This business seems fine, nothing notable here."

    with patch("proof_first.llm_client.is_mock", return_value=False), \
         patch("proof_first.llm_client.get_llm", return_value=_DriftingLLM()):
        claims = research_agent.research_lead(lead)

    assert any("has no website" in c.text.lower() for c in claims)


def test_verify_claim_live_llm_path_same_verdict_as_mock():
    """The adversarial LLM call must only add reasoning text, never change
    the deterministic verdict/confidence math."""
    lead = get_lead("lead_002")  # Acme Cafe: booking link independently 500s
    claim_mock = Claim(text="Acme Cafe's online booking link is broken or unreliable.")
    skeptic_agent.verify_claim(lead, claim_mock)

    claim_live = Claim(text="Acme Cafe's online booking link is broken or unreliable.")
    with patch("proof_first.llm_client.is_mock", return_value=False), \
         patch("proof_first.llm_client.get_llm", return_value=_FakeLiveLLM()):
        skeptic_agent.verify_claim(lead, claim_live)

    assert claim_live.verdict == claim_mock.verdict == Verdict.CORROBORATED
    assert claim_live.confidence == claim_mock.confidence
    assert "LIVE-LLM-STANDIN" not in claim_mock.reasoning
    assert "SkepticAgent LLM adversarial check" in claim_live.reasoning


def test_verify_claim_live_llm_never_overrides_verdict_enum():
    """Even though the LLM is asked to argue the claim is false, the
    verdict/confidence assignment must remain the fixed constant from the
    deterministic branch - the model's adversarial argument is reasoning
    text only, never a verdict."""
    lead = get_lead("lead_004")  # Sunrise Yoga: CONTRADICTED by design
    claim = Claim(text="Sunrise Yoga Studio has a website but no online booking mechanism.")

    with patch("proof_first.llm_client.is_mock", return_value=False), \
         patch("proof_first.llm_client.get_llm", return_value=_FakeLiveLLM()):
        skeptic_agent.verify_claim(lead, claim)

    assert claim.verdict == Verdict.CONTRADICTED
    assert claim.confidence == 0.15


def test_verify_all_live_llm_path_end_to_end_shape():
    """research_lead() -> verify_all() end-to-end with the live-LLM stand-in
    still returns claims of the expected shape/verdict enum values."""
    lead = get_lead("lead_008")  # Golden Wok Takeout: no website -> ACT
    with patch("proof_first.llm_client.is_mock", return_value=False), \
         patch("proof_first.llm_client.get_llm", return_value=_FakeLiveLLM()):
        claims = research_agent.research_lead(lead)
        claims = skeptic_agent.verify_all(lead, claims)

    assert len(claims) >= 1
    assert all(isinstance(c, Claim) for c in claims)
    assert all(c.verdict in (Verdict.CORROBORATED, Verdict.CONTRADICTED, Verdict.UNVERIFIABLE) for c in claims)
    assert any(c.verdict == Verdict.CORROBORATED for c in claims)
