"""
ResearchAgent - discovers candidate claims/opportunities about a lead from
ONE source pass (primary_signals). This intentionally mirrors what
SalesShortcut's research_lead_agent.py does today: a single research pass
that is trusted at face value and handed downstream. Nothing here
cross-checks itself - that is deliberately SkepticAgent's job, not this
agent's, so the pipeline has one clear place where "trust" is granted and
one clear place where it is challenged.

Phase 1 change: this now actually calls the LLM (llm_client.complete) to
generate the claim text, instead of a boolean-flag-to-string-template
map. The *detection* of which facts are notable enough to be worth a
claim is still a deterministic read of primary_signals - that isn't
"reasoning", it's just noticing what data exists - but the *wording* of
each claim is genuinely produced by the model now.

Why keep a template fallback at all: SkepticAgent (and several tests,
e.g. tests/test_real_site_scan_integration.py) key off specific anchor
phrases in claim.text ("no online booking mechanism", "broken or
unreliable", "listing hours", "has no website") to decide which
independent check applies. A live LLM's wording is not guaranteed to
contain those phrases verbatim, so each claim keeps a deterministic
template as a safety net: if the model's text doesn't include the
required anchor phrase, we fall back to the template rather than silently
breaking downstream routing. In MOCK_MODE we skip the model call's text
entirely and use the template directly, so the deterministic test suite
sees byte-identical output to the pre-Phase-1 behavior.
"""
from __future__ import annotations

from proof_first.models import Claim, ActivityEvent
from proof_first import storage
from proof_first import llm_client

_SYSTEM_PROMPT = (
    "You are ResearchAgent, part of a lead-qualification pipeline. You are "
    "given ONE source pass of primary signals about a local business. Write "
    "a single, factual, one-sentence claim describing the specific issue "
    "below. Do not speculate beyond what the signals say. Do not hedge - "
    "a second agent (SkepticAgent) is responsible for independently "
    "challenging this claim, not you."
)


def _draft_claim_text(business_name: str, template: str, primary_signals: dict, anchor_phrase: str) -> str:
    """Ask the LLM to phrase the claim. Falls back to the deterministic
    `template` whenever we're in MOCK_MODE (so the deterministic test
    suite is unaffected), or whenever a live response drops the anchor
    phrase SkepticAgent's routing depends on.
    """
    if llm_client.is_mock():
        return template

    prompt = (
        f"Business: {business_name}\n"
        f"Primary signals (one research pass, not yet cross-checked): {primary_signals}\n"
        f"Specific issue to phrase as a claim: {template}\n"
        f"Your claim MUST include this exact phrase: \"{anchor_phrase}\"."
    )
    text = llm_client.get_llm().complete(prompt, system=_SYSTEM_PROMPT).strip()
    if anchor_phrase.lower() not in text.lower():
        # Model drifted away from the required anchor phrase - safe fallback
        # so SkepticAgent's downstream routing never silently breaks.
        return template
    return text


def research_lead(lead: dict) -> list[Claim]:
    """Turn primary_signals into a list of candidate factual claims.

    Real system: primary_signals would be produced by Maps/site-scan tool
    calls. Here they come from the fixture (see data/leads.py docstring).
    """
    sig = lead["primary_signals"]
    name = lead["name"]
    claims: list[Claim] = []

    if sig.get("has_website") is False:
        anchor = "has no website"
        template = f"{name} {anchor}."
        claims.append(Claim(
            text=_draft_claim_text(name, template, sig, anchor),
            origin_agent="ResearchAgent",
            reasoning="Primary search pass found no business website.",
        ))

    if sig.get("has_website") and sig.get("site_has_booking_link") is False:
        anchor = "no online booking mechanism"
        template = f"{name} has a website but {anchor}."
        claims.append(Claim(
            text=_draft_claim_text(name, template, sig, anchor),
            origin_agent="ResearchAgent",
            reasoning="Website was found but no booking link/flow was detected on it.",
        ))

    if sig.get("site_has_booking_link") and sig.get("recent_review_snippets"):
        if any("book" in s.lower() for s in sig["recent_review_snippets"]):
            anchor = "broken or unreliable"
            template = f"{name}'s online booking link is {anchor}."
            claims.append(Claim(
                text=_draft_claim_text(name, template, sig, anchor),
                origin_agent="ResearchAgent",
                reasoning="Recent public reviews explicitly complain about the booking flow failing.",
            ))

    if sig.get("listing_hours_text"):
        anchor = "listing hours"
        template = f"{name}'s public {anchor} are '{sig['listing_hours_text']}'."
        claims.append(Claim(
            text=_draft_claim_text(name, template, sig, anchor),
            origin_agent="ResearchAgent",
            reasoning="Extracted directly from the business listing.",
        ))

    storage.log_activity(ActivityEvent(
        lead_id=lead["id"], agent="ResearchAgent",
        message=f"Generated {len(claims)} candidate claim(s) from a single research pass"
                f" ({'mock' if llm_client.is_mock() else 'live LLM'}).",
    ))
    return claims
