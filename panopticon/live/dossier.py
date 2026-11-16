"""Live dossier: what the SEC record says, what has been human-verified, and what has not."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from ..investigations import Investigation
from ..dossier import power_map
from .extract import _revenue, quarterly
from .livegraph import build
from .store import Store


def _b(x: float | None) -> str:
    return "—" if x is None else f"${x / 1e9:,.2f}B"


def metrics_table(store: Store, as_of: str, n: int = 6) -> list[str]:
    rev, gp = _revenue(store, as_of), quarterly(store, "GrossProfit", as_of)
    cogs = quarterly(store, "CostOfGoodsAndServicesSold", as_of)
    capex = quarterly(store, "PaymentsToAcquirePropertyPlantAndEquipment", as_of)
    inv = {r["end"]: r for r in store.fact_series("InventoryNet", as_of=as_of, duration="instant")}
    ends = sorted(set(rev) | set(inv))[-n:]
    L = ["| Quarter end | Revenue | Gross margin | Inventory | Days of inventory | Capex | First public |",
         "|---|---:|---:|---:|---:|---:|---|"]
    for e in ends:
        r = rev.get(e, {}).get("val")
        g = gp.get(e, {}).get("val")
        i = inv.get(e, {}).get("val")
        c = cogs.get(e, {}).get("val")
        first = min(x for x in [rev.get(e, {}).get("accepted_at"), inv.get(e, {}).get("accepted_at")] if x)
        L.append(f"| {e} | {_b(r)} | {f'{g / r:.1%}' if r and g else '—'} | {_b(i)} | "
                 f"{f'{i / c * 91:.0f}' if i and c else '—'} | {_b(capex.get(e, {}).get('val'))} | {first[:10]} |")
    return L


def render(store: Store, relationships: dict, ticker: str, as_of: str) -> str:
    focal = next(e for e in relationships["entities"] if e["attrs"].get("ticker") == ticker)
    st = store.stats()
    all_claims = [c for c in store.claims() if c["available_at"] <= as_of]
    by_status = Counter(c["status"] for c in all_claims)
    active = store.active_claims(as_of)
    L = [f"# Live dossier: {focal['name']} ({ticker})", "",
         f"_As of {as_of} · CIK {focal['attrs'].get('cik')} · Tier-1 source: SEC EDGAR · "
         f"{st['documents']} documents stored · claims: {by_status.get('approved', 0)} approved, "
         f"{by_status.get('pending', 0)} pending review, {by_status.get('rejected', 0)} rejected_", "",
         "> Only **human-approved** claims below are part of the graph state. Relationship edges marked "
         "*unverified* are drafts: they are shown for orientation and cannot produce OptLab packets.", ""]

    L += ["## Reported fundamentals (XBRL, point-in-time)", ""] + metrics_table(store, as_of) + [""]

    L += ["## Approved claims (active graph state)", ""]
    if not active:
        L += ["_None yet. Review candidates with `python3 -m panopticon live review`._", ""]
    fam = defaultdict(list)
    for c in active:
        fam[c["family"]].append(c)
    for f, cs in sorted(fam.items()):
        L.append(f"### {f}")
        for c in sorted(cs, key=lambda c: c["event_time"], reverse=True)[:12]:
            src = c["evidence"][0]
            L.append(f"- {c['statement']}  \n  _period {c['event_time']} · public {c['available_at']} · "
                     f"approved by {c['reviewed_by']} · [{src['accession']}]({src.get('url')})_")
        L.append("")

    pend = [c for c in all_claims if c["status"] == "pending"]
    L += ["## Awaiting human review", ""]
    for f, k in Counter(c["family"] for c in pend).most_common():
        L.append(f"- {f}: {k}")
    L += [""] if pend else ["- nothing pending", ""]

    cutoff = (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tx = [t for t in store.txns() if cutoff <= t["accepted_at"] <= as_of]
    L += ["## Insider activity, trailing 90 days (Form 4, all filings, unreviewed)", "",
          "| Kind | Transactions | Shares | Value | Under 10b5-1 plan |", "|---|---:|---:|---:|---:|"]
    agg: dict[str, list] = defaultdict(lambda: [0, 0.0, 0.0, 0])
    for t in tx:
        a = agg[t["kind"]]
        a[0] += 1
        a[1] += t["shares"] or 0
        a[2] += (t["shares"] or 0) * (t["price"] or 0)
        a[3] += t["plan_10b5_1"]
    for k, (n, sh, v, plan) in sorted(agg.items(), key=lambda kv: -kv[1][2]):
        L.append(f"| {k} | {n} | {sh:,.0f} | ${v / 1e6:,.1f}M | {plan} |")
    L.append("")

    L += ["## Relationship map", ""]
    edges = relationships["edges"]
    nv = sum(e["verified"] for e in edges)
    L.append(f"{nv} of {len(edges)} edges verified.")
    L += ["", "| Edge | Relationship | Strength | Status | Evidence |", "|---|---|---:|---|---|"]
    names = {e["id"]: e["name"] for e in relationships["entities"]}
    for e in edges:
        L.append(f"| `{e['id']}` {names[e['source']]} → {names[e['target']]} | {e['relationship']} | "
                 f"{e['strength']:.2f} | {'verified by ' + e['verified_by'] if e['verified'] else 'unverified draft'} | "
                 f"{'; '.join(e.get('evidence', [])) or '—'} |")
    L.append("")

    g = build(store, relationships, as_of)
    inv = Investigation(g, ticker, datetime.fromisoformat(as_of.replace("Z", "+00:00")).date())
    L += ["## Power map (structure from the draft map)", ""]
    for q, items in power_map(inv).items():
        L.append(f"**{q}**")
        L += [f"- {i}" for i in items] or ["- _none_"]
        L.append("")
    L += ["## OptLab", "",
          f"{len(inv.signals)} packet(s) eligible. Packets require approved claims travelling over *verified* edges "
          f"(currently {nv} verified).", ""]
    return "\n".join(L)
