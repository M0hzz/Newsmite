"""Temporal power graph store.

All traversal is done "as of" a date, so the graph can be replayed historically
(essential for backtesting in OptLab without look-ahead bias).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .model import (
    FLOW, Catalyst, Claim, Edge, EdgeType, Entity, Evidence, Flow, Layer,
    MarketAssumption, NodeType,
)


@dataclass(frozen=True)
class Hop:
    """One transmission step: a change at ``frm`` moves ``to`` through ``edge``."""
    frm: str
    to: str
    edge: Edge


NODE_LAYER: dict[NodeType, Layer] = {
    NodeType.SUBSIDIARY: Layer.INTERNAL, NodeType.EXECUTIVE: Layer.INTERNAL,
    NodeType.PRODUCT: Layer.INTERNAL, NodeType.PATENT: Layer.INTERNAL,
    NodeType.LABOR_CATEGORY: Layer.INTERNAL, NodeType.TECHNOLOGY: Layer.INTERNAL,
    NodeType.COMPANY: Layer.COMMERCIAL, NodeType.SUPPLIER: Layer.COMMERCIAL,
    NodeType.CUSTOMER: Layer.COMMERCIAL, NodeType.COMPETITOR: Layer.COMMERCIAL,
    NodeType.COMMODITY: Layer.PHYSICAL, NodeType.PORT: Layer.PHYSICAL,
    NodeType.REGION: Layer.PHYSICAL, NodeType.COUNTRY: Layer.PHYSICAL,
    NodeType.GOVERNMENT_AGENCY: Layer.GOVERNMENT, NodeType.LEGISLATIVE_BODY: Layer.GOVERNMENT,
    NodeType.REGULATION: Layer.GOVERNMENT, NodeType.GOVERNMENT_CONTRACT: Layer.GOVERNMENT,
    NodeType.BANK: Layer.CAPITAL, NodeType.FUND: Layer.CAPITAL,
    NodeType.MAJOR_SHAREHOLDER: Layer.CAPITAL, NodeType.DEBT_INSTRUMENT: Layer.CAPITAL,
    NodeType.SECURITY: Layer.EXPECTATIONS, NodeType.NARRATIVE: Layer.EXPECTATIONS,
    NodeType.ECONOMIC_INDICATOR: Layer.PHYSICAL, NodeType.EVENT: Layer.EXPECTATIONS,
}

# Edge types override node type for layer placement (a company that SUPPLIES you is commercial,
# a company that LENDS_TO you is capital).
EDGE_LAYER: dict[EdgeType, Layer] = {
    EdgeType.REGULATES: Layer.GOVERNMENT, EdgeType.SUBSIDIZES: Layer.GOVERNMENT,
    EdgeType.TAXES: Layer.GOVERNMENT, EdgeType.SANCTIONS: Layer.GOVERNMENT,
    EdgeType.CONTRACTS_WITH: Layer.GOVERNMENT, EdgeType.LOBBIES: Layer.GOVERNMENT,
    EdgeType.LENDS_TO: Layer.CAPITAL, EdgeType.INSURES: Layer.CAPITAL,
    EdgeType.SHIPS_THROUGH: Layer.PHYSICAL, EdgeType.EMPLOYS: Layer.INTERNAL,
}


class PowerGraph:
    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.edges: list[Edge] = []
        self.evidence: dict[str, Evidence] = {}
        self.claims: dict[str, Claim] = {}
        self.assumptions: dict[str, MarketAssumption] = {}
        self.catalysts: list[Catalyst] = []
        self.policies: dict[str, "PolicyItem"] = {}  # noqa: F821  (see government.py)
        self._out: dict[str, list[Edge]] = defaultdict(list)
        self._in: dict[str, list[Edge]] = defaultdict(list)

    # ---------------------------------------------------------------- mutation
    def add_entity(self, e: Entity) -> Entity:
        self.entities[e.id] = e
        return e

    def add_edge(self, e: Edge) -> Edge:
        for end in (e.source_entity, e.target_entity):
            if end not in self.entities:
                raise KeyError(f"edge references unknown entity {end!r}")
        if not (0 <= e.strength <= 1 and 0 <= e.confidence <= 1):
            raise ValueError("strength and confidence must be in [0, 1]")
        self.edges.append(e)
        self._out[e.source_entity].append(e)
        self._in[e.target_entity].append(e)
        return e

    def add_claim(self, c: Claim) -> Claim:
        self.claims[c.id] = c
        return c

    def add_evidence(self, ev: Evidence) -> Evidence:
        self.evidence[ev.id] = ev
        return ev

    def add_assumption(self, a: MarketAssumption) -> MarketAssumption:
        self.assumptions[a.id] = a
        return a

    def add_catalyst(self, c: Catalyst) -> Catalyst:
        self.catalysts.append(c)
        return c

    # ---------------------------------------------------------------- queries
    def name(self, entity_id: str) -> str:
        e = self.entities.get(entity_id)
        return e.name if e else entity_id

    def edges_of(self, entity_id: str, as_of: date) -> list[Edge]:
        return [e for e in self._out[entity_id] + self._in[entity_id] if e.active(as_of)]

    def downstream(self, entity_id: str, as_of: date) -> Iterable[Hop]:
        """Entities that a change at ``entity_id`` transmits to."""
        for e in self._out[entity_id]:
            if e.active(as_of) and FLOW[e.relationship] in (Flow.FORWARD, Flow.BOTH):
                yield Hop(entity_id, e.target_entity, e)
        for e in self._in[entity_id]:
            if e.active(as_of) and FLOW[e.relationship] in (Flow.BACKWARD, Flow.BOTH):
                yield Hop(entity_id, e.source_entity, e)

    def upstream(self, entity_id: str, as_of: date) -> Iterable[Hop]:
        """Entities whose changes transmit *into* ``entity_id``."""
        for e in self._in[entity_id]:
            if e.active(as_of) and FLOW[e.relationship] in (Flow.FORWARD, Flow.BOTH):
                yield Hop(e.source_entity, entity_id, e)
        for e in self._out[entity_id]:
            if e.active(as_of) and FLOW[e.relationship] in (Flow.BACKWARD, Flow.BOTH):
                yield Hop(e.target_entity, entity_id, e)

    def claims_about(self, entity_id: str, as_of: date) -> list[Claim]:
        return [c for c in self.claims.values()
                if c.subject == entity_id and c.first_observed.date() <= as_of]

    def layer_of(self, entity_id: str, via: Edge | None = None) -> Layer:
        if via is not None and via.relationship in EDGE_LAYER:
            return EDGE_LAYER[via.relationship]
        return NODE_LAYER.get(self.entities[entity_id].type, Layer.COMMERCIAL)

    def securities_for(self, entity_id: str) -> list[str]:
        """Tradable tickers attached to an entity (attrs['ticker'])."""
        t = self.entities[entity_id].attrs.get("ticker")
        return [t] if t else []

    # ---------------------------------------------------------------- export
    def to_json(self, as_of: date) -> str:
        return json.dumps({
            "as_of": as_of.isoformat(),
            "entities": [{"id": e.id, "name": e.name, "type": e.type.value, **e.attrs}
                         for e in self.entities.values()],
            "edges": [e.to_dict() for e in self.edges if e.active(as_of)],
            "claims": [c.to_dict() for c in self.claims.values()
                       if c.first_observed.date() <= as_of],
        }, indent=2)
