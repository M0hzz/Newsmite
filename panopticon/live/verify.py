"""Mechanical claim verification. These checks inform a human reviewer; they never approve anything.

For a fact-based claim, every normalized value it relied on is recomputed *as of the claim's
availability time*, exactly as the pipeline would, and must be:
  * TRUSTED (not quarantined)               -> no_quarantined_inputs
  * built from exactly the cited fact contexts -> inputs_match_recomputation
The structured calculation is re-run from the cited values and compared unrounded.
"""
from __future__ import annotations

from collections import defaultdict

from . import periods
from .store import Store

TOL = 1e-9


def _derivations(evidence: list[dict]) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for e in evidence:
        if e.get("type") != "xbrl_fact":
            continue
        for d in e.get("derivations") or ([e["derivation"]] if e.get("derivation") else []):
            out[(d["metric"], d["period_end"])].add(e["obs_id"])
    return out


def verify_claim(store: Store, c: dict) -> dict:
    ev = c["evidence"]
    latest_input = max((e.get("accepted_at") or "" for e in ev), default="")
    checks: dict = {
        "available_at": c["available_at"],
        "latest_input_public_at": latest_input,
        "available_at_equals_latest_input": c["available_at"] == latest_input if ev else None,
        "all_inputs_public_by_available_at": all((e.get("accepted_at") or "") <= c["available_at"] for e in ev),
        "supersedes": c.get("supersedes"),
    }
    ders = _derivations(ev)
    if not ders:
        return checks
    per = []
    for (metric, end), cited in sorted(ders.items()):
        v = periods.value_for(store, metric, end, as_of=c["available_at"])
        recomputed = {i["obs_id"] for i in v.inputs} if v else set()
        per.append({"metric": metric, "period_end": end, "status": v.status if v else "missing",
                    "reasons": v.reasons if v else ["value not reproducible at available_at"],
                    "cited_obs": sorted(cited), "recomputed_obs": sorted(recomputed),
                    "match": cited == recomputed})
    checks["inputs"] = per
    checks["no_quarantined_inputs"] = all(p["status"] == periods.TRUSTED for p in per)
    checks["inputs_match_recomputation"] = all(p["match"] for p in per)

    calc = c.get("calculation")
    if c["family"] == "INVENTORY" and calc:
        cur, pri = calc["ending_inventory"]["current"], calc["ending_inventory"]["prior"]
        ok = abs(cur["value"] / pri["value"] - 1 - calc["qoq_change"]["result_unrounded"]) < TOL
        cov = calc.get("ending_inventory_coverage")
        if cov:
            for side in ("current", "prior"):
                x = cov[side]
                ok &= abs(x["ending_inventory"]["value"] / x["quarterly_cogs"]["value"] * x["quarter_days"]
                          - x["result_unrounded"]) < TOL
                # quarter length must equal the COGS period actually used (inclusive dates)
                from datetime import date
                q = x["quarterly_cogs"]
                ok &= (date.fromisoformat(q["end"]) - date.fromisoformat(q["start"])).days + 1 == x["quarter_days"]
        checks["calculation_reproduces"] = bool(ok)
    if c["family"] == "PRICING_MARGIN" and calc:
        from datetime import date
        ok = True
        for side in ("current", "prior"):
            x = calc[side]
            rev, gp, cogs = x["revenue"], x["gross_profit"], x["cogs"]
            ok &= abs(gp["value"] / rev["value"] - x["margin_unrounded"]) < TOL
            # revenue and gross profit must cover the same quarter, of the stated length
            ok &= (rev["start"], rev["end"]) == (gp["start"], gp["end"]) == (x["quarter"]["start"],
                                                                             x["quarter"]["end"])
            ok &= (date.fromisoformat(rev["end"]) - date.fromisoformat(rev["start"])).days + 1 == \
                x["quarter"]["days"]
            ic = x["identity_check"]
            if ic["checked"]:
                residual = rev["value"] - cogs["value"] - gp["value"]
                ok &= abs(residual - ic["residual"]) < 1e-3 and abs(residual) <= ic["tolerance"]
        ok &= abs(calc["current"]["margin_unrounded"] - calc["prior"]["margin_unrounded"]
                  - calc["change"]["result_unrounded"]) < TOL
        checks["calculation_reproduces"] = bool(ok)
        checks["identity_checked_both_quarters"] = all(calc[s_]["identity_check"]["checked"]
                                                       for s_ in ("current", "prior"))
    return checks
