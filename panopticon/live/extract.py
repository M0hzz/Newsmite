"""Candidate-claim extraction from the evidence store.

Nothing here confirms anything. Every output is a *candidate* with status 'pending',
a deterministic id (so re-running cannot duplicate), the economic time it describes,
the acceptance time it became knowable, and the exact evidence it rests on.

Claim families (first live slice):
  INVENTORY        inventory level, QoQ change, days of inventory
  PRICING_MARGIN   gross margin and its quarter-over-quarter change
  CAPACITY_CAPEX   quarterly capital expenditure
  GEO_REGULATORY   10-K disclosures on China/CAC, export controls, CHIPS Act, tariffs, geography
  INSIDER          Form 4 transactions, classified (open-market vs grant vs tax withholding ...)
  CORPORATE_EVENT  selected 8-K items (outside the five MVP families; reviewable, never auto-signals)
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from .edgar import html_to_text
from .store import Store, sha256, short_hash

INVENTORY, PRICING_MARGIN, CAPACITY_CAPEX = "INVENTORY", "PRICING_MARGIN", "CAPACITY_CAPEX"
GEO_REGULATORY, INSIDER, CORPORATE_EVENT = "GEO_REGULATORY", "INSIDER", "CORPORATE_EVENT"

REVENUE_CONCEPTS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues")


def _b(x: float) -> str:
    return f"${x / 1e9:,.2f}B"


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def _fact_evidence(store: Store, rows: list[dict], note: str = "") -> list[dict]:
    ev = []
    for r in rows:
        doc = store.iter_rows("SELECT doc_id, url FROM documents WHERE accession=? LIMIT 1", (r["accession"],))
        passage = (f"XBRL us-gaap:{r['concept']} = {r['val']:,.0f} {r['unit']} for "
                   f"{(r['start'] + ' to ') if r['start'] else ''}{r['end']} (accession {r['accession']}){note}")
        ev.append({"type": "xbrl_fact", "accession": r["accession"], "concept": r["concept"], "unit": r["unit"],
                   "start": r["start"], "end": r["end"], "value": r["val"], "form": r["form"],
                   "accepted_at": r["accepted_at"], "doc_id": doc[0]["doc_id"] if doc else None,
                   "url": doc[0]["url"] if doc else
                   f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&accession={r['accession']}",
                   "passage": passage, "passage_sha256": sha256(passage.encode())})
    return ev


def _adjacent(prev_end: str, cur_end: str, lo: int = 80, hi: int = 100) -> bool:
    return lo <= (date.fromisoformat(cur_end) - date.fromisoformat(prev_end)).days <= hi


def _propose(store: Store, c: dict) -> bool:
    """Insert a candidate; link it to the claim it updates (same subject/metric/period, different content)."""
    prior = store.iter_rows(
        "SELECT claim_id FROM candidate_claims WHERE subject=? AND metric=? AND event_time=? AND claim_id<>? "
        "AND available_at<=? ORDER BY available_at DESC LIMIT 1",
        (c["subject"], c["metric"], c["event_time"], c["claim_id"], c["available_at"]))
    if prior and not c.get("supersedes"):
        c["supersedes"] = prior[0]["claim_id"]
    return store.put_candidate(c)


# ---------------------------------------------------------------- quarterly series
def quarterly(store: Store, concept: str, as_of: str | None = None) -> dict[str, dict]:
    """end -> {val, start, end, rows}. Uses direct ~90-day facts, else differences of year-to-date facts."""
    rows = store.fact_series(concept, as_of=as_of, duration="ytd")
    out: dict[str, dict] = {}
    for r in rows:
        if 80 <= r["days"] <= 100:
            out[r["end"]] = {"val": r["val"], "start": r["start"], "end": r["end"], "rows": [r],
                             "accepted_at": r["accepted_at"]}
    by_start: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_start[r["start"]].append(r)
    for start, rs in by_start.items():
        rs = sorted(rs, key=lambda r: r["end"])
        for prev, cur in zip(rs, rs[1:]):
            q_days = (date.fromisoformat(cur["end"]) - date.fromisoformat(prev["end"])).days
            if cur["end"] in out or not 80 <= q_days <= 100:
                continue
            out[cur["end"]] = {"val": cur["val"] - prev["val"], "start": prev["end"], "end": cur["end"],
                               "rows": [cur, prev], "derived": True,
                               "accepted_at": max(cur["accepted_at"], prev["accepted_at"])}
    return dict(sorted(out.items()))


def _revenue(store: Store, as_of: str | None = None) -> dict[str, dict]:
    for c in REVENUE_CONCEPTS:
        q = quarterly(store, c, as_of)
        if q:
            return q
    return {}


# ---------------------------------------------------------------- families
def inventory_claims(store: Store, subject: str, as_of: str | None = None) -> int:
    inv = store.fact_series("InventoryNet", as_of=as_of, duration="instant")
    cogs = quarterly(store, "CostOfGoodsAndServicesSold", as_of)
    n = 0
    for prev, cur in zip(inv, inv[1:]):
        if not _adjacent(prev["end"], cur["end"]):
            continue  # never compare against a non-adjacent period
        chg = cur["val"] / prev["val"] - 1
        stmt = f"Inventory {_b(cur['val'])} at {cur['end']}, {_pct(chg)} vs {prev['end']}"
        rows = [cur, prev]
        if cur["end"] in cogs and prev["end"] in cogs and cogs[cur["end"]]["val"] > 0:
            dio = cur["val"] / cogs[cur["end"]]["val"] * 91
            dio_prev = prev["val"] / cogs[prev["end"]]["val"] * 91
            stmt += f"; days of inventory {dio:.0f} vs {dio_prev:.0f}"
            rows += cogs[cur["end"]]["rows"] + cogs[prev["end"]]["rows"]
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(INVENTORY, subject, cur["end"], cur["val"], prev["val"]),
            "family": INVENTORY, "subject": subject, "metric": "inventory", "statement": stmt, "change": round(chg, 4),
            "event_time": cur["end"], "available_at": cur["accepted_at"], "evidence": _fact_evidence(store, rows)})
    return n


def margin_claims(store: Store, subject: str, as_of: str | None = None) -> int:
    rev, gp = _revenue(store, as_of), quarterly(store, "GrossProfit", as_of)
    ends = [e for e in rev if e in gp and rev[e]["val"] > 0]
    n = 0
    for prev, cur in zip(ends, ends[1:]):
        if not _adjacent(prev, cur):
            continue
        gm, gm_prev = gp[cur]["val"] / rev[cur]["val"], gp[prev]["val"] / rev[prev]["val"]
        delta = gm - gm_prev
        note = " (Q derived from year-to-date difference)" if gp[cur].get("derived") or rev[cur].get("derived") else ""
        stmt = (f"Gross margin {gm:.1%} in quarter ending {cur} vs {gm_prev:.1%} prior quarter "
                f"({delta * 1e4:+.0f} bp); revenue {_b(rev[cur]['val'])}{note}")
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(PRICING_MARGIN, subject, cur, round(gm, 6), round(gm_prev, 6)),
            "family": PRICING_MARGIN, "subject": subject, "metric": "gross_margin", "statement": stmt,
            "change": round(delta, 4), "event_time": cur,
            "available_at": max(gp[cur]["accepted_at"], rev[cur]["accepted_at"]),
            "evidence": _fact_evidence(store, gp[cur]["rows"] + rev[cur]["rows"], note)})
    return n


def capex_claims(store: Store, subject: str, as_of: str | None = None) -> int:
    cx = quarterly(store, "PaymentsToAcquirePropertyPlantAndEquipment", as_of)
    ends = list(cx)
    n = 0
    for i in range(1, len(ends)):
        cur, prev = cx[ends[i]], cx[ends[i - 1]]
        if not _adjacent(prev["end"], cur["end"]):
            continue
        chg = cur["val"] / prev["val"] - 1 if prev["val"] else 0.0
        stmt = f"Capex {_b(cur['val'])} in quarter ending {cur['end']}, {_pct(chg)} QoQ"
        yago = next((cx[e] for e in ends[:i] if _adjacent(e, cur["end"], 350, 380)), None)
        if yago and yago["val"]:
            stmt += f", {_pct(cur['val'] / yago['val'] - 1)} YoY"
        if cur.get("derived"):
            stmt += " (derived from year-to-date cash-flow figures)"
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(CAPACITY_CAPEX, subject, cur["end"], cur["val"], prev["val"]),
            "family": CAPACITY_CAPEX, "subject": subject, "metric": "capex", "statement": stmt,
            "change": round(chg, 4), "event_time": cur["end"], "available_at": cur["accepted_at"],
            "evidence": _fact_evidence(store, cur["rows"] + prev["rows"])})
    return n


def _insider_change(t) -> float:
    """Only discretionary open-market activity carries information; grants and tax withholding do not."""
    if t["kind"] == "open_market_sale":
        return -0.02 if t["plan_10b5_1"] else -0.10
    if t["kind"] == "open_market_purchase":
        return 0.10
    return 0.0


def insider_claims(store: Store, subject: str) -> int:
    amended = {r["amends_accession"] for r in store.documents() if r["amends_accession"]}
    n = 0
    for doc in store.documents():
        if doc["form"].split("/")[0] != "4" or doc["accession"] in amended:
            continue  # an amended Form 4 is represented by its amendment, not counted twice
        txns = store.txns(doc["accession"])
        if not txns:
            continue
        parts, change = [], 0.0
        for t in txns:
            val = (t["shares"] or 0) * (t["price"] or 0)
            parts.append(f"{t['kind'].replace('_', ' ')} {t['shares'] or 0:,.0f} sh"
                         + (f" @ ${t['price']:,.2f} (${val / 1e6:,.1f}M)" if t["price"] else "")
                         + (" [10b5-1 plan]" if t["plan_10b5_1"] else "")
                         + (" [derivative]" if t["derivative"] else ""))
            change += _insider_change(t)
        who = txns[0]["owner_name"] + (f" ({txns[0]['officer_title']})" if txns[0]["officer_title"] else
                                       " (director)" if txns[0]["is_director"] else "")
        stmt = f"Form 4: {who}: " + "; ".join(parts)
        ev = [{"type": "form4", "accession": doc["accession"], "doc_id": doc["doc_id"], "url": doc["url"],
               "accepted_at": doc["accepted_at"], "transactions": [dict(t) for t in txns],
               "passage": stmt, "passage_sha256": sha256(stmt.encode())}]
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(INSIDER, doc["accession"]), "family": INSIDER, "subject": subject,
            "metric": "insider_flow", "statement": stmt, "change": round(max(-0.3, min(0.3, change)), 4),
            "event_time": max(t["txn_date"] or doc["period"] or "" for t in txns),
            "available_at": doc["accepted_at"], "evidence": ev})
    return n


EIGHT_K_ITEMS = {
    "1.01": "Entry into a material definitive agreement", "1.02": "Termination of a material agreement",
    "2.01": "Completion of acquisition or disposition", "2.02": "Results of operations",
    "2.03": "Creation of a direct financial obligation", "2.05": "Exit or disposal (restructuring) costs",
    "2.06": "Material impairment", "3.01": "Delisting / listing-rule notice", "4.02": "Non-reliance on prior financials",
    "5.02": "Departure / appointment of directors or officers", "8.01": "Other events",
}


def _item_passage(text: str, item: str, limit: int = 700) -> tuple[str, int]:
    m = re.search(rf"Item\s*{re.escape(item)}\.?", text, flags=re.I)
    if not m:
        return "", -1
    body = text[m.end(): m.end() + limit * 2]
    nxt = re.search(r"\nItem\s*\d\.\d\d", body)
    body = body[: nxt.start()] if nxt else body
    return re.sub(r"\s+", " ", body).strip()[:limit], m.start()


def eight_k_claims(store: Store, subject: str) -> int:
    n = 0
    for doc in store.documents():
        if doc["form"].split("/")[0] != "8-K" or not doc["items"]:
            continue
        text = None
        for item in doc["items"].split(","):
            item = item.strip()
            if item not in EIGHT_K_ITEMS:
                continue
            text = text if text is not None else html_to_text(store.read_raw(doc["sha256"]))
            passage, offset = _item_passage(text, item)
            stmt = f"8-K Item {item} ({EIGHT_K_ITEMS[item]}) filed {doc['filing_date']}"
            ev = [{"type": "8k_item", "accession": doc["accession"], "doc_id": doc["doc_id"], "url": doc["url"],
                   "accepted_at": doc["accepted_at"], "doc_sha256": doc["sha256"], "item": item,
                   "text_offset": offset, "passage": passage, "passage_sha256": sha256(passage.encode())}]
            n += _propose(store, {
                "claim_id": "cc_" + short_hash(CORPORATE_EVENT, doc["accession"], item), "family": CORPORATE_EVENT,
                "subject": subject, "metric": f"8k_{item}", "statement": stmt, "change": 0.0,
                "event_time": doc["period"] or doc["filing_date"], "available_at": doc["accepted_at"], "evidence": ev})
    return n


EXPOSURE_PATTERNS = {
    "china_cac": r"Cyberspace Administration of China",
    "export_controls": r"export control|Entity List|Bureau of Industry and Security",
    "chips_act": r"CHIPS (?:and Science )?Act|CHIPS Program Office",
    "tariffs": r"\btariffs?\b",
    "taiwan": r"\bTaiwan\b",
    "japan": r"\bJapan(?:ese)? government\b|\bHiroshima\b",
    "customer_concentration": r"(?:top|largest) (?:five|ten|\d+) customers|accounted for (?:approximately )?\d+% of (?:our )?(?:total )?(?:net )?(?:sales|revenue)",
}


def _sentences(text: str):
    pos = 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z])|\n", text):
        yield pos, text[pos:m.start()]
        pos = m.end()
    yield pos, text[pos:]


def exposure_claims(store: Store, subject: str, per_pattern: int = 3) -> int:
    """10-K disclosure passages. Identical wording across years is one claim (same id); changed wording is new."""
    n = 0
    tenks = [d for d in store.documents() if d["form"].split("/")[0] == "10-K"]
    for idx, doc in enumerate(tenks):
        text = html_to_text(store.read_raw(doc["sha256"]))
        hits: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for off, sent in _sentences(text):
            s = re.sub(r"\s+", " ", sent).strip()
            if not 60 <= len(s) <= 900:
                continue
            for label, pat in EXPOSURE_PATTERNS.items():
                if len(hits[label]) < per_pattern and re.search(pat, s, flags=re.I):
                    hits[label].append((off, s))
        for label, found in hits.items():
            for off, s in found:
                psha = sha256(s.encode())
                cid = "cc_" + short_hash(GEO_REGULATORY, subject, label, psha)
                novelty = "first 10-K ingested" if idx == 0 else "new or changed wording vs earlier 10-K"
                ev = [{"type": "10k_passage", "accession": doc["accession"], "doc_id": doc["doc_id"],
                       "url": doc["url"], "accepted_at": doc["accepted_at"], "doc_sha256": doc["sha256"],
                       "text_offset": off, "passage": s, "passage_sha256": psha}]
                n += store.put_candidate({
                    "claim_id": cid, "family": GEO_REGULATORY, "subject": subject, "metric": f"exposure:{label}",
                    "statement": f"10-K ({doc['period']}) disclosure, {label.replace('_', ' ')} [{novelty}]: "
                                 f"{s[:220]}{'…' if len(s) > 220 else ''}",
                    "change": 0.0, "event_time": doc["period"] or doc["filing_date"],
                    "available_at": doc["accepted_at"], "evidence": ev})
    return n


def extract_all(store: Store, subject: str, since: str | None = None) -> dict[str, int]:
    """``since``: only propose claims that became public on/after this date (older facts are context)."""
    real_put = store.put_candidate

    def gated(c: dict) -> bool:
        return False if since and c["available_at"] < since else real_put(c)

    store.put_candidate = gated  # type: ignore[method-assign]
    try:
        return _extract_all(store, subject)
    finally:
        store.put_candidate = real_put  # type: ignore[method-assign]


def _extract_all(store: Store, subject: str) -> dict[str, int]:
    # Replay fact-based extraction at every moment new facts became public, so an original value
    # and its later restatement each get a claim (the newer one superseding the older).
    out = {INVENTORY: 0, PRICING_MARGIN: 0, CAPACITY_CAPEX: 0}
    moments = [r[0] for r in store.iter_rows("SELECT DISTINCT accepted_at FROM fact_observations ORDER BY 1")]
    for t in moments:
        out[INVENTORY] += inventory_claims(store, subject, t)
        out[PRICING_MARGIN] += margin_claims(store, subject, t)
        out[CAPACITY_CAPEX] += capex_claims(store, subject, t)
    out.update({
        INSIDER: insider_claims(store, subject),
        CORPORATE_EVENT: eight_k_claims(store, subject),
        GEO_REGULATORY: exposure_claims(store, subject),
    })
    store.commit()
    return out
