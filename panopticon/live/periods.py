"""XBRL period classification, quarter normalization and quarantine.

Every observation is classified purely from its dates (``fy``/``fp`` in SEC companyfacts describe
the *filing*, not the fact: a prior-year comparative in a FY2026 10-Q carries fy=2026):

  INSTANT          balance at a date (inventory)
  DIRECT_QUARTER   ~one fiscal quarter (84-100 days; 14-week quarters are 98 days)
  YEAR_TO_DATE     cumulative from a fiscal-year start, 6 or 9 months
  FISCAL_YEAR      a full fiscal year from a fiscal-year start (350-380 days)
  COMPARATIVE      any of the above reported in a filing whose own period is later (prior-period column)
  UNKNOWN          anything else; never used for claims or trusted metrics

Quarter flows (revenue, gross profit, cost of goods sold, capex):
  Q1 = direct Q1 (a quarter starting at the fiscal-year start)
  Q2 = 6M YTD - Q1;  Q3 = 9M YTD - 6M YTD;  Q4 = FY - 9M YTD
A valid direct-quarter context is preferred; when both direct and derived exist they must agree.
Every value carries the observation ids it was built from. Anything that fails a rule is
QUARANTINED: it stays as evidence and in the audit, but produces no claim, signal or trusted value.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .store import Store

INSTANT, DIRECT_QUARTER, YEAR_TO_DATE = "INSTANT", "DIRECT_QUARTER", "YEAR_TO_DATE"
FISCAL_YEAR, COMPARATIVE, UNKNOWN = "FISCAL_YEAR", "COMPARATIVE", "UNKNOWN"
TRUSTED, QUARANTINED = "trusted", "quarantined"

QUARTER_DAYS = (84, 100)
YTD_DAYS = ((170, 190), (260, 285))
FY_DAYS = (350, 380)
AGREE_TOL = 0.005          # direct vs derived, and the gross-profit identity: 0.5%
NONNEGATIVE = {"RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "CostOfGoodsAndServicesSold",
               "PaymentsToAcquirePropertyPlantAndEquipment", "InventoryNet"}
REVENUE_CONCEPTS = ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues")


def _d(s: str) -> date:
    return date.fromisoformat(s)


def days(o: dict) -> int | None:
    return None if not o.get("start") else (_d(o["end"]) - _d(o["start"])).days + 1  # inclusive


def fiscal_year_starts(obs: list[dict]) -> set[str]:
    """Starts of full-year or YTD contexts. A fiscal-year start is evidenced by a long context."""
    return {o["start"] for o in obs if o.get("start") and (days(o) or 0) >= 170}


def classify(o: dict, fy_starts: set[str]) -> str:
    n = days(o)
    if n is None:
        base = INSTANT
    elif QUARTER_DAYS[0] <= n <= QUARTER_DAYS[1]:
        base = DIRECT_QUARTER
    elif o["start"] in fy_starts and any(lo <= n <= hi for lo, hi in YTD_DAYS):
        base = YEAR_TO_DATE
    elif o["start"] in fy_starts and FY_DAYS[0] <= n <= FY_DAYS[1]:
        base = FISCAL_YEAR
    else:
        return UNKNOWN
    rp = o.get("report_period")
    if rp and o["end"] < rp and (_d(rp) - _d(o["end"])).days > 7:
        return COMPARATIVE
    return base


@dataclass
class Value:
    concept: str
    unit: str
    start: str | None          # inclusive
    end: str
    val: float | None
    method: str                # instant | direct | ytd_diff | direct+ytd_check
    inputs: list[dict]         # the observations used (with their classification)
    status: str = TRUSTED
    reasons: list[str] = field(default_factory=list)
    available_at: str = ""

    @property
    def days(self) -> int | None:
        return None if not self.start else (_d(self.end) - _d(self.start)).days + 1

    def quarantine(self, why: str) -> "Value":
        self.status = QUARANTINED
        self.reasons.append(why)
        return self

    def to_row(self) -> dict:
        return {"concept": self.concept, "unit": self.unit, "start": self.start, "end": self.end,
                "days": self.days, "value": self.val, "method": self.method, "status": self.status,
                "reasons": self.reasons, "available_at": self.available_at,
                "inputs": [{k: i.get(k) for k in ("obs_id", "accession", "start", "end", "val", "frame", "fy",
                                                   "fp", "form", "filed", "class")} for i in self.inputs]}


def _current(store: Store, concept: str, as_of: str | None) -> list[dict]:
    """Current value per fact context as of a time (first-availability preserved by the store)."""
    return store.fact_series(concept, as_of=as_of)


def observations(store: Store, concept: str, as_of: str | None = None) -> list[dict]:
    obs = _current(store, concept, as_of)
    fy = fiscal_year_starts(obs)
    for o in obs:
        o["class"] = classify(o, fy)
    return obs


def _primary(o: dict) -> str:
    """Base class ignoring COMPARATIVE (a comparative is still a valid context for its own period)."""
    return classify({**o, "report_period": None}, o["_fy"])


def quarters(store: Store, concept: str, as_of: str | None = None, unit: str = "USD") -> dict[str, Value]:
    """Normalized fiscal quarters keyed by quarter end; includes quarantined entries with reasons."""
    allobs = observations(store, concept, as_of)
    out: dict[str, Value] = {}
    units = {o["unit"] for o in allobs}
    obs = [o for o in allobs if o["unit"] == unit]
    fy = fiscal_year_starts(obs)
    for o in obs:
        o["_fy"] = fy
    usable = [o for o in obs if _primary(o) != UNKNOWN]

    # cumulative ladders per fiscal year: [(end, obs)] from the fiscal-year start
    ladders: dict[str, list[dict]] = defaultdict(list)
    direct: dict[str, list[dict]] = defaultdict(list)
    for o in usable:
        kind = _primary(o)
        if kind == DIRECT_QUARTER:
            direct[o["end"]].append(o)
            if o["start"] in fy:
                ladders[o["start"]].append(o)   # Q1: a quarter that starts the fiscal year
        elif kind in (YEAR_TO_DATE, FISCAL_YEAR):
            ladders[o["start"]].append(o)

    derived: dict[str, Value] = {}
    for start, rungs in ladders.items():
        by_end: dict[str, list[dict]] = defaultdict(list)
        for r in rungs:
            by_end[r["end"]].append(r)
        ends = sorted(by_end)
        for i, e in enumerate(ends):
            vals = {r["val"] for r in by_end[e]}
            cur = by_end[e][0]
            if len(vals) > 1:
                derived[e] = Value(concept, unit, None, e, None, "ytd_diff", by_end[e]).quarantine(
                    "multiple incompatible cumulative contexts for the same period")
                continue
            if i == 0:
                if QUARTER_DAYS[0] <= days(cur) <= QUARTER_DAYS[1]:
                    continue  # Q1 is a direct quarter; handled below
                derived[e] = Value(concept, unit, None, e, None, "ytd_diff", [cur]).quarantine(
                    f"missing derivation input: no earlier cumulative period from fiscal start {start}")
                continue
            prev = by_end[ends[i - 1]][0]
            gap = (_d(e) - _d(prev["end"])).days
            if not QUARTER_DAYS[0] <= gap <= QUARTER_DAYS[1]:
                derived[e] = Value(concept, unit, None, e, None, "ytd_diff", [cur, prev]).quarantine(
                    f"missing derivation input: previous cumulative ends {prev['end']} ({gap} days earlier)")
                continue
            q_start = (_d(prev["end"]) + timedelta(days=1)).isoformat()
            derived[e] = Value(concept, unit, q_start, e, cur["val"] - prev["val"], "ytd_diff", [cur, prev],
                               available_at=max(cur["accepted_at"], prev["accepted_at"]))

    for e in sorted(set(direct) | set(derived)):
        ds = direct.get(e, [])
        starts = {o["start"] for o in ds}
        if len(starts) > 1 or len({o["val"] for o in ds}) > 1:
            v = Value(concept, unit, None, e, None, "direct", ds).quarantine(
                "multiple incompatible direct-quarter contexts for the same quarter end")
        elif ds:
            o = ds[0]
            v = Value(concept, unit, o["start"], e, o["val"], "direct", [o], available_at=o["accepted_at"])
            dv = derived.get(e)
            if dv and dv.status == TRUSTED:
                v.method = "direct+ytd_check"
                v.inputs += dv.inputs
                if dv.start != v.start:
                    v.quarantine(f"direct quarter starts {v.start} but cumulative contexts imply {dv.start}")
                elif abs(dv.val - v.val) > AGREE_TOL * max(abs(v.val), 1):
                    v.quarantine(f"direct {v.val:,.0f} disagrees with cumulative derivation {dv.val:,.0f}")
        else:
            v = derived[e]
        if v.status == TRUSTED and concept in NONNEGATIVE and v.val is not None and v.val < 0:
            v.quarantine(f"nonsensical negative quarterly {concept}")
        if len(units) > 1:
            v.reasons.append(f"note: other units present and ignored: {sorted(units - {unit})}")
        out[e] = v
    return dict(sorted(out.items()))


def revenue_quarters(store: Store, as_of: str | None = None) -> dict[str, Value]:
    """Per quarter, the first revenue concept with a trusted value. Concepts are never mixed inside a derivation."""
    out: dict[str, Value] = {}
    for c in REVENUE_CONCEPTS:
        for e, v in quarters(store, c, as_of).items():
            if e not in out or (out[e].status != TRUSTED and v.status == TRUSTED):
                out[e] = v
    return dict(sorted(out.items()))


def instants(store: Store, concept: str, as_of: str | None = None, unit: str = "USD") -> dict[str, Value]:
    """Instant balances that sit on the reporting date of the filing that first reported them."""
    out: dict[str, Value] = {}
    # Valid balance dates: period ends of flow contexts and filing reporting dates.
    # Read every observation (not only first-seen rows): a filing that merely repeats a fact still
    # establishes its own reporting date.
    q = "SELECT DISTINCT report_period, \"end\", start FROM fact_observations WHERE 1=1"
    args: list = []
    if as_of:
        q += " AND accepted_at<=?"
        args.append(as_of)
    valid_dates: set[str] = set()
    for rp, e, st in store.iter_rows(q, args):
        if rp:
            valid_dates.add(rp)
        if st:
            valid_dates.add(e)
    by_end: dict[str, list[dict]] = defaultdict(list)
    for o in observations(store, concept, as_of):
        if o["unit"] == unit and o["start"] is None:
            by_end[o["end"]].append(o)
    for e, os_ in sorted(by_end.items()):
        if len({o["val"] for o in os_}) > 1:
            out[e] = Value(concept, unit, None, e, None, "instant", os_).quarantine(
                "multiple incompatible instant values for the same date")
            continue
        o = os_[0]
        v = Value(concept, unit, None, e, o["val"], "instant", [o], available_at=o["accepted_at"])
        rp = o.get("report_period")
        if e not in valid_dates:
            v.quarantine(f"instant date {e} is not a reporting-period end (filing period {rp})")
        if concept in NONNEGATIVE and v.val < 0:
            v.quarantine("nonsensical negative balance")
        out[e] = v
    return out


@dataclass
class QuarterRow:
    end: str
    revenue: Value | None
    gross_profit: Value | None
    cogs: Value | None
    inventory: Value | None
    capex: Value | None
    margin: float | None = None
    dio: float | None = None
    margin_status: str = QUARANTINED
    margin_reasons: list[str] = field(default_factory=list)


def quarter_table(store: Store, as_of: str | None = None) -> list[QuarterRow]:
    rev, gp = revenue_quarters(store, as_of), quarters(store, "GrossProfit", as_of)
    cogs = quarters(store, "CostOfGoodsAndServicesSold", as_of)
    capex = quarters(store, "PaymentsToAcquirePropertyPlantAndEquipment", as_of)
    inv = instants(store, "InventoryNet", as_of)
    rows = []
    for e in sorted(set(rev) | set(gp) | set(inv)):
        r = QuarterRow(e, rev.get(e), gp.get(e), cogs.get(e), inv.get(e), capex.get(e))
        ok = lambda v: v is not None and v.status == TRUSTED  # noqa: E731
        if not (ok(r.revenue) and ok(r.gross_profit)):
            r.margin_reasons.append("revenue or gross profit missing / quarantined")
        elif r.revenue.start != r.gross_profit.start or r.revenue.end != r.gross_profit.end:
            r.margin_reasons.append(f"revenue period {r.revenue.start}..{r.revenue.end} != gross profit period "
                                    f"{r.gross_profit.start}..{r.gross_profit.end}")
        elif r.revenue.val <= 0:
            r.margin_reasons.append("non-positive revenue")
        else:
            if ok(r.cogs) and r.cogs.start == r.revenue.start:
                gap = r.revenue.val - r.cogs.val - r.gross_profit.val
                if abs(gap) > AGREE_TOL * r.revenue.val:
                    r.margin_reasons.append(f"identity violated: revenue - COGS - gross profit = {gap:,.0f}")
            else:
                r.margin_reasons.append("note: identity unchecked (no matching COGS quarter)")
            if not any(not x.startswith("note:") for x in r.margin_reasons):
                r.margin = r.gross_profit.val / r.revenue.val
                r.margin_status = TRUSTED
        if ok(r.inventory) and ok(r.cogs) and r.cogs.val > 0:
            r.dio = r.inventory.val / r.cogs.val * r.cogs.days
        rows.append(r)
    return rows
