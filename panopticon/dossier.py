"""Causal dossier: a living model of one stock, rendered as Markdown."""
from __future__ import annotations

from collections import defaultdict

from .contradictions import by_kind
from .graph import NODE_LAYER
from .investigations import GOV, Investigation
from .model import EdgeType, Layer, NodeType

INPUT_TYPES = {NodeType.SUPPLIER, NodeType.COMMODITY}
ACCESS_EDGES = {EdgeType.REGULATES, EdgeType.SANCTIONS, EdgeType.CONTRACTS_WITH, EdgeType.TAXES}


def _bucket(x: float, hi: float = 0.5, mid: float = 0.2) -> str:
    return "High" if x >= hi else "Medium" if x >= mid else "Low"


def _lag(days: int) -> str:
    q = days / 91
    if q < 0.5:
        return "< 1 quarter"
    if q < 1.3:
        return "1 quarter"
    return f"{max(1, round(q - 0.5))}-{round(q + 0.5)} quarters"


def sensitivity_table(inv: Investigation) -> list[dict]:
    g = inv.g
    rows: dict[str, dict] = {}
    for p in inv.cone.paths:
        ent = g.entities[p.origin]
        if ent.type not in {NodeType.COMMODITY, NodeType.ECONOMIC_INDICATOR, NodeType.GOVERNMENT_AGENCY,
                            NodeType.COMPETITOR, NodeType.REGION, NodeType.PORT, NodeType.EVENT,
                            NodeType.TECHNOLOGY} or p.D == 0:
            continue
        if ent.name in rows and rows[ent.name]["_e"] >= p.E:
            continue
        rows[ent.name] = {"variable": ent.name, "direction": "Positive" if p.D > 0 else "Negative",
                          "lag": _lag(p.lag_days), "exposure": _bucket(p.E),
                          "confidence": _bucket(p.edge_confidence, 0.75, 0.5), "_e": p.E}
    return [{k: v for k, v in r.items() if k != "_e"} for r in sorted(rows.values(), key=lambda r: -r["_e"])][:14]


def power_map(inv: Investigation) -> dict[str, list[str]]:
    g = inv.g
    injure, rescue, inputs, access = [], [], [], []
    for p in sorted(inv.cone.paths, key=lambda p: -p.potential):
        if len(p.hops) != 1:
            continue
        e, ent = p.hops[0].edge, g.entities[p.origin]
        label = f"{ent.name} ({e.relationship.value}, s={e.strength:.2f})"
        if ent.type in INPUT_TYPES or e.relationship in (EdgeType.SUPPLIES, EdgeType.DEPENDS_ON):
            inputs.append(label)
        if e.relationship in ACCESS_EDGES or ent.type in GOV:
            access.append(label)
        if e.sign < 0 or (e.sign > 0 and e.strength >= 0.5):
            injure.append(label)
        if e.sign > 0 and (ent.type in GOV or ent.type in {NodeType.BANK, NodeType.FUND}
                           or e.relationship in (EdgeType.SUBSIDIZES, EdgeType.CONTRACTS_WITH, EdgeType.LENDS_TO)):
            rescue.append(label)
    for pol in g.policies.values():
        ag = g.entities.get(pol.agency or "")
        if ag:
            for lever in ag.attrs.get("levers", []):
                if inv.focal in lever.get("protects", []) and inv.focal in lever.get("benefits", []):
                    rescue.append(f"{ag.name}: {lever['action']}")
    rescue = list(dict.fromkeys(rescue))
    winners = [f"{r['entity']} (+{r['benefit']:.2f} via {'/'.join(r['mechanism'])})" for r in inv.failure_beneficiaries()]
    return {"Who can injure the company?": injure[:8], "Who can rescue it?": rescue[:6],
            "Who controls its inputs?": inputs[:8], "Who controls market access?": access[:6],
            "Who receives value if it loses?": winners[:6]}


def six_layers(inv: Investigation) -> dict[str, list[str]]:
    g = inv.g
    layers: dict[str, list[str]] = defaultdict(list)
    seen = set()
    for p in sorted(inv.cone.paths, key=lambda p: (len(p.hops), -p.potential)):
        if p.origin in seen:
            continue
        seen.add(p.origin)
        layers[g.layer_of(p.origin, p.hops[0].edge if len(p.hops) == 1 else None).value].append(
            f"{g.name(p.origin)} (hop {len(p.hops)}, potential {p.potential:.2f})")
    focal = g.entities[inv.focal]
    layers[Layer.INTERNAL.value].insert(0, f"Engine: {focal.attrs.get('engine', {}).get('how_it_makes_money', 'n/a')}")
    for a in g.assumptions.values():
        if a.security == inv.ticker:
            layers[Layer.EXPECTATIONS.value].append(f"{a.statement} [{a.holder}, crowdedness {a.crowdedness:.1f}]")
    a = focal.attrs
    layers[Layer.EXPECTATIONS.value].append(f"Coverage: {a.get('analyst_count')} analysts; "
                                            f"20d price trend {a.get('price_trend_20d', 0):+.0%}")
    return {k.value: layers.get(k.value, []) for k in Layer}


def render(inv: Investigation) -> str:
    g, ent = inv.g, inv.g.entities[inv.focal]
    eng = ent.attrs.get("engine", {})
    L = [f"# Causal dossier: {ent.name} ({inv.ticker})", "",
         f"_As of {inv.as_of.isoformat()} · {ent.attrs.get('provenance', '')} · "
         f"cone: {len(inv.cone.paths)} paths over {len(inv.cone.hop_of) - 1} entities_", ""]

    L += ["## Company engine", ""]
    for k, label in [("how_it_makes_money", "How does it really make money?"),
                     ("production_constraint", "What constrains production?"),
                     ("who_ultimately_pays", "Who ultimately pays?"),
                     ("pricing_power", "Where does pricing power come from?"),
                     ("valuation_assumption", "Which assumption supports the valuation?")]:
        L.append(f"- **{label}** {eng.get(k, '_unknown_')}")
    L.append("")

    L += ["## What changed (claims, not articles)", "",
          "| Influence | Claim | Status | Evidence quality | Novelty | Docs | Path |",
          "|---:|---|---|---:|---:|---:|---|"]
    for r in inv.claim_impacts()[:8]:
        L.append(f"| {r['influence']:+.3f} | {r['claim']} | {r['status']} | {r['evidence_quality']:.2f} | "
                 f"{r['novelty']:.2f} | {r['n_documents']} | {' → '.join(r['path'])} |")
    L.append("")

    L += ["## Power map", ""]
    for q, items in power_map(inv).items():
        L.append(f"**{q}**")
        L += [f"- {i}" for i in items] or ["- _none identified_"]
        L.append("")

    L += ["## Six power layers", ""]
    for layer, items in six_layers(inv).items():
        L.append(f"**{layer}**")
        L += [f"- {i}" for i in items[:8]] or ["- _no entities in cone_"]
        L.append("")

    L += ["## Sensitivity table", "", "| Variable | Direction | Lag | Exposure | Confidence |", "|---|---|---|---|---|"]
    for r in sensitivity_table(inv):
        L.append(f"| {r['variable']} | {r['direction']} | {r['lag']} | {r['exposure']} | {r['confidence']} |")
    L.append("")

    L += ["## Current contradictions", ""]
    titles = {"management_vs_operations": "Management versus operational evidence",
              "valuation_vs_policy": "Valuation versus policy trajectory",
              "price_vs_positioning": "Price action versus capital positioning",
              "consensus_vs_supply_chain": "Consensus versus supply-chain evidence",
              "narrative_vs_evidence": "Market narrative versus evidence"}
    groups = by_kind(inv.contradictions)
    for kind, title in titles.items():
        L.append(f"### {title}")
        for cx in groups.get(kind, []):
            L.append(f"- **{cx.market_assumption}** — confidence {cx.confidence:.2f}, resolves in "
                     f"{cx.resolution_window} (`{cx.contradiction_id}`)")
            L += [f"  - BUT {o}" for o in cx.conflicting_observations]
        if kind not in groups:
            L.append("- _none detected_")
        L.append("")

    L += ["## Government pipeline", "", "| Policy | Stage | Appropriated | P(economic effect) | Expected | Gaps |",
          "|---|---|---|---:|---|---|"]
    for p in g.policies.values():
        if inv.focal in p.affects or any(k in p.affects for k in inv.cone.hop_of):
            d = p.to_dict(inv.as_of)
            L.append(f"| {p.name} | {d['stage']} | {d['appropriated']} | {d['p_economic_effect']:.2f} | "
                     f"{d['expected_effect_date']} | {'; '.join(d['gaps']) or '—'} |")
    L.append("")

    L += ["## Upcoming resolution points", ""]
    L += [f"- {u['date']} · {u['kind']} · {u['description']}" + (f" (tests: {', '.join(u['tests'])})" if u['tests'] else "")
          for u in inv.upcoming(days=900)]
    L.append("")

    L += ["## Signals exported to OptLab", "", "| Signal | Direction | Horizon | Conf | Materiality | Awareness |",
          "|---|---|---|---:|---:|---:|"]
    for s in inv.signals:
        L.append(f"| `{s['signal_type']}` | {s['direction']} | {s['horizon']} | {s['confidence']:.2f} | "
                 f"{s['expected_materiality']:.2f} | {s['market_awareness']:.2f} |")
    L.append("")
    return "\n".join(L)
