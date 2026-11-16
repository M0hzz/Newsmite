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

from . import periods
from .edgar import html_to_text
from .store import Store, sha256, short_hash

INVENTORY, PRICING_MARGIN, CAPACITY_CAPEX = "INVENTORY", "PRICING_MARGIN", "CAPACITY_CAPEX"
GEO_REGULATORY, INSIDER, CORPORATE_EVENT = "GEO_REGULATORY", "INSIDER", "CORPORATE_EVENT"



def _b(x: float) -> str:
    return f"${x / 1e9:,.2f}B"


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def _value_evidence(store: Store, values: list) -> list[dict]:
    """One evidence entry per SEC fact context used, with how it was combined."""
    ev, seen = [], set()
    for v in values:
        formula = {"direct": "direct quarterly context", "direct+ytd_check": "direct quarterly context "
                   "(cross-checked against cumulative YTD difference)", "ytd_diff": "derived: later cumulative "
                   "context minus earlier cumulative context", "instant": "instant balance"}[v.method]
        for i in v.inputs:
            if i["obs_id"] in seen:
                continue
            seen.add(i["obs_id"])
            doc = store.iter_rows("SELECT doc_id, url FROM documents WHERE accession=? LIMIT 1", (i["accession"],))
            passage = (f"XBRL us-gaap:{i['concept']} = {i['val']:,.0f} {i['unit']} for "
                       f"{(i['start'] + ' to ') if i['start'] else ''}{i['end']} [{i.get('class')}] "
                       f"(accession {i['accession']}; used as {formula} for {v.concept} {v.end})")
            ev.append({"type": "xbrl_fact", "obs_id": i["obs_id"], "accession": i["accession"],
                       "concept": i["concept"], "unit": i["unit"], "start": i["start"], "end": i["end"],
                       "value": i["val"], "frame": i.get("frame"), "fy": i.get("fy"), "fp": i.get("fp"),
                       "form": i["form"], "filed": i["filed"], "class": i.get("class"),
                       "report_period": i.get("report_period"), "accepted_at": i["accepted_at"],
                       "derivation": {"metric": v.concept, "period_end": v.end, "method": v.method},
                       "doc_id": doc[0]["doc_id"] if doc else None, "url": doc[0]["url"] if doc else None,
                       "passage": passage, "passage_sha256": sha256(passage.encode())})
    return ev


def _adjacent(prev_end: str, cur_end: str, lo: int = 84, hi: int = 100) -> bool:
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
def _trusted(v) -> bool:
    return v is not None and v.status == periods.TRUSTED


def inventory_claims(store: Store, subject: str, as_of: str | None = None) -> int:
    rows = periods.quarter_table(store, as_of)
    inv = [r for r in rows if _trusted(r.inventory)]
    n = 0
    for prev, cur in zip(inv, inv[1:]):
        if not _adjacent(prev.end, cur.end):
            continue  # never compare against a non-adjacent period
        a, b = cur.inventory, prev.inventory
        chg = a.val / b.val - 1
        stmt = f"Inventory {_b(a.val)} at {cur.end}, {_pct(chg)} vs {prev.end}"
        used = [a, b]
        if cur.dio is not None and prev.dio is not None:
            stmt += f"; days of inventory {cur.dio:.0f} vs {prev.dio:.0f}"
            if cur.cogs.days != 91 or prev.cogs.days != 91:
                stmt += f" (quarters of {cur.cogs.days} and {prev.cogs.days} days)"
            used += [cur.cogs, prev.cogs]
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(INVENTORY, subject, cur.end, a.val, b.val, cur.dio and round(cur.dio)),
            "family": INVENTORY, "subject": subject, "metric": "inventory", "statement": stmt,
            "change": round(chg, 4), "event_time": cur.end, "available_at": max(v.available_at for v in used),
            "evidence": _value_evidence(store, used)})
    return n


def margin_claims(store: Store, subject: str, as_of: str | None = None) -> int:
    rows = [r for r in periods.quarter_table(store, as_of) if r.margin_status == periods.TRUSTED]
    n = 0
    for prev, cur in zip(rows, rows[1:]):
        if not _adjacent(prev.end, cur.end):
            continue
        delta = cur.margin - prev.margin
        derived = any(v.method == "ytd_diff" for v in (cur.revenue, cur.gross_profit))
        stmt = (f"Gross margin {cur.margin:.1%} in quarter ending {cur.end} vs {prev.margin:.1%} prior quarter "
                f"({delta * 1e4:+.0f} bp); revenue {_b(cur.revenue.val)}"
                + (" (quarter derived: fiscal-year total minus nine-month YTD)" if derived else ""))
        used = [cur.revenue, cur.gross_profit] + ([cur.cogs] if _trusted(cur.cogs) else [])
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(PRICING_MARGIN, subject, cur.end, round(cur.margin, 6),
                                           round(prev.margin, 6)),
            "family": PRICING_MARGIN, "subject": subject, "metric": "gross_margin", "statement": stmt,
            "change": round(delta, 4), "event_time": cur.end,
            "available_at": max(v.available_at for v in used + [prev.revenue, prev.gross_profit]),
            "evidence": _value_evidence(store, used + [prev.revenue, prev.gross_profit])})
    return n


def capex_claims(store: Store, subject: str, as_of: str | None = None) -> int:
    cx = {e: v for e, v in periods.quarters(store, "PaymentsToAcquirePropertyPlantAndEquipment", as_of).items()
          if _trusted(v)}
    ends = list(cx)
    n = 0
    for i in range(1, len(ends)):
        cur, prev = cx[ends[i]], cx[ends[i - 1]]
        if not _adjacent(prev.end, cur.end) or not prev.val:
            continue
        chg = cur.val / prev.val - 1
        stmt = f"Capex {_b(cur.val)} in quarter ending {cur.end}, {_pct(chg)} QoQ"
        used = [cur, prev]
        yago = next((cx[e] for e in ends[:i] if _adjacent(e, cur.end, 350, 380)), None)
        if yago and yago.val:
            stmt += f", {_pct(cur.val / yago.val - 1)} YoY"
            used.append(yago)
        if cur.method == "ytd_diff":
            stmt += " (derived from year-to-date cash-flow figures)"
        n += _propose(store, {
            "claim_id": "cc_" + short_hash(CAPACITY_CAPEX, subject, cur.end, cur.val, prev.val),
            "family": CAPACITY_CAPEX, "subject": subject, "metric": "capex", "statement": stmt,
            "change": round(chg, 4), "event_time": cur.end, "available_at": max(v.available_at for v in used),
            "evidence": _value_evidence(store, used)})
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
