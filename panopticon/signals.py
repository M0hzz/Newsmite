"""OptLab interface: small, versioned, immutable signal packets.

Panopticon generates hypotheses; OptLab tests them. Rules enforced here:

* No packet without at least one non-narrative claim. News alone can't produce a signal.
* Narrative sensors are used for one thing only: estimating *market awareness*.
* Every packet carries its clock. Structural/business/market signals are marked
  ``trading_clock_eligible: false`` unless OptLab feedback says a translation layer
  for that signal type has been tested.
* Packets are content-hashed and written to an append-only ledger with ``generated_at``
  and each claim's ``first_observed``, so backtests can replay what was known when.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .cone import HALF_LIFE_DAYS, Cone, ScoredPath, build_cone
from .contradictions import mine
from .graph import PowerGraph
from .model import Clock, Contradiction, EventFamily, SourceTier

MODEL_VERSION = "powergraph_0.1.0"
PACKET_SCHEMA = "panopticon.signal/1"


# ---------------------------------------------------------------- feedback from OptLab
@dataclass
class SignalPrior:
    weight: float = 1.0                 # multiplies expected_materiality
    predicts: str = "untested"          # direction | volatility | none | untested
    best_horizon: str | None = None
    translation_tested: bool = False    # may this signal type feed the trading clock?
    notes: str = ""


@dataclass
class FeedbackRegistry:
    """Empirical weights OptLab returns, keyed by (signal_type, industry)."""
    priors: dict[tuple[str, str], SignalPrior] = field(default_factory=dict)
    edge_multipliers: dict[str, float] = field(default_factory=dict)  # relationship -> strength multiplier

    def get(self, signal_type: str, industry: str) -> SignalPrior:
        return self.priors.get((signal_type, industry)) or self.priors.get((signal_type, "*")) or SignalPrior()

    @classmethod
    def load(cls, path: str | Path) -> "FeedbackRegistry":
        raw = json.loads(Path(path).read_text())
        reg = cls(edge_multipliers=raw.get("edge_multipliers", {}))
        for row in raw.get("signal_priors", []):
            reg.priors[(row["signal_type"], row.get("industry", "*"))] = SignalPrior(
                **{k: v for k, v in row.items() if k not in ("signal_type", "industry")})
        return reg

    def apply_to_graph(self, g: PowerGraph) -> None:
        """Measured transmission refines relationship importance."""
        for e in g.edges:
            m = self.edge_multipliers.get(e.relationship.value)
            if m is not None:
                e.strength = max(0.0, min(1.0, e.strength * m))


# ---------------------------------------------------------------- classification
SIGNAL_NAMES = {
    "MARGIN": ("SUPPLY_CHAIN_MARGIN_RELIEF", "SUPPLY_CHAIN_MARGIN_PRESSURE"),
    "CAPACITY": ("CAPACITY_RELIEF", "CAPACITY_CONSTRAINT"),
    "REGULATORY": ("REGULATORY_TAILWIND", "REGULATORY_HEADWIND"),
    "PROCUREMENT": ("PROCUREMENT_TAILWIND", "PROCUREMENT_HEADWIND"),
    "CAPITAL": ("CAPITAL_ACCESS_TAILWIND", "CAPITAL_ACCESS_HEADWIND"),
    "DEMAND": ("DEMAND_ACCELERATION", "DEMAND_DETERIORATION"),
}


def signal_family(sp: ScoredPath) -> str:
    """Directionless family; offsetting paths in one family are netted into a single packet."""
    metric = (sp.path.metrics() or [sp.claim.metric])[0]
    fam = sp.claim.event_type
    if metric == "gross_margin":
        return "MARGIN"
    if metric == "supply":
        return "CAPACITY"
    if fam == EventFamily.REGULATORY.value:
        return "REGULATORY"
    if fam == EventFamily.PROCUREMENT.value:
        return "PROCUREMENT"
    if fam == EventFamily.CAPITAL.value:
        return "CAPITAL"
    return "DEMAND"


def signal_type(sp: ScoredPath) -> str:
    up, down = SIGNAL_NAMES[signal_family(sp)]
    return up if sp.influence > 0 else down


def horizon(days_lo: int, days_hi: int) -> tuple[str, int]:
    q_lo, q_hi = max(0, round(days_lo / 91)), max(1, math.ceil(days_hi / 91))
    if q_hi <= 1 and days_hi <= 45:
        return "days_to_weeks", 45
    return f"{q_lo}_to_{q_hi}_quarters", q_hi * 91


def _noisy_or(ws: list[float]) -> float:
    return 1 - math.prod(1 - min(w, 0.99) for w in ws) if ws else 0.0


def _coverage_awareness(attrs: dict) -> float:
    return 0.15 + 0.65 * min(attrs.get("analyst_count", 10) / 30, 1.0)


def market_awareness(g: PowerGraph, claim_ids: list[str], entity_attrs: dict, mean_T: float) -> float:
    """Narrative sensors measure perception: how widely has this already been discussed?"""
    origins = set()
    for cid in claim_ids:
        for ev_id in g.claims[cid].sources:
            ev = g.evidence.get(ev_id)
            if ev and ev.tier == SourceTier.NARRATIVE:
                origins.add(ev.origin_root or ev.origin)
    news = _noisy_or([0.12] * len(origins))
    base = 1 - (1 - news) * (1 - _coverage_awareness(entity_attrs) * 0.6)
    return base + (1 - base) * (1 - mean_T)  # older information is more likely priced


# ---------------------------------------------------------------- packet
def content_hash(packet: dict) -> str:
    body = json.dumps({k: v for k, v in packet.items() if k not in ("signal_id", "generated_at", "packet_hash")},
                      sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def _finalize(packet: dict) -> dict:
    h = content_hash(packet)
    packet["packet_hash"] = h
    packet["signal_id"] = f"pg_{packet['valid_from'].replace('-', '')}_{h[:8]}"
    return dict(sorted(packet.items()))


def _gen_time(as_of: date) -> str:
    return datetime.combine(as_of, time(0, 42), tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def packets_from_cone(g: PowerGraph, cone: Cone, feedback: FeedbackRegistry | None = None,
                      min_materiality: float = 0.05) -> list[dict]:
    feedback = feedback or FeedbackRegistry()
    ent = g.entities[cone.focal]
    ticker = ent.attrs.get("ticker")
    if not ticker:
        return []
    industry = ent.attrs.get("industry", "*")
    groups: dict[str, list[ScoredPath]] = defaultdict(list)
    for sp in cone.scored:
        if sp.influence != 0:
            groups[signal_family(sp)].append(sp)

    out = []
    for fam, sps in groups.items():
        # Gates: no news-only or disputed claims, and no path through an unverified relationship.
        usable = [s for s in sps if s.claim.status not in ("narrative_only", "disputed") and s.path.verified]
        if not usable:
            continue  # news-only (or disputed-only) hypotheses never reach OptLab
        net = sum(s.influence for s in usable)
        stype = SIGNAL_NAMES[fam][0 if net > 0 else 1]
        hard = [s for s in usable if (s.influence > 0) == (net > 0)]
        opposite = [s for s in usable if (s.influence > 0) != (net > 0)]
        prior = feedback.get(stype, industry)
        materiality = min(1.0, math.tanh(abs(net) * 5) * prior.weight)
        if materiality < min_materiality:
            continue
        claim_ids = sorted({s.claim.id for s in hard})
        mean_T = sum(s.T for s in hard) / len(hard)
        lags = [s.path.lag_days for s in hard]
        clocks = {s.claim.clock for s in hard}
        clock = min(clocks, key=lambda c: HALF_LIFE_DAYS[c])
        hz, hz_days = horizon(min(lags), max(lags) + int(HALF_LIFE_DAYS[clock]))
        contra_ev = sorted({e for s in hard for e in s.claim.contradictions} |
                           {e for s in opposite for e in s.claim.sources})
        top, seen_paths = [], set()
        for s_ in sorted(hard, key=lambda s: -abs(s.influence)):
            key = tuple(s_.path.nodes)
            if key not in seen_paths:
                seen_paths.add(key)
                top.append(s_)
        top = top[:4]
        conf = _noisy_or([s.C for s in hard]) * (1 - 0.5 * _noisy_or([s.C for s in opposite]))
        invalidation = []
        for s in top[:2]:
            invalidation.append(f"{g.name(s.claim.subject)} {s.claim.metric} change reverses "
                                f"(supersedes {s.claim.id})")
            strongest = max(s.path.hops, key=lambda h: h.edge.strength)
            invalidation.append(f"{g.name(strongest.edge.source_entity)} {strongest.edge.relationship.value} "
                                f"{g.name(strongest.edge.target_entity)} relationship ends or is renegotiated")
        upcoming = sorted((c for c in g.catalysts
                           if c.security == ticker and cone.as_of <= c.date <= cone.as_of + timedelta(days=hz_days)),
                          key=lambda c: c.date)[:3]
        invalidation += [f"Resolution point {c.date.isoformat()}: {c.description} disconfirms" for c in upcoming]
        packet = {
            "schema": PACKET_SCHEMA,
            "generated_at": _gen_time(cone.as_of),
            "security": ticker,
            "signal_type": stype,
            "direction": "bullish" if net > 0 else "bearish",
            "clock": clock.value,
            "horizon": hz,
            "confidence": round(conf, 3),
            "novelty": round(sum(s.N for s in hard) / len(hard), 3),
            "expected_materiality": round(materiality, 3),
            "market_awareness": round(market_awareness(g, claim_ids, ent.attrs, mean_T), 3),
            "causal_paths": [[g.name(n) for n in s.path.nodes] + [m.replace("_", " ").title()
                                                                  for m in s.path.metrics()[:1]] for s in top],
            "claim_ids": claim_ids,
            "first_observed": min(g.claims[c].first_observed for c in claim_ids).isoformat(),
            "evidence_ids": sorted({e for s in hard for e in s.claim.sources}),
            "contradicting_evidence_ids": contra_ev,
            "valid_from": cone.as_of.isoformat(),
            "expires_at": (cone.as_of + timedelta(days=hz_days)).isoformat(),
            "invalidation_conditions": list(dict.fromkeys(invalidation)),
            "trading_clock_eligible": prior.translation_tested and clock != Clock.STRUCTURAL,
            "optlab_prior": {"predicts": prior.predicts, "weight": prior.weight},
            "model_version": MODEL_VERSION,
        }
        out.append(_finalize(packet))
    return sorted(out, key=lambda p: -p["expected_materiality"])


def packets_from_contradictions(g: PowerGraph, ticker: str, cxs: list[Contradiction], as_of: date,
                                feedback: FeedbackRegistry | None = None) -> list[dict]:
    feedback = feedback or FeedbackRegistry()
    out = []
    ent = next(e for e in g.entities.values() if e.attrs.get("ticker") == ticker)
    for cx in cxs:
        if cx.uses_unverified_edges:
            continue  # an unverified relationship cannot produce an OptLab packet
        stype = f"CONTRADICTION_{cx.kind.upper()}"
        prior = feedback.get(stype, ent.attrs.get("industry", "*"))
        assumption = next((a for a in g.assumptions.values() if a.security == ticker
                           and cx.market_assumption.startswith(a.statement)), None)
        if cx.kind == "price_vs_positioning" or assumption is None:
            direction = "volatility"
        else:
            direction = "bearish" if assumption.expected_direction > 0 else "bullish"
        crowd = assumption.crowdedness if assumption else 0.5
        days = 45 if cx.resolution_window == "weeks" else int(cx.resolution_window.split("-")[-1].split()[0]) * 91 \
            if "-" in cx.resolution_window else 91
        resolving = [c for c in g.catalysts if c.security == ticker and as_of <= c.date <= as_of + timedelta(days=days)]
        ev_ids = sorted({e for cid in cx.observation_claim_ids if cid in g.claims for e in g.claims[cid].sources}
                        | {cid for cid in cx.observation_claim_ids if cid not in g.claims})
        packet = {
            "schema": PACKET_SCHEMA,
            "generated_at": _gen_time(as_of),
            "security": ticker,
            "signal_type": stype,
            "direction": direction,
            "clock": Clock.MARKET.value if cx.kind == "price_vs_positioning" else Clock.BUSINESS.value,
            "horizon": horizon(0, days)[0],
            "confidence": round(cx.confidence, 3),
            "novelty": round(sum(g.claims[c].novelty for c in cx.observation_claim_ids if c in g.claims)
                             / max(1, sum(1 for c in cx.observation_claim_ids if c in g.claims)), 3)
            if any(c in g.claims for c in cx.observation_claim_ids) else 0.5,
            "expected_materiality": round(min(1.0, cx.confidence * (0.5 + crowd) * prior.weight), 3),
            "market_awareness": round(1 - crowd, 3),  # a crowded assumption = market believes the other side
            "causal_paths": [[cx.market_assumption, "CONTRADICTED_BY", o] for o in cx.conflicting_observations[:3]],
            "claim_ids": [c for c in cx.observation_claim_ids if c in g.claims],
            "contradiction_id": cx.contradiction_id,
            "evidence_ids": ev_ids,
            "contradicting_evidence_ids": [],
            "valid_from": as_of.isoformat(),
            "expires_at": (as_of + timedelta(days=days)).isoformat(),
            "invalidation_conditions": [f"{c.description} ({c.date.isoformat()}) resolves in favour of the assumption"
                                        for c in resolving] or ["Conflicting observations reverse"],
            "trading_clock_eligible": prior.translation_tested,
            "optlab_prior": {"predicts": prior.predicts, "weight": prior.weight},
            "model_version": MODEL_VERSION,
        }
        out.append(_finalize(packet))
    return out


def generate(g: PowerGraph, ticker: str, as_of: date, feedback: FeedbackRegistry | None = None) -> list[dict]:
    focal = next((e.id for e in g.entities.values() if e.attrs.get("ticker") == ticker), None)
    if focal is None:
        raise KeyError(f"unknown ticker {ticker!r}")
    cone = build_cone(g, focal, as_of)
    return packets_from_cone(g, cone, feedback) + \
        packets_from_contradictions(g, ticker, mine(g, ticker, as_of, cone), as_of, feedback)


# ---------------------------------------------------------------- ledger
class SignalLedger:
    """Append-only JSONL. A signal_id can be written once; rewriting with new content is refused."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _index(self) -> dict[str, str]:
        idx = {}
        for line in self.path.read_text().splitlines():
            if line.strip():
                p = json.loads(line)
                idx[p["signal_id"]] = p["packet_hash"]
        return idx

    def append(self, packets: list[dict]) -> int:
        idx = self._index()
        n = 0
        with self.path.open("a") as f:
            for p in packets:
                if content_hash(p) != p.get("packet_hash"):
                    raise ValueError(f"packet {p.get('signal_id')} was modified after hashing")
                if p["signal_id"] in idx:
                    if idx[p["signal_id"]] != p["packet_hash"]:
                        raise ValueError(f"immutability violation for {p['signal_id']}")
                    continue
                f.write(json.dumps(p, sort_keys=True) + "\n")
                n += 1
        return n

    def known_as_of(self, when: datetime) -> list[dict]:
        """Only packets that existed at ``when`` — the backtest view."""
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                p = json.loads(line)
                if datetime.fromisoformat(p["generated_at"].replace("Z", "+00:00")) <= when:
                    out.append(p)
        return out
