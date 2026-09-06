"""
Core data structures shared by every agent in the ProofFirst pipeline.

These are plain dataclasses (no framework lock-in) so the pipeline can run
with or without google-adk / a live LLM. Every object here is what actually
gets persisted to storage.py and rendered by the dashboard - nothing is
UI-only or decorative.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import List, Optional, Dict, Any


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> float:
    return time.time()


class Verdict(str, Enum):
    CORROBORATED = "CORROBORATED"
    CONTRADICTED = "CONTRADICTED"
    UNVERIFIABLE = "UNVERIFIABLE"


class RecommendedAction(str, Enum):
    ACT = "ACT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLOSE = "CLOSE"


class ActionStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    BLOCKED = "BLOCKED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


@dataclass
class Evidence:
    text: str
    source: str            # e.g. "google_business_profile", "instagram", "site_scan"
    supports: bool          # True if this evidence supports the claim, False if it contradicts it
    retrieved_at: float = field(default_factory=_now)


@dataclass
class Claim:
    id: str = field(default_factory=_id)
    text: str = ""
    origin_agent: str = "ResearchAgent"
    verdict: Optional[Verdict] = None
    confidence: float = 0.0
    supporting_evidence: List[Evidence] = field(default_factory=list)
    contradicting_evidence: List[Evidence] = field(default_factory=list)
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value if self.verdict else None
        return d


@dataclass
class EvidenceDossier:
    lead_id: str
    lead_name: str
    claims: List[Claim] = field(default_factory=list)
    overall_confidence: float = 0.0
    recommended_action: Optional[RecommendedAction] = None
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "lead_name": self.lead_name,
            "claims": [c.to_dict() for c in self.claims],
            "overall_confidence": self.overall_confidence,
            "recommended_action": self.recommended_action.value if self.recommended_action else None,
            "created_at": self.created_at,
        }


@dataclass
class ProposedAction:
    id: str = field(default_factory=_id)
    lead_id: str = ""
    defect_claim_id: str = ""
    description: str = ""
    action_type: str = ""          # must be a member of ACTION_WHITELIST
    reversible: bool = True
    risk: str = "LOW"
    status: ActionStatus = ActionStatus.PENDING_APPROVAL
    block_reason: Optional[str] = None
    verification_result: Optional[str] = None
    artifact_url: Optional[str] = None
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class ActivityEvent:
    """One row in the agent-trace timeline the dashboard renders live."""
    id: str = field(default_factory=_id)
    lead_id: str = ""
    agent: str = ""
    message: str = ""
    ts: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LiveActionStatus(str, Enum):
    AUTO_EXECUTED = "AUTO_EXECUTED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED_EXECUTED = "APPROVED_EXECUTED"
    REJECTED = "REJECTED"


@dataclass
class CallSession:
    """One live phone call against a lead. Twilio's CallSid is the
    natural external key; `id` is our internal one so the dashboard/API
    never has to know about Twilio directly."""
    id: str = field(default_factory=_id)
    lead_id: str = ""
    twilio_call_sid: str = ""
    status: str = "IN_PROGRESS"  # IN_PROGRESS | COMPLETED
    started_at: float = field(default_factory=_now)
    ended_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LiveActionRequest:
    """A change the caller asked for verbally, mid-call. This is the
    live-call analogue of ProposedAction, but it always resolves to a
    terminal state within the same request (auto-execute) or drops into
    the SAME pending-approval queue action_executor already understands
    (PENDING_APPROVAL) - it does not invent a second approval mechanism.
    """
    id: str = field(default_factory=_id)
    call_session_id: str = ""
    lead_id: str = ""
    raw_transcript: str = ""          # what Twilio's speech-to-text heard
    change_type: str = ""             # e.g. "adjust_wording", "change_followup_time", "adjust_quantity", "adjust_order_value"
    change_value: Any = None          # parsed target value, e.g. new quantity/date
    risk: str = "LOW"                 # LOW | HIGH - from the threshold check
    skeptic_confidence: float = 1.0   # from the lead's latest EvidenceDossier
    status: LiveActionStatus = LiveActionStatus.PENDING_APPROVAL
    reason: str = ""                  # human-readable why AUTO_EXECUTED or PENDING_APPROVAL
    spoken_reply: str = ""            # exactly what the agent said on the call
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# The only action types the ActionExecutor is ever allowed to run.
# PreflightVerifierAgent BLOCKs anything not in this list - this is the
# guardrail equivalent of SalesShortcut's before_tool_callback pattern.
ACTION_WHITELIST = {
    "propose_listing_correction",   # draft a corrected business-listing detail
    "generate_booking_preview",     # build a live, reversible preview page proving a working booking flow
    "generate_corrected_artifact",  # e.g. a corrected hours/menu snippet
}
