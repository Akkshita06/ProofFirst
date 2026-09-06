"""
Baseline: exactly what the design doc's Evaluation Plan specifies -
a single pass that writes a pitch and lists the "facts" it used, with
NO independent verification step and no real-world action.

This intentionally reuses research_agent.research_lead() (the same single
research pass ProofFirst also starts from) so the comparison is fair: both
systems see identical primary_signals. The only difference under test is
whether a Skeptic/verification step exists afterward.
"""
from __future__ import annotations

from dataclasses import dataclass

from proof_first.agents import research_agent


@dataclass
class BaselineResult:
    lead_id: str
    lead_name: str
    claims_used: list[str]
    pitch: str


def run_baseline(lead: dict) -> BaselineResult:
    claims = research_agent.research_lead(lead)
    claim_texts = [c.text for c in claims]
    pitch = (
        f"Hi {lead['name']},\n\n"
        + "\n".join(f"- {t}" for t in claim_texts)
        + "\n\nWe'd love to help fix this - let's talk.\n"
    )
    return BaselineResult(lead_id=lead["id"], lead_name=lead["name"], claims_used=claim_texts, pitch=pitch)
