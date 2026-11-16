"""Shock propagation: a causal sandbox.

Inject a hypothetical change at any node; the engine walks the graph *downstream*
(the direction state changes travel) and returns ordered consequences.

State convention (what a signed ``change`` means at each node type):
  companies / regions / indicators -> economic activity / health
  commodities and input-price nodes -> price
  regulators                         -> stringency
  agencies with budgets              -> spending
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .graph import Hop, PowerGraph
from .model import EdgeType, Layer, NodeType

GOV_TYPES = {NodeType.GOVERNMENT_AGENCY, NodeType.LEGISLATIVE_BODY, NodeType.REGULATION,
             NodeType.GOVERNMENT_CONTRACT}
CAPITAL_TYPES = {NodeType.BANK, NodeType.FUND, NodeType.MAJOR_SHAREHOLDER, NodeType.DEBT_INSTRUMENT}


@dataclass
class Shock:
    node: str
    change: float
    label: str


@dataclass
class Impact:
    node: str
    effect: float          # signed, net across paths
    confidence: float      # max path confidence
    min_hops: int
    lag_days: int          # lag of the strongest path
    paths: list[tuple[float, float, list[Hop]]] = field(default_factory=list)  # (effect, conf, hops)

    @property
    def gross_pos(self) -> float:
        return sum(e for e, _, _ in self.paths if e > 0)

    @property
    def gross_neg(self) -> float:
        return sum(e for e, _, _ in self.paths if e < 0)


@dataclass
class ShockResult:
    shock: Shock
    as_of: date
    impacts: dict[str, Impact]
    graph: PowerGraph

    # ------------------------------------------------------------ views
    def _named(self, imps: list[Impact]) -> list[dict]:
        g = self.graph
        out = []
        for i in imps:
            best = max(i.paths, key=lambda p: abs(p[0]))
            out.append({
                "entity": g.name(i.node), "id": i.node,
                "ticker": g.entities[i.node].attrs.get("ticker"),
                "effect": round(i.effect, 4), "confidence": round(i.confidence, 3),
                "lag_days": i.lag_days,
                "via": " -> ".join([g.name(best[2][0].frm)] + [f"{g.name(h.to)}" for h in best[2]]),
                "mechanism": [h.edge.relationship.value for h in best[2]],
            })
        return out

    def order(self, n: int) -> list[Impact]:
        sel = [i for i in self.impacts.values() if (i.min_hops == n if n < 3 else i.min_hops >= 3)]
        return sorted(sel, key=lambda i: -abs(i.effect))

    def delayed(self, min_lag: int = 90) -> list[Impact]:
        return sorted([i for i in self.impacts.values() if i.lag_days >= min_lag], key=lambda i: -abs(i.effect))

    def substitutions(self) -> list[Impact]:
        subs = []
        for i in self.impacts.values():
            best = max(i.paths, key=lambda p: abs(p[0]))
            if i.effect > 0 and best[2][-1].edge.relationship in (EdgeType.SUBSTITUTES_FOR, EdgeType.COMPETES_WITH):
                subs.append(i)
        return sorted(subs, key=lambda i: -i.effect)

    def contradictory(self, ratio: float = 0.35) -> list[Impact]:
        return [i for i in self.impacts.values()
                if i.gross_pos > 0 and i.gross_neg < 0
                and min(i.gross_pos, -i.gross_neg) / max(i.gross_pos, -i.gross_neg) >= ratio]

    def government_responses(self, hurt_threshold: float = 0.05) -> list[dict]:
        """Agencies whose constituents are hurt and that hold a lever they can pull."""
        g = self.graph
        out = []
        for ent in g.entities.values():
            if ent.type not in GOV_TYPES:
                continue
            for lever in ent.attrs.get("levers", []):
                hurt = [n for n in lever.get("protects", [])
                        if n in self.impacts and self.impacts[n].effect <= -hurt_threshold]
                if hurt:
                    out.append({"agency": ent.name, "possible_response": lever["action"],
                                "triggered_by": [g.name(h) for h in hurt],
                                "beneficiaries": [g.name(b) for b in lever.get("benefits", [])]})
        return out

    def capital_consequences(self) -> list[dict]:
        g = self.graph
        rows = []
        for i in self.impacts.values():
            ent = g.entities[i.node]
            lev = ent.attrs.get("net_debt_to_ebitda")
            if ent.type in CAPITAL_TYPES:
                rows.append({"entity": ent.name, "effect": round(i.effect, 4), "note": "capital holder exposed"})
            elif lev is not None and lev >= 2.5 and i.effect < 0:
                rows.append({"entity": ent.name, "effect": round(i.effect, 4),
                             "note": f"levered ({lev}x net debt/EBITDA): refinancing vulnerability"})
        return sorted(rows, key=lambda r: r["effect"])

    def securities(self) -> list[Impact]:
        return sorted([i for i in self.impacts.values() if self.graph.entities[i.node].attrs.get("ticker")],
                      key=lambda i: -abs(i.effect))

    def misalignment(self) -> list[dict]:
        """Where the effect is large and the market is least likely to have it in the price."""
        g = self.graph
        rows = []
        for i in self.securities():
            a = g.entities[i.node].attrs
            awareness = market_awareness(a)
            reasons = []
            if a.get("analyst_count", 30) <= 5:
                reasons.append(f"low analyst coverage ({a.get('analyst_count')} analysts)")
            consensus_has = [x for x in g.assumptions.values()
                             if x.security == a["ticker"] and math.copysign(1, i.effect) == x.expected_direction]
            if not consensus_has:
                reasons.append("no consensus assumption already points this way")
            else:
                awareness = min(1.0, awareness + 0.25)
            for p in g.policies.values():
                if i.node in p.affects and p.appropriated:
                    reasons.append(f"relevant budget already appropriated ({p.name})")
            rows.append({"ticker": a["ticker"], "entity": g.name(i.node), "effect": round(i.effect, 4),
                         "market_awareness": round(awareness, 2),
                         "misalignment": round(abs(i.effect) * (1 - awareness), 4), "reasons": reasons})
        return sorted(rows, key=lambda r: -r["misalignment"])

    # ------------------------------------------------------------ output
    def to_dict(self) -> dict:
        return {
            "shock": {"node": self.shock.node, "change": self.shock.change, "label": self.shock.label},
            "as_of": self.as_of.isoformat(),
            "first_order": self._named(self.order(1)),
            "second_order": self._named(self.order(2)),
            "third_order_plus": self._named(self.order(3)),
            "delayed_effects": self._named(self.delayed()),
            "substitutions": self._named(self.substitutions()),
            "government_responses": self.government_responses(),
            "capital_market_consequences": self.capital_consequences(),
            "contradictory_effects": self._named(self.contradictory()),
            "greatest_net_exposure": self._named(self.securities()[:8]),
            "expectations_least_aligned": self.misalignment()[:5],
        }

    def render(self) -> str:
        d = self.to_dict()

        def lines(rows, k=6):
            if not rows:
                return ["  (none above threshold)"]
            return [f"  {'+' if r['effect'] > 0 else '-'} {r['entity']:<34} {r['effect']:+.3f}"
                    f"  lag {r['lag_days']:>3}d  via {r['via']}" for r in rows[:k]]

        out = [f"Shock: {self.shock.label}", ""]
        for title, key in [("Immediate (1st order)", "first_order"), ("Second order", "second_order"),
                           ("Third order+", "third_order_plus"), ("Delayed effects (>=90d)", "delayed_effects"),
                           ("Substitutions", "substitutions"), ("Contradictory effects", "contradictory_effects")]:
            out += [title + ":"] + lines(d[key]) + [""]
        out.append("Government responses:")
        out += [f"  - {r['agency']}: {r['possible_response']} (triggered by {', '.join(r['triggered_by'])})"
                for r in d["government_responses"]] or ["  (none)"]
        out += ["", "Capital-market consequences:"]
        out += [f"  - {r['entity']}: {r['effect']:+.3f} {r['note']}" for r in d["capital_market_consequences"]] \
            or ["  (none)"]
        out += ["", "Potential mispricing (effect x (1 - market awareness)):"]
        for r in d["expectations_least_aligned"]:
            out.append(f"  - {r['ticker']:<5} effect {r['effect']:+.3f}  awareness {r['market_awareness']:.2f}"
                       f"  score {r['misalignment']:.3f}")
            out += [f"      * {x}" for x in r["reasons"]]
        return "\n".join(out)


def market_awareness(attrs: dict) -> float:
    """Crude prior for how much of a company's news flow the market processes."""
    coverage = min(attrs.get("analyst_count", 10) / 30, 1.0)
    return 0.15 + 0.65 * coverage


def propagate(g: PowerGraph, shock: Shock, as_of: date, max_hops: int = 5,
              min_effect: float = 0.004, paths_per_node: int = 4) -> ShockResult:
    if shock.node not in g.entities:
        raise KeyError(f"unknown entity {shock.node!r}")
    acc: dict[str, list[tuple[float, float, list[Hop]]]] = defaultdict(list)
    frontier: list[tuple[float, float, list[Hop]]] = [(shock.change, 1.0, [])]
    for _ in range(max_hops):
        nxt = []
        for eff, conf, hops in frontier:
            here = hops[-1].to if hops else shock.node
            seen = {shock.node} | {h.to for h in hops}
            for hop in g.downstream(here, as_of):
                if hop.to in seen or hop.edge.sign == 0:
                    continue
                e2 = eff * hop.edge.strength * hop.edge.sign
                if abs(e2) < min_effect:
                    continue
                item = (e2, conf * hop.edge.confidence, hops + [hop])
                if len(acc[hop.to]) < paths_per_node:
                    acc[hop.to].append(item)
                    nxt.append(item)
        frontier = sorted(nxt, key=lambda x: -abs(x[0]))[:400]
        if not frontier:
            break

    impacts: dict[str, Impact] = {}
    for node, paths in acc.items():
        best = max(paths, key=lambda p: abs(p[0]))
        net = sum(p[0] for p in paths)
        net = math.tanh(net)  # saturate: overlapping paths are not fully independent
        impacts[node] = Impact(node=node, effect=net, confidence=max(p[1] for p in paths),
                               min_hops=min(len(p[2]) for p in paths),
                               lag_days=sum(h.edge.lag_days for h in best[2]), paths=paths)
    return ShockResult(shock=shock, as_of=as_of, impacts=impacts, graph=g)
