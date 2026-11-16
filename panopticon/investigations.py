"""Ask questions, don't browse feeds.

``Investigation`` builds the cone, contradictions and signals for a ticker once, then
answers the standard questions and assembles the home screen:

  3 meaningful changes, 2 unresolved contradictions, 1 expanding risk path,
  1 overlooked beneficiary, upcoming resolution points.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .cone import Cone, build_cone
from .contradictions import mine
from .government import PolicyStage
from .graph import PowerGraph
from .model import EdgeType, Layer, NodeType
from .shocks import Shock, market_awareness, propagate
from .signals import FeedbackRegistry, packets_from_cone, packets_from_contradictions

GOV = {NodeType.GOVERNMENT_AGENCY, NodeType.LEGISLATIVE_BODY, NodeType.REGULATION, NodeType.GOVERNMENT_CONTRACT}

QUESTIONS = {
    "controls": "What controls this company?",
    "must_remain_true": "What must remain true for its valuation to hold?",
    "geo_concentration": "Which dependencies are geographically concentrated?",
    "government": "Which government bodies can change its economics?",
    "management_contradicted": "Where is management's account contradicted?",
    "demand_observers": "Which neighboring companies observe its demand first?",
    "leading_variables": "Which variables lead its revenue by one or two quarters?",
    "failure_beneficiaries": "Which entities benefit if this company fails?",
    "material_changes": "What changed this week that crosses a materiality threshold?",
    "treated_as_certain": "What is the market treating as certain that is actually unresolved?",
}


class Investigation:
    def __init__(self, g: PowerGraph, ticker: str, as_of: date, feedback: FeedbackRegistry | None = None):
        self.g, self.ticker, self.as_of = g, ticker, as_of
        focal = next((e.id for e in g.entities.values() if e.attrs.get("ticker") == ticker), None)
        if focal is None:
            raise KeyError(f"unknown ticker {ticker!r}")
        self.focal = focal
        self.cone: Cone = build_cone(g, focal, as_of)
        self.contradictions = mine(g, ticker, as_of, self.cone)
        self.signals = packets_from_cone(g, self.cone, feedback) + \
            packets_from_contradictions(g, ticker, self.contradictions, as_of, feedback)

    # ------------------------------------------------------------ helpers
    def _n(self, i: str) -> str:
        return self.g.name(i)

    def claim_impacts(self) -> list[dict]:
        """Claims ranked by their total influence on the focal company."""
        agg: dict[str, float] = defaultdict(float)
        best: dict[str, object] = {}
        for sp in self.cone.scored:
            if sp.claim.status in ("narrative_only", "disputed"):
                continue
            agg[sp.claim.id] += sp.influence
            if sp.claim.id not in best or abs(sp.influence) > abs(best[sp.claim.id].influence):
                best[sp.claim.id] = sp
        rows = []
        for cid, inf in agg.items():
            c = self.g.claims[cid]
            sp = best[cid]
            rows.append({"claim_id": cid, "claim": c.claim, "status": c.status, "event_type": c.event_type,
                         "first_observed": c.first_observed.isoformat(), "influence": round(inf, 4),
                         "novelty": round(c.novelty, 2), "evidence_quality": round(c.evidence_quality, 2),
                         "n_documents": len(c.sources) + len(c.contradictions),
                         "path": [self._n(n) for n in sp.path.nodes]})
        return sorted(rows, key=lambda r: -abs(r["influence"]))

    # ------------------------------------------------------------ questions
    def controls(self) -> dict:
        by_layer: dict[str, list] = defaultdict(list)
        seen = set()
        for p in sorted(self.cone.paths, key=lambda p: -p.potential):
            if len(p.hops) != 1 or p.origin in seen:
                continue
            seen.add(p.origin)
            e = p.hops[0].edge
            by_layer[self.g.layer_of(p.origin, e).value].append({
                "entity": self._n(p.origin), "relationship": e.relationship.value,
                "strength": e.strength, "confidence": e.confidence,
                "sign": {1.0: "+", -1.0: "-", 0.0: "?"}[e.sign]})
        return {k: v[:6] for k, v in sorted(by_layer.items())}

    def must_remain_true(self) -> list[dict]:
        cx_by_assumption = defaultdict(list)
        for cx in self.contradictions:
            cx_by_assumption[cx.market_assumption].append(cx)
        rows = []
        for a in sorted(self.g.assumptions.values(), key=lambda a: -a.crowdedness):
            if a.security != self.ticker:
                continue
            key = f"{a.statement} ({a.holder.replace('_', ' ')})"
            cxs = cx_by_assumption.get(key, [])
            rows.append({"assumption": a.statement, "held_by": a.holder, "crowdedness": a.crowdedness,
                         "status": "contradicted" if cxs else "unchallenged",
                         "max_contradiction_confidence": round(max((c.confidence for c in cxs), default=0), 2)})
        return rows

    def geo_concentration(self) -> list[dict]:
        rows = []
        for p in self.cone.paths:
            ent = self.g.entities[p.origin]
            gc = ent.attrs.get("geo_concentration")
            if (gc and gc >= 0.5) or ent.attrs.get("chokepoint"):
                rows.append({"entity": ent.name, "concentration": gc, "chokepoint": bool(ent.attrs.get("chokepoint")),
                             "exposure": round(p.potential, 3), "via": [self._n(n) for n in p.nodes]})
        best = {}
        for r in rows:
            if r["entity"] not in best or r["exposure"] > best[r["entity"]]["exposure"]:
                best[r["entity"]] = r
        return sorted(best.values(), key=lambda r: -r["exposure"])

    def government(self) -> list[dict]:
        rows, seen = [], set()
        for p in sorted(self.cone.paths, key=lambda p: -p.potential):
            ent = self.g.entities[p.origin]
            if ent.type not in GOV or ent.id in seen:
                continue
            seen.add(ent.id)
            pols = [pol.to_dict(self.as_of) for pol in self.g.policies.values() if pol.agency == ent.id]
            rows.append({"body": ent.name, "roles": ent.attrs.get("roles", []),
                         "overseen_by": [self._n(c) for c in ent.attrs.get("overseen_by", [])],
                         "statute": ent.attrs.get("statute"), "active_rulemakings": ent.attrs.get("active_rulemakings", []),
                         "exposure": round(p.potential, 3), "via": [self._n(n) for n in p.nodes], "policies": pols})
        lobbying = self.g.entities[self.focal].attrs.get("lobbying", [])
        return rows + ([{"lobbying_by_company": lobbying}] if lobbying else [])

    def management_contradicted(self) -> list[dict]:
        return [c.to_dict() for c in self.contradictions if c.kind == "management_vs_operations"]

    def demand_observers(self) -> list[dict]:
        rows = []
        for p in self.cone.paths:
            e = p.hops[-1].edge
            if len(p.hops) == 1 and (e.relationship in (EdgeType.LEADS, EdgeType.BUYS_FROM)) \
                    and "demand" in e.affected_metrics:
                rows.append({"entity": self._n(p.origin), "relationship": e.relationship.value,
                             "lead_days": e.lag_days, "strength": e.strength})
        # Suppliers see the focal company's orders before its revenue prints.
        for e in self.g.edges:
            if e.source_entity == self.focal and e.relationship == EdgeType.BUYS_FROM and e.active(self.as_of):
                rows.append({"entity": self._n(e.target_entity), "relationship": "SEES_ORDERS_FROM (supplier)",
                             "lead_days": e.lag_days, "strength": e.strength})
        return sorted(rows, key=lambda r: (-r["lead_days"], -r["strength"]))

    def leading_variables(self) -> list[dict]:
        rows = []
        for p in self.cone.paths:
            if set(p.metrics()) & {"revenue", "demand"} and 45 <= p.lag_days <= 200:
                rows.append({"variable": self._n(p.origin), "lag_days": p.lag_days,
                             "direction": "+" if p.D > 0 else "-" if p.D < 0 else "?",
                             "exposure": round(p.potential, 3)})
        best = {}
        for r in rows:
            if r["variable"] not in best or r["exposure"] > best[r["variable"]]["exposure"]:
                best[r["variable"]] = r
        return sorted(best.values(), key=lambda r: -r["exposure"])[:10]

    def failure_beneficiaries(self) -> list[dict]:
        res = propagate(self.g, Shock(self.focal, -1.0, f"{self._n(self.focal)} fails"), self.as_of)
        winners = [i for i in res.impacts.values() if i.effect > 0]
        return [{"entity": self._n(i.node), "ticker": self.g.entities[i.node].attrs.get("ticker"),
                 "benefit": round(i.effect, 3),
                 "mechanism": [h.edge.relationship.value for h in max(i.paths, key=lambda p: abs(p[0]))[2]]}
                for i in sorted(winners, key=lambda i: -i.effect)[:8]]

    def material_changes(self, days: int = 7, threshold: float = 0.02) -> list[dict]:
        cutoff = self.as_of - timedelta(days=days)
        return [r for r in self.claim_impacts()
                if r["first_observed"][:10] >= cutoff.isoformat() and abs(r["influence"]) >= threshold]

    def treated_as_certain(self) -> list[dict]:
        rows = []
        for a in self.g.assumptions.values():
            if a.security != self.ticker:
                continue
            if a.depends_on_policy and a.depends_on_policy in self.g.policies:
                p = self.g.policies[a.depends_on_policy]
                if p.stage < PolicyStage.ENFORCEMENT:
                    rows.append({"assumption": a.statement, "unresolved_because": p.gaps(),
                                 "policy_stage": p.stage.name, "p_effect": round(p.p_effect(), 2)})
            elif a.crowdedness >= 0.6 and any(c.market_assumption.startswith(a.statement) for c in self.contradictions):
                rows.append({"assumption": a.statement, "unresolved_because":
                             ["independent operational evidence points the other way"],
                             "crowdedness": a.crowdedness})
        return rows

    def answer(self, key: str):
        return {"question": QUESTIONS[key], "answer": getattr(self, key)()}

    # ------------------------------------------------------------ home screen
    def expanding_risk_path(self, lookback_days: int = 14) -> dict | None:
        """The negative path whose influence grew most versus ``lookback_days`` ago."""
        past = build_cone(self.g, self.focal, self.as_of - timedelta(days=lookback_days))
        before = defaultdict(float)
        for sp in past.scored:
            before[" ".join(sp.path.nodes)] += sp.influence
        now = defaultdict(float)
        rep = {}
        for sp in self.cone.scored:
            if sp.claim.status in ("narrative_only", "disputed"):
                continue
            k = " ".join(sp.path.nodes)
            now[k] += sp.influence
            rep[k] = sp
        growth = [(now[k] - before.get(k, 0.0), k) for k in now if now[k] < 0 and len(rep[k].path.hops) >= 2]
        if not growth:
            return None
        delta, k = min(growth)
        sp = rep[k]
        return {"path": [self._n(n) for n in sp.path.nodes], "influence_now": round(now[k], 4),
                "influence_before": round(before.get(k, 0.0), 4), "change": round(delta, 4),
                "driver": sp.claim.claim, "lag_days": sp.path.lag_days}

    def overlooked_beneficiary(self) -> dict | None:
        """Shock the graph with the focal company's largest negative change and find the winner the
        market is least likely to be watching: direct propagation winners plus beneficiaries of the
        government responses that shock would trigger, scored by benefit x (1 - market awareness)."""
        neg = [r for r in self.claim_impacts() if r["influence"] < 0]
        if not neg:
            return None
        top = self.g.claims[neg[0]["claim_id"]]
        res = propagate(self.g, Shock(top.subject, top.change, top.claim), self.as_of)
        cands: dict[str, dict] = {}
        for i in res.impacts.values():
            if i.effect > 0 and i.node != self.focal and self.g.entities[i.node].attrs.get("ticker"):
                cands[i.node] = {"effect": i.effect, "reasons": []}
        for r in res.government_responses():
            for b in r["beneficiaries"]:
                ent = next(e for e in self.g.entities.values() if e.name == b)
                if ent.id == self.focal or not ent.attrs.get("ticker"):
                    continue
                c = cands.setdefault(ent.id, {"effect": 0.0, "reasons": []})
                c["effect"] += 0.05  # prior for a plausible-but-unconfirmed policy response
                c["reasons"].append(f"likely government response: {r['possible_response']} ({r['agency']})")
        if not cands:
            return None
        for eid, c in cands.items():
            attrs = self.g.entities[eid].attrs
            c["reasons"].insert(0, f"{attrs.get('analyst_count', '?')} covering analysts")
            for p in self.g.policies.values():
                if p.affects.get(eid, 0) > 0 and p.appropriated:
                    c["effect"] += 0.02
                    c["reasons"].append(f"budget already appropriated: {p.name}")
            if not any(a.security == attrs["ticker"] for a in self.g.assumptions.values()):
                c["reasons"].append("no tracked consensus assumption reflects this")
            c["score"] = c["effect"] * (1 - market_awareness(attrs))
        eid, c = max(cands.items(), key=lambda kv: kv[1]["score"])
        return {"ticker": self.g.entities[eid].attrs["ticker"], "entity": self._n(eid), "trigger": top.claim,
                "score": round(c["score"], 4), "reasons": c["reasons"]}

    def upcoming(self, days: int = 240) -> list[dict]:
        end = self.as_of + timedelta(days=days)
        return [{"date": c.date.isoformat(), "kind": c.kind, "description": c.description, "tests": c.resolves}
                for c in sorted(self.g.catalysts, key=lambda c: c.date)
                if c.security == self.ticker and self.as_of <= c.date <= end]

    def home(self) -> dict:
        n_docs = sum(1 for e in self.g.evidence.values() if e.published.date() <= self.as_of)
        n_claims = sum(1 for c in self.g.claims.values() if c.first_observed.date() <= self.as_of)
        return {
            "ticker": self.ticker, "entity": self._n(self.focal), "as_of": self.as_of.isoformat(),
            "provenance": self.g.entities[self.focal].attrs.get("provenance"),
            "reduction": {"documents": n_docs, "claims": n_claims, "material": len(self.material_changes(days=30))},
            "meaningful_changes": self.claim_impacts()[:3],
            "unresolved_contradictions": [c.to_dict() for c in self.contradictions[:2]],
            "expanding_risk_path": self.expanding_risk_path(),
            "overlooked_beneficiary": self.overlooked_beneficiary(),
            "upcoming_resolution_points": self.upcoming(),
            "investigations": [{"key": k, "question": q} for k, q in QUESTIONS.items()],
            "signals_exported": len(self.signals),
        }
