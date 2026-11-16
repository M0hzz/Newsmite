"""Contradiction mining: which independently observed facts cannot all remain true?

Four detectors, matching the dossier's contradiction classes:

  management_vs_operations   management guidance vs. operational evidence in the graph
  consensus_vs_supply_chain  consensus/sell-side assumption vs. supply-chain evidence
  valuation_vs_policy        an assumption that needs a policy outcome the pipeline hasn't produced
  price_vs_positioning       price action vs. capital positioning (shorts, insiders, skew)

Narrative-only claims are never admitted as observations: they describe perception,
which is what the assumptions already are.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date

from .cone import Cone, build_cone, temporal_factor
from .graph import PowerGraph
from .model import Claim, Contradiction, EventFamily, MarketAssumption
from .government import PolicyStage

MIN_INDEPENDENT_OBSERVATIONS = 2


def _cid(*parts: str) -> str:
    return "cx_" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def _noisy_or(ws: list[float]) -> float:
    return 1 - math.prod(1 - min(w, 0.99) for w in ws) if ws else 0.0


def window_text(days: int) -> str:
    q = days / 91
    if q < 0.5:
        return "weeks"
    if q < 1.25:
        return "1 quarter"
    lo, hi = max(1, math.floor(q)), math.ceil(q) + 1
    return f"{lo}-{hi} quarters"


def _ticker_entity(g: PowerGraph, ticker: str) -> str | None:
    for e in g.entities.values():
        if e.attrs.get("ticker") == ticker:
            return e.id
    return None


def _observations(g: PowerGraph, cone: Cone, metric: str) -> list[tuple[Claim, float, float, str, int, bool]]:
    """(claim, signed implication for focal metric, weight, description, lag_days, all_edges_verified)."""
    obs = []
    for c in g.claims_about(cone.focal, cone.as_of):
        if c.metric == metric and c.status != "narrative_only" and c.change != 0:
            obs.append((c, math.copysign(1, c.change), c.evidence_quality * temporal_factor(c, cone.as_of),
                        c.claim, 0, True))
    for sp in cone.scored:
        if metric not in sp.path.metrics() or sp.claim.status == "narrative_only" or sp.influence == 0:
            continue
        w = sp.C * sp.T * min(1.0, max(0.25, abs(sp.E * sp.M) * 5))
        via = " -> ".join(g.name(n) for n in sp.path.nodes)
        obs.append((sp.claim, math.copysign(1, sp.influence), w, f"{sp.claim.claim} [{via}]", sp.path.lag_days,
                    sp.path.verified))
    return obs


def assumption_vs_evidence(g: PowerGraph, a: MarketAssumption, cone: Cone) -> Contradiction | None:
    obs = _observations(g, cone, a.metric)
    against: dict[str, tuple] = {}
    support: dict[str, tuple] = {}
    for o in obs:
        bucket = against if o[1] != a.expected_direction else support
        if o[0].id not in bucket or o[2] > bucket[o[0].id][2]:
            bucket[o[0].id] = o
    if len(against) < MIN_INDEPENDENT_OBSERVATIONS:
        return None
    conf = _noisy_or([o[2] for o in against.values()]) * (1 - 0.5 * _noisy_or([o[2] for o in support.values()]))
    kind = {"management_guidance": "management_vs_operations",
            "market_narrative": "narrative_vs_evidence"}.get(a.holder, "consensus_vs_supply_chain")
    lag = max(o[4] for o in against.values())
    metrics = sorted({a.metric} | {m for sp in cone.scored if sp.claim.id in against for m in sp.path.metrics()})
    return Contradiction(
        contradiction_id=_cid(a.id, *sorted(against)),
        kind=kind,
        market_assumption=f"{a.statement} ({a.holder.replace('_', ' ')})",
        conflicting_observations=[o[3] for o in sorted(against.values(), key=lambda o: -o[2])],
        resolution_window=window_text(max(lag, 45)),
        confidence=conf,
        affected_metrics=metrics,
        candidate_securities=[a.security],
        observation_claim_ids=sorted(against),
        uses_unverified_edges=not all(o[5] for o in against.values()),
    )


def assumption_vs_policy(g: PowerGraph, a: MarketAssumption, as_of: date) -> Contradiction | None:
    if not a.depends_on_policy or a.depends_on_policy not in g.policies:
        return None
    p = g.policies[a.depends_on_policy]
    if p.stage >= PolicyStage.ENFORCEMENT:
        return None
    pe = p.p_effect()
    eta = p.expected_effect_date(as_of)
    obs = [f"{p.name} is at stage {p.stage.name.lower()}, not enacted"] + \
          [g_.capitalize() for g_ in p.gaps()] + \
          [f"Estimated P(reaches company economics) = {pe:.0%}; expected no earlier than {eta.isoformat()}"]
    return Contradiction(
        contradiction_id=_cid(a.id, p.id),
        kind="valuation_vs_policy",
        market_assumption=f"{a.statement} ({a.holder.replace('_', ' ')})",
        conflicting_observations=obs,
        resolution_window=window_text((eta - as_of).days),
        confidence=(1 - pe) * 0.9,
        affected_metrics=[a.metric],
        candidate_securities=[a.security],
        observation_claim_ids=list(p.evidence_ids),
    )


def price_vs_positioning(g: PowerGraph, entity_id: str, as_of: date) -> Contradiction | None:
    ent = g.entities[entity_id]
    trend = ent.attrs.get("price_trend_20d")
    if trend is None or abs(trend) < 0.05:
        return None
    caps = [c for c in g.claims_about(entity_id, as_of)
            if c.event_type == EventFamily.CAPITAL.value and (c.change > 0) != (trend > 0)]
    if len(caps) < MIN_INDEPENDENT_OBSERVATIONS:
        return None
    return Contradiction(
        contradiction_id=_cid(entity_id, "positioning", *sorted(c.id for c in caps)),
        kind="price_vs_positioning",
        market_assumption=f"Price trend {trend:+.0%} over 20 sessions implies the market is "
                          f"{'leaning in' if trend > 0 else 'exiting'}",
        conflicting_observations=[c.claim for c in caps],
        resolution_window="weeks",
        confidence=_noisy_or([c.evidence_quality * temporal_factor(c, as_of) for c in caps]),
        affected_metrics=["positioning", "volatility"],
        candidate_securities=[ent.attrs["ticker"]],
        observation_claim_ids=sorted(c.id for c in caps),
    )


def mine(g: PowerGraph, ticker: str, as_of: date, cone: Cone | None = None) -> list[Contradiction]:
    focal = _ticker_entity(g, ticker)
    if focal is None:
        raise KeyError(f"unknown ticker {ticker!r}")
    cone = cone or build_cone(g, focal, as_of)
    out: list[Contradiction] = []
    for a in g.assumptions.values():
        if a.security != ticker:
            continue
        for cx in (assumption_vs_evidence(g, a, cone), assumption_vs_policy(g, a, as_of)):
            if cx:
                out.append(cx)
    cx = price_vs_positioning(g, focal, as_of)
    if cx:
        out.append(cx)
    return sorted(out, key=lambda c: -c.confidence)


def by_kind(cxs: list[Contradiction]) -> dict[str, list[Contradiction]]:
    d: dict[str, list[Contradiction]] = defaultdict(list)
    for c in cxs:
        d[c.kind].append(c)
    return dict(d)
