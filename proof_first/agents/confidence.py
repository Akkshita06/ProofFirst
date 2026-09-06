"""
Small, deterministic, explainable confidence formula shared by
SkepticAgent and EvidenceSynthesisAgent.

One-sentence rule: confidence starts at a neutral 0.5 and is pushed
toward 1.0 or 0.0 in the direction the verdict points, by an amount
proportional to how independent, corroborated, non-contradicted, and
fresh the evidence behind that verdict is; when the verdict itself is
neutral (UNVERIFIABLE), confidence just stays close to 0.5, nudged down
a little as evidence quality drops.

This replaces hand-picked literals (0.85, 0.94, 0.2, ...) with a single
formula so every confidence number traces back to four named, comparable
factors instead of being invented independently per branch. It is
deliberately simple (one weighted sum) rather than "precise" - the
weights and 0-1 factor inputs are estimates, not measurements, and the
output is rounded to 2 decimal places to avoid implying false precision.
"""
from __future__ import annotations

# Weights for the four factors below. They sum to 1.0 so `strength` (the
# weighted sum) is itself always in [0, 1].
_WEIGHT_SOURCE_INDEPENDENCE = 0.35   # how independent the checking source is from the original claim's source
_WEIGHT_CORROBORATION = 0.30        # how many independent evidence items point the same way
_WEIGHT_NO_CONTRADICTION = 0.25     # whether this evidence agrees with everything else we have (reviews, other monitors)
_WEIGHT_FRESHNESS = 0.10            # how current/live the evidence is, vs a stale or indirect signal

# How far a directional (CORROBORATED/CONTRADICTED) verdict is allowed to
# move confidence away from the neutral midpoint at maximum evidence
# strength (0.5 +/- 0.44 => 0.94 / 0.06).
_DIRECTIONAL_SWING = 0.44
# How far a neutral (UNVERIFIABLE) verdict is allowed to drift below the
# midpoint as evidence quality drops.
_NEUTRAL_DRIFT = 0.20

_MIN_CONFIDENCE = 0.05
_MAX_CONFIDENCE = 0.95


def _corroboration_score(corroboration_count: float) -> float:
    """Linearly capped at 1.0. `corroboration_count` is usually a plain
    item count (0, 1, 2, ...), but callers may pass a fraction (e.g. 0.5)
    for a single indirect/weak signal - such as "no counter-evidence
    found" - that shouldn't earn the same full credit as a direct,
    positive independent confirmation."""
    return min(max(corroboration_count, 0.0), 1.0)


def compute_confidence(
    *,
    direction: int,
    source_independence: float,
    corroboration_count: float,
    contradiction_present: bool,
    freshness: float,
) -> float:
    """Compute a claim's confidence score.

    direction: +1 if the evidence supports the original claim being true
        (verdict CORROBORATED), -1 if it undermines it (verdict
        CONTRADICTED), 0 if the verdict is neutral (UNVERIFIABLE).
    source_independence: 0-1, how independent the checking source is.
    corroboration_count: number of independent evidence items agreeing
        with this verdict's direction.
    contradiction_present: True if this evidence conflicts with some
        other evidence we already have (a second monitor, or the
        original review/listing signal).
    freshness: 0-1, how current/live the evidence is.
    """
    strength = (
        _WEIGHT_SOURCE_INDEPENDENCE * source_independence
        + _WEIGHT_CORROBORATION * _corroboration_score(corroboration_count)
        + _WEIGHT_NO_CONTRADICTION * (0.0 if contradiction_present else 1.0)
        + _WEIGHT_FRESHNESS * freshness
    )

    if direction == 0:
        confidence = 0.5 - _NEUTRAL_DRIFT * (1.0 - strength)
    else:
        confidence = 0.5 + direction * _DIRECTIONAL_SWING * strength

    return round(min(max(confidence, _MIN_CONFIDENCE), _MAX_CONFIDENCE), 2)
