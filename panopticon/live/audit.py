"""XBRL truth audit: every displayed number traced to its SEC fact contexts, and cross-checked
against the text of the filing that reported it."""
from __future__ import annotations

import hashlib
import json
import re

from . import periods
from .edgar import html_to_text
from .store import Store

METRICS = [("Revenue", "revenue"), ("Gross profit", "gross_profit"), ("Cost of goods sold", "cogs"),
           ("Inventory", "inventory"), ("Capex", "capex")]
HEADER = ("| Metric | Quarter end | Value | Status | Method | Concept | Fact value | Unit | Start | End | Days | "
          "Frame | FY | FP | Form | Filed | Accession | Class | Obs id |")


def _m(x: float | None) -> str:
    return "—" if x is None else f"{x / 1e6:,.0f}M"


def audit_rows(store: Store, as_of: str | None = None, ends: list[str] | None = None) -> list[dict]:
    rows = []
    for r in periods.quarter_table(store, as_of):
        if ends and r.end not in ends:
            continue
        for label, attr in METRICS:
            v = getattr(r, attr)
            if v is None:
                continue
            rows.append({"metric": label, "quarter_end": r.end, "value": v.val, "status": v.status,
                         "method": v.method, "reasons": v.reasons,
                         "start": v.start, "days": v.days, "inputs": v.to_row()["inputs"], "concept": v.concept,
                         "unit": v.unit})
        rows.append({"metric": "Gross margin", "quarter_end": r.end, "value": r.margin,
                     "status": r.margin_status, "method": "gross profit / revenue", "reasons": r.margin_reasons,
                     "start": r.revenue.start if r.revenue else None, "days": None, "inputs": [], "concept": "",
                     "unit": "ratio"})
        rows.append({"metric": "Days of inventory", "quarter_end": r.end, "value": r.dio,
                     "status": periods.TRUSTED if r.dio is not None else periods.QUARANTINED,
                     "method": "inventory / quarterly COGS x quarter days", "reasons": [] if r.dio is not None else
                     ["inventory or COGS quarter not trusted"], "start": None, "days": None, "inputs": [],
                     "concept": "", "unit": "days"})
    return rows


def render_audit(store: Store, as_of: str | None = None, ends: list[str] | None = None) -> str:
    rows = audit_rows(store, as_of, ends)
    L = ["# XBRL truth audit", "", f"_as of {as_of or 'latest'}; periods: {', '.join(ends) if ends else 'all'}_", "",
         HEADER, "|" + "---|" * (HEADER.count("|") - 1)]
    for row in rows:
        val = (f"{row['value']:.1%}" if row["unit"] == "ratio" and row["value"] is not None else
               f"{row['value']:.0f}" if row["unit"] == "days" and row["value"] is not None else
               _m(row["value"]) if row["unit"] == "USD" else "—")
        base = f"| {row['metric']} | {row['quarter_end']} | {val} | {row['status']} | {row['method']} |"
        if not row["inputs"]:
            L.append(base + " " + " | ".join([""] * 14) + " |")
        for k, i in enumerate(row["inputs"]):
            d = None if not i["start"] else periods.days(i)
            cells = [row["concept"], _m(i["val"]), row["unit"], i["start"] or "(instant)", i["end"], str(d or ""),
                     i["frame"] or "", str(i["fy"] or ""), i["fp"] or "", i["form"] or "", i["filed"] or "",
                     i["accession"], i["class"] or "", i["obs_id"]]
            L.append((base if k == 0 else "| ↳ | | | | |") + " " + " | ".join(cells) + " |")
        for why in row["reasons"]:
            L.append(f"| | | | | ⚠ {why} |" + " |" * 14)
    return "\n".join(L)


def reconcile(store: Store, as_of: str | None = None) -> list[dict]:
    """For every input fact of every trusted table value, find the number (in $ millions, as filed) in the
    text of the filing that reported it."""
    texts: dict[str, str | None] = {}
    out = []
    for row in audit_rows(store, as_of):
        if row["status"] != periods.TRUSTED or not row["inputs"]:
            continue
        for i in row["inputs"]:
            acc = i["accession"]
            if acc not in texts:
                docs = store.iter_rows("SELECT sha256 FROM documents WHERE accession=? AND form IN "
                                       "('10-Q','10-K','10-Q/A','10-K/A') LIMIT 1", (acc,))
                texts[acc] = html_to_text(store.read_raw(docs[0]["sha256"])) if docs else None
            t = texts[acc]
            needle = f"{abs(i['val']) / 1e6:,.0f}"
            if t is None:
                found, ctx = None, "source filing outside ingested window"
            else:
                m = re.search(rf"(?<![\d,]){re.escape(needle)}(?![\d,])", t)
                found = m is not None
                ctx = re.sub(r"\s+", " ", t[max(0, m.start() - 70): m.end() + 10]) if m else ""
            out.append({"metric": row["metric"], "quarter_end": row["quarter_end"], "accession": acc,
                        "fact": f"{i['start'] or ''}..{i['end']}", "value_millions": needle, "found_in_filing": found,
                        "context": ctx})
    return out


def table_fingerprint(store: Store, as_of: str | None = None) -> str:
    """Deterministic hash of the validated table — two clean rebuilds must produce the same value."""
    rows = [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k not in ("reasons",)}
            for r in audit_rows(store, as_of)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
