"""Immutable SEC evidence references for relationship edges.

A relationship may only be verified on the strength of an exact passage from an ingested SEC
filing. A reference pins:

  accession, form, acceptance time, URL        which filing
  doc_sha256                                    the exact raw bytes of the document (EDGAR archives are immutable)
  text_offset + passage + passage_sha256        the exact passage in the normalized text of those bytes
  extractor                                     how text was derived from the bytes

Anyone holding the same filing can re-derive the text and confirm the passage is at that offset with
that hash. Free-text citations ("10-K p.24") are not evidence and cannot verify an edge.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .edgar import html_to_text
from .store import Store, sha256

EXTRACTOR = "html_to_text/v1+collapse_whitespace"
TEXT_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A")
_cache: dict[str, str] = {}


def normalized_text(store: Store, doc_sha: str) -> str:
    if doc_sha not in _cache:
        _cache[doc_sha] = re.sub(r"\s+", " ", html_to_text(store.read_raw(doc_sha))).strip()
    return _cache[doc_sha]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _doc(store: Store, accession: str):
    rows = store.iter_rows("SELECT * FROM documents WHERE accession=? AND form IN (%s) ORDER BY doc_id LIMIT 1"
                           % ",".join("?" * len(TEXT_FORMS)), (accession, *TEXT_FORMS))
    return rows[0] if rows else None


def search(store: Store, pattern: str, forms: tuple[str, ...] = ("10-K", "10-K/A"), width: int = 220,
           limit: int = 20) -> list[dict]:
    """Find candidate passages (case-insensitive regex) in ingested filings, newest first."""
    out = []
    docs = [d for d in store.documents() if d["form"] in forms]
    for d in sorted(docs, key=lambda d: d["accepted_at"], reverse=True):
        t = normalized_text(store, d["sha256"])
        for m in re.finditer(pattern, t, flags=re.I):
            a = max(0, t.rfind(". ", 0, m.start()) + 2)
            b = t.find(". ", m.end())
            b = len(t) if b < 0 else b + 1
            sentence = t[a:b] if b - a <= 900 else t[max(0, m.start() - width // 2): m.end() + width // 2]
            out.append({"accession": d["accession"], "form": d["form"], "period": d["period"],
                        "offset": t.find(sentence), "sentence": sentence})
            if len(out) >= limit:
                return out
    return out


def make_ref(store: Store, accession: str, quote: str, occurrence: int | None = None) -> dict:
    """Pin an exact quote from an ingested filing. The quote must occur exactly once unless an occurrence
    index is given."""
    d = _doc(store, accession)
    if d is None:
        raise KeyError(f"accession {accession} is not ingested (or has no text document)")
    q = _norm(quote)
    if len(q) < 30:
        raise ValueError("quote too short to be unambiguous evidence (min 30 characters)")
    t = normalized_text(store, d["sha256"])
    hits = [m.start() for m in re.finditer(re.escape(q), t)]
    if not hits:
        raise ValueError("quote not found verbatim in the filing text")
    if len(hits) > 1 and occurrence is None:
        raise ValueError(f"quote occurs {len(hits)} times; pass --occurrence 0..{len(hits) - 1}")
    off = hits[occurrence or 0]
    return {
        "type": "sec_passage",
        "accession": d["accession"], "form": d["form"], "period": d["period"],
        "accepted_at": d["accepted_at"], "url": d["url"], "doc_id": d["doc_id"],
        "doc_sha256": d["sha256"], "extractor": EXTRACTOR,
        "text_offset": off, "passage": q, "passage_sha256": sha256(q.encode()),
        "pinned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def ref_id(ref: dict) -> str:
    return f"{ref['accession']}@{ref['text_offset']}#{ref['passage_sha256'][:12]}"


def validate_ref(store: Store, ref: dict) -> tuple[bool, str]:
    """Re-derive the passage from the stored raw bytes and confirm every pinned property."""
    if ref.get("type") != "sec_passage":
        return False, "not an SEC passage reference"
    d = _doc(store, ref["accession"])
    if d is None:
        return False, f"filing {ref['accession']} not ingested here (run live ingest)"
    if d["sha256"] != ref["doc_sha256"]:
        return False, "document bytes differ from the pinned sha256"
    if ref.get("extractor") != EXTRACTOR:
        return False, f"extractor {ref.get('extractor')} != {EXTRACTOR}"
    if sha256(ref["passage"].encode()) != ref["passage_sha256"]:
        return False, "passage text does not match its pinned hash"
    t = normalized_text(store, d["sha256"])
    if t[ref["text_offset"]: ref["text_offset"] + len(ref["passage"])] != ref["passage"]:
        return False, "passage not found at the pinned offset"
    return True, "ok"


def edge_evidence_status(store: Store, edge: dict) -> tuple[bool, list[str]]:
    """An edge is evidence-backed iff it has >= 1 SEC passage ref and every ref re-validates."""
    refs = edge.get("sec_evidence") or []
    if not refs:
        return False, ["no SEC evidence references"]
    problems = [f"{ref_id(r)}: {why}" for r in refs for ok, why in [validate_ref(store, r)] if not ok]
    return not problems, problems
