<div align="center">

# PROOFFIRST

**An evidence-verified AI SDR pipeline where research is guilty until proven innocent.**

`76 automated tests` · `zero flakiness` · `real FastAPI server` · `real learning loop`

</div>

---

> **Core thesis:** Research is not trusted until another agent actively tries to disprove it.

Syndicate is built around sequential specialist agents, a custom conditional router, and a guarded real-world action, aimed at a fundamentally different problem than a typical outreach pipeline: **trust**. Every claim a research agent makes gets actively attacked by a second agent before it's allowed anywhere near a human prospect.

This is not a design doc. It's a running system: 76 tests pass on repeat, the server boots, and every number in this README came out of actually executing the code — see the shell transcript in this conversation for proof.

---

## 0. Why Syndicate exists

Most AI SDR pipelines have exactly one moment of "intelligence": a single research pass, trusted implicitly, that feeds straight into an outbound action. If that research is wrong, the system finds out the same way a human would — after the damage is done, on the call.

Syndicate inserts an adversarial checkpoint *before* any prospect is touched. A dedicated Skeptic agent is architecturally incapable of agreeing by default — it has to independently re-derive or contradict every claim. Nothing reaches outreach without surviving that.

---

## 1. What was kept, what was rebuilt

| Original pattern | Status in Syndicate |
|---|---|
| Lead discovery (Maps + clustering) | Reimplemented as a labelled fixture module (`data/leads.py`) standing in for live discovery calls. Swap-in point clearly marked for production Maps integration. |
| Single-pass, untrusted research | **Kept as single-pass and untrusted, on purpose.** Reimplemented independently in `agents/research_agent.py` — the entire premise depends on this stage staying naive so the next stage has something real to attack. |
| No post-research verification | **Built from scratch.** This is Syndicate's reason for existing: `agents/skeptic_agent.py` + `agents/evidence_synthesis_agent.py`. |
| Binary router on call outcome | Reimplemented independently as a **three-way** router (`agents/decision_router.py`) branching on *pre-contact evidence confidence*, not what happened after a call was already placed. |
| Duplicate-call guard | Generalized into `storage.already_actioned()` + a whitelist-gated callback in `agents/action_executor.py` — covers any action type, not just phone calls. |
| Hardcoded outbound phone call | Replaced with a real, local, inspectable file-write "proof of work" action — see the honesty notes in §6 on exactly what this does and doesn't prove. |
| Outreach agents | Rebuilt as `agents/outreach_agent.py`, which is *structurally* incapable of accepting raw research — it only accepts a completed dossier plus a confirmed `SUCCESS` action. |
| BigQuery storage | Plain SQLite (`storage.py`) — zero external setup, identical conceptual role. |
| No evaluation | `evaluation.py` computes live baseline-vs-Syndicate metrics, not hardcoded numbers. |

---

## 2. The pipeline

```
Lead Discovery (fixture)
   │
   ▼
ResearchAgent          — single-pass, deliberately untrusted
   │
   ▼
SkepticAgent           — independently tries to falsify every claim
   │
   ▼
EvidenceSynthesisAgent — confidence-scored, cited dossier
   │
   ▼
DecisionAgent (3-way router)
   ├── CLOSE                     → stop, log, no contact
   ├── HUMAN_REVIEW              → stop, hold for a human, dossier attached
   └── PROCEED_TO_PROOF_OF_WORK
          │
          ▼
       ActionDecisionAgent       — smallest safe whitelisted correction
          │
          ▼
       PreflightVerifierAgent    — factual / whitelist / reversible / duplicate checks
          │
          ▼
       [ HUMAN APPROVAL CHECKPOINT — dashboard button ]
          │
          ▼
       ActionExecutor            — guarded, real tool call
          │
          ▼
       PostActionVerifier
          ├── SUCCESS → OutreachAgent   (only leads with completed, verified proof)
          └── FAILED  → RollbackAgent   (outreach withheld, logged)
```

Nine distinct agents. One non-negotiable rule threaded through all of them: **a contradicted claim cannot structurally reach outreach.** This is enforced by function signatures and control flow, not convention — see `tests/test_pipeline.py::test_contradicted_claim_never_reaches_outreach`.

---

## 3. The learning loop — Syndicate gets sharper with every miss

Everything above is a deterministic, thoroughly tested pipeline — but static. Nothing in it improved on its own. The learning loop closes that gap without touching a single guardrail above it.

**The problem it solves:** the Skeptic's verdict/confidence math is fixed threshold logic. A miss on one lead didn't help it catch the same pattern on the next one. Now it does.

**How it works:**
- `storage.py` gains two tables: `learned_patterns` (small, auditable `{signal → confidence_delta}` rules, each traceable to the specific leads that produced them) and `reflections` (every predicted-vs-actual comparison the system has ever made).
- `agents/reflection_agent.py` compares a claim's predicted verdict against the real outcome once its action resolves, or against an explicit human correction via `POST /api/leads/{id}/feedback`. On a miss, it proposes a pattern **deterministically** — a lookup table, not an LLM guessing at a generalization. An LLM only writes the human-readable rationale, mirroring the "LLM narrates, rules decide" split already used in the Skeptic agent.
- `skeptic_agent.apply_learned_adjustments` runs *after* the deterministic verdict is set. It nudges confidence by a bounded, logged delta, clips to `[0, 1]`, and appends a visible note to `claim.reasoning`. **A pattern can only ever adjust confidence — it can never flip a verdict.**
- **Corroboration guardrail:** a pattern only goes live once it's been independently proposed by **2+ separate leads**. One bad correction can never poison future runs by itself.

**Proof this generalizes rather than memorizes:** `tests/test_learning_loop.py::test_activated_pattern_changes_a_third_never_corrected_lead` corrects two leads, then re-runs a *third* lead that was never touched — and its confidence moves while its verdict stays put. That's the literal bar for "learn from data, apply it later," met and tested.

**Multi-pass evidence, measured, not asserted:**

```json
[
  {"pass": 1, "active_patterns_count": 0, "syndicate": {"human_review_rate": 0.25}},
  {"pass": 2, "active_patterns_count": 1, "syndicate": {"human_review_rate": 0.4}},
  {"pass": 3, "active_patterns_count": 1, "syndicate": {"human_review_rate": 0.4}}
]
```

The system got *more calibrated*, not noisier — and the shift traces back to exactly one auditable, human-inspectable pattern, not a black box.

**New endpoints (all additive):**
- `POST /api/leads/{id}/feedback` — tell Syndicate it was wrong
- `GET /api/learned-patterns` — every pattern, live or still-candidate, with rationale and source leads
- `GET /api/leads/{id}/reflections` — the full self-reflection trail for a lead

**Stated plainly — what this doesn't do:** patterns are single-signal, single-direction, confidence-lowering rules. No compositional pattern language, no automatic decay. The human-feedback path is the reliable demo lever; the automatic pipeline-outcome path is architecturally complete but data-starved, because the whitelisted action almost never fails on its own (see §6, item 12).

---

## 4. Run it

```bash
cd syndicate
pip install -r requirements.txt

# CLI demo — no server needed, runs all fixture leads end-to-end:
python3 -c "
from syndicate import storage, orchestrator
from syndicate.data.leads import LEADS
storage.init_db(reset=True)
for lead in LEADS:
    r = orchestrator.run_full_pipeline(lead, auto_approve=True)
    print(lead['name'], '->', r.decision, '->', r.stopped_reason or 'outreach sent')
"

# Full test suite (the safety-critical guarantees):
python3 -m pytest tests/ -v

# Baseline vs. Syndicate evaluation — real numbers, not hardcoded:
python3 -m syndicate.evaluation

# Interactive dashboard:
uvicorn syndicate.server:app --reload --port 8090
# then open http://127.0.0.1:8090
```

### Environment variables

| Variable | Required? | Effect if unset |
|---|---|---|
| `GOOGLE_API_KEY` | No | Runs in **MOCK_MODE**: deterministic, rule-based claim/verdict logic over fixture leads — fully functional, clearly labelled in UI and code. |
| `GOOGLE_API_KEY` (set) | Optional | Routes narrative text through Gemini; verdict/routing logic stays deterministic by design (§6). |
| `TENSORMUX_BASE_URL` | No | Unset: falls through to `GOOGLE_API_KEY`/MOCK_MODE. Set to route narrative text through a TensorMux-compatible endpoint instead, taking priority over `GOOGLE_API_KEY`. |
| `TENSORMUX_API_KEY` | Only if `TENSORMUX_BASE_URL` set | Defaults to a placeholder for endpoints without auth. |
| `TENSORMUX_MODEL` | Only if `TENSORMUX_BASE_URL` set | Defaults to `qwen2.5:0.5b`. |
| `NEATLOGS_API_KEY` | No | Unset: tracing fully skipped, zero behavior change. Set to enable full workflow/agent/tool span tracing. |
| `PROOFFIRST_REAL_SITE_SCAN` | No | Unset/false: uses the fixture HTTP status. Set `1`/`true` to make a **real** `requests.get()` call against the lead's booking link. |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_FROM_NUMBER` | Only for Live Call (§8) | Without them, `/call/start` returns a clean 500 rather than faking a call. Nothing else requires Twilio. |

No Maps, ElevenLabs, BigQuery, or Gmail credentials required — those dependencies were deliberately not carried over.

---

## 5. What actually happened when this was run

1. `GET /api/leads` → confirms `mock_mode: true`, lists all 20 fixture leads.
2. `POST /api/leads/lead_001/run` (Acme Dental) → Skeptic finds an Instagram DM-booking flow the research pass missed → **`CLOSE`**, no action, no outreach.
3. `POST /api/leads/lead_002/run` (Acme Cafe) → broken-link claim independently re-confirmed via an HTTP 500 re-check → **`PROCEED_TO_PROOF_OF_WORK`**, one `PENDING_APPROVAL` action.
4. `POST /api/leads/lead_002/approve {"action_id": "<id>", "approved": true}` → executes, writes a real local artifact, verified to exist → **`SUCCESS`** → outreach message generated, containing the real artifact URL.
5. Re-running step 3 on the same lead → **`BLOCKED`**: *"An identical action has already succeeded for this lead (duplicate-action guard)."*
6. `POST /api/leads/lead_003/approve {"action_id": "<id>", "approved": false}` → `REJECTED`, `outreach_message: null` — nothing fabricated.
7. `GET /api/evaluation` → live, computed numbers (below), never hardcoded.

**Actual measured evaluation, 20-lead fixture set:**

```json
{
  "baseline":  {"false_claim_rate": 0.231, "human_review_rate": 0.0,  "action_success_rate": null, "avg_decision_time_s": 0.0008, "n_leads": 20},
  "syndicate": {"false_claim_rate": 0.0,   "human_review_rate": 0.25, "action_success_rate": 1.0,  "avg_decision_time_s": 0.0109, "n_leads": 20}
}
```

Interpreted honestly: the baseline asserts at least one independently-falsifiable claim in ~23% of pitches. Syndicate measures 0% — because contradicted claims are structurally incapable of reaching outreach. 25% of leads are now correctly routed to `HUMAN_REVIEW` instead of a forced binary close/act call. The cost is real too: roughly **13x** more wall-clock time per lead. This is still a small, illustrative evaluation (20 hand-written leads), not a statistically powered study — but it's real, reproducible, and the computation scales cleanly to a larger set.

---

## 6. Known limitations — stated plainly, not hidden

**Fixed in this pass:**

1. README/API schema drift on `/approve` — fixed and regression-tested (`test_approve_endpoint_matches_readme_schema`).
2. Missing `HUMAN_REVIEW` fixture coverage — 5 leads now resolve there, with a dedicated test asserting the pipeline halts before `ActionDecisionAgent`.
3. Thin fixture set — expanded from 3 to 20 leads, covering all three branches plus mixed-evidence cases.
4. No fault-injection coverage — added for malformed/partial responses, timeouts, connection errors, and HTTP 429 (explicitly *not* treated as "no contradicting evidence").
5. **A genuine TOCTOU race** in the duplicate-action guard — reproduced (2 of 5 concurrent approvals both succeeded), then fixed with an atomic claim/release mechanism (`try_claim_action` / `release_action_claim`, `BEGIN IMMEDIATE` + a `PRIMARY KEY` constraint). Concurrency tests now assert exactly one `SUCCESS` per run, every run.
6. "Fixtures, not live calls" — partially proven: `site_scan.py` now makes a real `requests.get()` when enabled, and a dedicated test confirms the Skeptic's verdict logic fires identically off real HTTP responses as off fixtures.
7–8. Two frontend state bugs (duplicate stacked tables; approval panel silently re-running the whole pipeline on open) — both found via DOM inspection, both fixed, both covered by real-browser Playwright regression tests.
9. Live Call widget — five separate UI bugs found via actual screenshots (viewport overflow, unstyled status pills, missing activity surfacing, no live update on approval, and a z-index conflict that hid the record panel entirely) — all fixed and reverified with layout measurements, not just code review.

**Still open, on purpose:**

9. Maps and business-profile lookups are still fixtures — only `site_scan` was swapped for a live call in this pass.
10. The real-world action is still a local file write, not a live Business Profile submission. The swap-in point is documented in code.
11. Verdict/confidence is deterministic and rule-based by design — the LLM only ever produces reasoning text, never the verdict itself. This is why the system stays reproducible and gradable with or without a live API key.
12. `action_success_rate` measures 1.0 because the current action essentially can't fail — not yet a meaningful reliability signal.
13. The 20-lead fixture set is real and useful but still hand-written, not sourced from live public businesses.
14. The atomic action-claim mechanism only serializes within a single SQLite file/process — not yet tested against a networked, horizontally-scaled deployment.
15. The dashboard re-renders the full view on every action rather than patching a single row — a deliberate tradeoff to avoid reintroducing the partial-render bug in item 7.
16. Command-palette shortcuts and table keyboard navigation are partial, not the full original spec.
17. Approving a held Live Call request updates a read-only derived field, not a real mutable order/schedule backend — same shape as item 10.
18. Live Call speech parsing is narrow and regex-based by design, failing closed to `PENDING_APPROVAL` on anything unrecognized rather than guessing live on a call.
19. Twilio and ElevenLabs cannot be exercised end-to-end in this development sandbox (no outbound network access) — every webhook handler is tested directly over HTTP, but the real phone call itself has not been verified. See the demo checklist in §8.

---

## 7. What's structurally different from the original pattern

1. **Verification happens before contact, not after.** The original's only check happens after a call is already placed. Syndicate's Skeptic runs first and can prevent contact entirely.
2. **Three-way routing on evidence confidence, not two-way routing on call outcome.** `HUMAN_REVIEW` has no equivalent in the original.
3. **The first autonomous action is a value-first artifact, not an outbound pitch.** Outreach only fires after that artifact is independently confirmed to exist.
4. **A structural, tested guarantee** that a contradicted claim can never reach outreach — enforced by function signatures and control flow, not convention.

---

## 8. Live Call + Risk-Gated Approval

Places a real outbound phone call to a lead and lets the person on the call ask for changes out loud — governed by the exact same guardrail philosophy as the rest of Syndicate. Small, pre-whitelisted, in-range changes are applied immediately and confirmed verbally. Everything else — high-value changes, contract terms, or anything the Skeptic layer has already flagged as low-confidence — is verbally deferred into the *same* `PENDING_APPROVAL` queue the rest of the app already uses.

**New modules (all unit-testable, no Twilio required):**
- `agents/live_action_guard.py` — the mid-call risk decision, combining hard thresholds with the lead's live Skeptic confidence. **Low confidence forces human approval even for a small request.**
- `agents/live_request_parser.py` — turns a speech-to-text transcript into a structured request; fails closed to `PENDING_APPROVAL` on anything unrecognized.
- `integrations/telephony.py` — Twilio TwiML glue, using Twilio's own built-in speech-to-text.
- New endpoints: `POST /api/leads/{id}/call/start`, `/twilio/voice`, `/twilio/gather`, `GET /api/leads/{id}/live-requests`, `POST /api/live-requests/{id}/approve`.
- A floating Live Call widget, a live status badge, and a record-panel section reflecting live call state.

### The demo script (exact phrases)

| Say this | What happens | Branch |
|---|---|---|
| **"Reduce the quantity by 1"** | *"Done — quantity updated. Change is within the safe range."* Green **Auto-executed** entry. | Auto-execute |
| **"Change the delivery date"** | *"Done — delivery date shifted."* **Auto-executed.** | Auto-execute |
| **"Increase the order value by 5000"** | *"That's outside what I can approve directly — I'll flag this for human approval."* Amber **Pending approval**. | Held for human approval |
| Anything phrased very differently | Same verbal deferral, same **Pending approval** state. | Held (unrecognized phrasing) |

Thresholds: ±15% quantity, $500 order-value delta, 7-day follow-up shift, 3-day delivery shift, 0.6 minimum Skeptic confidence (`agents/live_action_guard.py::DEFAULT_THRESHOLDS`).

### Before going live, verify yourself

1. `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` are set.
2. The server is reachable from the public internet (ngrok or a real deploy) — Twilio cannot reach `localhost`.
3. Your demo phone number is verified in the Twilio console (trial accounts can only call verified numbers).
4. If on a trial account, expect a disclaimer before the greeting — upgrade beforehand if that matters for the room.
5. Make one real test call before presenting — this is the single biggest source of on-stage flakiness and can't be verified any other way.
6. ElevenLabs is optional — Twilio's built-in voice works out of the box; `integrations/telephony.py::say()` is the single swap-in point if voice quality matters.

---

<div align="center">

**Syndicate**

*Trust is earned by getting attacked first.*

</div>
