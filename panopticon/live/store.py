"""Point-in-time evidence store (SQLite + content-addressed raw files).

Invariants (each has a test in tests/test_live.py):
  * raw documents are immutable; re-ingesting the same accession is a no-op
  * every document keeps accession, form, filing date, acceptance time, period, URL, sha256
  * an amendment is a new document linked to the original; the original is never touched
  * filing/acceptance time (when the market could know) is stored separately from the
    economic period (what the fact is about)
  * a fact reported again in a later filing is an *observation* of the same fact, not a new fact;
    a different value for the same fact is a *restatement* that supersedes without deleting
  * extraction only creates *candidate* claims; a human approves or rejects them;
    every state change is appended to claim_history
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,            -- accession/filename
    accession TEXT NOT NULL,
    cik TEXT NOT NULL,
    form TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    accepted_at TEXT NOT NULL,          -- ISO UTC: when the market could first see it
    period TEXT,                        -- economic period the document reports on
    items TEXT,
    url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    amends_accession TEXT,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_acc ON documents(accession);

CREATE TABLE IF NOT EXISTS document_conflicts (   -- same doc_id fetched again with different bytes
    doc_id TEXT, sha256 TEXT, fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS fact_observations (
    obs_id TEXT PRIMARY KEY,            -- hash(fact_key, accession)
    fact_key TEXT NOT NULL,             -- concept|unit|start|end
    concept TEXT NOT NULL, unit TEXT NOT NULL,
    start TEXT, "end" TEXT NOT NULL,
    val REAL NOT NULL,
    accession TEXT NOT NULL, form TEXT, fp TEXT, fy INTEGER,
    filed TEXT NOT NULL, accepted_at TEXT NOT NULL,
    kind TEXT NOT NULL                  -- original | repeat | restatement
);
CREATE INDEX IF NOT EXISTS ix_fact_key ON fact_observations(fact_key, accepted_at);

CREATE TABLE IF NOT EXISTS insider_txns (
    txn_id TEXT PRIMARY KEY,            -- accession#n
    accession TEXT NOT NULL, accepted_at TEXT NOT NULL,
    owner_cik TEXT, owner_name TEXT, officer_title TEXT,
    is_officer INTEGER, is_director INTEGER, is_ten_pct INTEGER,
    derivative INTEGER NOT NULL, security TEXT, txn_date TEXT, code TEXT, kind TEXT NOT NULL,
    shares REAL, price REAL, acq_disp TEXT, owned_after REAL, plan_10b5_1 INTEGER
);

CREATE TABLE IF NOT EXISTS candidate_claims (
    claim_id TEXT PRIMARY KEY,          -- deterministic: rerunning extraction cannot duplicate
    family TEXT NOT NULL, subject TEXT NOT NULL, metric TEXT NOT NULL,
    statement TEXT NOT NULL,
    change REAL NOT NULL,
    event_time TEXT NOT NULL,           -- economic time (period end / transaction date)
    available_at TEXT NOT NULL,         -- acceptance time of the source filing
    evidence TEXT NOT NULL,             -- JSON list of {doc_id, accession, url, passage, passage_sha256, ...}
    supersedes TEXT,                    -- claim_id this one updates (restatement / newer period)
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    created_at TEXT NOT NULL,
    reviewed_by TEXT, reviewed_at TEXT, review_note TEXT
);

CREATE TABLE IF NOT EXISTS claim_history (
    claim_id TEXT NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL, actor TEXT, note TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def short_hash(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


class Store:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "panopticon.db")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ raw documents
    def raw_path(self, digest: str) -> Path:
        return self.root / "raw" / digest[:2] / digest

    def read_raw(self, digest: str) -> bytes:
        return self.raw_path(digest).read_bytes()

    def has_document(self, doc_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() is not None

    def put_document(self, meta: dict, content: bytes) -> tuple[str, bool]:
        """Store raw bytes + metadata. Returns (sha256, inserted)."""
        digest = sha256(content)
        p = self.raw_path(digest)
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        row = self.db.execute("SELECT sha256 FROM documents WHERE doc_id=?", (meta["doc_id"],)).fetchone()
        if row:
            if row["sha256"] != digest:
                self.db.execute("INSERT INTO document_conflicts VALUES (?,?,?)", (meta["doc_id"], digest, now()))
                self.db.commit()
            return row["sha256"], False
        self.db.execute(
            "INSERT INTO documents (doc_id, accession, cik, form, filing_date, accepted_at, period, items, url, "
            "sha256, size, amends_accession, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (meta["doc_id"], meta["accession"], meta["cik"], meta["form"], meta["filing_date"], meta["accepted_at"],
             meta.get("period"), meta.get("items"), meta["url"], digest, len(content),
             meta.get("amends_accession"), now()))
        self.db.commit()
        return digest, True

    def documents(self, form: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM documents", ()
        if form:
            q, args = q + " WHERE form=?", (form,)
        return self.db.execute(q + " ORDER BY accepted_at", args).fetchall()

    def accepted_at(self, accession: str) -> str | None:
        r = self.db.execute("SELECT accepted_at FROM documents WHERE accession=? LIMIT 1", (accession,)).fetchone()
        return r["accepted_at"] if r else None

    # ------------------------------------------------------------ facts
    def put_fact(self, concept: str, unit: str, start: str | None, end: str, val: float, accession: str,
                 form: str, fp: str, fy: int, filed: str, accepted_at: str) -> str:
        """Record one observation; classify as original / repeat / restatement. Idempotent."""
        fact_key = f"{concept}|{unit}|{start or ''}|{end}"
        obs_id = short_hash(fact_key, accession)
        if self.db.execute("SELECT 1 FROM fact_observations WHERE obs_id=?", (obs_id,)).fetchone():
            return "exists"
        prior = self.db.execute(
            "SELECT val FROM fact_observations WHERE fact_key=? AND accepted_at<? ORDER BY accepted_at DESC LIMIT 1",
            (fact_key, accepted_at)).fetchone()
        kind = "original" if prior is None else ("repeat" if prior["val"] == val else "restatement")
        self.db.execute("INSERT INTO fact_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (obs_id, fact_key, concept, unit, start, end, val, accession, form, fp, fy, filed,
                         accepted_at, kind))
        return kind

    def fact_series(self, concept: str, as_of: str | None = None, duration: str | None = None) -> list[dict]:
        """Latest known value per (start, end) as of a timestamp. duration: 'instant' | 'quarter' | 'ytd' | None."""
        q = "SELECT * FROM fact_observations WHERE concept=?"
        args: list = [concept]
        if as_of:
            q += " AND accepted_at<=?"
            args.append(as_of)
        latest: dict[str, sqlite3.Row] = {}
        for r in self.db.execute(q + " ORDER BY accepted_at", args):
            # Keep the latest *value*, but the FIRST time that value became public: a repeat in a
            # later filing must not move a fact's availability forward (or backward).
            prev = latest.get(r["fact_key"])
            if prev is None or prev["val"] != r["val"]:
                latest[r["fact_key"]] = r
        rows = [dict(r) for r in latest.values()]
        for r in rows:
            r["days"] = None if not r["start"] else (
                datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).days
        if duration == "instant":
            rows = [r for r in rows if r["start"] is None]
        elif duration == "quarter":
            rows = [r for r in rows if r["days"] is not None and 80 <= r["days"] <= 100]
        elif duration == "ytd":
            rows = [r for r in rows if r["days"] is not None]
        return sorted(rows, key=lambda r: (r["end"], r["days"] or 0))

    # ------------------------------------------------------------ insiders
    def put_txn(self, row: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO insider_txns VALUES (:txn_id,:accession,:accepted_at,:owner_cik,:owner_name,"
            ":officer_title,:is_officer,:is_director,:is_ten_pct,:derivative,:security,:txn_date,:code,:kind,"
            ":shares,:price,:acq_disp,:owned_after,:plan_10b5_1)", row)
        return cur.rowcount == 1

    def txns(self, accession: str | None = None) -> list[sqlite3.Row]:
        if accession:
            return self.db.execute("SELECT * FROM insider_txns WHERE accession=? ORDER BY txn_id",
                                   (accession,)).fetchall()
        return self.db.execute("SELECT * FROM insider_txns ORDER BY accepted_at").fetchall()

    # ------------------------------------------------------------ claims
    def put_candidate(self, c: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO candidate_claims (claim_id, family, subject, metric, statement, change, "
            "event_time, available_at, evidence, supersedes, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["claim_id"], c["family"], c["subject"], c["metric"], c["statement"], c["change"], c["event_time"],
             c["available_at"], json.dumps(c["evidence"]), c.get("supersedes"), "pending", now()))
        if cur.rowcount == 1:
            self.db.execute("INSERT INTO claim_history VALUES (?,?,?,?,?)",
                            (c["claim_id"], now(), "proposed", "extractor", None))
            return True
        return False

    def claims(self, status: str | None = None, as_of: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM candidate_claims WHERE 1=1", []
        if status:
            q += " AND status=?"
            args.append(status)
        if as_of:
            q += " AND available_at<=?"
            args.append(as_of)
        out = []
        for r in self.db.execute(q + " ORDER BY available_at, claim_id", args):
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"])
            out.append(d)
        return out

    def claim(self, claim_id: str) -> dict | None:
        rows = [c for c in self.claims() if c["claim_id"].startswith(claim_id)]
        return rows[0] if len(rows) == 1 else None

    def review(self, claim_id: str, decision: str, reviewer: str, note: str | None = None) -> None:
        if decision not in ("approved", "rejected", "pending"):
            raise ValueError(decision)
        if not reviewer:
            raise ValueError("a reviewer name is required")
        self.db.execute("UPDATE candidate_claims SET status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                        "WHERE claim_id=?", (decision, reviewer, now(), note, claim_id))
        self.db.execute("INSERT INTO claim_history VALUES (?,?,?,?,?)", (claim_id, now(), decision, reviewer, note))
        self.db.commit()

    def history(self, claim_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM claim_history WHERE claim_id=? ORDER BY rowid", (claim_id,)).fetchall()

    def active_claims(self, as_of: str) -> list[dict]:
        """Approved claims visible at ``as_of`` that no *visible* approved claim supersedes."""
        approved = self.claims(status="approved", as_of=as_of)
        superseded = {c["supersedes"] for c in approved if c["supersedes"]}
        return [c for c in approved if c["claim_id"] not in superseded]

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def stats(self) -> dict:
        one = lambda q: self.db.execute(q).fetchone()[0]  # noqa: E731
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "documents_by_form": {r[0]: r[1] for r in self.db.execute(
                "SELECT form, COUNT(*) FROM documents GROUP BY form ORDER BY 2 DESC")},
            "fact_observations": {r[0]: r[1] for r in self.db.execute(
                "SELECT kind, COUNT(*) FROM fact_observations GROUP BY kind")},
            "insider_transactions": {r[0]: r[1] for r in self.db.execute(
                "SELECT kind, COUNT(*) FROM insider_txns GROUP BY kind ORDER BY 2 DESC")},
            "claims": {r[0]: r[1] for r in self.db.execute(
                "SELECT status, COUNT(*) FROM candidate_claims GROUP BY status")},
            "document_conflicts": one("SELECT COUNT(*) FROM document_conflicts"),
        }

    def iter_rows(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return self.db.execute(sql, tuple(args)).fetchall()
