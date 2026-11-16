"""SEC EDGAR connector (Tier 1).

Identity is the CIK, never the ticker. Requests carry a User-Agent built ONLY from the
EDGAR_CONTACT_NAME / EDGAR_CONTACT_EMAIL environment variables; it is never written to disk
or logs. Requests are rate-limited well under SEC's 10/second fair-access limit.

Pipeline: EDGAR document -> immutable raw evidence (store.py) -> candidate claim (extract.py)
-> human verification (store.review) -> active graph state (livegraph.py) -> dossier.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from typing import Callable

from .store import Store

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
DEFAULT_FORMS = ("10-K", "10-Q", "8-K", "4")
XBRL_CONCEPTS = (
    "InventoryNet",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "GrossProfit",
    "CostOfGoodsAndServicesSold",
    "PaymentsToAcquirePropertyPlantAndEquipment",
)

Transport = Callable[[str], bytes]


def user_agent() -> str:
    name = os.environ.get("EDGAR_CONTACT_NAME", "").strip()
    email = os.environ.get("EDGAR_CONTACT_EMAIL", "").strip()
    if not name or "@" not in email:
        raise RuntimeError("Set EDGAR_CONTACT_NAME and EDGAR_CONTACT_EMAIL (SEC requires a contact in the "
                           "User-Agent). They are read from the environment only and never stored.")
    return f"MarketPanopticon/0.1 {name} {email}"


class EdgarClient:
    def __init__(self, transport: Transport | None = None, min_interval: float = 0.15, retries: int = 4):
        self._transport = transport
        self._ua = None if transport else user_agent()
        self.min_interval, self.retries = min_interval, retries
        self._last = 0.0
        self.requests = 0

    def get(self, url: str) -> bytes:
        if self._transport:
            self.requests += 1
            return self._transport(url)
        for attempt in range(self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": self._ua, "Accept-Encoding": "identity"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    self.requests += 1
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise RuntimeError("unreachable")

    def json(self, url: str) -> dict:
        return json.loads(self.get(url))


def resolve_cik(client: EdgarClient, ticker: str) -> tuple[str, str]:
    data = client.json(f"{SEC_WWW}/files/company_tickers.json")
    for row in data.values():
        if row["ticker"].upper() == ticker.upper():
            return f"{int(row['cik_str']):010d}", row["title"]
    raise KeyError(f"ticker {ticker!r} not found in SEC company_tickers.json")


def _base_form(form: str) -> str:
    return form[:-2] if form.endswith("/A") else form


def filing_rows(submissions: dict, forms: tuple[str, ...], since: str) -> list[dict]:
    r = submissions["filings"]["recent"]
    rows = []
    for i in range(len(r["form"])):
        form = r["form"][i]
        if _base_form(form) not in forms or r["filingDate"][i] < since:
            continue
        rows.append({k: r[k][i] for k in ("accessionNumber", "filingDate", "reportDate", "acceptanceDateTime",
                                          "form", "items", "primaryDocument")})
    rows.sort(key=lambda x: x["acceptanceDateTime"])
    # Link each amendment to the latest earlier original with the same base form and period.
    for row in rows:
        if row["form"].endswith("/A"):
            originals = [o for o in rows if o["form"] == _base_form(row["form"])
                         and o["reportDate"] == row["reportDate"]
                         and o["acceptanceDateTime"] < row["acceptanceDateTime"]]
            row["amends"] = originals[-1]["accessionNumber"] if originals else None
    return rows


def doc_url(cik: str, accession: str, filename: str) -> str:
    return f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{filename}"


def raw_filename(form: str, primary: str) -> str:
    """Form 4's primaryDocument points at an XSL-rendered view; the raw XML has the same name at the root."""
    if _base_form(form) == "4" and "/" in primary:
        return primary.split("/")[-1]
    return primary


def _iso_utc(s: str) -> str:
    return s.replace(".000Z", "Z")


# ---------------------------------------------------------------- Form 4
FORM4_KIND = {
    "P": "open_market_purchase", "S": "open_market_sale", "A": "grant_award", "F": "tax_withholding",
    "M": "option_exercise", "X": "option_exercise", "C": "conversion", "G": "gift",
    "D": "disposition_to_issuer", "J": "other", "W": "inheritance", "I": "discretionary_plan",
}


def _v(el, path: str):
    x = el.find(path)
    if x is None:
        return None
    v = x.find("value")
    t = (v.text if v is not None else x.text)
    return t.strip() if t else None


def parse_form4(xml: bytes, accession: str, accepted_at: str) -> list[dict]:
    root = ET.fromstring(xml)
    ro = root.find("reportingOwner")
    rel = ro.find("reportingOwnerRelationship") if ro is not None else None
    flag = lambda tag: 1 if rel is not None and (rel.findtext(tag) or "").strip().lower() in ("1", "true") else 0  # noqa
    footnotes = {f.get("id"): (f.text or "") for f in root.iter("footnote")}
    plan_doc = (root.findtext("aff10b5One") or "").strip().lower() in ("1", "true")
    out = []
    n = 0
    for derivative, tag in ((0, "nonDerivativeTransaction"), (1, "derivativeTransaction")):
        for tx in root.iter(tag):
            n += 1
            code = _v(tx, "transactionCoding/transactionCode")
            fids = [f.get("id") for f in tx.iter("footnoteId")]
            plan = plan_doc or any("10b5-1" in footnotes.get(f, "") for f in fids)
            num = lambda p: float(_v(tx, p)) if _v(tx, p) not in (None, "") else None  # noqa: E731
            out.append({
                "txn_id": f"{accession}#{n}", "accession": accession, "accepted_at": accepted_at,
                "owner_cik": ro.findtext("reportingOwnerId/rptOwnerCik") if ro is not None else None,
                "owner_name": ro.findtext("reportingOwnerId/rptOwnerName") if ro is not None else None,
                "officer_title": (rel.findtext("officerTitle") or "").strip() if rel is not None else "",
                "is_officer": flag("isOfficer"), "is_director": flag("isDirector"),
                "is_ten_pct": flag("isTenPercentOwner"), "derivative": derivative,
                "security": _v(tx, "securityTitle"), "txn_date": _v(tx, "transactionDate"), "code": code,
                "kind": FORM4_KIND.get(code or "", "other"),
                "shares": num("transactionAmounts/transactionShares"),
                "price": num("transactionAmounts/transactionPricePerShare"),
                "acq_disp": _v(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                "owned_after": num("postTransactionAmounts/sharesOwnedFollowingTransaction"),
                "plan_10b5_1": 1 if plan else 0,
            })
    return out


# ---------------------------------------------------------------- ingest
def ingest(store: Store, client: EdgarClient, ticker: str, since: str,
           forms: tuple[str, ...] = DEFAULT_FORMS, fact_lookback_days: int = 450,
           log: Callable[[str], None] = lambda s: None) -> dict:
    cik, title = resolve_cik(client, ticker)
    log(f"{ticker} -> CIK {cik} ({title})")
    subs = client.json(f"{SEC_DATA}/submissions/CIK{cik}.json")
    rows = filing_rows(subs, forms, since)
    new_docs = 0
    for row in rows:
        acc, form = row["accessionNumber"], row["form"]
        fname = raw_filename(form, row["primaryDocument"])
        doc_id = f"{acc}/{fname}"
        accepted = _iso_utc(row["acceptanceDateTime"])
        if store.has_document(doc_id):
            continue  # EDGAR archives are immutable: never refetch
        url = doc_url(cik, acc, fname)
        content = client.get(url)
        _, inserted = store.put_document({
            "doc_id": doc_id, "accession": acc, "cik": cik, "form": form, "filing_date": row["filingDate"],
            "accepted_at": accepted, "period": row["reportDate"] or None, "items": row["items"] or None,
            "url": url, "amends_accession": row.get("amends")}, content)
        new_docs += inserted
        if inserted and _base_form(form) == "4":
            for t in parse_form4(content, acc, accepted):
                store.put_txn(t)
        log(f"  {form:<5} {acc} {row['filingDate']} {fname}")
    store.commit()

    # XBRL facts. Each fact is tied to the accession that reported it; availability is that filing's
    # acceptance time. Facts from filings we did not ingest get a conservative end-of-day timestamp.
    facts = client.json(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik}.json")
    cutoff = (date.fromisoformat(since) - timedelta(days=fact_lookback_days)).isoformat()
    accepted_by_acc = {r["accessionNumber"]: _iso_utc(r["acceptanceDateTime"]) for r in
                       filing_rows(subs, ("10-K", "10-Q", "8-K"), cutoff)}
    kinds: dict[str, int] = {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for concept in XBRL_CONCEPTS:
        for unit, obs in gaap.get(concept, {}).get("units", {}).items():
            for o in obs:
                if o["filed"] < cutoff or o.get("form", "").split("/")[0] not in ("10-K", "10-Q"):
                    continue
                accepted = accepted_by_acc.get(o["accn"]) or f"{o['filed']}T23:59:59Z"
                k = store.put_fact(concept, unit, o.get("start"), o["end"], float(o["val"]), o["accn"],
                                   o.get("form"), o.get("fp"), o.get("fy"), o["filed"], accepted)
                kinds[k] = kinds.get(k, 0) + 1
    store.commit()
    return {"cik": cik, "company": title, "filings_listed": len(rows), "new_documents": new_docs,
            "fact_observations": kinds, "http_requests": client.requests}


# ---------------------------------------------------------------- text
class _Text:
    """Visible text from (inline-XBRL) HTML; hidden ix:header content is dropped."""

    def __init__(self, html: bytes):
        from html.parser import HTMLParser

        out: list[str] = []
        skip = [0]

        class P(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "ix:header"):
                    skip[0] += 1
                if tag in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "td"):
                    out.append("\n")

            def handle_endtag(self, tag):
                if tag in ("script", "style", "ix:header") and skip[0]:
                    skip[0] -= 1

            def handle_data(self, d):
                if not skip[0]:
                    out.append(d)

        P(convert_charrefs=True).feed(html.decode("utf-8", errors="replace"))
        text = "".join(out).replace("\xa0", " ")
        self.text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def html_to_text(html: bytes) -> str:
    return _Text(html).text
