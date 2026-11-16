"""Dependency cone: the bounded sub-graph that can plausibly move one stock.

Expansion is best-first on *structural potential* (product of strength x confidence
along the path), and stops when potential falls below a threshold. That is what keeps
"everything affects everything" from swamping the system.

Each path then gets a realized influence score from the claims observed at its origin:

    I(p) = E x D x M x C x T x N

    E  economic exposure      product of edge strengths
    D  directionality          product of edge signs (+1, -1, 0 for unknown sign)
    M  magnitude of change     the claim's signed change at the path origin
    C  confidence              product of edge confidences x claim evidence quality
    T  temporal relevance      claim-age decay (by clock) x edge staleness decay
    N  novelty                 claim novelty relative to what was already known
"""
from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field
from datetime import date

from .graph import Hop, PowerGraph
from .model import Claim, Clock, EdgeType

RELATIVE_EDGES = {EdgeType.COMPETES_WITH, EdgeType.SUBSTITUTES_FOR}

HALF_LIFE_DAYS = {
    Clock.STRUCTURAL: 730.0,
    Clock.BUSINESS: 90.0,
    Clock.MARKET: 10.0,
    Clock.TRADING: 0.5,
}
EDGE_STALENESS_HALF_LIFE = 365.0


@dataclass
class Path:
    hops: list[Hop]                       # ordered origin -> ... -> focal

    @property
    def origin(self) -> str:
        return self.hops[0].frm

    @property
    def nodes(self) -> list[str]:
        return [self.hops[0].frm] + [h.to for h in self.hops]

    @property
    def E(self) -> float:
        return math.prod(h.edge.strength for h in self.hops)

    @property
    def D(self) -> float:
        return math.prod(h.edge.sign for h in self.hops)

    @property
    def edge_confidence(self) -> float:
        return math.prod(h.edge.confidence for h in self.hops)

    @property
    def lag_days(self) -> int:
        return sum(h.edge.lag_days for h in self.hops)

    @property
    def verified(self) -> bool:
        return all(h.edge.verified for h in self.hops)

    @property
    def potential(self) -> float:
        return self.E * self.edge_confidence

    def metrics(self) -> list[str]:
        """Metrics of the focal company the final hop lands on."""
        return list(self.hops[-1].edge.affected_metrics)

    def staleness(self, as_of: date) -> float:
        return math.prod(0.5 ** (max((as_of - h.edge.last_verified).days, 0) / EDGE_STALENESS_HALF_LIFE)
                         for h in self.hops)

    def describe(self, g: PowerGraph) -> str:
        parts = [g.name(self.origin)]
        for h in self.hops:
            arrow = "+" if h.edge.sign > 0 else ("-" if h.edge.sign < 0 else "?")
            parts.append(f"-[{h.edge.relationship.value} {arrow}{h.edge.strength:.2f}]-> {g.name(h.to)}")
        return " ".join(parts)


@dataclass
class ScoredPath:
    path: Path
    claim: Claim
    E: float
    D: float
    M: float
    C: float
    T: float
    N: float

    @property
    def influence(self) -> float:
        return self.E * self.D * self.M * self.C * self.T * self.N

    def to_dict(self, g: PowerGraph) -> dict:
        return {
            "path": self.path.describe(g),
            "claim_id": self.claim.id,
            "claim": self.claim.claim,
            "factors": {k: round(getattr(self, k), 4) for k in "EDMCTN"},
            "influence": round(self.influence, 5),
            "lag_days": self.path.lag_days,
            "metrics": self.path.metrics(),
        }


@dataclass
class Cone:
    focal: str
    as_of: date
    paths: list[Path]
    hop_of: dict[str, int]
    scored: list[ScoredPath] = field(default_factory=list)

    def by_hop(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for n, h in sorted(self.hop_of.items(), key=lambda x: x[1]):
            out.setdefault(h, []).append(n)
        return out

    def net_influence(self) -> float:
        return sum(s.influence for s in self.scored)


def temporal_factor(claim: Claim, as_of: date) -> float:
    age = max((as_of - claim.event_time.date()).days, 0)
    return 0.5 ** (age / HALF_LIFE_DAYS[claim.clock])


def build_cone(g: PowerGraph, focal: str, as_of: date, max_hops: int = 5,
               threshold: float = 0.01, max_paths_per_node: int = 3) -> Cone:
    if focal not in g.entities:
        raise KeyError(f"unknown entity {focal!r}")
    counter = itertools.count()
    # max-heap on potential; each item is a path ending at focal, growing backwards
    heap: list[tuple[float, int, list[Hop]]] = []
    for hop in g.upstream(focal, as_of):
        p = hop.edge.strength * hop.edge.confidence
        if p >= threshold:
            heapq.heappush(heap, (-p, next(counter), [hop]))

    paths: list[Path] = []
    hop_of: dict[str, int] = {focal: 0}
    per_node: dict[str, int] = {}
    while heap:
        neg_p, _, hops = heapq.heappop(heap)
        origin = hops[0].frm
        if per_node.get(origin, 0) >= max_paths_per_node:
            continue
        per_node[origin] = per_node.get(origin, 0) + 1
        path = Path(hops)
        paths.append(path)
        hop_of[origin] = min(hop_of.get(origin, 99), len(hops))
        if len(hops) >= max_hops:
            continue
        visited = set(path.nodes)
        for up in g.upstream(origin, as_of):
            if up.frm in visited:
                continue
            p = -neg_p * up.edge.strength * up.edge.confidence
            if p >= threshold:
                heapq.heappush(heap, (-p, next(counter), [up] + hops))

    cone = Cone(focal=focal, as_of=as_of, paths=paths, hop_of=hop_of)
    cone.scored = score_paths(g, cone)
    return cone


def _latest_claims(g: PowerGraph, entity_id: str, as_of: date) -> list[Claim]:
    """Drop claims superseded by a newer observation of the same subject and metric."""
    latest: dict[str, Claim] = {}
    for c in g.claims_about(entity_id, as_of):
        if c.metric not in latest or c.event_time > latest[c.metric].event_time:
            latest[c.metric] = c
    return list(latest.values())


def _is_relative(path: Path) -> bool:
    return any(h.edge.relationship in RELATIVE_EDGES for h in path.hops)


def score_paths(g: PowerGraph, cone: Cone) -> list[ScoredPath]:
    # Origins with a direct (non-competitive) route to the focal company. A shock at such an
    # origin is a *common* shock: it hits the focal company and its rivals alike, so routing it
    # through a competition edge would wrongly count a rival's pain as the focal company's gain.
    direct_origins = {p.origin for p in cone.paths if not _is_relative(p)}
    scored = []
    for path in cone.paths:
        if path.D == 0:
            continue
        if _is_relative(path) and path.origin in direct_origins:
            continue
        for claim in _latest_claims(g, path.origin, cone.as_of):
            sp = ScoredPath(
                path=path, claim=claim,
                E=path.E, D=path.D, M=claim.change,
                C=path.edge_confidence * claim.evidence_quality,
                T=temporal_factor(claim, cone.as_of) * path.staleness(cone.as_of),
                N=claim.novelty,
            )
            scored.append(sp)
    scored.sort(key=lambda s: abs(s.influence), reverse=True)
    return scored
