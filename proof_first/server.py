"""
FastAPI server: exposes the pipeline as JSON endpoints and serves the
Evidence Command Center dashboard (static/index.html + app.js) that polls
them. Run with:

    uvicorn proof_first.server:app --reload --port 8090

No template engine / no client framework build step - deliberately plain
so it runs with zero extra tooling during a hackathon weekend.
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

# Neatlogs (optional sponsor-tool tracing integration - see NEATLOGS_API_KEY
# in the README) instruments google.generativeai at *import time*, so this
# must run before proof_first.llm_client (or anything that imports it) is
# imported anywhere below. Fully skipped - no import, no init call - when
# NEATLOGS_API_KEY is unset, which is the default and leaves behavior
# unchanged.
if os.environ.get("NEATLOGS_API_KEY"):
    try:
        import neatlogs
        neatlogs.init(
            api_key=os.environ.get("NEATLOGS_API_KEY"),
            instrumentations=["google-genai"],
        )
    except Exception as e:  # pragma: no cover - graceful degrade
        print(f"[server] Neatlogs init failed, continuing without tracing: {e}")

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from proof_first import storage, orchestrator, llm_client, proof_graph
from proof_first.data.leads import LEADS, get_lead
from proof_first.models import ActionStatus, CallSession, LiveActionRequest, LiveActionStatus
from proof_first.agents import live_action_guard, live_request_parser
from proof_first.integrations import telephony
from proof_first.integrations import site_scan
from fastapi import Request, Form
import copy
from unittest.mock import patch as _mock_patch

app = FastAPI(title="ProofFirst")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def _startup():
    storage.init_db(reset=False)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/leads")
def api_leads():
    return {"leads": [{"id": l["id"], "name": l["name"], "vertical": l["vertical"]} for l in LEADS],
            "mock_mode": llm_client.is_mock()}


### --- Chaos Mode -----------------------------------------------------
#
# This does not add any new failure-handling logic. Every branch below
# routes through the exact same site_scan.SiteScanOutcome / SkepticAgent
# behavior already exercised in tests/test_fault_injection.py - the
# `?inject=` param just forces the independent site-scan check into one
# of those already-tested failure modes on demand, instead of only being
# reachable by mocking `requests.get` inside pytest. Fail-closed routing
# to UNVERIFIABLE (and therefore HUMAN_REVIEW downstream) is the existing,
# already-tested behavior of skeptic_agent._check_broken_link_claim - this
# just makes it clickable.

CHAOS_MODES = ("timeout", "429", "conflicting_source")


def _chaos_check_booking_link(mode: str):
    """Returns a stand-in for site_scan.check_booking_link that ignores
    its real arguments and always returns the SiteScanResult for the
    requested failure mode - the same result shapes
    tests/test_fault_injection.py already asserts on."""

    def _fake(url, fixture_status):
        if mode == "timeout":
            return site_scan.SiteScanResult(
                status=None,
                outcome=site_scan.SiteScanOutcome.TIMEOUT,
                detail="[Chaos Mode] Injected timeout: simulated network timeout during the independent site-scan re-check.",
            )
        if mode == "429":
            return site_scan.SiteScanResult(
                status=429,
                outcome=site_scan.SiteScanOutcome.RATE_LIMITED,
                detail="[Chaos Mode] Injected 429: simulated rate-limit response from the independent site-scan re-check.",
            )
        if mode == "conflicting_source":
            # DEFINITIVE first check (broken) - the disagreement with the
            # second independent monitor is injected via secondary_signals
            # below, reusing the existing two-source-disagreement branch.
            return site_scan.SiteScanResult(
                status=500,
                outcome=site_scan.SiteScanOutcome.DEFINITIVE,
                detail="[Chaos Mode] Injected definitive check: first independent monitor reports HTTP 500.",
            )
        raise ValueError(f"unknown chaos mode {mode!r}")

    return _fake


@app.post("/api/leads/{lead_id}/run")
def api_run_research(lead_id: str, inject: Optional[str] = None):
    """Runs Research -> Skeptic -> Evidence Synthesis -> Decision only.
    Stops BEFORE any real-world action so the UI can show the human
    approval checkpoint for the ACT branch.

    `?inject=timeout|429|conflicting_source` (Chaos Mode) forces the
    independent site_scan re-check into one of the failure modes already
    covered by tests/test_fault_injection.py, so the fail-closed routing
    to UNVERIFIABLE / HUMAN_REVIEW can be triggered live instead of only
    asserted in pytest. No new failure-handling logic is added here."""
    try:
        lead = get_lead(lead_id)
    except KeyError:
        raise HTTPException(404, "unknown lead")

    if inject is not None and inject not in CHAOS_MODES:
        raise HTTPException(400, f"unknown inject mode '{inject}' - choose one of {list(CHAOS_MODES)}")

    if inject:
        # Work on a copy so injected fault conditions never leak into the
        # shared LEADS fixtures used by other requests/tests.
        lead = copy.deepcopy(lead)
        if inject == "conflicting_source":
            lead.setdefault("secondary_signals", {})
            # Force a second independent monitor that disagrees with the
            # injected first check (HTTP 500) above - the exact shape
            # test_two_disagreeing_independent_sources_yield_unverifiable_not_a_pick
            # already exercises.
            lead["secondary_signals"]["booking_link_http_status_secondary_check"] = 200

        storage.log_activity_compat(
            lead_id, "ChaosMode", f"Injecting '{inject}' fault into the independent site_scan re-check for this run."
        )
        with _mock_patch.object(site_scan, "check_booking_link", _chaos_check_booking_link(inject)):
            dossier, decision = orchestrator.run_research_through_decision(lead)
    else:
        dossier, decision = orchestrator.run_research_through_decision(lead)

    proposed = None
    if decision == "PROCEED_TO_PROOF_OF_WORK":
        proposed = orchestrator.propose_action_for_lead(lead, dossier)

    return {
        "dossier": dossier.to_dict(),
        "decision": decision,
        "proposed_action": proposed.to_dict() if proposed else None,
        "chaos_injected": inject,
    }


class ApprovalRequest(BaseModel):
    action_id: str
    approved: bool


@app.post("/api/leads/{lead_id}/approve")
def api_approve(lead_id: str, body: ApprovalRequest):
    lead = get_lead(lead_id)
    actions = storage.list_actions(lead_id)
    match = next((a for a in actions if a["id"] == body.action_id), None)
    if not match:
        raise HTTPException(404, "action not found")

    from proof_first.models import ProposedAction
    action = ProposedAction(**{**match, "status": ActionStatus(match["status"])})

    action = orchestrator.approve_action(action, lead["name"], approved=body.approved)

    outreach_message = None
    outreach_blocked_reason = None
    if action.status == ActionStatus.SUCCESS:
        dossier_dict = storage.get_dossier(lead_id)
        from proof_first.models import EvidenceDossier, Claim, Verdict
        claims = []
        for c in dossier_dict["claims"]:
            claim = Claim(id=c["id"], text=c["text"], origin_agent=c["origin_agent"],
                           verdict=Verdict(c["verdict"]) if c["verdict"] else None,
                           confidence=c["confidence"], reasoning=c["reasoning"])
            claims.append(claim)
        dossier = EvidenceDossier(lead_id=lead_id, lead_name=lead["name"], claims=claims,
                                   overall_confidence=dossier_dict["overall_confidence"])
        try:
            outreach_message = orchestrator.finalize_outreach(dossier, action)
        except orchestrator.outreach_agent.OutreachRateLimitExceeded as e:
            # Rate-limit block must be visible to the caller, not swallowed
            # into a generic 200-with-no-message response. Kept as an
            # additive field (outreach_message stays present, just None)
            # rather than changing the meaning of any existing field, so
            # test_api.py's existing assertions are unaffected.
            outreach_blocked_reason = str(e)

    return {
        "action": action.to_dict(),
        "outreach_message": outreach_message,
        "outreach_blocked_reason": outreach_blocked_reason,
    }


@app.get("/api/leads/{lead_id}/activity")
def api_activity(lead_id: str):
    return {"activity": storage.list_activity(lead_id)}


@app.get("/api/leads/{lead_id}/proof-graph")
def api_proof_graph(lead_id: str):
    """Read-only: assembles the dossier/action/activity/outreach rows this
    lead already has in storage into an explicit Task -> Claim ->
    Evidence(+/-) -> Decision -> Action -> Verification -> Outcome tree, so
    a judge can click from the final outcome back to the specific evidence
    that produced it. Adds no new instrumentation and writes nothing -
    it's a pure reshape of rows the pipeline was already logging."""
    try:
        return proof_graph.build_proof_graph(lead_id)
    except KeyError:
        raise HTTPException(404, "unknown lead")


@app.get("/api/leads/{lead_id}/outreach")
def api_outreach(lead_id: str):
    return {"message": storage.get_outreach(lead_id)}


@app.get("/api/leads/{lead_id}/action")
def api_get_action(lead_id: str):
    """Pure read-only fetch of a lead's latest proposed/decided action plus
    its dossier, for UI surfaces (e.g. the approval panel) that only need to
    *view* existing state. Unlike POST /run, this never touches orchestrator,
    never calls an agent, and never inserts a new row anywhere - it is exactly
    storage.list_actions(lead_id)[0] + storage.get_dossier(lead_id), the same
    additive read pattern as /api/overview."""
    actions = storage.list_actions(lead_id)
    latest_action = actions[0] if actions else None
    dossier = storage.get_dossier(lead_id)
    return {"action": latest_action, "dossier": dossier}


class FeedbackRequest(BaseModel):
    claim_id: str
    correct_verdict: str  # "CORROBORATED" | "CONTRADICTED" | "UNVERIFIABLE"


@app.post("/api/leads/{lead_id}/feedback")
def api_feedback(lead_id: str, body: FeedbackRequest):
    """Track 1 learning loop, human-in-the-loop path: a person tells the
    system a claim's predicted verdict was wrong. ReflectionAgent turns that
    into a candidate learned_pattern (promoted once a second, independent
    lead corroborates it - see storage.upsert_learned_pattern), which
    SkepticAgent then consults on every future run. This is the fastest way
    to demonstrate "the agent learns and applies it later" live."""
    from proof_first.agents import reflection_agent
    lead = get_lead(lead_id)
    dossier_dict = storage.get_dossier(lead_id)
    if dossier_dict is None:
        raise HTTPException(404, "no dossier for this lead yet - run it first")

    from proof_first.models import EvidenceDossier, Claim, Verdict
    claims = []
    for c in dossier_dict["claims"]:
        claim = Claim(id=c["id"], text=c["text"], origin_agent=c["origin_agent"],
                       confidence=c["confidence"], reasoning=c["reasoning"])
        claim.verdict = Verdict(c["verdict"]) if c["verdict"] else None
        claims.append(claim)
    dossier = EvidenceDossier(lead_id=dossier_dict["lead_id"], lead_name=dossier_dict["lead_name"], claims=claims)

    pattern_id = reflection_agent.reflect_on_human_feedback(lead, dossier, body.claim_id, body.correct_verdict)
    return {"pattern_id": pattern_id, "learned": pattern_id is not None}


@app.get("/api/learned-patterns")
def api_learned_patterns():
    """Read-only: every pattern the ReflectionAgent has ever proposed,
    whether promoted (active) or still awaiting a second corroborating
    lead. This is the dashboard surface for "show me what the agent has
    learned so far"."""
    return {"patterns": storage.list_all_patterns()}


@app.get("/api/leads/{lead_id}/reflections")
def api_reflections(lead_id: str):
    """Read-only: the self-reflection trail for one lead - every
    predicted-vs-actual comparison ReflectionAgent has made, whether or
    not it produced a learned pattern."""
    return {"reflections": storage.list_reflections(lead_id)}


class StartCallRequest(BaseModel):
    to_phone_number: str  # E.164, e.g. "+15551234567" - must be a Twilio-verified number on a trial account


@app.post("/api/leads/{lead_id}/call/start")
def api_start_call(lead_id: str, body: StartCallRequest, request: Request):
    """Places an outbound call via Twilio's REST API. Requires
    TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER env vars
    and outbound network access to api.twilio.com - see the setup note in
    the accompanying writeup for what to verify before the demo."""
    lead = get_lead(lead_id)  # 404s via KeyError -> unhandled here on purpose: matches other endpoints' style
    try:
        from twilio.rest import Client
    except ImportError:
        raise HTTPException(500, "twilio package not installed - run: pip install twilio")

    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not (sid and token and from_number):
        raise HTTPException(500, "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER not set")

    base_url = str(request.base_url).rstrip("/")
    voice_url = f"{base_url}/twilio/voice?lead_id={lead_id}"

    client = Client(sid, token)
    call = client.calls.create(to=body.to_phone_number, from_=from_number, url=voice_url, method="POST")

    session = CallSession(lead_id=lead_id, twilio_call_sid=call.sid)
    storage.save_call_session(session)
    storage.log_activity_compat(lead_id, "LiveCallAgent", f"Outbound call placed (Twilio SID {call.sid}).")
    return {"call_session_id": session.id, "twilio_call_sid": call.sid}


def _current_skeptic_confidence(lead_id: str) -> float:
    dossier = storage.get_dossier(lead_id)
    if not dossier:
        # No research run yet for this lead - fail closed rather than
        # assuming high confidence with nothing to back it.
        return 0.0
    return dossier.get("overall_confidence", 0.0)


@app.post("/twilio/voice")
async def twilio_voice(request: Request, lead_id: str):
    """First webhook Twilio hits when the call connects."""
    lead = get_lead(lead_id)
    gather_url = f"{str(request.base_url).rstrip('/')}/twilio/gather?lead_id={lead_id}"
    greeting = (
        f"Hi, this is the ProofFirst assistant calling about {lead['name']}. "
        f"You can ask me to make a change - for example, adjust the delivery date "
        f"or reduce the quantity - and I'll handle it live, or flag it for approval "
        f"if it's outside what I can do on my own."
    )
    twiml = telephony.gather_next_instruction(gather_url, prompt=greeting)
    from fastapi.responses import Response
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/gather")
async def twilio_gather(request: Request, lead_id: str, SpeechResult: str = Form(default=""), CallSid: str = Form(default="")):
    """Fires every time Twilio finishes listening for one spoken
    instruction. This is the mid-call invocation point requested in the
    spec: it runs the SAME guard used everywhere else in the app
    (live_action_guard.evaluate), and either executes immediately or
    drops a PENDING_APPROVAL row the dashboard/approve endpoint already
    knows how to render and resolve."""
    lead = get_lead(lead_id)
    session_dict = storage.get_call_session_by_twilio_sid(CallSid)
    call_session_id = session_dict["id"] if session_dict else CallSid

    transcript = SpeechResult.strip()
    gather_url = f"{str(request.base_url).rstrip('/')}/twilio/gather?lead_id={lead_id}"

    if not transcript:
        twiml = telephony.gather_next_instruction(gather_url, prompt="Sorry, I didn't catch that - go ahead.")
        from fastapi.responses import Response
        return Response(content=twiml, media_type="application/xml")

    parsed = live_request_parser.parse(transcript, lead)
    confidence = _current_skeptic_confidence(lead_id)
    decision = live_action_guard.evaluate(parsed.change_type, parsed.change_value, confidence)

    req = LiveActionRequest(
        call_session_id=call_session_id,
        lead_id=lead_id,
        raw_transcript=transcript,
        change_type=parsed.change_type,
        change_value=parsed.change_value,
        risk=decision.risk,
        skeptic_confidence=confidence,
    )

    if decision.auto_execute:
        req.status = LiveActionStatus.AUTO_EXECUTED
        req.reason = decision.reason
        req.spoken_reply = f"Done - {_describe_change(parsed)}. {decision.reason}"
        storage.log_activity_compat(
            lead_id, "LiveActionGuard",
            f"AUTO-EXECUTED live request '{transcript}' -> {parsed.change_type} ({decision.reason})",
        )
    else:
        req.status = LiveActionStatus.PENDING_APPROVAL
        req.reason = decision.reason
        req.spoken_reply = (
            f"That's outside what I can approve directly - I'll flag this for human approval. {decision.reason}"
        )
        storage.log_activity_compat(
            lead_id, "LiveActionGuard",
            f"HELD FOR APPROVAL live request '{transcript}' -> {parsed.change_type} ({decision.reason})",
        )

    storage.save_live_action_request(req)

    twiml = telephony.respond_and_continue_listening(req.spoken_reply, gather_url)
    from fastapi.responses import Response
    return Response(content=twiml, media_type="application/xml")


def _describe_change(parsed) -> str:
    ct, cv = parsed.change_type, parsed.change_value
    if ct == "adjust_quantity":
        return f"quantity updated to {cv.get('new_quantity')}"
    if ct == "adjust_delivery_date":
        return f"delivery date shifted by {cv.get('shift_days')} day(s)"
    if ct == "change_followup_time":
        return f"follow-up shifted by {cv.get('shift_days')} day(s)"
    if ct == "adjust_wording":
        return "wording updated"
    return "change applied"


@app.get("/api/leads/{lead_id}/live-requests")
def api_live_requests(lead_id: str):
    """Polled by the dashboard while a call is active - same polling
    mechanism the rest of the UI already uses, just on a shorter
    interval (see app.js). Every AUTO_EXECUTED / PENDING_APPROVAL row
    shows up here the instant twilio_gather() writes it."""
    return {"live_requests": storage.list_live_action_requests(lead_id)}


@app.post("/api/live-requests/{request_id}/approve")
def api_approve_live_request(request_id: str, approved: bool):
    """Resolves a PENDING_APPROVAL live request from the dashboard (the
    human-in-the-loop path for the high-risk branch). This intentionally
    does NOT re-implement approval logic - it updates the live request's
    terminal status; wiring the actual downstream mutation (e.g. writing
    the approved quantity/date to the lead record) is a few lines here
    once you tell me where lead order/schedule data actually lives, since
    that wasn't in the uploaded pipeline (data/leads.py currently ships
    fixtures, not a live order backend)."""
    matches = [r for r in storage.list_live_action_requests() if r["id"] == request_id]
    if not matches:
        raise HTTPException(404, "live action request not found")
    req_dict = matches[0]
    req_dict["status"] = LiveActionStatus.APPROVED_EXECUTED.value if approved else LiveActionStatus.REJECTED.value
    from proof_first.models import LiveActionRequest as LAR, LiveActionStatus as LAS
    req = LAR(**{**req_dict, "status": LAS(req_dict["status"])})
    storage.save_live_action_request(req)
    storage.log_activity_compat(
        req.lead_id, "LiveActionGuard",
        f"Human {'APPROVED' if approved else 'REJECTED'} held live request: {req.raw_transcript}",
    )
    return {"live_request": req.to_dict()}


@app.get("/api/evaluation")
def api_evaluation():
    from proof_first.evaluation import run_evaluation
    return run_evaluation()


@app.get("/api/overview")
def api_overview():
    """Additive read-only aggregation for the workspace UI: joins the lead
    fixtures with whatever dossier/action/activity state already exists in
    SQLite for each lead. Does not touch pipeline logic - it only reads
    what storage.py already persists, so a lead with no dossier yet simply
    reports status "not_started" instead of being invented."""
    dossiers = {d["lead_id"]: d for d in storage.list_dossiers()}
    actions = storage.list_actions()
    actions_by_lead: dict[str, list] = {}
    for a in actions:
        actions_by_lead.setdefault(a["lead_id"], []).append(a)
    activity = storage.list_activity()
    activity_by_lead: dict[str, list] = {}
    for ev in activity:
        activity_by_lead.setdefault(ev["lead_id"], []).append(ev)

    rows = []
    for lead in LEADS:
        lid = lead["id"]
        dossier = dossiers.get(lid)
        lead_actions = sorted(actions_by_lead.get(lid, []), key=lambda a: a["created_at"], reverse=True)
        lead_activity = sorted(activity_by_lead.get(lid, []), key=lambda e: e["ts"], reverse=True)
        latest_action = lead_actions[0] if lead_actions else None
        latest_event = lead_activity[0] if lead_activity else None

        if dossier is None:
            status = "not_started"
        elif latest_action and latest_action["status"] in ("SUCCESS", "EXECUTED"):
            status = "verified"
        elif latest_action and latest_action["status"] == "BLOCKED":
            status = "blocked"
        elif latest_action and latest_action["status"] == "PENDING_APPROVAL":
            status = "pending"
        elif dossier.get("recommended_action") == "CLOSE":
            status = "blocked"
        else:
            status = "reviewed"

        rows.append({
            "id": lid,
            "name": lead["name"],
            "vertical": lead["vertical"],
            "confidence": dossier.get("overall_confidence") if dossier else None,
            "recommended_action": dossier.get("recommended_action") if dossier else None,
            "evidence_count": sum(
                len(c.get("supporting_evidence", [])) + len(c.get("contradicting_evidence", []))
                for c in dossier.get("claims", [])
            ) if dossier else 0,
            "claim_count": len(dossier.get("claims", [])) if dossier else 0,
            "action_status": latest_action["status"] if latest_action else None,
            "action_id": latest_action["id"] if latest_action else None,
            "status": status,
            "last_event": {
                "agent": latest_event["agent"],
                "message": latest_event["message"],
                "ts": latest_event["ts"],
            } if latest_event else None,
        })

    return {"leads": rows, "mock_mode": llm_client.is_mock()}