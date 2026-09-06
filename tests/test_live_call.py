from proof_first.agents import live_action_guard as g
from proof_first.agents import live_request_parser as p


def test_small_quantity_change_auto_executes_with_high_confidence():
    d = g.evaluate("adjust_quantity", {"original_quantity": 100, "new_quantity": 92}, skeptic_confidence=0.9)
    assert d.auto_execute is True
    assert d.risk == "LOW"


def test_large_quantity_change_requires_approval_even_with_high_confidence():
    d = g.evaluate("adjust_quantity", {"original_quantity": 100, "new_quantity": 50}, skeptic_confidence=0.95)
    assert d.auto_execute is False
    assert d.risk == "HIGH"


def test_small_change_still_blocked_by_low_skeptic_confidence():
    # This is the spec's requirement (b): low confidence forces human
    # approval even for a request that is otherwise small.
    d = g.evaluate("adjust_quantity", {"original_quantity": 100, "new_quantity": 92}, skeptic_confidence=0.3)
    assert d.auto_execute is False
    assert "confidence" in d.reason.lower()


def test_order_value_change_never_auto_executes_by_default():
    d = g.evaluate("adjust_order_value", {"delta": 50}, skeptic_confidence=0.99)
    assert d.auto_execute is False  # not in SAFE_CHANGE_TYPES at all


def test_unrecognized_change_type_fails_closed():
    d = g.evaluate("rewrite_the_contract", {}, skeptic_confidence=0.99)
    assert d.auto_execute is False
    assert d.risk == "HIGH"


def test_parser_matches_reduce_quantity_phrasing():
    parsed = p.parse("reduce the quantity by 5", {"order": {"quantity": 20}})
    assert parsed.change_type == "adjust_quantity"
    assert parsed.change_value["new_quantity"] == 15


def test_parser_matches_bare_delivery_date_phrasing():
    parsed = p.parse("can you change the delivery date", {})
    assert parsed.change_type == "adjust_delivery_date"


def test_parser_falls_back_to_unrecognized_for_out_of_scope_ask():
    parsed = p.parse("actually just rewrite the whole contract", {})
    assert parsed.change_type == "unrecognized"


def test_end_to_end_low_risk_transcript_auto_executes():
    parsed = p.parse("reduce the quantity by 2", {"order": {"quantity": 100}})
    d = g.evaluate(parsed.change_type, parsed.change_value, skeptic_confidence=0.8)
    assert d.auto_execute is True


def test_end_to_end_high_risk_transcript_is_held():
    parsed = p.parse("increase the order value by 5000", {})
    d = g.evaluate(parsed.change_type, parsed.change_value, skeptic_confidence=0.99)
    assert d.auto_execute is False
