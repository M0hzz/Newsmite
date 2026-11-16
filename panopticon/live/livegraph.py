"""Active graph state: verified-or-draft relationships + human-approved claims, as of a time."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from ..graph import PowerGraph
from ..model import Claim, Clock, Edge, EdgeType, Entity, Evidence, NodeType, SourceTier
from .store import Store

FAMILY_TO_EVENT = {
    "INVENTORY": "CAPACITY_CHANGE", "PRICING_MARGIN": "PRICING_INPUT_COST_CHANGE",
    "CAPACITY_CAPEX": "CAPACITY_CHANGE", "GEO_REGULATORY": "REGULATORY_CHANGE",
    "INSIDER": "CAPITAL_POSITIONING_CHANGE", "CORPORATE_EVENT": "CORPORATE_EVENT",
}
FAMILY_CLOCK = {"INSIDER": Clock.MARKET, "GEO_REGULATORY": Clock.STRUCTURAL}


def load_relationships(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def save_relationships(path: str | Path, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=1) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _edge(data: dict, edge_id: str) -> dict:
    edge = next((e for e in data["edges"] if e["id"] == edge_id), None)
    if edge is None:
        raise KeyError(edge_id)
    return edge


def migrate_relationships(data: dict) -> bool:
    """Free-text citations are not evidence: move them aside and un-verify anything that relied on them."""
    changed = False
    for e in data["edges"]:
        legacy = e.pop("evidence", None)
        if legacy:
            e.setdefault("legacy_citations", []).extend(legacy)
            changed = True
        elif legacy is not None:
            changed = True
        e.setdefault("sec_evidence", [])
        e.setdefault("history", [])
        if e.get("verified") and not e["sec_evidence"]:
            e.update(verified=False, verified_by=None, verified_at=None)
            e["history"].append({"at": _now(), "action": "unverified", "by": "migration",
                                 "note": "free-text citation is not SEC evidence"})
            changed = True
    data["evidence_policy"] = ("Edges are verified only by immutable SEC passage references (accession, document "
                               "sha256, exact passage, offset, passage sha256). Free-text citations are ignored.")
    return changed


def cite_edge(path: str | Path, store: Store, edge_id: str, accession: str, quote: str, by: str,
              occurrence: int | None = None) -> dict:
    from .relevidence import make_ref, ref_id
    if not by:
        raise ValueError("a reviewer name is required")
    data = load_relationships(path)
    migrate_relationships(data)
    edge = _edge(data, edge_id)
    ref = make_ref(store, accession, quote, occurrence)
    if any(r["passage_sha256"] == ref["passage_sha256"] and r["accession"] == ref["accession"]
           for r in edge["sec_evidence"]):
        raise ValueError("this passage is already cited on this edge")
    ref["cited_by"] = by
    edge["sec_evidence"].append(ref)
    edge["history"].append({"at": _now(), "action": "cited", "by": by, "note": ref_id(ref)})
    save_relationships(path, data)
    return edge


def verify_edge(path: str | Path, store: Store, edge_id: str, reviewer: str, note: str | None = None,
                verified: bool = True) -> dict:
    """Verify requires >= 1 SEC passage reference, every one of which re-validates against the stored filing."""
    from .relevidence import edge_evidence_status
    if not reviewer:
        raise ValueError("a reviewer name is required")
    data = load_relationships(path)
    migrate_relationships(data)
    edge = _edge(data, edge_id)
    if verified:
        ok, problems = edge_evidence_status(store, edge)
        if not ok:
            raise ValueError("cannot verify: " + "; ".join(problems))
    edge["verified"] = verified
    edge["verified_by"] = reviewer if verified else None
    edge["verified_at"] = _now() if verified else None
    edge["history"].append({"at": _now(), "action": "verified" if verified else "unverified", "by": reviewer,
                            "note": note})
    save_relationships(path, data)
    return edge


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def build(store: Store, relationships: dict, as_of: str) -> PowerGraph:
    """``as_of`` is an ISO UTC timestamp; only documents accepted by then are visible."""
    g = PowerGraph()
    as_of_date = _parse_ts(as_of).date()
    for e in relationships["entities"]:
        g.add_entity(Entity(e["id"], e["name"], NodeType(e["type"]), {**e.get("attrs", {}),
                                                                      "provenance": "live: SEC EDGAR + draft map"}))
    from .relevidence import edge_evidence_status, ref_id
    relationships = json.loads(json.dumps(relationships))
    migrate_relationships(relationships)
    for e in relationships["edges"]:
        # An edge counts as verified only if its SEC references re-validate against the stored filings
        # *and* every cited filing was public by as_of (no look-ahead through relationship evidence).
        evidence_ok = edge_evidence_status(store, e)[0] if e.get("verified") else False
        public = all(r["accepted_at"] <= as_of for r in e["sec_evidence"])
        g.add_edge(Edge(
            e["source"], EdgeType(e["relationship"]), e["target"],
            effective_from=date.fromisoformat(e["effective_from"]), effective_to=None,
            strength=e["strength"], direction=e["direction"], confidence=e["confidence"],
            evidence_ids=[ref_id(r) for r in e["sec_evidence"]] or [f"draft:{e['id']}"],
            last_verified=date.fromisoformat((e.get("verified_at") or e["recorded_at"])[:10]),
            lag_days=e["lag_days"], affected_metrics=e["metrics"], clock=Clock(e["clock"]),
            recorded_at=date.fromisoformat(e["recorded_at"]),
            verified=bool(e.get("verified")) and evidence_ok and public,
            verified_by=e.get("verified_by")))
    for c in store.active_claims(as_of):
        ev_ids = []
        for i, ev in enumerate(c["evidence"]):
            eid = f"{c['claim_id']}#{i}"
            ev_ids.append(eid)
            g.add_evidence(Evidence(eid, ev["type"], SourceTier.AUTHORITATIVE, _parse_ts(ev["accepted_at"]),
                                    ev.get("url"), "SEC EDGAR", c["claim_id"], 1, None, ev.get("passage", "")[:300]))
        g.add_claim(Claim(
            id=c["claim_id"], claim=c["statement"], event_type=FAMILY_TO_EVENT.get(c["family"], c["family"]),
            entities=[c["subject"]], event_time=_parse_ts(c["event_time"] + ("" if "T" in c["event_time"] else "T00:00:00Z")),
            first_observed=_parse_ts(c["available_at"]),
            status="confirmed",  # Tier-1 source AND human-approved; extraction alone never gets here
            novelty=1.0, market_relevance=0.5, evidence_quality=0.95, sources=ev_ids, contradictions=[],
            subject=c["subject"], metric=c["metric"], change=c["change"],
            clock=FAMILY_CLOCK.get(c["family"], Clock.BUSINESS)))
    assert all(cl.first_observed.date() <= as_of_date for cl in g.claims.values())
    return g
