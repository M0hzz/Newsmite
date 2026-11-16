"""Claim resolution: collapse many documents into few state changes.

An upstream extractor (LLM or rules) reads each document and emits a ``claim_key``
plus, once per key, a ``ClaimDraft`` describing the state change in graph terms.
This module does the part that must be deterministic and auditable:

* group evidence by claim
* count syndicated copies once (shared ``origin_root``)
* weight by sensor tier, so one filing outweighs fifty reposts
* detect disputes and set status
* score novelty against what the system already knew
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .model import TIER_WEIGHT, Claim, Clock, Evidence, SourceTier

# News is one weak sensor. However many outlets repeat a story, narrative-only support
# can never push evidence quality above this cap, and never makes a claim "confirmed".
NARRATIVE_ONLY_CAP = 0.35


@dataclass
class ClaimDraft:
    claim_key: str
    claim: str
    event_type: str
    entities: list[str]
    event_time: datetime
    subject: str
    metric: str
    change: float
    market_relevance: float
    clock: Clock = Clock.BUSINESS
    extra: dict = field(default_factory=dict)


def _independent_sources(evs: list[Evidence]) -> dict[str, Evidence]:
    """One representative per independent origin: the best-tier document from it."""
    best: dict[str, Evidence] = {}
    for ev in evs:
        root = ev.origin_root or ev.origin
        if root not in best or ev.tier < best[root].tier:
            best[root] = ev
    return best


def _noisy_or(weights: list[float]) -> float:
    return 1 - math.prod(1 - w for w in weights) if weights else 0.0


def evidence_quality(evs: list[Evidence]) -> tuple[float, float]:
    """Return (support, dispute) scores in [0, 1]."""
    sup = _independent_sources([e for e in evs if e.polarity > 0])
    dis = _independent_sources([e for e in evs if e.polarity < 0])
    return (_noisy_or([TIER_WEIGHT[e.tier] for e in sup.values()]),
            _noisy_or([TIER_WEIGHT[e.tier] for e in dis.values()]))


def novelty(draft: ClaimDraft, prior: list[Claim], half_life_days: float = 30.0) -> float:
    """1.0 = never seen; 0.0 = the market has known this exact thing for a while."""
    sims = []
    for p in prior:
        if p.subject != draft.subject or p.metric != draft.metric:
            continue
        if (p.change > 0) != (draft.change > 0):
            continue  # a reversal is news
        dt = abs((draft.event_time - p.event_time).total_seconds()) / 86400
        same_type = 1.0 if p.event_type == draft.event_type else 0.6
        sims.append(same_type * 0.5 ** (dt / half_life_days))
    return 1.0 - max(sims, default=0.0)


class ClaimResolver:
    def __init__(self, prior_claims: list[Claim] | None = None) -> None:
        self.prior = list(prior_claims or [])
        self._n = 0

    def resolve(self, drafts: list[ClaimDraft], evidence: list[Evidence]) -> list[Claim]:
        by_key: dict[str, list[Evidence]] = defaultdict(list)
        for ev in evidence:
            by_key[ev.claim_key].append(ev)

        out: list[Claim] = []
        for d in drafts:
            evs = by_key.get(d.claim_key, [])
            if not evs:
                continue
            support, dispute = evidence_quality(evs)
            supporting = [e for e in evs if e.polarity > 0]
            has_tier1 = any(e.tier == SourceTier.AUTHORITATIVE for e in supporting)
            narrative_only = bool(supporting) and all(e.tier == SourceTier.NARRATIVE for e in supporting)
            n_indep = len(_independent_sources([e for e in supporting if e.tier != SourceTier.NARRATIVE]))
            if narrative_only:
                support = min(support, NARRATIVE_ONLY_CAP)
            if dispute > 0.5 * support:
                status = "disputed"
            elif narrative_only:
                status = "narrative_only"
            elif has_tier1:
                status = "confirmed"
            elif n_indep >= 2:
                status = "corroborated"
            else:
                status = "single_sensor"
            quality = support * (1 - 0.5 * dispute)
            self._n += 1
            claim = Claim(
                id=f"cl_{d.claim_key}",
                claim=d.claim,
                event_type=d.event_type,
                entities=d.entities,
                event_time=d.event_time,
                first_observed=min(e.published for e in evs),
                status=status,
                novelty=novelty(d, self.prior),
                market_relevance=d.market_relevance,
                evidence_quality=quality,
                sources=sorted(e.id for e in evs if e.polarity > 0),
                contradictions=sorted(e.id for e in evs if e.polarity < 0),
                subject=d.subject,
                metric=d.metric,
                change=d.change,
                clock=d.clock,
            )
            out.append(claim)
            self.prior.append(claim)
        return out
