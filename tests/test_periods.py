"""Regression fixtures for XBRL period normalization.

Run normally to test the current implementation. Set PANOPTICON_OLD_QUARTERS=1 to run the same
fixtures against the pre-fix implementation (panopticon/live/extract.py on `main`, loaded from git):
the failures it produces are the regression this patch fixes.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from panopticon.live import periods
from panopticon.live.store import Store

ROOT = Path(__file__).resolve().parents[1]
USE_OLD = os.environ.get("PANOPTICON_OLD_QUARTERS") == "1"

# Fiscal calendar shaped like Micron's: 52/53-week years ending near Sep 1.
FY25 = "2024-08-30"
FY26 = "2025-08-29"
Q = {  # quarter ends
    "25Q1": "2024-11-28", "25Q2": "2025-02-27", "25Q3": "2025-05-29", "25Q4": "2025-08-28",
    "26Q1": "2025-11-27", "26Q2": "2026-02-26", "26Q3": "2026-05-28", "26Q4": "2026-09-03",
}
ACC = {k: f"0000000001-{k}" for k in Q}
ACCEPT = {k: f"{Q[k][:4]}-{int(Q[k][5:7]) % 12 + 1:02d}-20T22:00:00Z" if k[-1] != "4" else
          f"{int(Q[k][:4]) + (1 if Q[k][5:7] == '12' else 0)}-10-05T12:00:00Z" for k in Q}
ACCEPT["25Q4"], ACCEPT["26Q4"] = "2025-10-03T12:00:00Z", "2026-10-09T12:00:00Z"
ACCEPT["26Q1"] = "2025-12-17T22:00:00Z"


def nxt(d):
    from datetime import date, timedelta
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def fact(self, concept, start, end, val, filing, unit="USD", form=None):
        rp = Q[filing]
        self.store.put_fact(concept, unit, start, end, float(val), ACC[filing], form or
                            ("10-K" if filing.endswith("Q4") else "10-Q"), "x", 2026, ACCEPT[filing][:10],
                            ACCEPT[filing], report_period=rp)

    def flows(self, concept, fy_start, qkeys, quarterly_vals, filing_prefix, direct=(1, 2, 3), ytd=True,
              comparatives=None):
        """Report a fiscal year like a real issuer: direct quarter columns, 6M/9M YTD, FY total."""
        cum = 0.0
        prev_end = None
        for i, (k, v) in enumerate(zip(qkeys, quarterly_vals), start=1):
            cum += v
            start = fy_start if i == 1 else nxt(prev_end)
            if i in direct:
                self.fact(concept, start, Q[k], v, k)
            if ytd and i in (2, 3):
                self.fact(concept, fy_start, Q[k], cum, k)
            if i == 4:
                self.fact(concept, fy_start, Q[k], cum, k)
            prev_end = Q[k]
        for (start, end, val, filing) in comparatives or []:
            self.fact(concept, start, end, val, filing)

    def close(self):
        self.store.close()
        self.tmp.cleanup()


# ------------------------------------------------------------------ implementations under test
class New:
    def __init__(self, store):
        self.rows = {r.end: r for r in periods.quarter_table(store)}
        self.rev = periods.revenue_quarters(store)
        self.gp = periods.quarters(store, "GrossProfit")
        self.capex = periods.quarters(store, "PaymentsToAcquirePropertyPlantAndEquipment")
        self.inv = periods.instants(store, "InventoryNet")

    @staticmethod
    def _trusted(d, e):
        v = d.get(e)
        return v.val if v is not None and v.status == periods.TRUSTED else None

    def revenue(self, e): return self._trusted(self.rev, e)
    def gross_profit(self, e): return self._trusted(self.gp, e)
    def capex_q(self, e): return self._trusted(self.capex, e)
    def inventory(self, e): return self._trusted(self.inv, e)

    def margin(self, e):
        r = self.rows.get(e)
        return r.margin if r and r.margin_status == periods.TRUSTED else None


class Old:
    """Adapter over the pre-fix code, loaded from git `main` (everything it returns counted as trusted)."""

    def __init__(self, store):
        src = subprocess.run(["git", "show", "main:panopticon/live/extract.py"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
        mod = types.ModuleType("panopticon.live._extract_main")
        mod.__package__ = "panopticon.live"
        exec(compile(src, "main:extract.py", "exec"), mod.__dict__)
        self.rev = mod._revenue(store)
        self.gp = mod.quarterly(store, "GrossProfit")
        self.capex = mod.quarterly(store, "PaymentsToAcquirePropertyPlantAndEquipment")
        self.inv = {r["end"]: r for r in store.fact_series("InventoryNet", duration="instant")}

    def revenue(self, e): return self.rev.get(e, {}).get("val")
    def gross_profit(self, e): return self.gp.get(e, {}).get("val")
    def capex_q(self, e): return self.capex.get(e, {}).get("val")
    def inventory(self, e): return self.inv.get(e, {}).get("val")

    def margin(self, e):
        r, g = self.revenue(e), self.gross_profit(e)
        return g / r if r and g is not None else None


IMPL = Old if USE_OLD else New


def micron_like(fx, gp_override=None):
    """Two fiscal years of revenue / GP / COGS / capex / inventory with comparatives and repeats."""
    k25, k26 = ["25Q1", "25Q2", "25Q3", "25Q4"], ["26Q1", "26Q2", "26Q3", "26Q4"]
    rev25, rev26 = [8709e6, 8053e6, 9301e6, 11315e6], [13643e6, 23860e6, 41456e6, 54229e6]
    cogs25, cogs26 = [5323e6, 5090e6, 5793e6, 6299e6], [5997e6, 6105e6, 6400e6, 7182e6]
    gp25 = [r - c for r, c in zip(rev25, cogs25)]
    gp26 = gp_override or [r - c for r, c in zip(rev26, cogs26)]
    # prior-year comparatives repeated in FY26 10-Qs (same values -> repeats, not new facts)
    comps = [(nxt(Q["25Q1"]), Q["25Q2"], rev25[1], "26Q2"), (FY25, Q["25Q2"], sum(rev25[:2]), "26Q2"),
             (nxt(Q["25Q2"]), Q["25Q3"], rev25[2], "26Q3"), (FY25, Q["25Q3"], sum(rev25[:3]), "26Q3")]
    fx.flows("RevenueFromContractWithCustomerExcludingAssessedTax", FY25, k25, rev25, "25")
    fx.flows("RevenueFromContractWithCustomerExcludingAssessedTax", FY26, k26, rev26, "26", comparatives=comps)
    fx.flows("CostOfGoodsAndServicesSold", FY25, k25, cogs25, "25")
    fx.flows("CostOfGoodsAndServicesSold", FY26, k26, cogs26, "26")
    fx.flows("GrossProfit", FY25, k25, gp25, "25")
    fx.flows("GrossProfit", FY26, k26, gp26, "26")
    # capex: cash-flow statement reports only Q1 and cumulative YTD
    fx.flows("PaymentsToAcquirePropertyPlantAndEquipment", FY25, k25, [3206e6, 4055e6, 2938e6, 5658e6], "25",
             direct=(1,))
    fx.flows("PaymentsToAcquirePropertyPlantAndEquipment", FY26, k26, [5389e6, 6387e6, 7826e6, 11110e6], "26",
             direct=(1,))
    for k, v in zip(k25 + k26, [8709e6, 9007e6, 8727e6, 8355e6, 8205e6, 8267e6, 8567e6, 10372e6]):
        fx.fact("InventoryNet", None, Q[k], v, k)
    fx.fact("InventoryNet", None, Q["25Q4"], 8355e6, "26Q1")  # prior year-end balance, comparative column


class PeriodNormalization(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def test_quarters_are_not_cumulative_or_annual(self):
        micron_like(self.fx)
        im = IMPL(self.fx.store)
        self.assertAlmostEqual(im.revenue(Q["26Q3"]), 41456e6)     # direct
        self.assertAlmostEqual(im.revenue(Q["26Q4"]), 54229e6)     # FY - 9M (98-day quarter)
        self.assertAlmostEqual(im.revenue(Q["25Q3"]), 9301e6)
        self.assertAlmostEqual(im.margin(Q["26Q3"]), (41456 - 6400) / 41456, places=6)
        self.assertAlmostEqual(im.capex_q(Q["26Q2"]), 6387e6)       # 6M - Q1
        self.assertAlmostEqual(im.capex_q(Q["26Q4"]), 11110e6)      # FY - 9M
        self.assertAlmostEqual(im.inventory(Q["26Q4"]), 10372e6)
        if not USE_OLD:
            row = im.rows[Q["26Q4"]]
            self.assertEqual(row.cogs.days, 98)
            self.assertAlmostEqual(row.dio, 10372 / 7182 * 98, places=6)

    def test_other_units_are_never_mixed_in(self):
        micron_like(self.fx)
        # same concept and period, different unit (e.g. a per-share or 'pure' tag): must be ignored
        self.fx.fact("PaymentsToAcquirePropertyPlantAndEquipment", FY26, Q["26Q3"], 1.0, "26Q3", unit="pure")
        im = IMPL(self.fx.store)
        self.assertAlmostEqual(im.capex_q(Q["26Q3"]), 7826e6)
        self.assertAlmostEqual(im.capex_q(Q["26Q4"]), 11110e6)

    def test_incompatible_direct_contexts_are_quarantined(self):
        micron_like(self.fx)
        # a second "quarter" context ending on the same date with a different start and value
        self.fx.fact("RevenueFromContractWithCustomerExcludingAssessedTax", "2026-02-20", Q["26Q3"], 44e9, "26Q3")
        im = IMPL(self.fx.store)
        self.assertIsNone(im.revenue(Q["26Q3"]))
        self.assertIsNone(im.margin(Q["26Q3"]))

    def test_nonsensical_negative_derivation_is_quarantined(self):
        micron_like(self.fx)
        # a mis-scoped FY total smaller than the 9-month YTD (e.g. a segment-level tag) -> negative Q4
        st = self.fx.store
        st.db.execute("UPDATE fact_observations SET val=70e9 WHERE concept LIKE 'RevenueFrom%' AND start=? AND "
                      "\"end\"=?", (FY26, Q["26Q4"]))
        im = IMPL(st)
        q4 = im.revenue(Q["26Q4"])
        self.assertTrue(q4 is None, f"negative/implausible Q4 revenue {q4} must not be trusted")
        self.assertIsNone(im.margin(Q["26Q4"]))

    def test_mismatched_revenue_and_gross_profit_periods_block_margin(self):
        micron_like(self.fx)
        # gross profit reported for a slightly different quarter boundary than revenue
        st = self.fx.store
        st.db.execute("DELETE FROM fact_observations WHERE concept='GrossProfit' AND \"end\"=? AND start<>?",
                      (Q["26Q3"], FY26))
        st.db.execute("DELETE FROM fact_observations WHERE concept='GrossProfit' AND \"end\"=? AND start=?",
                      (Q["26Q3"], FY26))
        self.fx.fact("GrossProfit", "2026-03-06", Q["26Q3"], 33e9, "26Q3")
        im = IMPL(st)
        self.assertIsNone(im.margin(Q["26Q3"]))

    def test_gross_profit_identity_violation_blocks_margin(self):
        gp = [13643e6 - 5997e6, 23860e6 - 6105e6, 39e9, 54229e6 - 7182e6]   # Q3 GP inconsistent with rev-COGS
        micron_like(self.fx, gp_override=gp)
        im = IMPL(self.fx.store)
        self.assertIsNone(im.margin(Q["26Q3"]))
        self.assertIsNotNone(im.margin(Q["26Q2"]))

    def test_missing_quarter_input_is_not_invented(self):
        micron_like(self.fx)
        st = self.fx.store
        st.db.execute("DELETE FROM fact_observations WHERE concept LIKE 'Payments%' AND start=? AND \"end\"=?",
                      (FY26, Q["26Q2"]))   # 6M capex missing
        im = IMPL(st)
        self.assertIsNone(im.capex_q(Q["26Q2"]))
        self.assertIsNone(im.capex_q(Q["26Q3"]))
        self.assertAlmostEqual(im.capex_q(Q["26Q4"]), 11110e6)

    def test_concept_switch_between_years(self):
        micron_like(self.fx)
        st = self.fx.store
        # FY25 revenue only under the older 'Revenues' concept
        st.db.execute("UPDATE fact_observations SET concept='Revenues', fact_key=REPLACE(fact_key,"
                      "'RevenueFromContractWithCustomerExcludingAssessedTax','Revenues') "
                      "WHERE concept LIKE 'RevenueFrom%' AND start>=? AND \"end\"<=?", (FY25, Q["25Q4"]))
        im = IMPL(st)
        self.assertAlmostEqual(im.revenue(Q["25Q3"]), 9301e6)
        self.assertAlmostEqual(im.revenue(Q["26Q3"]), 41456e6)

    def test_conflicting_revenue_concepts_are_quarantined(self):
        micron_like(self.fx)
        # Same quarter under the second concept: individually trusted, materially different value.
        self.fx.fact("Revenues", nxt(Q["26Q2"]), Q["26Q3"], 39e9, "26Q3")
        im = IMPL(self.fx.store)
        self.assertIsNone(im.revenue(Q["26Q3"]), "composite revenue must not silently pick one concept")
        self.assertIsNone(im.margin(Q["26Q3"]))
        self.assertAlmostEqual(im.revenue(Q["26Q2"]), 23860e6)   # other quarters unaffected

    def test_agreeing_revenue_concepts_keep_both_as_lineage(self):
        if USE_OLD:
            self.skipTest("old implementation has no lineage")
        micron_like(self.fx)
        self.fx.fact("Revenues", nxt(Q["26Q2"]), Q["26Q3"], 41456e6, "26Q3")
        v = periods.revenue_quarters(self.fx.store)[Q["26Q3"]]
        self.assertEqual(v.status, periods.TRUSTED)
        self.assertEqual(v.concept, "RevenueFromContractWithCustomerExcludingAssessedTax")
        self.assertIn("Revenues", {i["concept"] for i in v.inputs})

    def test_inventory_requires_reporting_date_instant(self):
        micron_like(self.fx)
        self.fx.fact("InventoryNet", None, "2026-04-15", 9.9e9, "26Q3")   # mid-quarter instant
        im = IMPL(self.fx.store)
        self.assertIsNone(im.inventory("2026-04-15"))
        self.assertAlmostEqual(im.inventory(Q["26Q3"]), 8567e6)

    def test_classification(self):
        if USE_OLD:
            self.skipTest("classification does not exist in the old implementation")
        micron_like(self.fx)
        self.fx.fact("RevenueFromContractWithCustomerExcludingAssessedTax", "2026-01-01", Q["26Q3"], 1e9, "26Q3")
        classes = {}
        for o in periods.observations(self.fx.store, "RevenueFromContractWithCustomerExcludingAssessedTax"):
            classes[(o["start"], o["end"])] = o["class"]
        self.assertEqual(classes[(FY26, Q["26Q4"])], periods.FISCAL_YEAR)
        self.assertEqual(classes[(FY26, Q["26Q3"])], periods.YEAR_TO_DATE)
        self.assertEqual(classes[(nxt(Q["26Q2"]), Q["26Q3"])], periods.DIRECT_QUARTER)
        self.assertEqual(classes[("2026-01-01", Q["26Q3"])], periods.UNKNOWN)
        # prior-year quarter first seen in its own filing is DIRECT_QUARTER; its repeat in a FY26 10-Q
        # does not change that (the store keeps first availability)
        self.assertEqual(classes[(nxt(Q["25Q2"]), Q["25Q3"])], periods.DIRECT_QUARTER)
        inv = {o["end"]: o["class"] for o in periods.observations(self.fx.store, "InventoryNet")}
        self.assertEqual(inv[Q["26Q3"]], periods.INSTANT)

    def test_every_value_exposes_its_inputs(self):
        if USE_OLD:
            self.skipTest("old implementation has no derivation lineage")
        micron_like(self.fx)
        q4 = periods.revenue_quarters(self.fx.store)[Q["26Q4"]]
        self.assertEqual(q4.method, "ytd_diff")
        self.assertEqual(sorted(i["end"] for i in q4.inputs), [Q["26Q3"], Q["26Q4"]])
        self.assertTrue(all(i["obs_id"] and i["accession"] for i in q4.inputs))
        row = {r.end: r for r in periods.quarter_table(self.fx.store)}[Q["26Q3"]]
        self.assertEqual({i["concept"] for i in row.margin_inputs},
                         {"RevenueFromContractWithCustomerExcludingAssessedTax", "GrossProfit",
                          "CostOfGoodsAndServicesSold"})
        self.assertIn("35,056 / revenue 41,456", row.margin_formula)
        self.assertEqual({i["concept"] for i in row.dio_inputs}, {"InventoryNet", "CostOfGoodsAndServicesSold"})
        self.assertIn("x 91 days", row.dio_formula)
        cx = periods.quarters(self.fx.store, "PaymentsToAcquirePropertyPlantAndEquipment")[Q["26Q3"]]
        self.assertEqual({(i["start"], i["end"]) for i in cx.inputs}, {(FY26, Q["26Q3"]), (FY26, Q["26Q2"])})


class SourceCheckGrading(unittest.TestCase):
    def test_levels(self):
        from panopticon.live.audit import _grade
        fact = {"val": 41456e6, "concept": "RevenueFromContractWithCustomerExcludingAssessedTax", "end": "2026-05-28"}
        stmt = ("Consolidated Statements of Operations\n(In millions)\nQuarter Ended\nMay 28,\n2026\n"
                "Revenue\n$\n41,456 \n")
        self.assertEqual(_grade(stmt, fact)[0], "STATEMENT_LINE_MATCH")
        mdna = "Results of Operations\n(In millions)\nMay 28, 2026\nRevenue $ 41,456"
        self.assertEqual(_grade(mdna, fact)[0], "LINE_ITEM_MATCH")   # right label/date, not on a statement
        self.assertIn("Consolidated Statements of Operations", _grade(stmt, fact)[1])
        no_header = "(In millions)\nRevenue\n$\n41,456\n"
        self.assertEqual(_grade(no_header, fact)[0], "LABEL_ONLY")
        self.assertEqual(_grade("Backlog grew to 41,456 units", fact)[0], "NUMBER_ONLY")
        self.assertEqual(_grade("Revenue $ 41,457", fact)[0], "MISSING")
        capex = {"val": 30712e6, "concept": "PaymentsToAcquirePropertyPlantAndEquipment", "end": "2026-09-03"}
        cf = "(In millions)\nSeptember 3,\n2026\nExpenditures for property, plant, and equipment\n(30,712)"
        self.assertEqual(_grade(cf, capex)[0], "LINE_ITEM_MATCH")
        self.assertEqual(_grade("Consolidated Statements of Cash Flows\n" + cf, capex)[0], "STATEMENT_LINE_MATCH")
        self.assertEqual(_grade(cf.replace("(30,712)", "30,712"), capex)[0], "MISSING")  # wrong sign as filed


if __name__ == "__main__":
    unittest.main()
