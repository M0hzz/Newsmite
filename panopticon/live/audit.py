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


def _plain(inputs: list[dict]) -> list[dict]:
    keys = ("obs_id", "concept", "accession", "start", "end", "val", "frame", "fy", "fp", "form", "filed", "class")
    seen, out = set(), []
    for i in inputs:
        if i["obs_id"] not in seen:
            seen.add(i["obs_id"])
            out.append({k: i.get(k) for k in keys})
    return out


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
                     "status": r.margin_status, "method": r.margin_formula or "gross profit / revenue",
                     "reasons": r.margin_reasons, "start": r.revenue.start if r.revenue else None, "days": None,
                     "inputs": _plain(r.margin_inputs), "concept": "(computed)", "unit": "ratio"})
        rows.append({"metric": "Days of inventory", "quarter_end": r.end, "value": r.dio,
                     "status": periods.TRUSTED if r.dio is not None else periods.QUARANTINED,
                     "method": r.dio_formula or "inventory / quarterly COGS x quarter days",
                     "reasons": [] if r.dio is not None else ["inventory or COGS quarter not trusted"],
                     "start": None, "days": None, "inputs": _plain(r.dio_inputs), "concept": "(computed)",
                     "unit": "days"})
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
            cells = [i.get("concept") or row["concept"], _m(i["val"]), row["unit"], i["start"] or "(instant)", i["end"], str(d or ""),
                     i["frame"] or "", str(i["fy"] or ""), i["fp"] or "", i["form"] or "", i["filed"] or "",
                     i["accession"], i["class"] or "", i["obs_id"]]
            L.append((base if k == 0 else "| ↳ | | | | |") + " " + " | ".join(cells) + " |")
        for why in row["reasons"]:
            L.append(f"| | | | | ⚠ {why} |" + " |" * 14)
    return "\n".join(L)


LABELS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("Revenue", "Net sales", "Net revenue"),
    "Revenues": ("Revenue", "Net sales", "Net revenue"),
    "GrossProfit": ("Gross margin", "Gross profit"),
    "CostOfGoodsAndServicesSold": ("Cost of goods sold", "Cost of sales"),
    "InventoryNet": ("Inventories", "Inventory"),
    "PaymentsToAcquirePropertyPlantAndEquipment": ("property, plant, and equipment", "property, plant and equipment",
                                                   "capital expenditures"),
}
NEGATIVE_AS_FILED = {"PaymentsToAcquirePropertyPlantAndEquipment"}   # shown in parentheses on the cash-flow statement
LEVELS = ("STATEMENT_LINE_MATCH", "LINE_ITEM_MATCH", "LABEL_ONLY", "NUMBER_ONLY", "MISSING")


def _long_date(iso: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}, {d.year}"


def _grade(text: str, i: dict, label_window: int = 160, header_window: int = 6000) -> tuple[str, str]:
    """Best evidence level for one fact across all occurrences of its value in the filing text."""
    needle = f"{abs(i['val']) / 1e6:,.0f}"
    pat = rf"\({re.escape(needle)}\)" if i["concept"] in NEGATIVE_AS_FILED else \
        rf"(?<![\d,(]){re.escape(needle)}(?![\d,)])"
    labels = LABELS.get(i["concept"], ())
    header = re.compile(r"\s+".join(re.escape(w) for w in _long_date(i["end"]).split()))  # headers wrap lines
    in_millions = re.search(r"in millions", text, flags=re.I) is not None
    best, ctx = "MISSING", ""
    for m in re.finditer(pat, text):
        before = re.sub(r"\s+", " ", text[max(0, m.start() - label_window): m.start()])
        has_label = any(lab.lower() in before.lower() for lab in labels)
        window = text[max(0, m.start() - header_window): m.start()]
        has_header = header.search(window) is not None
        # the nearest preceding statement heading must be closer than any section break (e.g. MD&A tables)
        near = text[max(0, m.start() - 3000): m.start()]
        titles = re.findall(r"Consolidated (?:Statements? of [A-Z][A-Za-z ,]+?|Balance Sheets?)(?=\n|\()", near)
        level = ("STATEMENT_LINE_MATCH" if has_label and has_header and in_millions and titles else
                 "LINE_ITEM_MATCH" if has_label and has_header and in_millions else
                 "LABEL_ONLY" if has_label else "NUMBER_ONLY")
        if LEVELS.index(level) < LEVELS.index(best):
            stmt = f"[{titles[-1].strip()}] " if titles else "[no statement heading nearby] "
            best, ctx = level, stmt + re.sub(r"\s+", " ", text[max(0, m.start() - 60): m.end() + 10])
        if best == "STATEMENT_LINE_MATCH":
            break
    return best, ctx


def source_check(store: Store, as_of: str | None = None) -> list[dict]:
    """Numeric source-presence check with line-item and period-header proximity.

    For each input fact of each trusted displayed value (including computed metrics), look for the value
    (in $ millions, as filed) in the text of the filing that reported it, near the expected line-item label
    and after the period's column-header date. This is a heuristic proximity test, NOT semantic statement
    reconciliation: statement, column and line placement still need a human read of the filing.
    """
    texts: dict[str, str | None] = {}
    out, seen = [], set()
    for row in audit_rows(store, as_of):
        if row["status"] != periods.TRUSTED:
            continue
        for i in row["inputs"]:
            if (i["obs_id"], row["metric"], row["quarter_end"]) in seen:
                continue
            seen.add((i["obs_id"], row["metric"], row["quarter_end"]))
            def text_of(acc: str) -> str | None:
                if acc not in texts:
                    docs = store.iter_rows("SELECT sha256 FROM documents WHERE accession=? AND form IN "
                                           "('10-Q','10-K','10-Q/A','10-K/A') LIMIT 1", (acc,))
                    texts[acc] = html_to_text(store.read_raw(docs[0]["sha256"])) if docs else None
                return texts[acc]

            acc = i["accession"]
            if text_of(acc) is not None:
                level, ctx = _grade(texts[acc], i)
            else:
                # Original filing not ingested: check later filings that repeated the identical fact
                # (prior-period comparative columns), and say so.
                level, ctx = "NOT_INGESTED", "source filing outside ingested window"
                fk = store.iter_rows("SELECT fact_key FROM fact_observations WHERE obs_id=?", (i["obs_id"],))
                for r in store.iter_rows("SELECT accession FROM fact_observations WHERE fact_key=? AND val=? AND "
                                         "accession<>? ORDER BY accepted_at", (fk[0][0], i["val"], acc)) if fk else []:
                    if text_of(r["accession"]) is not None:
                        lv, cx = _grade(texts[r["accession"]], i)
                        level, ctx = lv, f"via comparative column in {r['accession']}: {cx}"
                        acc = r["accession"]
                        break
            out.append({"metric": row["metric"], "quarter_end": row["quarter_end"], "concept": i["concept"],
                        "accession": acc, "fact": f"{i['start'] or 'instant'}..{i['end']}",
                        "value_millions": f"{abs(i['val']) / 1e6:,.0f}", "level": level, "context": ctx})
    return out


def table_fingerprint(store: Store, as_of: str | None = None) -> str:
    """Deterministic hash of the validated table — two clean rebuilds must produce the same value."""
    rows = [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in r.items() if k not in ("reasons",)}
            for r in audit_rows(store, as_of)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
