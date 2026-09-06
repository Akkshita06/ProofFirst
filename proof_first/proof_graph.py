"""
ProofGraph - reshapes the flat, already-logged pipeline state into an
explicit, judge-facing tree:

    Task -> Claim -> Evidence(+/-) -> Decision -> Action -> Verification -> Outcome

This is deliberately additive-only: it adds NO new instrumentation. Every
field it reads (dossier claims/evidence, actions, activity log messages,
the outreach message) is already being written by storage.py today via
evidence_synthesis_agent, skeptic_agent, decision_router, action_decision_
agent, preflight_verifier, action_executor, post_action_verifier, and
outreach_agent. This module only assembles those existing rows into
parent/child form for display - it does not change pipeline behavior and
does not write anything new to storage.

Node shape (JSON-friendly dict):
    {
        "id": str,              # stable id, unique within a lead's graph
        "node_type": str,       # Task | Claim | Evidence | Decision | Action
                                 # | Verification | Outcome
        "title": str,           # short label
        "detail": str,          # the longer, already-logged explanation
        "status": str | None,   # verdict / action status / etc, when applicable
        "ts": float | None,     # timestamp, when known
        "meta": dict,           # extra structured fields (confidence, source, ...)
        "children": [Node, ...]
    }
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from proof_first import storage
from proof_first.data.leads import get_lead


def _node(
    node_id: str,
    node_type: str,
    title: str,
    detail: str = "",
    meta: Optional[dict] = None,
    status: Optional[str] = None,
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "id": node_id,
        "node_type": node_type,
        "title": title,
        "detail": detail,
        "status": status,
        "ts": ts,
        "meta": meta or {},
        "children": [],
    }


def _latest_message(activity: List[dict], agent: str) -> Optional[dict]:
    """activity is ASC by ts (see storage.list_activity) - the last match
    for an agent is that agent's most recent logged line for this lead."""
    matches = [e for e in activity if e["agent"] == agent]
    return matches[-1] if matches else None


def _evidence_nodes(claim_id: str, items: List[dict], supports: bool) -> List[dict]:
    nodes = []
    polarity = "supports" if supports else "contradicts"
    for i, ev in enumerate(items):
        nodes.append(_node(
            node_id=f"{claim_id}:evidence:{polarity}:{i}",
            node_type="Evidence",
            title=ev.get("text", ""),
            detail=f"Source: {(ev.get('source') or 'unknown').replace('_', ' ')}",
            meta={"source": ev.get("source"), "supports": ev.get("supports", supports)},
            status="SUPPORTS" if supports else "CONTRADICTS",
            ts=ev.get("retrieved_at"),
        ))
    return nodes


def _claim_node(claim: dict) -> dict:
    node = _node(
        node_id=claim["id"],
        node_type="Claim",
        title=claim.get("text", ""),
        detail=claim.get("reasoning", ""),
        meta={"origin_agent": claim.get("origin_agent"), "confidence": claim.get("confidence")},
        status=claim.get("verdict"),
    )
    node["children"].extend(_evidence_nodes(claim["id"], claim.get("supporting_evidence") or [], True))
    node["children"].extend(_evidence_nodes(claim["id"], claim.get("contradicting_evidence") or [], False))
    return node


def _pick_anchor_claim_id(dossier: dict, action: Optional[dict]) -> Optional[str]:
    """Which claim is the final decision actually about, for the purposes
    of nesting Decision/Action/Verification/Outcome underneath it. Mirrors
    the same logic each real agent already used to get here:
      - if an action was proposed, it already names its target via
        defect_claim_id (action_decision_agent) - just use that.
      - otherwise (CLOSE/HUMAN_REVIEW with no action), fall back to the
        same claim evidence_synthesis_agent's math was actually driven by:
        the lowest-confidence CONTRADICTED claim if any exist (that's what
        capped overall_confidence), else the highest-confidence
        CORROBORATED claim.
    """
    if action:
        return action.get("defect_claim_id")
    claims = dossier.get("claims") or []
    contradicted = [c for c in claims if c.get("verdict") == "CONTRADICTED"]
    corroborated = [c for c in claims if c.get("verdict") == "CORROBORATED"]
    if contradicted:
        return min(contradicted, key=lambda c: c.get("confidence", 0))["id"]
    if corroborated:
        return max(corroborated, key=lambda c: c.get("confidence", 0))["id"]
    return None


def _outcome(node_id: str, title: str, detail: str, status: Optional[str] = None) -> dict:
    return _node(node_id=node_id, node_type="Outcome", title=title, detail=detail, status=status)


def _action_branch(lead_id: str, action: dict, activity: List[dict]) -> dict:
    decide_msg = _latest_message(activity, "ActionDecisionAgent")
    action_node = _node(
        node_id=action["id"],
        node_type="Action",
        title=action.get("description") or action.get("action_type") or "Proposed action",
        detail=decide_msg["message"] if decide_msg else "",
        meta={
            "action_type": action.get("action_type"),
            "risk": action.get("risk"),
            "reversible": action.get("reversible"),
        },
        status=action.get("status"),
        ts=action.get("created_at"),
    )

    status = action.get("status")

    if status == "BLOCKED":
        preflight_msg = _latest_message(activity, "PreflightVerifierAgent")
        action_node["children"].append(_outcome(
            f"outcome:{action['id']}",
            "Blocked before execution",
            preflight_msg["message"] if preflight_msg else (action.get("block_reason") or "Blocked at preflight."),
            status="BLOCKED",
        ))
        return action_node

    if status == "REJECTED":
        action_node["children"].append(_outcome(
            f"outcome:{action['id']}", "Rejected by human reviewer",
            "A human declined to approve this action - it was never executed.",
            status="REJECTED",
        ))
        return action_node

    verify_msg = _latest_message(activity, "PostActionVerifier")
    exec_msg = _latest_message(activity, "ActionExecutor")

    if action.get("verification_result") or verify_msg:
        verification = _node(
            node_id=f"verify:{action['id']}",
            node_type="Verification",
            title=action.get("verification_result") or (verify_msg["message"] if verify_msg else ""),
            detail=exec_msg["message"] if exec_msg else "",
            meta={"artifact_url": action.get("artifact_url")},
            status=status,
            ts=verify_msg["ts"] if verify_msg else action.get("created_at"),
        )
        action_node["children"].append(verification)

        if status == "SUCCESS":
            outreach_msg = _latest_message(activity, "OutreachAgent")
            outreach_text = storage.get_outreach(lead_id)
            verification["children"].append(_outcome(
                f"outcome:{lead_id}", "Outreach sent",
                outreach_text or (outreach_msg["message"] if outreach_msg else "Outreach generated."),
                status="SUCCESS",
            ))
        elif status == "ROLLED_BACK":
            rollback_msg = _latest_message(activity, "RollbackAgent")
            verification["children"].append(_outcome(
                f"outcome:{action['id']}", "Rolled back - no outreach sent",
                rollback_msg["message"] if rollback_msg else "Action failed verification and was rolled back.",
                status="ROLLED_BACK",
            ))
        elif status == "FAILED":
            verification["children"].append(_outcome(
                f"outcome:{action['id']}", "Failed - no outreach sent",
                action.get("verification_result") or "Execution failed.",
                status="FAILED",
            ))
    else:
        action_node["children"].append(_outcome(
            f"outcome:{action['id']}", f"Awaiting next step ({status})",
            "Approved but not yet executed/verified." if status == "APPROVED" else "Awaiting human approval.",
            status=status,
        ))

    return action_node


def _decision_branch(lead_id: str, dossier: dict, action: Optional[dict], activity: List[dict]) -> dict:
    decision_msg = _latest_message(activity, "DecisionAgent")
    decision_label = dossier.get("recommended_action") or "UNKNOWN"
    title = decision_msg["message"].split(":", 1)[0] if decision_msg else decision_label
    decision = _node(
        node_id=f"decision:{lead_id}",
        node_type="Decision",
        title=title,
        detail=decision_msg["message"] if decision_msg else f"overall_confidence={dossier.get('overall_confidence')}",
        meta={
            "overall_confidence": dossier.get("overall_confidence"),
            "recommended_action": dossier.get("recommended_action"),
        },
        ts=decision_msg["ts"] if decision_msg else dossier.get("created_at"),
    )

    if action:
        decision["children"].append(_action_branch(lead_id, action, activity))
    else:
        if decision_label == "PROCEED_TO_PROOF_OF_WORK":
            no_action_msg = _latest_message(activity, "ActionDecisionAgent")
            reason = no_action_msg["message"] if no_action_msg else "No safe whitelisted action was available."
        else:
            reason = decision_msg["message"] if decision_msg else "No action was taken for this lead."
        decision["children"].append(_outcome(f"outcome:{lead_id}", "No action taken", reason))

    return decision


def build_proof_graph(lead_id: str) -> dict:
    """Returns {"lead_id", "lead_name", "root"} where root is the Task node
    with the full Claim/Evidence/Decision/Action/Verification/Outcome tree
    nested beneath it. Raises KeyError via get_lead if lead_id is unknown."""
    lead = get_lead(lead_id)
    dossier = storage.get_dossier(lead_id)
    activity = storage.list_activity(lead_id)

    task = _node(
        node_id=f"task:{lead_id}",
        node_type="Task",
        title=f"Lead: {lead['name']}",
        detail=f"vertical={lead.get('vertical')}",
        meta={"lead_id": lead_id, "vertical": lead.get("vertical")},
        ts=dossier.get("created_at") if dossier else None,
    )

    if not dossier:
        task["children"].append(_outcome(
            f"outcome:{lead_id}", "Not started", "No research has been run yet for this lead.",
        ))
        return {"lead_id": lead_id, "lead_name": lead["name"], "root": task}

    claims = dossier.get("claims") or []
    claim_nodes = {c["id"]: _claim_node(c) for c in claims}

    actions = storage.list_actions(lead_id)
    latest_action = actions[0] if actions else None  # storage sorts created_at DESC

    anchor_id = _pick_anchor_claim_id(dossier, latest_action)
    decision_branch = _decision_branch(lead_id, dossier, latest_action, activity)

    task_children = list(claim_nodes.values())
    if anchor_id and anchor_id in claim_nodes:
        claim_nodes[anchor_id]["children"].append(decision_branch)
    else:
        task_children.append(decision_branch)
    task["children"] = task_children

    return {"lead_id": lead_id, "lead_name": lead["name"], "root": task}
