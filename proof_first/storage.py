"""
Persistence layer. Deliberately plain SQLite (stdlib only, zero setup) so
`python -m proof_first.demo` and the dashboard both work with no external
database dependency during a hackathon weekend.

Persists exactly the state the spec's Phase 10 asked for: claims, evidence,
verdicts, confidence, proposed actions, approvals, execution status,
verification result, rollback result, outreach result, and the activity
timeline - all queryable by lead_id so a repeat run against the same lead
does not blindly redo verification/action (see `already_actioned`).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

DB_PATH = Path(__file__).parent / "prooffirst.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS dossiers (
    lead_id TEXT PRIMARY KEY,
    lead_name TEXT,
    dossier_json TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    lead_id TEXT,
    action_json TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS activity (
    id TEXT PRIMARY KEY,
    lead_id TEXT,
    agent TEXT,
    message TEXT,
    ts REAL
);

CREATE TABLE IF NOT EXISTS outreach (
    lead_id TEXT PRIMARY KEY,
    message TEXT,
    created_at REAL
);

-- Short-lived claim used to make the duplicate-action guard atomic under
-- concurrency (see try_claim_action / release_action_claim below). The
-- PRIMARY KEY constraint is what actually prevents two concurrent
-- executions of the same (lead_id, action_type) from both winning a
-- check-then-act race - `already_actioned` alone is a plain SELECT and
-- is vulnerable to a TOCTOU race between two overlapping requests.
CREATE TABLE IF NOT EXISTS action_claims (
    lead_id TEXT,
    action_type TEXT,
    action_id TEXT,
    claimed_at REAL,
    PRIMARY KEY (lead_id, action_type)
);

CREATE TABLE IF NOT EXISTS call_sessions (
    id TEXT PRIMARY KEY,
    lead_id TEXT,
    twilio_call_sid TEXT,
    session_json TEXT,
    started_at REAL
);

CREATE TABLE IF NOT EXISTS live_action_requests (
    id TEXT PRIMARY KEY,
    call_session_id TEXT,
    lead_id TEXT,
    request_json TEXT,
    created_at REAL
);

-- Per-lead outreach rate limiting (Track 1 hardening). One row per
-- outbound OutreachAgent send, keyed by lead_id + timestamp. Kept as its
-- own table (not layered onto action_claims/actions) so the duplicate-
-- action guard's semantics stay untouched: this counts *sends over a
-- rolling time window*, not "has this action ever happened" - a
-- fundamentally different question with a different query shape (COUNT
-- over a time-bounded WHERE clause vs. a permanent PRIMARY KEY claim).
CREATE TABLE IF NOT EXISTS outreach_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_sends_lead_ts ON outreach_sends(lead_id, ts);

-- Track 1 learning loop: patterns the ReflectionAgent has generalised from
-- past SkepticAgent misses (verdict disagreed with the eventual outcome).
-- `trigger_signal` is a small deterministic match expression evaluated by
-- agents/pattern_matching.py against a claim's own signals - never
-- free-text matched by an LLM, so applying a pattern stays auditable and
-- reproducible. A pattern only becomes `active` once corroborated by more
-- than one source lead (see promote_pattern_if_corroborated) - this is the
-- guardrail against generalising from a single noisy example.
CREATE TABLE IF NOT EXISTS learned_patterns (
    pattern_id TEXT PRIMARY KEY,
    claim_type TEXT NOT NULL,           -- e.g. "booking_claim", "broken_link_claim"
    trigger_signal TEXT NOT NULL,       -- json: {"signal_key": ..., "signal_value": ...}
    confidence_delta REAL NOT NULL,     -- bounded, applied on top of the deterministic base score
    rationale TEXT,                     -- human-readable, shown on the dashboard
    source_lead_ids TEXT,               -- json list, provenance for auditability
    times_applied INTEGER DEFAULT 0,
    times_correct INTEGER DEFAULT 0,
    created_at REAL,
    active INTEGER DEFAULT 0            -- 0 = candidate (not yet corroborated), 1 = active/applied
);

-- One row per reflection the ReflectionAgent runs, whether or not it
-- produced a promoted pattern - this is the visible "self-reflection"
-- trace the dashboard/demo shows, not just the end result.
CREATE TABLE IF NOT EXISTS reflections (
    id TEXT PRIMARY KEY,
    lead_id TEXT,
    claim_id TEXT,
    predicted_verdict TEXT,
    actual_outcome TEXT,
    was_miss INTEGER,
    pattern_id TEXT,                    -- null if no pattern was proposed/promoted
    rationale TEXT,
    created_at REAL
);
"""


def _conn() -> sqlite3.Connection:
    # Each call opens (and the caller closes) its own connection - no
    # connection is ever shared across requests/threads, so there is no
    # risk of cross-request state bleeding through a cached connection
    # object. `isolation_level=None` + explicit BEGIN IMMEDIATE below is
    # used only in try_claim_action, where a real atomic check-and-insert
    # is required; every other function here keeps sqlite3's default
    # implicit-transaction behavior.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False) -> None:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = _conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def save_dossier(dossier) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO dossiers (lead_id, lead_name, dossier_json, created_at) VALUES (?, ?, ?, ?)",
        (dossier.lead_id, dossier.lead_name, json.dumps(dossier.to_dict()), dossier.created_at),
    )
    conn.commit()
    conn.close()


def get_dossier(lead_id: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    row = conn.execute("SELECT dossier_json FROM dossiers WHERE lead_id=?", (lead_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def list_dossiers() -> List[Dict[str, Any]]:
    conn = _conn()
    rows = conn.execute("SELECT dossier_json FROM dossiers ORDER BY created_at DESC").fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def save_action(action) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO actions (id, lead_id, action_json, created_at) VALUES (?, ?, ?, ?)",
        (action.id, action.lead_id, json.dumps(action.to_dict()), action.created_at),
    )
    conn.commit()
    conn.close()


def list_actions(lead_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn()
    if lead_id:
        rows = conn.execute("SELECT action_json FROM actions WHERE lead_id=? ORDER BY created_at DESC", (lead_id,)).fetchall()
    else:
        rows = conn.execute("SELECT action_json FROM actions ORDER BY created_at DESC").fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def already_actioned(lead_id: str, action_type: str) -> bool:
    """Duplicate-action guard - equivalent role to SalesShortcut's
    prevent_duplicate_call_callback, but generalised to any whitelisted
    action type. This is the PERMANENT guard once an action has actually
    succeeded; see try_claim_action for the guard that also holds during
    the brief window while an action is still in flight."""
    for a in list_actions(lead_id):
        if a["action_type"] == action_type and a["status"] in ("SUCCESS", "EXECUTED"):
            return True
    return False


def try_claim_action(lead_id: str, action_type: str, action_id: str) -> bool:
    """Atomically reserve the (lead_id, action_type) slot so that two
    concurrent executions can never both proceed - unlike a plain
    already_actioned() SELECT followed later by an INSERT, this closes
    the check-then-act race window by making the check AND the claim a
    single atomic operation (the action_claims PRIMARY KEY does the real
    work; BEGIN IMMEDIATE ensures SQLite takes the write lock before the
    check, not after).

    Returns True if this caller now holds the claim, False if the slot is
    already claimed (by an in-flight execution) or already permanently
    actioned (a prior success). Callers MUST call release_action_claim()
    once the action reaches a terminal state, whether it succeeded or not.
    """
    if already_actioned(lead_id, action_type):
        return False

    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO action_claims (lead_id, action_type, action_id, claimed_at) VALUES (?, ?, ?, ?)",
                (lead_id, action_type, action_id, time.time()),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False
        # Re-check already_actioned INSIDE the transaction we now hold the
        # write lock for, so a success recorded by a racing thread between
        # our first check and acquiring the lock is not missed.
        if already_actioned(lead_id, action_type):
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def release_action_claim(lead_id: str, action_type: str) -> None:
    conn = _conn()
    conn.execute("DELETE FROM action_claims WHERE lead_id=? AND action_type=?", (lead_id, action_type))
    conn.commit()
    conn.close()


def count_recent_outreach_sends(lead_id: str, window_seconds: float) -> int:
    """How many outreach sends have been recorded for this lead within the
    last `window_seconds`. Plain read - see try_claim_outreach_send for the
    atomic check-and-record version actually used as the rate-limit gate."""
    conn = _conn()
    cutoff = time.time() - window_seconds
    row = conn.execute(
        "SELECT COUNT(*) FROM outreach_sends WHERE lead_id=? AND ts > ?",
        (lead_id, cutoff),
    ).fetchone()
    conn.close()
    return row[0]


def try_claim_outreach_send(lead_id: str, limit: int, window_seconds: float = 3600.0) -> tuple[bool, int]:
    """Atomically check-and-record a per-lead outreach send against the
    rolling-window rate limit, mirroring try_claim_action's BEGIN IMMEDIATE
    pattern so two concurrent sends for the same lead can never both slip
    past the limit (the same TOCTOU race a plain count-then-insert would
    have - see tests/test_concurrency.py for the equivalent duplicate-
    action race this pattern already defends against).

    Returns (allowed, count):
      - If allowed is True, the send has already been recorded and `count`
        is the number of sends for this lead in the window INCLUDING this
        one.
      - If allowed is False, nothing was recorded and `count` is the
        number of sends already present in the window (i.e. == limit).
    """
    cutoff = time.time() - window_seconds
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM outreach_sends WHERE lead_id=? AND ts > ?",
                (lead_id, cutoff),
            ).fetchone()[0]
            if count >= limit:
                conn.execute("ROLLBACK")
                return False, count
            conn.execute(
                "INSERT INTO outreach_sends (lead_id, ts) VALUES (?, ?)",
                (lead_id, time.time()),
            )
            conn.execute("COMMIT")
            return True, count + 1
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def log_activity(event) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO activity (id, lead_id, agent, message, ts) VALUES (?, ?, ?, ?, ?)",
        (event.id, event.lead_id, event.agent, event.message, event.ts),
    )
    conn.commit()
    conn.close()


def list_activity(lead_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn()
    if lead_id:
        rows = conn.execute("SELECT id, lead_id, agent, message, ts FROM activity WHERE lead_id=? ORDER BY ts ASC", (lead_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id, lead_id, agent, message, ts FROM activity ORDER BY ts ASC").fetchall()
    conn.close()
    return [{"id": r[0], "lead_id": r[1], "agent": r[2], "message": r[3], "ts": r[4]} for r in rows]


def save_call_session(session) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO call_sessions (id, lead_id, twilio_call_sid, session_json, started_at) VALUES (?, ?, ?, ?, ?)",
        (session.id, session.lead_id, session.twilio_call_sid, json.dumps(session.to_dict()), session.started_at),
    )
    conn.commit()
    conn.close()


def get_call_session(session_id: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    row = conn.execute("SELECT session_json FROM call_sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def get_call_session_by_twilio_sid(call_sid: str) -> Optional[Dict[str, Any]]:
    conn = _conn()
    row = conn.execute("SELECT session_json FROM call_sessions WHERE twilio_call_sid=?", (call_sid,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def save_live_action_request(req) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO live_action_requests (id, call_session_id, lead_id, request_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (req.id, req.call_session_id, req.lead_id, json.dumps(req.to_dict()), req.created_at),
    )
    conn.commit()
    conn.close()


def list_live_action_requests(lead_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn()
    if lead_id:
        rows = conn.execute(
            "SELECT request_json FROM live_action_requests WHERE lead_id=? ORDER BY created_at DESC", (lead_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT request_json FROM live_action_requests ORDER BY created_at DESC").fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def log_activity_compat(lead_id: str, agent: str, message: str) -> None:
    """Convenience wrapper around log_activity for call sites that only
    have plain strings on hand (e.g. the Twilio webhook handlers), so
    they don't need to import ActivityEvent just to log one line."""
    from proof_first.models import ActivityEvent
    log_activity(ActivityEvent(lead_id=lead_id, agent=agent, message=message))


def save_outreach(lead_id: str, message: str) -> None:
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO outreach (lead_id, message, created_at) VALUES (?, ?, ?)",
        (lead_id, message, time.time()),
    )
    conn.commit()
    conn.close()


def get_outreach(lead_id: str) -> Optional[str]:
    conn = _conn()
    row = conn.execute("SELECT message FROM outreach WHERE lead_id=?", (lead_id,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Track 1 learning loop: learned_patterns + reflections
# ---------------------------------------------------------------------------

_PATTERN_COLUMNS = (
    "pattern_id", "claim_type", "trigger_signal", "confidence_delta", "rationale",
    "source_lead_ids", "times_applied", "times_correct", "created_at", "active",
)


def _pattern_row_to_dict(row) -> Dict[str, Any]:
    d = dict(zip(_PATTERN_COLUMNS, row))
    d["trigger_signal"] = json.loads(d["trigger_signal"])
    d["source_lead_ids"] = json.loads(d["source_lead_ids"] or "[]")
    d["active"] = bool(d["active"])
    return d


def upsert_learned_pattern(pattern: Dict[str, Any]) -> None:
    """Insert a new candidate pattern, or - if a pattern with the same
    (claim_type, trigger_signal) already exists - merge this lead into its
    provenance and bump it toward promotion instead of creating a duplicate.
    This is what lets a second, independent lead corroborate a first lead's
    proposed pattern rather than each miss spawning its own row."""
    conn = _conn()
    existing = conn.execute(
        "SELECT pattern_id, source_lead_ids FROM learned_patterns WHERE claim_type=? AND trigger_signal=?",
        (pattern["claim_type"], json.dumps(pattern["trigger_signal"], sort_keys=True)),
    ).fetchone()

    if existing:
        pattern_id, existing_sources_json = existing
        sources = set(json.loads(existing_sources_json or "[]"))
        sources.update(pattern["source_lead_ids"])
        active = 1 if len(sources) >= 2 else 0  # corroboration guardrail: 2+ independent leads
        conn.execute(
            "UPDATE learned_patterns SET source_lead_ids=?, active=? WHERE pattern_id=?",
            (json.dumps(sorted(sources)), active, pattern_id),
        )
        conn.commit()
        conn.close()
        return pattern_id, active == 1

    pattern_id = pattern.get("pattern_id") or ("pat_" + json.dumps(pattern["trigger_signal"], sort_keys=True).__hash__().__str__()[-10:])
    conn.execute(
        "INSERT INTO learned_patterns (pattern_id, claim_type, trigger_signal, confidence_delta, "
        "rationale, source_lead_ids, times_applied, times_correct, created_at, active) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, 0)",
        (
            pattern_id, pattern["claim_type"], json.dumps(pattern["trigger_signal"], sort_keys=True),
            pattern["confidence_delta"], pattern.get("rationale", ""),
            json.dumps(pattern["source_lead_ids"]), time.time(),
        ),
    )
    conn.commit()
    conn.close()
    return pattern_id, False


def get_active_patterns(claim_type: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn()
    if claim_type:
        rows = conn.execute(
            "SELECT * FROM learned_patterns WHERE active=1 AND claim_type=?", (claim_type,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM learned_patterns WHERE active=1").fetchall()
    conn.close()
    return [_pattern_row_to_dict(r) for r in rows]


def list_all_patterns() -> List[Dict[str, Any]]:
    conn = _conn()
    rows = conn.execute("SELECT * FROM learned_patterns ORDER BY created_at DESC").fetchall()
    conn.close()
    return [_pattern_row_to_dict(r) for r in rows]


def record_pattern_outcome(pattern_id: str, was_correct: bool) -> None:
    conn = _conn()
    conn.execute(
        "UPDATE learned_patterns SET times_applied = times_applied + 1, "
        "times_correct = times_correct + ? WHERE pattern_id=?",
        (1 if was_correct else 0, pattern_id),
    )
    conn.commit()
    conn.close()


def reset_learned_patterns() -> None:
    """Used by the multi-pass evaluation harness to start pass 1 from a
    clean slate (no accumulated learning) so later passes can be compared
    against it honestly."""
    conn = _conn()
    conn.execute("DELETE FROM learned_patterns")
    conn.execute("DELETE FROM reflections")
    conn.commit()
    conn.close()


def log_reflection(reflection: Dict[str, Any]) -> None:
    conn = _conn()
    conn.execute(
        "INSERT INTO reflections (id, lead_id, claim_id, predicted_verdict, actual_outcome, "
        "was_miss, pattern_id, rationale, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            reflection["id"], reflection["lead_id"], reflection["claim_id"],
            reflection["predicted_verdict"], reflection["actual_outcome"],
            1 if reflection["was_miss"] else 0, reflection.get("pattern_id"),
            reflection.get("rationale", ""), time.time(),
        ),
    )
    conn.commit()
    conn.close()


def list_reflections(lead_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = _conn()
    if lead_id:
        rows = conn.execute(
            "SELECT id, lead_id, claim_id, predicted_verdict, actual_outcome, was_miss, pattern_id, rationale, created_at "
            "FROM reflections WHERE lead_id=? ORDER BY created_at ASC", (lead_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, lead_id, claim_id, predicted_verdict, actual_outcome, was_miss, pattern_id, rationale, created_at "
            "FROM reflections ORDER BY created_at ASC"
        ).fetchall()
    conn.close()
    cols = ("id", "lead_id", "claim_id", "predicted_verdict", "actual_outcome", "was_miss", "pattern_id", "rationale", "created_at")
    return [dict(zip(cols, r)) for r in rows]
