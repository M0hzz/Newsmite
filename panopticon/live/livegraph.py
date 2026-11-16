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


def verify_edge(path: str | Path, edge_id: str, reviewer: str, evidence: str, verified: bool = True) -> dict:
    if not reviewer or (verified and not evidence):
        raise ValueError("verifying an edge requires a reviewer and a citation (e.g. '10-K FY2026 Item 1A, p.24')")
    data = load_relationships(path)
    edge = next((e for e in data["edges"] if e["id"] == edge_id), None)
    if edge is None:
        raise KeyError(edge_id)
    edge["verified"] = verified
    edge["verified_by"] = reviewer if verified else None
    edge["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if verified else None
    if evidence:
        edge.setdefault("evidence", []).append(evidence)
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
    for e in relationships["edges"]:
        g.add_edge(Edge(
            e["source"], EdgeType(e["relationship"]), e["target"],
            effective_from=date.fromisoformat(e["effective_from"]), effective_to=None,
            strength=e["strength"], direction=e["direction"], confidence=e["confidence"],
            evidence_ids=list(e.get("evidence", [])) or [f"draft:{e['id']}"],
            last_verified=date.fromisoformat((e.get("verified_at") or e["recorded_at"])[:10]),
            lag_days=e["lag_days"], affected_metrics=e["metrics"], clock=Clock(e["clock"]),
            recorded_at=date.fromisoformat(e["recorded_at"]), verified=bool(e.get("verified")),
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
