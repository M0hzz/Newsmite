"""Core data model for the power graph.

Everything in Panopticon is one of four objects:

* ``Entity``   – a node (company, agency, commodity, port, narrative, ...)
* ``Edge``     – a temporal, evidence-backed relationship between two entities
* ``Evidence`` – a raw document (filing, article, dataset row). Never the primary object.
* ``Claim``    – a normalized state change that evidence supports or disputes.

Strength and confidence are deliberately separate:
  strength   = how economically important the relationship is
  confidence = how sure we are that the representation is correct
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum, IntEnum
from typing import Any


class NodeType(str, Enum):
    COMPANY = "Company"
    SECURITY = "Security"
    SUBSIDIARY = "Subsidiary"
    EXECUTIVE = "Executive"
    SUPPLIER = "Supplier"
    CUSTOMER = "Customer"
    COMPETITOR = "Competitor"
    COMMODITY = "Commodity"
    PRODUCT = "Product"
    TECHNOLOGY = "Technology"
    COUNTRY = "Country"
    REGION = "Region"
    PORT = "Port"
    GOVERNMENT_AGENCY = "Government agency"
    LEGISLATIVE_BODY = "Legislative body"
    REGULATION = "Regulation"
    GOVERNMENT_CONTRACT = "Government contract"
    BANK = "Bank"
    FUND = "Fund"
    MAJOR_SHAREHOLDER = "Major shareholder"
    DEBT_INSTRUMENT = "Debt instrument"
    PATENT = "Patent"
    LABOR_CATEGORY = "Labor category"
    ECONOMIC_INDICATOR = "Economic indicator"
    NARRATIVE = "Narrative"
    EVENT = "Event"


class EdgeType(str, Enum):
    SUPPLIES = "SUPPLIES"
    BUYS_FROM = "BUYS_FROM"
    COMPETES_WITH = "COMPETES_WITH"
    SUBSTITUTES_FOR = "SUBSTITUTES_FOR"
    REGULATES = "REGULATES"
    SUBSIDIZES = "SUBSIDIZES"
    TAXES = "TAXES"
    SANCTIONS = "SANCTIONS"
    LOBBIES = "LOBBIES"
    CONTRACTS_WITH = "CONTRACTS_WITH"
    OWNS = "OWNS"
    LENDS_TO = "LENDS_TO"
    INSURES = "INSURES"
    EMPLOYS = "EMPLOYS"
    DEPENDS_ON = "DEPENDS_ON"
    SHIPS_THROUGH = "SHIPS_THROUGH"
    EXPOSED_TO = "EXPOSED_TO"
    CORRELATED_WITH = "CORRELATED_WITH"
    LEADS = "LEADS"
    CONFIRMS = "CONFIRMS"
    CONTRADICTS = "CONTRADICTS"


class Flow(str, Enum):
    """Which way a change in state travels along an edge of this type."""
    FORWARD = "forward"    # a change at source moves target
    BACKWARD = "backward"  # a change at target moves source
    BOTH = "both"
    NONE = "none"          # epistemic edges (CONFIRMS/CONTRADICTS) do not transmit shocks


# "A SUPPLIES B": A's health moves B.        "A EXPOSED_TO copper": copper moves A.
# "A OWNS B": B's results move A (backward). "A COMPETES_WITH B": both ways.
FLOW: dict[EdgeType, Flow] = {
    EdgeType.SUPPLIES: Flow.FORWARD,
    EdgeType.BUYS_FROM: Flow.FORWARD,
    EdgeType.COMPETES_WITH: Flow.BOTH,
    EdgeType.SUBSTITUTES_FOR: Flow.BOTH,
    EdgeType.REGULATES: Flow.FORWARD,
    EdgeType.SUBSIDIZES: Flow.FORWARD,
    EdgeType.TAXES: Flow.FORWARD,
    EdgeType.SANCTIONS: Flow.FORWARD,
    EdgeType.LOBBIES: Flow.FORWARD,
    EdgeType.CONTRACTS_WITH: Flow.FORWARD,
    EdgeType.OWNS: Flow.BACKWARD,
    EdgeType.LENDS_TO: Flow.FORWARD,
    EdgeType.INSURES: Flow.FORWARD,
    EdgeType.EMPLOYS: Flow.BACKWARD,
    EdgeType.DEPENDS_ON: Flow.BACKWARD,
    EdgeType.SHIPS_THROUGH: Flow.BACKWARD,
    EdgeType.EXPOSED_TO: Flow.BACKWARD,
    EdgeType.CORRELATED_WITH: Flow.BOTH,
    EdgeType.LEADS: Flow.FORWARD,
    EdgeType.CONFIRMS: Flow.NONE,
    EdgeType.CONTRADICTS: Flow.NONE,
}

DIRECTION_SIGN = {
    "positive_dependency": 1.0,   # source up -> target up
    "negative_dependency": -1.0,  # source up -> target down (costs, competition, sanctions)
    "ambiguous": 0.0,             # known link, unknown sign: contributes to attention, not to net effect
}


class Clock(str, Enum):
    STRUCTURAL = "structural"  # years
    BUSINESS = "business"      # quarters
    MARKET = "market"          # days to weeks
    TRADING = "trading"        # seconds to hours  (OptLab's domain, not Panopticon's)


class EventFamily(str, Enum):
    """MVP scope: only these five families are modelled."""
    REGULATORY = "REGULATORY_CHANGE"
    PROCUREMENT = "PROCUREMENT_CONTRACT_CHANGE"
    CAPACITY = "CAPACITY_CHANGE"
    PRICING = "PRICING_INPUT_COST_CHANGE"
    CAPITAL = "CAPITAL_POSITIONING_CHANGE"


class Layer(str, Enum):
    INTERNAL = "A: Internal organism"
    COMMERCIAL = "B: Commercial ecosystem"
    PHYSICAL = "C: Physical dependencies"
    GOVERNMENT = "D: Government power"
    CAPITAL = "E: Capital power"
    EXPECTATIONS = "F: Expectations"


class SourceTier(IntEnum):
    """Sensor hierarchy, ranked by proximity to underlying reality (lower = closer)."""
    AUTHORITATIVE = 1  # filings, court records, Federal Register, contract awards
    OPERATIONAL = 2    # job postings, prices, shipping, permits
    CAPITAL = 3        # holdings, insiders, short interest, spreads, skew
    NARRATIVE = 4      # calls, trade press, media, social


TIER_WEIGHT = {
    SourceTier.AUTHORITATIVE: 0.95,
    SourceTier.OPERATIONAL: 0.75,
    SourceTier.CAPITAL: 0.65,
    SourceTier.NARRATIVE: 0.35,
}


@dataclass
class Entity:
    id: str
    name: str
    type: NodeType
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source_entity: str
    relationship: EdgeType
    target_entity: str
    effective_from: date
    effective_to: date | None
    strength: float
    direction: str
    confidence: float
    evidence_ids: list[str]
    last_verified: date
    lag_days: int = 0
    affected_metrics: list[str] = field(default_factory=list)
    clock: Clock = Clock.BUSINESS
    # Bitemporal: when the relationship was true (effective_*) vs. when *we knew it* (recorded_at).
    # Point-in-time queries require both, so backtests never see relationships discovered later.
    recorded_at: date | None = None
    # Human verification gate. Unverified edges are shown in dossiers but can never carry an
    # OptLab packet (signals.py) or a packet-producing contradiction.
    verified: bool = False
    verified_by: str | None = None

    def active(self, as_of: date) -> bool:
        known = self.recorded_at is None or self.recorded_at <= as_of
        return known and self.effective_from <= as_of and (self.effective_to is None or as_of < self.effective_to)

    @property
    def sign(self) -> float:
        return DIRECTION_SIGN[self.direction]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_entity": self.source_entity,
            "relationship": self.relationship.value,
            "target_entity": self.target_entity,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "strength": self.strength,
            "direction": self.direction,
            "confidence": self.confidence,
            "evidence_ids": self.evidence_ids,
            "last_verified": self.last_verified.isoformat(),
            "recorded_at": (self.recorded_at or self.effective_from).isoformat(),
            "lag_days": self.lag_days,
            "affected_metrics": self.affected_metrics,
            "verified": self.verified,
        }


@dataclass
class Evidence:
    id: str
    source_type: str              # e.g. "sec_filing", "news", "job_postings"
    tier: SourceTier
    published: datetime
    url: str | None
    origin: str                   # outlet/publisher; syndicated copies share an origin_root
    claim_key: str                # output of the extractor: which claim this evidence speaks to
    polarity: int = 1             # +1 supports the claim, -1 disputes it
    origin_root: str | None = None  # wire service a syndicated story came from
    excerpt: str = ""


@dataclass
class Claim:
    id: str
    claim: str
    event_type: str
    entities: list[str]
    event_time: datetime
    first_observed: datetime
    status: str                   # confirmed | reported | disputed | rumor
    novelty: float
    market_relevance: float
    evidence_quality: float
    sources: list[str]
    contradictions: list[str]
    # What changed, in graph terms: which node, which metric, which way, how much.
    subject: str = ""
    metric: str = ""
    change: float = 0.0           # signed fractional change in the subject's state
    clock: Clock = Clock.BUSINESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "claim": self.claim,
            "event_time": self.event_time.isoformat(),
            "first_observed": self.first_observed.isoformat(),
            "entities": self.entities,
            "event_type": self.event_type,
            "status": self.status,
            "novelty": round(self.novelty, 3),
            "market_relevance": round(self.market_relevance, 3),
            "evidence_quality": round(self.evidence_quality, 3),
            "sources": self.sources,
            "contradictions": self.contradictions,
            "subject": self.subject,
            "metric": self.metric,
            "change": self.change,
            "clock": self.clock.value,
        }


@dataclass
class MarketAssumption:
    """Something the market (or management) currently believes. Lives in Layer F."""
    id: str
    security: str
    statement: str
    metric: str
    expected_direction: int       # +1 / -1
    holder: str                   # consensus | management_guidance | market_narrative | sell_side
    crowdedness: float = 0.5      # 0..1, how widely held / how much valuation leans on it
    depends_on_policy: str | None = None  # PolicyItem id if the assumption needs a policy outcome


@dataclass
class Contradiction:
    contradiction_id: str
    market_assumption: str
    conflicting_observations: list[str]
    resolution_window: str
    confidence: float
    affected_metrics: list[str]
    candidate_securities: list[str]
    kind: str = "assumption_vs_observation"
    observation_claim_ids: list[str] = field(default_factory=list)
    uses_unverified_edges: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "contradiction_id": self.contradiction_id,
            "kind": self.kind,
            "market_assumption": self.market_assumption,
            "conflicting_observations": self.conflicting_observations,
            "resolution_window": self.resolution_window,
            "confidence": round(self.confidence, 3),
            "affected_metrics": self.affected_metrics,
            "candidate_securities": self.candidate_securities,
            "observation_claim_ids": self.observation_claim_ids,
        }


@dataclass
class Catalyst:
    """An upcoming resolution point."""
    security: str
    date: date
    kind: str      # earnings | contract_award | court | rulemaking | debt_maturity | product | capacity
    description: str
    resolves: list[str] = field(default_factory=list)  # contradiction or assumption ids it tests
