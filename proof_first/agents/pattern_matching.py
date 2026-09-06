"""
Deterministic matcher for learned_patterns.

A pattern's trigger_signal is always a small, explicit dict:
    {"signal_key": "<key in lead['secondary_signals']>", "expected": <value>}

Matching is a plain equality check against the lead's own secondary_signals -
never an LLM judgment call - so applying a learned pattern stays exactly as
auditable as the deterministic _check_* functions in skeptic_agent.py. This
is deliberate: memory should adjust confidence, not replace deterministic
verdict logic with something unpredictable.
"""
from __future__ import annotations

from typing import Any, Dict


def matches(secondary_signals: Dict[str, Any], trigger_signal: Dict[str, Any]) -> bool:
    key = trigger_signal.get("signal_key")
    expected = trigger_signal.get("expected")
    if key is None:
        return False
    actual = secondary_signals.get(key)
    return actual == expected


def build_trigger_signal(signal_key: str, expected: Any) -> Dict[str, Any]:
    return {"signal_key": signal_key, "expected": expected}
