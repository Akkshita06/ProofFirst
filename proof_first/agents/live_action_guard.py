"""
LiveActionGuard - the mid-call sibling of action_executor.py's guard
boundary.

action_executor.py guards a single async action AFTER a human has already
clicked approve. This module guards a request BEFORE any human is
involved at all - it is the thing that decides whether a human needs to
be involved in the first place. Same philosophy as decision_router.py:
this is a plain function making a control-flow decision outside the LLM,
not a model "deciding" via free text, so the demo's outcome is
reproducible and explainable on stage.

Two signals combine, per the spec:
  (a) a hard threshold on the requested change (order value, or the
      change_type simply being outside the pre-set safe list at all)
  (b) the existing Skeptic-layer confidence for this lead - low
      confidence forces human approval even for a change that would
      otherwise be small.

Nothing here talks to Twilio, the DB, or an LLM - it is a pure function
so it can be unit tested with plain dicts, exactly like decision_router.route().
"""
from __future__ import annotations

from dataclasses import dataclass

# Change types considered inherently low-risk IF they also pass the
# numeric/threshold check below. Anything not in this set is high-risk
# by default - mirrors ACTION_WHITELIST's fail-closed posture in models.py.
SAFE_CHANGE_TYPES = {
    "adjust_wording",       # tweak proposal wording
    "change_followup_time", # move a follow-up time
    "adjust_quantity",      # small quantity tweak within range
    "adjust_delivery_date", # move a delivery date within range
}

# Hard thresholds. These are intentionally simple/explicit (not learned,
# not LLM-judged) so a judge can be told the exact number live and then
# ask you to cross it.
DEFAULT_THRESHOLDS = {
    "max_quantity_delta_pct": 0.15,     # +/-15% of original quantity is auto-approvable
    "max_order_value_delta": 500.00,    # in absolute currency units
    "max_followup_shift_days": 7,       # follow-up can move up to 7 days without approval
    "max_delivery_shift_days": 3,
    "min_skeptic_confidence": 0.6,      # below this, EVERYTHING requires human approval
}


@dataclass
class RiskDecision:
    auto_execute: bool
    risk: str        # "LOW" | "HIGH"
    reason: str


def _within_threshold(change_type: str, change_value: dict, thresholds: dict) -> tuple[bool, str]:
    if change_type == "adjust_wording":
        return True, "Wording tweaks carry no financial/contractual risk."

    if change_type == "change_followup_time":
        days = abs(change_value.get("shift_days", 0))
        if days <= thresholds["max_followup_shift_days"]:
            return True, f"Follow-up shift of {days}d is within the {thresholds['max_followup_shift_days']}d safe window."
        return False, f"Follow-up shift of {days}d exceeds the {thresholds['max_followup_shift_days']}d safe window."

    if change_type == "adjust_delivery_date":
        days = abs(change_value.get("shift_days", 0))
        if days <= thresholds["max_delivery_shift_days"]:
            return True, f"Delivery shift of {days}d is within the {thresholds['max_delivery_shift_days']}d safe window."
        return False, f"Delivery shift of {days}d exceeds the {thresholds['max_delivery_shift_days']}d safe window."

    if change_type == "adjust_quantity":
        original = change_value.get("original_quantity")
        new = change_value.get("new_quantity")
        if not original:
            return False, "No original quantity on file to compute a safe delta against - failing closed."
        delta_pct = abs(new - original) / original
        limit = thresholds["max_quantity_delta_pct"]
        if delta_pct <= limit:
            return True, f"Quantity change of {delta_pct:.0%} is within the {limit:.0%} safe range."
        return False, f"Quantity change of {delta_pct:.0%} exceeds the {limit:.0%} safe range."

    if change_type == "adjust_order_value":
        # Order value / contract-term changes are never in SAFE_CHANGE_TYPES
        # on their own merit - this branch exists so the reason string is
        # specific rather than a generic "unrecognized change type".
        delta = abs(change_value.get("delta", float("inf")))
        limit = thresholds["max_order_value_delta"]
        if delta <= limit:
            return True, f"Order-value change of {delta:.2f} is within the {limit:.2f} threshold."
        return False, f"Order-value change of {delta:.2f} exceeds the {limit:.2f} threshold."

    return False, f"'{change_type}' is not a whitelisted live-change type - failing closed."


def evaluate(
    change_type: str,
    change_value: dict,
    skeptic_confidence: float,
    thresholds: dict | None = None,
) -> RiskDecision:
    """The single decision point. Called from the Twilio webhook handler
    mid-call AND reused by tests - no Twilio/DB dependency here."""
    thresholds = thresholds or DEFAULT_THRESHOLDS

    if change_type not in SAFE_CHANGE_TYPES:
        return RiskDecision(
            auto_execute=False, risk="HIGH",
            reason=f"'{change_type}' is not on the pre-set safe list - requires human approval.",
        )

    threshold_ok, threshold_reason = _within_threshold(change_type, change_value, thresholds)
    if not threshold_ok:
        return RiskDecision(auto_execute=False, risk="HIGH", reason=threshold_reason)

    min_conf = thresholds["min_skeptic_confidence"]
    if skeptic_confidence < min_conf:
        return RiskDecision(
            auto_execute=False, risk="HIGH",
            reason=(
                f"{threshold_reason} However, Skeptic-layer confidence for this lead is "
                f"{skeptic_confidence:.2f}, below the {min_conf:.2f} floor required to "
                f"auto-approve even a small change - requires human approval."
            ),
        )

    return RiskDecision(
        auto_execute=True, risk="LOW",
        reason=f"{threshold_reason} Skeptic confidence {skeptic_confidence:.2f} >= {min_conf:.2f} floor.",
    )
