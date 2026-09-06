"""
Regression tests for the Track 1 learning loop added in this pass:
storage.learned_patterns/reflections, agents/reflection_agent.py, and
skeptic_agent.apply_learned_adjustments.

These assert the loop's actual guarantees, not just that it runs:
  - a single correction never activates a pattern (corroboration guard)
  - a second, independent correction on a DIFFERENT lead does activate it
  - once active, the pattern measurably changes a THIRD, never-corrected
    lead's confidence/routing - this is what "learns and applies later" means
  - the deterministic verdict itself is never flipped by a pattern, only confidence
"""
from __future__ import annotations

from proof_first import storage
from proof_first.data.leads import get_lead
from proof_first.agents import research_agent, skeptic_agent, reflection_agent
from proof_first.models import EvidenceDossier, Verdict


def _dossier_for(lead_id: str) -> EvidenceDossier:
    lead = get_lead(lead_id)
    claims = research_agent.research_lead(lead)
    claims = skeptic_agent.verify_all(lead, claims)
    return EvidenceDossier(lead_id=lead["id"], lead_name=lead["name"], claims=claims)


def _booking_claim(dossier: EvidenceDossier):
    return next(c for c in dossier.claims if "no online booking" in c.text.lower())


def setup_function(_):
    storage.init_db(reset=True)


def test_single_correction_does_not_activate_pattern():
    dossier = _dossier_for("lead_012")
    claim = _booking_claim(dossier)
    pattern_id = reflection_agent.reflect_on_human_feedback(get_lead("lead_012"), dossier, claim.id, "CONTRADICTED")
    assert pattern_id is not None
    assert storage.get_active_patterns() == []  # candidate only, not yet corroborated


def test_second_independent_correction_activates_pattern():
    d1 = _dossier_for("lead_012")
    c1 = _booking_claim(d1)
    reflection_agent.reflect_on_human_feedback(get_lead("lead_012"), d1, c1.id, "CONTRADICTED")

    d2 = _dossier_for("lead_019")
    c2 = _booking_claim(d2)
    reflection_agent.reflect_on_human_feedback(get_lead("lead_019"), d2, c2.id, "CONTRADICTED")

    active = storage.get_active_patterns(claim_type="booking_claim")
    assert len(active) == 1
    assert set(active[0]["source_lead_ids"]) == {"lead_012", "lead_019"}


def test_activated_pattern_changes_a_third_never_corrected_lead():
    # Baseline: lead_009 shares the same decisive signal value as lead_012/019
    # and is CORROBORATED at high confidence before any learning happens.
    before = _booking_claim(_dossier_for("lead_009"))
    assert before.verdict == Verdict.CORROBORATED
    assert before.confidence >= 0.75

    d1 = _dossier_for("lead_012")
    reflection_agent.reflect_on_human_feedback(get_lead("lead_012"), d1, _booking_claim(d1).id, "CONTRADICTED")
    d2 = _dossier_for("lead_019")
    reflection_agent.reflect_on_human_feedback(get_lead("lead_019"), d2, _booking_claim(d2).id, "CONTRADICTED")

    after = _booking_claim(_dossier_for("lead_009"))
    # Verdict itself is never flipped by a learned pattern - only confidence.
    assert after.verdict == Verdict.CORROBORATED
    assert after.confidence < before.confidence
    assert "learned adjustment" in after.reasoning


def test_reflections_are_logged_even_when_no_pattern_is_promoted():
    dossier = _dossier_for("lead_012")
    claim = _booking_claim(dossier)
    reflection_agent.reflect_on_human_feedback(get_lead("lead_012"), dossier, claim.id, "CONTRADICTED")
    rows = storage.list_reflections("lead_012")
    assert len(rows) == 1
    assert rows[0]["was_miss"] == 1
