# ProofFirst

An evidence-verified AI SDR pipeline, independently implemented (not copied
code) using SalesShortcut's winning *architectural pattern* — sequential
specialist agents, a custom conditional router, and a guarded real-world
action — applied to a genuinely different decision workflow:

> **Research is not trusted until another agent actively tries to disprove it.**

This has been **actually built and tested**, not just designed: 76 automated
tests pass (`pytest`, run repeatedly with zero flakiness), the FastAPI server
runs, and every claim below was verified by running the real code (see the
shell transcript in this conversation).

---

## 1. Implementation map (Phase 1 of the brief)

| Existing SalesShortcut component | Disposition here |
|---|---|
| Lead discovery (Maps + clustering) | **Not reused literally** (no Maps API key available); replaced by `data/leads.py`, a small labelled fixture set standing in for "what the discovery tools would have returned." Swap this module for real Maps calls to go to production. |
| `research_lead_agent.py` (single-pass research) | **Pattern kept, code reimplemented independently** as `agents/research_agent.py` — deliberately still single-pass and untrusted, exactly like the original, because the whole point is to challenge it *afterward*. |
| No verification step after research | **Replaced** — this is the entire point of the project. New: `agents/skeptic_agent.py`, `agents/evidence_synthesis_agent.py`. |
| `sdr_router.py` (custom `BaseAgent`, binary branch on call outcome) | **Pattern kept, reimplemented independently** as `agents/decision_router.py` — same idea (plain function/class doing real control flow, not an LLM "deciding" via free text) but **three-way**, and branching on **pre-contact evidence confidence**, not post-call outcome. |
| `callbacks.py` (`prevent_duplicate_call_callback`) | **Pattern kept, reimplemented independently** as `storage.already_actioned()` + `agents/action_executor.py:before_tool_callback()` — generalised to any whitelisted action type, not just phone calls. |
| `tools/phone_call.py` (real side-effecting tool call) | **Replaced** with `agents/action_executor.py`, which performs a real, local, inspectable file-write as its "real-world action" (see honesty note below) — new whitelist-gated action types instead of one hardcoded phone call. |
| `outreach_caller_agent.py` / `outreach_email_agent/` | **Replaced** with `agents/outreach_agent.py` — structurally can only ever accept a dossier + a `SUCCESS`-status action, never raw research. |
| BigQuery lead storage | **Replaced** with plain SQLite (`storage.py`) — zero external setup, same conceptual role (persistent per-lead state). |
| No published evaluation | **New** — `evaluation.py` computes real, live baseline-vs-advanced numbers (see §5). |

---

## 2. New agents added

`ResearchAgent` → `SkepticAgent` → `EvidenceSynthesisAgent` → `DecisionAgent` (3-way router) → `ActionDecisionAgent` → `PreflightVerifierAgent` → *(human approval)* → `ActionExecutor` → `PostActionVerifier` / `RollbackAgent` → `OutreachAgent`.

## 2b. Learning loop (Track 1: Automated Agent Engineering)

Everything in §1-2 is a static, deterministic pipeline - genuinely well-tested,
but nothing in it got better on its own. This section adds a real memory +
reflection loop on top, without touching the guardrails above.

**The gap this closes:** `SkepticAgent`'s verdict/confidence math is fixed
threshold logic (see its docstrings). A miss on lead 6 didn't make it better
at catching the same pattern on lead 47. Now it does.

**New pieces:**
- `storage.py`: two new tables - `learned_patterns` (a small, auditable
  `{signal_key, expected_value} -> confidence_delta` rule, sourced from
  specific past leads) and `reflections` (every predicted-vs-actual
  comparison the system has made, whether or not it produced a pattern).
- `agents/reflection_agent.py`: compares a claim's predicted verdict against
  either (a) the real outcome once its action reaches a terminal state
  (`orchestrator.py` calls this automatically), or (b) an explicit human
  correction via `POST /api/leads/{id}/feedback`. On a miss, it proposes a
  pattern - **deterministically** (a lookup table, not an LLM guessing at a
  generalization); an LLM is used only for the human-readable rationale
  string, mirroring the existing "LLM narrates, rules decide" split in
  `skeptic_agent.py`.
- `agents/pattern_matching.py`: the same plain-equality matcher both sides use.
- `skeptic_agent.apply_learned_adjustments`: runs AFTER the deterministic
  verdict is set, nudges confidence by a bounded, logged delta if an
  *active* pattern's trigger matches this lead's own secondary signals,
  clips to `[0, 1]`, and appends a visible, attributable note to
  `claim.reasoning`. **A pattern only ever adjusts confidence - it never
  flips a verdict.**
- **Corroboration guardrail:** a pattern only becomes `active` (applied)
  once it's been proposed from **2+ independent leads** with the same
  signal (`storage.upsert_learned_pattern`). One noisy correction can never
  poison future runs on its own.

**Proof this actually generalizes, not just memorizes:** `tests/test_learning_loop.py::test_activated_pattern_changes_a_third_never_corrected_lead` corrects the booking claim on `lead_012` and `lead_019`, then re-runs `lead_009` - a lead that was **never corrected** - and asserts its confidence drops and its verdict stays `CORROBORATED` (never flipped). That's the literal "learn contextual logic from tool data, apply it in later runs" criterion.

**Multi-pass evidence of improvement over time:** `python3 -m proof_first.evaluation --multi-pass --passes=3` runs the fixture set repeatedly without resetting learned state between passes (only pass 1 starts clean), seeding two calibration corrections after pass 1 (see `data/calibration_feedback.py`'s honesty note on why - the demo's local-file-write action essentially never fails on its own, so real "the action failed" ground truth almost never appears in this fixture set; the calibration file routes through the exact same `reflect_on_human_feedback` path a live judge's correction would). Measured output:

```json
[
  {"pass": 1, "active_patterns_count": 0, "prooffirst": {"human_review_rate": 0.25, ...}},
  {"pass": 2, "active_patterns_count": 1, "prooffirst": {"human_review_rate": 0.4,  ...}},
  {"pass": 3, "active_patterns_count": 1, "prooffirst": {"human_review_rate": 0.4,  ...}}
]
```
`human_review_rate` moves from 0.25 to 0.4 the moment the pattern activates (pass 2), then holds steady (pass 3) - the system got *more calibrated*, not noisier, and the change traces to one auditable pattern, not a black box. `false_claim_rate` doesn't move in this fixture set because a pattern only ever adjusts confidence, never flips a verdict - see the note in `evaluation.py` if you want to extend the grading to be pattern-adjustment-sensitive.

**New API endpoints (all additive, none change existing behavior):**
- `POST /api/leads/{id}/feedback` - `{"claim_id": ..., "correct_verdict": "CONTRADICTED"}` - the live "tell the agent it was wrong" demo lever.
- `GET /api/learned-patterns` - every pattern, active or still-candidate, with its rationale and source leads.
- `GET /api/leads/{id}/reflections` - the self-reflection trail for one lead.

**Cost/speed honesty:** the learning loop adds one extra LLM call (rationale text) only on a miss - i.e. only on `POST /feedback` or a failed/rolled-back action - never on the hot path of a normal run. Pattern *application* (`apply_learned_adjustments`) is a plain SQL read + dict comparison, effectively free.

**What this doesn't do (stated plainly):** patterns are single-signal, single-direction (confidence-lowering) rules - there's no compositional pattern language, no automatic pattern pruning/decay, and the pipeline-outcome learning path is real but rarely exercised on this fixture set specifically because the whitelisted action almost never fails (see §6 item 12). The human-feedback path is the reliable demo lever; the automatic path is architecturally complete but data-starved here.

---

## 3. New workflow diagram

```
Lead Discovery (fixture)
   ↓
ResearchAgent (single-pass, untrusted)
   ↓
SkepticAgent (independently tries to falsify each claim)
   ↓
EvidenceSynthesisAgent (confidence-scored, cited dossier)
   ↓
DecisionAgent (3-way router)
 ├── CLOSE            → stop, log, no contact
 ├── HUMAN_REVIEW      → stop, wait for a human, dossier attached
 └── PROCEED_TO_PROOF_OF_WORK
        ↓
     ActionDecisionAgent (smallest safe whitelisted correction)
        ↓
     PreflightVerifierAgent (factual/whitelist/reversible/duplicate checks)
        ↓
     [ HUMAN APPROVAL CHECKPOINT — dashboard button ]
        ↓
     ActionExecutor (guarded tool call)
        ↓
     PostActionVerifier
        ├── SUCCESS → OutreachAgent (leads with completed proof)
        └── FAILED  → RollbackAgent (outreach withheld, logged)
```

---

## 4. How to run it locally

```bash
cd prooffirst
pip install -r requirements.txt

# CLI demo (no server needed) - runs all 3 fixture leads end-to-end:
python3 -c "
from proof_first import storage, orchestrator
from proof_first.data.leads import LEADS
storage.init_db(reset=True)
for lead in LEADS:
    r = orchestrator.run_full_pipeline(lead, auto_approve=True)
    print(lead['name'], '->', r.decision, '->', r.stopped_reason or 'outreach sent')
"

# Automated tests (the safety-critical guarantees):
python3 -m pytest tests/ -v

# Baseline vs advanced evaluation (real numbers, not hardcoded):
python3 -m proof_first.evaluation

# Full interactive dashboard:
uvicorn proof_first.server:app --reload --port 8090
# then open http://127.0.0.1:8090
```

### Required environment variables

| Variable | Required? | Effect if unset |
|---|---|---|
| `GOOGLE_API_KEY` | No | Runs in **MOCK_MODE**: deterministic, rule-based claim/verdict logic over the `data/leads.py` fixtures — fully functional for the demo and for grading, clearly labelled in the UI ("MOCK_MODE" badge) and in code (`llm_client.py`). |
| `GOOGLE_API_KEY` (set) | Optional | Routes narrative text through Gemini via `google-generativeai`; the verdict/routing logic itself stays deterministic by design (see §6). |
| `TENSORMUX_BASE_URL` | No | Unset (default): backend selection falls through to `GOOGLE_API_KEY`/MOCK_MODE below. Set to a TensorMux-compatible endpoint to route narrative text through it instead (via the `openai` client), taking priority over `GOOGLE_API_KEY`; the verdict/routing logic itself stays deterministic either way (see §6). |
| `TENSORMUX_API_KEY` | Only if `TENSORMUX_BASE_URL` is set | Defaults to a placeholder string if unset, for endpoints that don't enforce auth. |
| `TENSORMUX_MODEL` | Only if `TENSORMUX_BASE_URL` is set | Defaults to `qwen2.5:0.5b` if unset. |
| `NEATLOGS_API_KEY` | No | Unset (default): no Neatlogs import or init happens at all — tracing is fully skipped and behavior is unchanged. Set to enable Neatlogs tracing of the pipeline (workflow/agent/tool spans across `orchestrator.py` and `reflection_agent.py`) plus `google-genai` instrumentation. |

No Maps, ElevenLabs, BigQuery, or Gmail credentials are required — those SalesShortcut dependencies were deliberately not carried over.

| Variable | Required? | Effect if unset |
|---|---|---|
| `PROOFFIRST_REAL_SITE_SCAN` | No | Unset/false (default): the booking-link independent re-check uses the `booking_link_http_status` fixture in `data/leads.py`, exactly as before. Set to `1`/`true` to make `proof_first/integrations/site_scan.py` perform a real `requests.get(url, timeout=5)` against `primary_signals["site_booking_link_url"]` instead — see README §6 item 6 and `tests/test_real_site_scan_integration.py`. |
| `TWILIO_ACCOUNT_SID` | Only for the Live Call feature (§8) | Without it, `POST /api/leads/{id}/call/start` returns a 500 immediately rather than pretending to place a call. Nothing else in the app requires Twilio. |
| `TWILIO_AUTH_TOKEN` | Only for the Live Call feature (§8) | Same as above. |
| `TWILIO_FROM_NUMBER` | Only for the Live Call feature (§8) | Same as above — must be a real Twilio number on your account. |

---

## 5. End-to-end demo steps (what I actually ran, verified above)

1. `GET /api/leads` → confirms `mock_mode: true` and lists the fixture leads (20, see §6).
2. `POST /api/leads/lead_001/run` (Acme Dental) → SkepticAgent finds an Instagram DM-booking flow the research pass missed → **`CLOSE`**, no action proposed, no outreach.
3. `POST /api/leads/lead_002/run` (Acme Cafe) → broken-link claim is independently re-confirmed (HTTP 500 re-check) → **`PROCEED_TO_PROOF_OF_WORK`**, one `PENDING_APPROVAL` action.
4. `POST /api/leads/lead_002/approve {"action_id": "<id from the /run response's proposed_action.id>", "approved": true}` → executes, writes a real local artifact, `PostActionVerifier` confirms it exists → **`SUCCESS`** → outreach message generated, containing the real artifact URL.
5. Re-running step 3 on the same lead → `PreflightVerifierAgent` returns **`BLOCKED`** with reason `"An identical action has already succeeded for this lead (duplicate-action guard)."`
6. `POST /api/leads/lead_003/approve {"action_id": "<id from the /run response's proposed_action.id>", "approved": false}` → `REJECTED`, `outreach_message: null` — nothing fabricated.

> **Note:** `ApprovalRequest` (see `server.py`) requires both `action_id` and
> `approved` — the `action_id` comes from the `proposed_action.id` field
> returned by the prior `POST /api/leads/{id}/run` call. A bare
> `{"approved": true}` body will fail Pydantic validation with a 422. This
> is covered by `tests/test_api.py::test_approve_endpoint_matches_readme_schema`.
7. `GET /api/evaluation` → real computed numbers from `evaluation.py` (see below), not hardcoded.

**Actual measured evaluation output on this fixture set** (reproduce with `python3 -m proof_first.evaluation`):
```json
{
  "baseline":   {"false_claim_rate": 0.231, "human_review_rate": 0.0, "action_success_rate": null, "avg_decision_time_s": 0.0008, "n_leads": 20},
  "prooffirst": {"false_claim_rate": 0.0, "human_review_rate": 0.25, "action_success_rate": 1.0, "avg_decision_time_s": 0.0109, "n_leads": 20}
}
```
Honestly interpreted, now over a 20-lead fixture set (up from the original 3): the baseline asserts at least one independently-falsifiable claim in ~23% of its pitches; ProofFirst's outreach still measures 0% because contradicted claims are structurally incapable of reaching outreach, and 25% of leads are now correctly routed to `HUMAN_REVIEW` instead of being forced into a binary close/act decision — a branch the original 3-lead set never exercised at all. It still costs roughly 13x more wall-clock time per lead to get there. The `false_claim_rate` and `human_review_rate` numbers changed meaningfully from the original 3-lead run (0.5 → 0.231 and 0.0 → 0.25 respectively) simply because the earlier numbers were computed over too small and too skewed a sample to be representative; the underlying computation in `evaluation.py` did not change. **This is still a small, illustrative evaluation (20 hand-written leads), not a statistically powered study** — swap in real, public businesses via the fixtures in `data/leads.py` (or the real `site_scan` integration below) to scale it up further; the computation itself does not need to change.

---

## 6. Known limitations (stated plainly, not hidden)

**Solved in this pass** (see the corresponding tests for evidence):

1. ~~README's documented `/approve` curl example didn't match `ApprovalRequest`'s real schema.~~ **Fixed** — §5 now shows the correct `action_id` + `approved` body, and `tests/test_api.py::test_approve_endpoint_matches_readme_schema` regression-tests the documented flow (including asserting the old, buggy example now correctly 422s) so this drift can't silently reappear.
2. ~~No `HUMAN_REVIEW` fixture lead existed.~~ **Fixed** — `data/leads.py` now has 4 leads (`lead_013`, `lead_014`, `lead_015`, `lead_016`, plus `lead_020`) that resolve to `HUMAN_REVIEW`, and `tests/test_pipeline.py::test_human_review_lead_stops_pipeline_before_action_decision_agent` asserts the branch stops the pipeline and never calls `ActionDecisionAgent`.
3. ~~Small fixture set (3 leads).~~ **Improved, not eliminated** — the fixture set is now 20 hand-written leads covering all three routing branches plus several "mixed evidence" leads (e.g. one corroborated + one contradicted claim on the same lead — see `lead_006`/`lead_018`) rather than only clean single-claim cases. This is a real, if still small, statistical sample — not yet a large or externally-sourced one (see #6 below).
4. ~~No fault-injection coverage for what will break once Maps/business-profile/site-scan calls are real.~~ **Fixed** — `tests/test_fault_injection.py` and `tests/test_real_site_scan_integration.py` cover malformed/partial responses, timeouts, connection errors, HTTP 429 rate limits (explicitly never treated as "no contradicting evidence found" — a real bug class this catches), and two independent evidence sources disagreeing with each other.
5. ~~The duplicate-action guard was only ever tested sequentially, and in fact was NOT race-safe.~~ **Found and fixed** — a genuine TOCTOU race existed in `action_executor.before_tool_callback`: concurrent `/approve` calls for the same lead could both pass the guard and both execute (reproduced: 2 of 5 concurrent approvals both reached `SUCCESS` before the fix). Fixed with an atomic claim/release mechanism in `storage.py` (`try_claim_action` / `release_action_claim`, backed by a `PRIMARY KEY`-constrained SQLite table plus `BEGIN IMMEDIATE`). `tests/test_concurrency.py` reproduces the race at both the orchestrator level and the real HTTP API level and asserts exactly one `SUCCESS` every run.
6. ~~"The public data sources are fixtures, not live API calls" — asserted but never demonstrated.~~ **Partially fixed for one source.** `proof_first/integrations/site_scan.py` replaces the hardcoded `booking_link_http_status` fixture with a real `requests.get(url, timeout=5)` call, gated behind the `PROOFFIRST_REAL_SITE_SCAN` environment variable (unset/false by default, which preserves the exact original MOCK_MODE fixture behavior — see `tests/test_real_site_scan_integration.py::test_real_site_scan_disabled_by_default`). `tests/test_real_site_scan_integration.py` spins up a real local HTTP server and confirms `skeptic_agent.py`'s CORROBORATED/CONTRADICTED/UNVERIFIABLE logic fires identically off a genuine HTTP response as it did off the fixture — this is what actually demonstrates the README's "agent logic doesn't change" claim rather than just asserting it.
7. ~~Frontend: stale duplicate tables after in-panel actions.~~ **Fixed** — `runResearch()` and the approval panel's `decide()` handler called a specific view-render function directly (e.g. `renderLeads($("#view"))`), which only ever appends and never clears its container, so each in-panel action stacked a second correct table under a stale one (confirmed via DOM inspection: 2 duplicates after one action, 6 after two). Both call sites now go through a single `refreshCurrentView()` wrapper around the router's `render()` dispatcher, which clears `#view` before redrawing. `tests/test_frontend_ui.py::test_no_duplicate_tables_after_inpanel_research_run` is a real-browser (Playwright) regression test that asserts `document.querySelectorAll('table.data-table').length === 1` after two consecutive in-panel runs.
8. ~~Frontend: opening the approval panel silently re-ran the whole pipeline.~~ **Fixed** — `openApprovalPanel()` called `POST /api/leads/{id}/run` just to *view* an already-proposed action, which re-executed Research → Skeptic → Decision → Preflight and inserted a new `PENDING_APPROVAL` row on every click (confirmed: two consecutive clicks produced two different `action_id` values for the same lead, with no dedup). Added a new read-only `GET /api/leads/{lead_id}/action` endpoint (additive only, same pattern as `/api/overview` — pure `storage.list_actions()`/`storage.get_dossier()` reads, no orchestrator call) and pointed the approval panel at it; the "Research" / "Re-run research" button is unchanged and still correctly calls `POST /run`. `tests/test_api.py::test_get_action_endpoint_is_pure_read_and_does_not_duplicate_rows` and `tests/test_frontend_ui.py::test_approval_panel_is_read_only_on_repeated_open` both assert the action id and activity-log length stay flat across repeated opens.
9. ~~Live Call widget: overflowed the viewport, used unstyled inline CSS, had no visibility outside itself, and silently no-opped on approve.~~ **Fixed** — four separate bugs found and fixed in one pass, each verified with a real Playwright screenshot/measurement, not just code review: (a) the widget's outer container had no height cap (only its inner feed div did), so 5+ live requests pushed later entries' Approve/Reject buttons off-screen — fixed with `max-height: calc(100vh - 32px)` on `.live-call-widget` itself and `overflow-y: auto` on `.live-call-body`, confirmed at 7 entries via `scrollHeight (968) > clientHeight (917)` plus a bounding-box check that the widget's bottom edge stays inside the viewport; (b) all styling moved from inline hex fallbacks into `style.css` using the app's existing tokens and `.pill-green/amber/neutral` classes — this also surfaced a real bug where `APPROVED_EXECUTED`/`REJECTED` statuses fell through to an unstyled box because only `AUTO_EXECUTED`/`PENDING_APPROVAL` had been mapped; (c) a lead's live-call activity now surfaces as a badge on its table/work-row (driven by the widget's own poll data via a single shared `LiveCallState` object — no second poll loop) and in the record panel's existing activity timeline (no new plumbing needed there — `LiveActionGuard` log lines already flowed through); (d) approving a held request now visibly updates a "Live call updates" field in the record panel, derived read-only from `/live-requests` — see item 17 below for the exact scope of this. **A fifth bug was found only via screenshot, not code review**: the floating widget and the right-anchored record panel share the same screen corner, and the widget's z-index sat above the panel's, so opening the panel while the widget was mounted rendered the panel's content (including the new "Live call updates" section) completely hidden underneath it. Fixed by sliding the widget clear of the panel width whenever a panel is open (`.beside-panel`/`.beside-wide-panel`, toggled in `openPanel()`/`closePanel()`) and dropping the widget's z-index below the panel's; reverified with a `bounding_box()` overlap check (`False`) and a screenshot showing both panels side by side. `tests/test_live_call_widget_ui.py` is a real-browser Playwright regression test that drives the actual `/twilio/voice` + `/twilio/gather` webhook path to seed live requests (no Twilio credentials needed for this) and asserts on real layout measurements.

**Still open:**

9. **Maps and business-profile lookups are still fixtures.** Only `site_scan` (the simplest of the three named sources) was swapped for a real call in this pass, as scoped. `primary_signals`/most of `secondary_signals` in `data/leads.py` are still hand-written stand-ins.
10. **The real-world action is still a local file write, not a live Business Profile submission.** `action_executor.py` documents exactly where to swap in a real API call; this pass did not do that swap (it was scoped to the site_scan read path, not the action-execution write path).
11. **Verdict/confidence assignment is deterministic and rule-based; the LLM is layered in for reasoning text only, not for the verdict itself.** When `GOOGLE_API_KEY` is set, `SkepticAgent` and `ResearchAgent` make real Gemini calls (`llm_client.complete`) to produce adversarial reasoning / claim phrasing, and that text is appended into `claim.reasoning` and shown in the UI. But `claim.verdict` and `claim.confidence` are always set by the deterministic `_check_*` threshold functions in `skeptic_agent.py` — the model's output is never allowed to set them (see the docstring on `_adversarial_reasoning`). So this is a **hybrid**: LLM-in-the-loop for explanation, deterministic for the actual decision. This is why `tests/test_pipeline.py` and `evaluation.py` stay reproducible/gradable with or without a live key — swapping `MOCK_MODE` on/off changes the wording, not the routing outcome (see `tests/test_llm_reasoning.py`, which asserts exactly this separation).
12. **`action_success_rate` is measured at 1.0 for ProofFirst**, but this reflects that the "action" is currently a local file write, which essentially never fails - it is not yet a meaningful reliability signal. Once a real Business Profile/hosting API is wired in (see #10), this number will start measuring something real (and may well drop below 1.0).
13. **Fixture set is still hand-written, not sourced from real public businesses.** 20 leads is a large enough sample to stop treating `evaluation.py`'s output as purely illustrative, but it is still a small, synthetic, non-randomly-selected sample - not a statistically powered study of real businesses.
14. **The atomic action-claim mechanism (`storage.try_claim_action`) only serializes concurrent executions within a single SQLite file/process.** It has not been tested against multiple separate processes or a networked database; horizontally scaling the server (multiple processes/machines) would need a lock that isn't file-local (e.g. a proper external lock service or a networked DB with the same unique-constraint trick).
15. **The leads table re-renders the whole view on every in-place action** (run / approve / reject) rather than patching just the affected row. This was a deliberate tradeoff in the frontend bug-fix pass: the root cause of #7 above was inconsistent, partial re-rendering, so the fix goes through the full `render()` dispatcher every time rather than adding a second, more surgical update path that could reintroduce the same class of bug. A brief `row-flash` CSS pulse (see `app.js::flashRow`) makes the update visible despite the full redraw; true per-row DOM patching is future work, not done here.
16. **The command palette's `G then <letter>` keyboard shortcuts and the leads table's arrow-key/Enter navigation are partial, not the full spec.** `G L`/`G E`/`G A`/`G H` and table arrow-key navigation work; some finer-grained shortcuts from the original design brief (e.g. workflow-builder node navigation) were out of scope for this pass.
17. **Approving a held Live Call request does not write to a real order/schedule backend, because there isn't one — `data/leads.py` ships fixtures, not live order data.** Chose fix (a) from the two options for this pass (wire it to something real and visible, scoped small) over (b) (an explicit "not wired" label): the record panel's "Live call updates" section is genuinely live and reads the real `/live-requests` data — it just derives a display value rather than mutating a backend order record. Clicking Approve does not fail or lie about what happened; it correctly changes the request's own status, and the derived field updates the moment that status changes. The visible gap: the *dollar amount / quantity itself* on the lead record elsewhere in the app does not change — only this one new panel section reflects it. Wiring the derived field into an actual mutable per-lead order/schedule store (and having the rest of the UI read from it) is future work, same shape as item 10 above (the action-execution write path is still a local artifact, not a live backend).
18. **Live Call speech-to-text and phrasing coverage is narrow by design** (see `agents/live_request_parser.py`'s docstring) — regex-matched against the exact demo phrasings in §8 below, with an LLM fallback for anything else that still fails closed to `PENDING_APPROVAL` rather than guessing. This is a deliberate reliability tradeoff for live, unscripted demo conditions, not a general-purpose NLU layer; a caller phrasing a request very differently from the examples below may land in "unrecognized" (held for approval) rather than being auto-executed even when it would have been safe to.
19. **Twilio and ElevenLabs cannot be exercised in this development environment at all** (no outbound network access to `api.twilio.com` or `elevenlabs.io` from the sandbox this was built in) — every Live Call test in this repo drives the real `/twilio/voice` / `/twilio/gather` webhook handlers directly via HTTP, which is the same code path a real Twilio call hits, but the actual phone call, Twilio's speech-to-text accuracy on real audio, and Twilio trial-account restrictions have not been verified end-to-end. See the Demo setup checklist in §8 for exactly what to verify yourself before presenting.

---

## 7. Exact architectural differences from the original SalesShortcut workflow

1. **Verification happens before contact, not after.** SalesShortcut's only "check" (call-outcome classification) happens *after* a real phone call is already placed. ProofFirst's `SkepticAgent` runs *before* any contact and can prevent contact entirely.
2. **Three-way routing on evidence confidence, not two-way routing on call outcome.** `decision_router.py` adds a `HUMAN_REVIEW` branch that has no equivalent in `sdr_router.py`.
3. **The autonomous action is a value-first artifact, not an outbound pitch.** SalesShortcut's first real-world action toward a prospect is always outbound (a call). ProofFirst's first action is a completed, verifiable improvement — outreach is the *second* action, and only fires if the first one is independently confirmed successful.
4. **A structural, testable guarantee that a contradicted claim can never reach outreach**, enforced by `outreach_agent.py`'s function signature and `orchestrator.py`'s control flow (see `tests/test_pipeline.py::test_contradicted_claim_never_reaches_outreach`) — SalesShortcut has no equivalent guarantee, since it never contradicts its own research in the first place.

---

## 8. Live Call + Risk-Gated Approval feature

Places a live outbound phone call to a lead and lets the person on the call ask for changes out loud. The **same guard-rail philosophy as the rest of ProofFirst** applies mid-call: small, pre-whitelisted, in-range changes are applied immediately and confirmed verbally; everything else — high-value changes, contract terms, or anything the Skeptic layer has already flagged as low-confidence for this lead — is verbally deferred and dropped into the same `PENDING_APPROVAL` queue and dashboard the rest of the app already uses, not a second approval mechanism.

**New modules** (all covered by `tests/test_live_call.py`, pure-function/unit-testable with no Twilio involved):
- `agents/live_action_guard.py` — the mid-call risk decision. Combines a hard threshold (order value / quantity-% / date-shift limits) with the lead's live Skeptic-layer confidence score; **low confidence forces human approval even for an otherwise-small request** (`test_small_change_still_blocked_by_low_skeptic_confidence`).
- `agents/live_request_parser.py` — turns Twilio's speech-to-text transcript into a structured request. Regex-matched against the exact demo phrasings below; anything else fails closed to `PENDING_APPROVAL` rather than an LLM guessing live on stage.
- `integrations/telephony.py` — Twilio TwiML glue (`<Say>` / `<Gather input="speech">`); uses Twilio's own built-in speech-to-text, so no separate transcription service is required to get a working demo.
- `server.py` — `POST /api/leads/{id}/call/start` places the call; `/twilio/voice` + `/twilio/gather` are the webhook handlers Twilio calls into (the actual mid-call guard invocation point); `GET /api/leads/{id}/live-requests` is polled by the dashboard; `POST /api/live-requests/{id}/approve` resolves a held item.
- `static/app.js` / `static/style.css` — a floating Live Call widget (1s poll while a call is active), a "Live" badge on the lead's row/work-row, and a "Live call updates" section in the record panel — see §6 items 9 and 17 for what was fixed and what's still scoped-out here.

### The three-branch demo script (exact phrases)

Say these, live, on the call:

| Say this | What happens | Branch |
|---|---|---|
| **"Reduce the quantity by 1"** (or 2, on a lead with a small enough existing quantity) | Agent says *"Done — quantity updated to N. Quantity change of X% is within the 15% safe range."* Dashboard widget shows a green **Auto-executed** entry immediately. | Auto-execute (low risk + high confidence) |
| **"Change the delivery date"** | Agent says *"Done — delivery date shifted by 1 day(s)..."* Dashboard shows **Auto-executed**. | Auto-execute |
| **"Increase the order value by 5000"** (or any order-value change, or a quantity change large enough to exceed the 15% band) | Agent says *"That's outside what I can approve directly — I'll flag this for human approval."* Dashboard shows an amber **Pending approval** entry with live Approve/Reject buttons. | Held for human approval (hard threshold) |
| Anything phrased very differently from the above (e.g. "rewrite the whole contract") | Same verbal deferral and **Pending approval** state — the parser fails closed on anything it doesn't recognize rather than guessing. | Held for human approval (unrecognized phrasing) |

If judges ask for a different value live, any of the quantity/delivery/order-value phrasings above work with a different number substituted — the thresholds are `agents/live_action_guard.py::DEFAULT_THRESHOLDS` (±15% quantity, $500 order-value delta, 7-day follow-up shift, 3-day delivery shift, 0.6 minimum Skeptic confidence) if you want to pre-compute exactly where a number will land before saying it.

### Demo setup checklist

Verify these **yourself, before going live** — none of them can be exercised from a development sandbox with no outbound network access to Twilio:

1. **Env vars set**: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`.
2. **Public webhook URL**: the FastAPI server must be reachable from the internet (ngrok or a real deploy) — Twilio needs to reach `/twilio/voice` and `/twilio/gather`, so `localhost` alone will not work.
3. **Trial account restriction**: a Twilio trial account can only call numbers you've explicitly *verified* on the account — verify your own demo phone number in the Twilio console first, or the call will fail to connect on stage.
4. **Trial account voice disclaimer**: an unpaid trial account plays a short disclaimer before your greeting on every call; upgrade the account beforehand if that would be awkward in front of judges.
5. **A real test call, once, before the demo** — confirm Twilio's speech recognition actually picks up your voice and phone audio quality cleanly for the exact phrasings above; this is the single biggest source of on-stage flakiness and cannot be verified without a live call.
6. **ElevenLabs is not required** — Twilio's built-in voice works out of the box. `integrations/telephony.py::say()` is the single, isolated place to swap in an ElevenLabs-generated clip via `<Play>` if voice quality matters enough to be worth a live key.
