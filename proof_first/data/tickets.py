"""
Sample support-ticket fixtures - Phase 2 (Prove Genericity).

This is the SECOND task type: verifying a support-ticket ROOT-CAUSE claim,
instead of verifying a local-business LEAD claim. It exists to prove that
`Claim` / `Evidence` / `EvidenceDossier` / `ProposedAction`
(proof_first/models.py) and the orchestrator control-flow functions
(`run_research_through_decision`, `propose_action_for_lead`) are actually
domain-neutral, not just structurally capable of it - see
agents/ticket_diagnosis_agent.py and demo_phase2.py.

Same honesty note as data/leads.py: `primary_signals` stands in for what a
single, untrusted research pass would pull from the ticket + a `git blame`/
deploy-log grep; `secondary_signals` stands in for what a SECOND,
independent check (an incident timeline, a rollback result, another
service's error rate) would show. These are hand-written, clearly-labelled
synthetic examples, not real incident data.

Each fixture is built to land on a specific decision branch, exactly like
data/leads.py, so the demo can show all three routes:
  - ticket_001 -> CORROBORATED root cause  -> PROCEED_TO_PROOF_OF_WORK (ACT)
  - ticket_002 -> CONTRADICTED root cause  -> CLOSE
  - ticket_003 -> UNVERIFIABLE root cause  -> HUMAN_REVIEW
"""
from __future__ import annotations

from typing import Dict, Any, List

TICKETS: List[Dict[str, Any]] = [
    {
        "id": "ticket_001",
        "name": "checkout-service 500 spike (INC-2201)",
        "system": "checkout-service",
        "primary_signals": {
            # what a single, untrusted pass over the ticket + deploy log turns up
            "recent_deploy": "payment-lib v2.3.1",
            "deploy_time_correlates_with_spike": True,
            "error_signature": "NullPointerException in PaymentValidator",
            "affected_endpoint": "/checkout",
        },
        "secondary_signals": {
            # what an INDEPENDENT second check (the actual rollback outcome)
            # turns up - this is the thing the first pass never looked at
            "rollback_performed": True,
            "error_rate_after_rollback": "dropped to baseline within 4 minutes",
        },
    },
    {
        "id": "ticket_002",
        "name": "auth-service timeouts (INC-2214)",
        "system": "auth-service",
        "primary_signals": {
            "recent_deploy": "auth-service v5.0",
            "deploy_time_correlates_with_spike": True,
            "error_signature": "connection timeout to redis",
            "affected_endpoint": "/login",
        },
        "secondary_signals": {
            # independent check: a shared Redis cluster incident that also
            # hit other, unrelated services that did NOT deploy anything -
            # this is what contradicts "the deploy caused it"
            "shared_dependency_incident_id": "INC-2213",
            "other_undeployed_services_also_errored": True,
        },
    },
    {
        "id": "ticket_003",
        "name": "search-service p99 latency (INC-2229)",
        "system": "search-service",
        "primary_signals": {
            "recent_deploy": "search-service v1.9",
            "deploy_time_correlates_with_spike": False,
            "error_signature": "p99 latency 3x baseline, no errors",
            "affected_endpoint": "/search",
        },
        "secondary_signals": {
            # no rollback was attempted and no shared-dependency incident is
            # on record either - nothing here can corroborate OR contradict
            "rollback_performed": False,
            "shared_dependency_incident_id": None,
        },
    },
]


def get_ticket(ticket_id: str) -> Dict[str, Any]:
    for ticket in TICKETS:
        if ticket["id"] == ticket_id:
            return ticket
    raise KeyError(ticket_id)
