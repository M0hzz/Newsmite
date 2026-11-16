"""Acceptance tests for the live SEC pipeline. Offline: a fake transport serves EDGAR-shaped fixtures."""
import copy
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from panopticon.investigations import Investigation
from panopticon.live import edgar
from panopticon.live.edgar import EdgarClient, ingest, parse_form4
from panopticon.live.extract import extract_all
from panopticon.live.livegraph import build, verify_edge
from panopticon.live.store import Store, sha256

CIK = "0000999999"
ACC_Q2, ACC_Q3, ACC_K, ACC_KA = "0000999999-26-000002", "0000999999-26-000003", "0000999999-26-000004", \
    "0000999999-26-000005"
ACC_F4, ACC_F4A, ACC_8K = "0001111111-26-000001", "0001111111-26-000002", "0000999999-26-000006"

FORM4 = b"""<?xml version="1.0"?><ownershipDocument><documentType>4</documentType>
<periodOfReport>2026-07-01</periodOfReport><issuer><issuerCik>0000999999</issuerCik></issuer>
<reportingOwner><reportingOwnerId><rptOwnerCik>0001111111</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName>
</reportingOwnerId><reportingOwnerRelationship><isDirector>false</isDirector><isOfficer>true</isOfficer>
<isTenPercentOwner>false</isTenPercentOwner><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner>
<aff10b5One>false</aff10b5One>
<nonDerivativeTable>
 <nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-07-01</value></transactionDate>
  <transactionCoding><transactionCode>S</transactionCode><footnoteId id="F1"/></transactionCoding>
  <transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare>
  <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
  <postTransactionAmounts><sharesOwnedFollowingTransaction><value>9000</value></sharesOwnedFollowingTransaction></postTransactionAmounts></nonDerivativeTransaction>
 <nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-07-01</value></transactionDate>
  <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>500</value></transactionShares><transactionPricePerShare><value>51</value></transactionPricePerShare>
  <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
 <nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-07-01</value></transactionDate>
  <transactionCoding><transactionCode>A</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>2000</value></transactionShares><transactionPricePerShare><value>0</value></transactionPricePerShare>
  <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
 <nonDerivativeTransaction><securityTitle><value>Common</value></securityTitle><transactionDate><value>2026-07-01</value></transactionDate>
  <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>700</value></transactionShares><transactionPricePerShare><value>50</value></transactionPricePerShare>
  <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></nonDerivativeTransaction>
</nonDerivativeTable>
<derivativeTable><derivativeTransaction><securityTitle><value>Option</value></securityTitle><transactionDate><value>2026-07-01</value></transactionDate>
  <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
  <transactionAmounts><transactionShares><value>300</value></transactionShares>
  <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts></derivativeTransaction></derivativeTable>
<footnotes><footnote id="F1">Sold pursuant to a Rule 10b5-1 trading plan adopted March 1, 2026.</footnote></footnotes>
</ownershipDocument>"""

TENK = (b"<html><body><ix:header><div>hidden xbrl header Taiwan</div></ix:header>"
        b"<p>We manufacture our products within our own facilities located in Taiwan, Singapore and Japan, "
        b"and disruptions there could affect supply.</p><p>Sales are subject to export control regulations "
        b"administered by the Bureau of Industry and Security, which may restrict shipments.</p></body></html>")
EIGHTK = (b"<html><body><p>Item 5.02 Departure of Directors or Certain Officers.</p><p>On July 1, 2026 the "
          b"Chief Financial Officer resigned effective immediately.</p><p>Item 9.01 Exhibits</p></body></html>")


def filings(include_ka=False):
    rows = [
        (ACC_Q2, "2026-03-20", "2026-02-26", "2026-03-19T22:00:00.000Z", "10-Q", "", "x-20260226.htm"),
        (ACC_Q3, "2026-06-25", "2026-05-28", "2026-06-24T22:00:00.000Z", "10-Q", "", "x-20260528.htm"),
        (ACC_F4, "2026-07-03", "2026-07-01", "2026-07-02T21:00:00.000Z", "4", "", "xslF345X06/form4.xml"),
        (ACC_F4A, "2026-07-10", "2026-07-01", "2026-07-09T21:00:00.000Z", "4/A", "", "xslF345X06/form4a.xml"),
        (ACC_8K, "2026-07-03", "2026-07-01", "2026-07-02T20:00:00.000Z", "8-K", "5.02,9.01", "x8k.htm"),
        (ACC_K, "2026-10-09", "2026-09-03", "2026-10-09T12:00:00.000Z", "10-K", "", "x-20260903.htm"),
    ]
    if include_ka:
        rows.append((ACC_KA, "2026-11-01", "2026-09-03", "2026-10-31T12:00:00.000Z", "10-K/A", "", "x-k-a.htm"))
    keys = ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items", "primaryDocument")
    return {"filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}}}


def facts(restated=False):
    q = lambda s, e, v, a, f, fp, filed: {"start": s, "end": e, "val": v, "accn": a, "form": f, "fp": fp,  # noqa
                                          "fy": 2026, "filed": filed}
    i = lambda e, v, a, f, fp, filed: {"end": e, "val": v, "accn": a, "form": f, "fp": fp, "fy": 2026,  # noqa
                                       "filed": filed}
    inv = [i("2026-02-26", 1000, ACC_Q2, "10-Q", "Q2", "2026-03-20"),
           i("2026-05-28", 1100, ACC_Q3, "10-Q", "Q3", "2026-06-25"),
           i("2026-05-28", 1100, ACC_K, "10-K", "FY", "2026-10-09"),          # repeat of the same fact
           i("2026-09-03", 1300, ACC_K, "10-K", "FY", "2026-10-09")]
    if restated:
        inv.append(i("2026-09-03", 1250, ACC_KA, "10-K/A", "FY", "2026-11-01"))  # restatement
    rev = [q("2025-11-28", "2026-02-26", 500, ACC_Q2, "10-Q", "Q2", "2026-03-20"),
           q("2026-02-27", "2026-05-28", 600, ACC_Q3, "10-Q", "Q3", "2026-06-25"),
           q("2026-02-27", "2026-05-28", 600, ACC_K, "10-K", "FY", "2026-10-09")]
    gp = [q("2025-11-28", "2026-02-26", 200, ACC_Q2, "10-Q", "Q2", "2026-03-20"),
          q("2026-02-27", "2026-05-28", 300, ACC_Q3, "10-Q", "Q3", "2026-06-25")]
    return {"facts": {"us-gaap": {"InventoryNet": {"units": {"USD": inv}},
                                  "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": rev}},
                                  "GrossProfit": {"units": {"USD": gp}}}}}


class FakeSEC:
    def __init__(self):
        self.include_ka = False
        self.calls = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url.endswith("company_tickers.json"):
            return json.dumps({"0": {"cik_str": 999999, "ticker": "XMU", "title": "TEST MEMORY CORP"}}).encode()
        if "/submissions/" in url:
            return json.dumps(filings(self.include_ka)).encode()
        if "/companyfacts/" in url:
            return json.dumps(facts(self.include_ka)).encode()
        if url.endswith((".xml",)):
            return FORM4
        if url.endswith("x8k.htm"):
            return EIGHTK
        return TENK if "x-2026" in url or "x-k-a" in url else b"<html><body><p>10-Q body</p></body></html>"


REL = {
    "focal": "XMU",
    "entities": [{"id": "XMU", "name": "Test Memory", "type": "Company", "attrs": {"ticker": "XMU"}},
                 {"id": "DRAM_PX", "name": "DRAM price", "type": "Commodity", "attrs": {}}],
    "edges": [{"id": "e1", "source": "XMU", "relationship": "EXPOSED_TO", "target": "DRAM_PX", "strength": 0.8,
               "direction": "positive_dependency", "confidence": 0.8, "metrics": ["gross_margin"], "lag_days": 30,
               "clock": "business", "effective_from": "2024-01-01", "recorded_at": "2026-01-01",
               "verified": False, "verified_by": None, "verified_at": None, "evidence": []}],
}


class LiveAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.sec = FakeSEC()
        self.client = EdgarClient(transport=self.sec)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_pipeline(self):
        res = ingest(self.store, self.client, "XMU", "2026-01-01")
        res["candidates"] = extract_all(self.store, "XMU", "2026-01-01")
        return res

    # 1
    def test_rerun_creates_no_duplicates(self):
        self.run_pipeline()
        before = self.store.stats()
        calls = len(self.sec.calls)
        res = self.run_pipeline()
        self.assertEqual(res["new_documents"], 0)
        self.assertEqual(sum(res["candidates"].values()), 0)
        self.assertEqual(self.store.stats(), before)
        doc_fetches = [u for u in self.sec.calls[calls:] if "/Archives/" in u]
        self.assertEqual(doc_fetches, [], "immutable filings must not be re-downloaded")

    # 2
    def test_documents_keep_full_provenance(self):
        self.run_pipeline()
        for d in self.store.documents():
            for k in ("accession", "accepted_at", "period", "form", "url", "sha256", "filing_date"):
                self.assertTrue(d[k], f"{k} missing on {d['doc_id']}")
            self.assertEqual(sha256(self.store.read_raw(d["sha256"])), d["sha256"])
        f4 = self.store.documents(form="4")[0]
        self.assertTrue(f4["url"].endswith("/form4.xml"), "Form 4 must store the raw XML, not the XSL view")

    # 3
    def test_amendment_does_not_erase_original(self):
        self.run_pipeline()
        orig = {d["doc_id"]: d["sha256"] for d in self.store.documents(form="10-K")}
        self.sec.include_ka = True
        self.run_pipeline()
        self.assertEqual({d["doc_id"]: d["sha256"] for d in self.store.documents(form="10-K")}, orig)
        ka = self.store.documents(form="10-K/A")
        self.assertEqual(len(ka), 1)
        self.assertEqual(ka[0]["amends_accession"], ACC_K)
        self.assertEqual(self.store.documents(form="4/A")[0]["amends_accession"], ACC_F4)

    # 4
    def test_filing_time_and_economic_period_are_separate(self):
        self.run_pipeline()
        inv = [c for c in self.store.claims() if c["family"] == "INVENTORY"]
        latest = max(inv, key=lambda c: c["event_time"])
        self.assertEqual(latest["event_time"], "2026-09-03")           # economic period
        self.assertEqual(latest["available_at"], "2026-10-09T12:00:00Z")  # when the market could know
        k = self.store.documents(form="10-K")[0]
        self.assertNotEqual(k["period"], k["accepted_at"][:10])

    # 5
    def test_form4_classification(self):
        rows = parse_form4(FORM4, "acc", "2026-07-02T21:00:00Z")
        kinds = [(r["kind"], r["plan_10b5_1"], r["derivative"]) for r in rows]
        self.assertEqual(kinds, [("open_market_sale", 1, 0), ("open_market_sale", 0, 0), ("grant_award", 0, 0),
                                 ("tax_withholding", 0, 0), ("option_exercise", 0, 1)])
        self.run_pipeline()
        claims = [c for c in self.store.claims() if c["family"] == "INSIDER"]
        # the original Form 4 is amended by the 4/A, so only the amendment is represented
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["evidence"][0]["accession"], ACC_F4A)
        # only discretionary open-market sales carry signal: plan sale -0.02, non-plan sale -0.10
        self.assertAlmostEqual(claims[0]["change"], -0.12)

    # 6
    def test_identical_facts_are_not_counted_twice(self):
        self.run_pipeline()
        kinds = {r["accession"]: r["kind"] for r in self.store.iter_rows(
            "SELECT * FROM fact_observations WHERE concept='InventoryNet' AND \"end\"='2026-05-28'")}
        self.assertEqual(kinds, {ACC_Q3: "original", ACC_K: "repeat"})
        may = [c for c in self.store.claims() if c["family"] == "INVENTORY" and c["event_time"] == "2026-05-28"]
        self.assertEqual(len(may), 1)
        self.assertEqual(may[0]["available_at"], "2026-06-24T22:00:00Z", "a repeat must not move availability")

    # 7
    def test_later_filings_update_without_destroying_history(self):
        self.run_pipeline()
        old = next(c for c in self.store.claims() if c["family"] == "INVENTORY" and c["event_time"] == "2026-09-03")
        self.store.review(old["claim_id"], "approved", "tester")
        self.sec.include_ka = True
        self.run_pipeline()
        new = [c for c in self.store.claims() if c["family"] == "INVENTORY" and c["event_time"] == "2026-09-03"
               and c["claim_id"] != old["claim_id"]]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["supersedes"], old["claim_id"])
        self.assertIsNotNone(self.store.claim(old["claim_id"]), "superseded claim must remain")
        self.store.review(new[0]["claim_id"], "approved", "tester")
        ids_before = {c["claim_id"] for c in self.store.active_claims("2026-10-15T00:00:00Z")}
        ids_after = {c["claim_id"] for c in self.store.active_claims("2026-11-05T00:00:00Z")}
        self.assertIn(old["claim_id"], ids_before)
        self.assertNotIn(new[0]["claim_id"], ids_before)
        self.assertIn(new[0]["claim_id"], ids_after)
        self.assertNotIn(old["claim_id"], ids_after)
        restated = self.store.iter_rows("SELECT kind FROM fact_observations WHERE accession=?", (ACC_KA,))
        self.assertEqual([r["kind"] for r in restated], ["restatement"])

    # 8
    def test_extraction_never_confirms(self):
        self.run_pipeline()
        self.assertTrue(self.store.claims())
        self.assertEqual({c["status"] for c in self.store.claims()}, {"pending"})
        g = build(self.store, REL, "2026-12-31T00:00:00Z")
        self.assertEqual(g.claims, {})
        with self.assertRaises(ValueError):
            self.store.review(self.store.claims()[0]["claim_id"], "approved", "")

    # 9
    def test_replay_excludes_unavailable_documents(self):
        self.run_pipeline()
        for c in self.store.claims():
            self.store.review(c["claim_id"], "approved", "tester")
        cut = "2026-10-09T11:59:59Z"   # one second before the 10-K was accepted
        visible = self.store.active_claims(cut)
        self.assertTrue(visible)
        self.assertTrue(all(c["available_at"] <= cut for c in visible))
        self.assertFalse(any(c["event_time"] == "2026-09-03" and c["family"] == "INVENTORY" for c in visible))
        self.assertEqual([r for r in self.store.fact_series("InventoryNet", as_of=cut) if r["end"] == "2026-09-03"], [])
        g = build(self.store, REL, cut)
        self.assertTrue(all(cl.first_observed.isoformat() <= "2026-10-09T11:59:59+00:00" for cl in g.claims.values()))

    # 10
    def test_unverified_relationship_cannot_produce_packet(self):
        cid = "cc_dram_test"
        self.store.put_candidate({"claim_id": cid, "family": "PRICING_MARGIN", "subject": "DRAM_PX", "metric": "price",
                                  "statement": "DRAM contract price +20%", "change": 0.2, "event_time": "2026-11-01",
                                  "available_at": "2026-11-02T00:00:00Z",
                                  "evidence": [{"type": "x", "accepted_at": "2026-11-02T00:00:00Z", "passage": "p"}]})
        self.store.review(cid, "approved", "tester")
        as_of = "2026-11-10T00:00:00Z"
        g = build(self.store, REL, as_of)
        self.assertEqual(Investigation(g, "XMU", date(2026, 11, 10)).signals, [])
        rel_path = Path(self.tmp.name) / "rel.json"
        rel_path.write_text(json.dumps(REL))
        with self.assertRaises(ValueError):
            verify_edge(rel_path, "e1", "tester", "")          # verification needs a citation
        verify_edge(rel_path, "e1", "tester", "10-K FY2026 Item 1A")
        g2 = build(self.store, json.loads(rel_path.read_text()), as_of)
        self.assertTrue(Investigation(g2, "XMU", date(2026, 11, 10)).signals)


class QuarterLengthTests(unittest.TestCase):
    def test_days_of_inventory_uses_actual_quarter_length(self):
        from panopticon.live.periods import Value
        direct = Value("C", "USD", "2026-02-27", "2026-05-28", 100.0, "direct", [])
        q4 = Value("C", "USD", "2026-05-29", "2026-09-03", 100.0, "ytd_diff", [])  # 53-week year: 14-week Q4
        self.assertEqual(direct.days, 91)
        self.assertEqual(q4.days, 98)


class IdentityTests(unittest.TestCase):
    def test_contact_required_and_never_persisted(self):
        saved = {k: os.environ.pop(k, None) for k in ("EDGAR_CONTACT_NAME", "EDGAR_CONTACT_EMAIL")}
        try:
            with self.assertRaises(RuntimeError):
                EdgarClient()
            os.environ["EDGAR_CONTACT_NAME"] = "Tester"
            os.environ["EDGAR_CONTACT_EMAIL"] = "sentinel-contact@example.invalid"
            self.assertIn("sentinel-contact@example.invalid", edgar.user_agent())
            with tempfile.TemporaryDirectory() as d:
                st = Store(d)
                ingest(st, EdgarClient(transport=FakeSEC()), "XMU", "2026-01-01")
                extract_all(st, "XMU")
                st.db.close()
                for p in Path(d).rglob("*"):
                    if p.is_file():
                        self.assertNotIn(b"sentinel-contact", p.read_bytes(), p)
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
