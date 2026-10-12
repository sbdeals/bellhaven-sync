"""SQLite persistence for pipeline runs, proposals and reviewer decisions.

Idempotency contract (see spec): proposals are keyed by fingerprint. A fingerprint whose
status is already decided (approved/applied/rejected/failed) is never re-opened. Pending
proposals are refreshed in place. Pending proposals that a run no longer produces are
marked `stale`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import DECIDED_STATUSES, PRIORITY_ORDER, Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    site_count        INTEGER DEFAULT 0,
    crm_count         INTEGER DEFAULT 0,
    proposals_new     INTEGER DEFAULT 0,
    proposals_refreshed INTEGER DEFAULT 0,
    proposals_previously_decided INTEGER DEFAULT 0,
    proposals_stale   INTEGER DEFAULT 0,
    notes             TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proposals (
    fingerprint        TEXT PRIMARY KEY,
    kind               TEXT NOT NULL,
    priority           TEXT NOT NULL,
    title              TEXT NOT NULL,
    summary            TEXT NOT NULL,
    subject_account_id TEXT,
    site_slug          TEXT,
    actions_json       TEXT NOT NULL,
    evidence_json      TEXT NOT NULL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    status             TEXT NOT NULL DEFAULT 'pending',
    first_seen_run     INTEGER NOT NULL,
    last_seen_run      INTEGER NOT NULL,
    decided_at         TEXT,
    decided_by         TEXT,
    reviewer_note      TEXT,
    applied_at         TEXT,
    apply_result_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class StoredProposal:
    proposal: Proposal
    status: str
    first_seen_run: int
    last_seen_run: int
    decided_at: str | None = None
    decided_by: str | None = None
    reviewer_note: str | None = None
    applied_at: str | None = None
    apply_result: Any = None

    @property
    def fingerprint(self) -> str:
        return self.proposal.fingerprint

    def to_dict(self) -> dict[str, Any]:
        d = self.proposal.to_dict()
        d.update(
            status=self.status,
            first_seen_run=self.first_seen_run,
            last_seen_run=self.last_seen_run,
            decided_at=self.decided_at,
            decided_by=self.decided_by,
            reviewer_note=self.reviewer_note,
            applied_at=self.applied_at,
            apply_result=self.apply_result,
        )
        return d


@dataclass
class UpsertSummary:
    new: int = 0
    refreshed: int = 0
    previously_decided: int = 0
    stale: int = 0


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ----------------------------------------------------------------- runs
    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, site_count: int, crm_count: int, summary: UpsertSummary, notes: str = "") -> None:
        self.conn.execute(
            """UPDATE runs SET finished_at=?, site_count=?, crm_count=?, proposals_new=?,
               proposals_refreshed=?, proposals_previously_decided=?, proposals_stale=?, notes=?
               WHERE run_id=?""",
            (_now(), site_count, crm_count, summary.new, summary.refreshed, summary.previously_decided, summary.stale, notes, run_id),
        )
        self.conn.commit()

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ proposals
    def upsert_proposals(self, proposals: Iterable[Proposal], run_id: int) -> UpsertSummary:
        """Record this run's proposals. Returns counts. Marks pending proposals that are
        missing from this run as stale."""
        summary = UpsertSummary()
        seen: set[str] = set()
        for p in proposals:
            seen.add(p.fingerprint)
            row = self.conn.execute("SELECT status FROM proposals WHERE fingerprint=?", (p.fingerprint,)).fetchone()
            if row is None:
                self.conn.execute(
                    """INSERT INTO proposals (fingerprint, kind, priority, title, summary, subject_account_id,
                       site_slug, actions_json, evidence_json, confidence, status, first_seen_run, last_seen_run)
                       VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (
                        p.fingerprint, str(p.kind), p.priority, p.title, p.summary, p.subject_account_id,
                        p.site_slug, json.dumps([a.to_dict() for a in p.actions]), json.dumps(p.evidence),
                        p.confidence, run_id, run_id,
                    ),
                )
                summary.new += 1
            elif row["status"] in DECIDED_STATUSES:
                self.conn.execute("UPDATE proposals SET last_seen_run=? WHERE fingerprint=?", (run_id, p.fingerprint))
                summary.previously_decided += 1
            else:  # pending or stale -> refresh and (re)open
                self.conn.execute(
                    """UPDATE proposals SET kind=?, priority=?, title=?, summary=?, subject_account_id=?, site_slug=?,
                       actions_json=?, evidence_json=?, confidence=?, status='pending', last_seen_run=?
                       WHERE fingerprint=?""",
                    (
                        str(p.kind), p.priority, p.title, p.summary, p.subject_account_id, p.site_slug,
                        json.dumps([a.to_dict() for a in p.actions]), json.dumps(p.evidence), p.confidence,
                        run_id, p.fingerprint,
                    ),
                )
                summary.refreshed += 1
        # anything still pending that this run did not produce is stale
        pending = self.conn.execute("SELECT fingerprint FROM proposals WHERE status='pending'").fetchall()
        for r in pending:
            if r["fingerprint"] not in seen:
                self.conn.execute("UPDATE proposals SET status='stale' WHERE fingerprint=?", (r["fingerprint"],))
                summary.stale += 1
        self.conn.commit()
        return summary

    def _row_to_stored(self, r: sqlite3.Row) -> StoredProposal:
        p = Proposal.from_dict(
            {
                "fingerprint": r["fingerprint"],
                "kind": r["kind"],
                "priority": r["priority"],
                "title": r["title"],
                "summary": r["summary"],
                "subject_account_id": r["subject_account_id"],
                "site_slug": r["site_slug"],
                "actions": json.loads(r["actions_json"]),
                "evidence": json.loads(r["evidence_json"]),
                "confidence": r["confidence"],
            }
        )
        return StoredProposal(
            proposal=p,
            status=r["status"],
            first_seen_run=r["first_seen_run"],
            last_seen_run=r["last_seen_run"],
            decided_at=r["decided_at"],
            decided_by=r["decided_by"],
            reviewer_note=r["reviewer_note"],
            applied_at=r["applied_at"],
            apply_result=json.loads(r["apply_result_json"]) if r["apply_result_json"] else None,
        )

    def get(self, fingerprint: str) -> StoredProposal | None:
        r = self.conn.execute("SELECT * FROM proposals WHERE fingerprint=?", (fingerprint,)).fetchone()
        return self._row_to_stored(r) if r else None

    def list(self, status: str | None = None, kind: str | None = None) -> list[StoredProposal]:
        sql, params = "SELECT * FROM proposals", []
        clauses = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = [self._row_to_stored(r) for r in self.conn.execute(sql, params).fetchall()]
        rows.sort(key=lambda s: (PRIORITY_ORDER.get(s.proposal.priority, 9), str(s.proposal.kind), s.proposal.title))
        return rows

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM proposals GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------- decisions
    def decide(self, fingerprint: str, status: str, *, by: str, note: str = "") -> None:
        assert status in ("approved", "rejected"), status
        self.conn.execute(
            "UPDATE proposals SET status=?, decided_at=?, decided_by=?, reviewer_note=? WHERE fingerprint=?",
            (status, _now(), by, note, fingerprint),
        )
        self.conn.commit()

    def mark_applied(self, fingerprint: str, result: Any) -> None:
        self.conn.execute(
            "UPDATE proposals SET status='applied', applied_at=?, apply_result_json=? WHERE fingerprint=?",
            (_now(), json.dumps(result, default=str), fingerprint),
        )
        self.conn.commit()

    def mark_failed(self, fingerprint: str, result: Any) -> None:
        self.conn.execute(
            "UPDATE proposals SET status='failed', applied_at=?, apply_result_json=? WHERE fingerprint=?",
            (_now(), json.dumps(result, default=str), fingerprint),
        )
        self.conn.commit()

    def reopen(self, fingerprint: str) -> None:
        """Put a rejected/failed proposal back in the queue (explicit reviewer action)."""
        self.conn.execute(
            "UPDATE proposals SET status='pending', decided_at=NULL, decided_by=NULL, reviewer_note=NULL, applied_at=NULL, apply_result_json=NULL WHERE fingerprint=?",
            (fingerprint,),
        )
        self.conn.commit()
