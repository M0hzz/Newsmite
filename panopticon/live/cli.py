"""`python3 -m panopticon live ...` — the human-gated SEC pipeline.

  live ingest MU --since 2025-06-01        fetch filings + XBRL facts, then extract candidate claims
  live status                              counts by form / fact kind / claim status
  live review [--family F] [--limit N]     list pending candidate claims
  live show <claim_id>                     one claim with full evidence and history
  live approve <id> [<id>...] --by NAME    approve (or: live reject ... --by NAME --note "...")
  live approve --family INVENTORY --by NAME   approve every pending claim in a family
  live dossier MU [--as-of TS]             markdown dossier from approved state
  live rel list | live rel verify <edge_id> --by NAME --evidence "10-K FY2026 Item 1A"
  live signals MU                          OptLab packets (empty until edges are verified)
  live audit MU [--periods ...]            every displayed number with its SEC fact contexts and formulas
  live source-check MU [--periods ...]     numeric source-presence check (proximity heuristic, not reconciliation)

Data lives in ./data/live (override with PANOPTICON_DATA); it is git-ignored.
EDGAR contact: EDGAR_CONTACT_NAME and EDGAR_CONTACT_EMAIL environment variables.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from ..investigations import Investigation
from . import dossier as live_dossier
from .edgar import EdgarClient, ingest
from .extract import extract_all
from .livegraph import build, load_relationships, verify_edge
from .store import Store

REL_DIR = Path(__file__).resolve().parents[2] / "data" / "relationships"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_parser(sub) -> None:
    live = sub.add_parser("live", help="SEC EDGAR live pipeline (human-gated)")
    live.add_argument("--data", default=os.environ.get("PANOPTICON_DATA", "data/live"))
    ls = live.add_subparsers(dest="live_cmd", required=True)
    p = ls.add_parser("ingest")
    p.add_argument("ticker")
    p.add_argument("--since", default="2025-06-01")
    p.add_argument("--forms", default="10-K,10-Q,8-K,4")
    ls.add_parser("status")
    p = ls.add_parser("review")
    p.add_argument("ticker", nargs="?", help="optional; claims are filtered to this subject")
    p.add_argument("--family")
    p.add_argument("--limit", type=int, default=None, help="text default 25; json default all")
    p.add_argument("--status", default="pending", help="pending | approved | rejected | all")
    p.add_argument("--format", choices=("text", "json"), default="text")
    p = ls.add_parser("list", help="claims by status (pending | approved | rejected | superseded | all)")
    p.add_argument("--ticker")
    p.add_argument("--family")
    p.add_argument("--status", default="pending")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--format", choices=("text", "json"), default="text")
    p = ls.add_parser("closure-check", help="verify a claim family is closed (one active claim per period, checks)")
    p.add_argument("--ticker", required=True)
    p.add_argument("--family", required=True)
    ls.add_parser("lifecycle-sync", help="apply the supersession rule to claims approved before it existed")
    p = ls.add_parser("show")
    p.add_argument("claim_id")
    for name in ("approve", "reject"):
        p = ls.add_parser(name)
        p.add_argument("claim_ids", nargs="*")
        p.add_argument("--family", help="act on ALL pending claims in this family (after spot-checking them)")
        p.add_argument("--by", required=True, help="reviewer name (recorded in claim history)")
        p.add_argument("--note")
    p = ls.add_parser("dossier")
    p.add_argument("ticker")
    p.add_argument("--as-of", default=None)
    p = ls.add_parser("audit", help="every displayed number with its SEC fact contexts")
    p.add_argument("ticker")
    p.add_argument("--periods", help="comma-separated quarter ends, e.g. 2025-05-29,2026-05-28")
    p.add_argument("--as-of", default=None)
    for name in ("source-check", "reconcile"):
        p = ls.add_parser(name, help="numeric source-presence check with line-item/period-header proximity "
                                     "(not semantic reconciliation)")
        p.add_argument("ticker")
        p.add_argument("--as-of", default=None)
        p.add_argument("--periods", help="only these quarter ends (comma-separated)")
    p = ls.add_parser("signals")
    p.add_argument("ticker")
    p.add_argument("--as-of", default=None)
    rel = ls.add_parser("rel")
    rs = rel.add_subparsers(dest="rel_cmd", required=True)
    p = rs.add_parser("list")
    p.add_argument("ticker", nargs="?", default="MU")
    p = rs.add_parser("verify")
    p.add_argument("edge_id")
    p.add_argument("--ticker", default="MU")
    p.add_argument("--by", required=True)
    p.add_argument("--evidence", required=True, help="citation, e.g. '10-K FY2026 Item 1A p.24'")
    p = rs.add_parser("unverify")
    p.add_argument("edge_id")
    p.add_argument("--ticker", default="MU")
    p.add_argument("--by", required=True)


def review_packet(store: Store, rows: list[dict], args) -> dict:
    """Claims with full evidence, calculation, history and mechanical checks (checks never approve)."""
    from .verify import verify_claim
    claims = [{**r, "history": [dict(h) for h in store.history(r["claim_id"])], "checks": verify_claim(store, r)}
              for r in rows]
    return {"generated_at": _now(), "filters": {"ticker": args.ticker, "family": args.family,
                                                 "status": args.status}, "count": len(claims), "claims": claims,
            "note": "Checks are mechanical aids. Approval requires a human decision: live approve <id> --by NAME."}


def _review(store: Store, args, out) -> int:
    rows = [r for r in store.claims(status=None if args.status == "all" else args.status)
            if (not args.family or r["family"] == args.family)
            and (not args.ticker or r["subject"] == args.ticker.upper())]
    if args.format == "json":
        rows = rows[: args.limit] if args.limit else rows
        out.write(json.dumps(review_packet(store, rows, args), indent=2) + "\n")
        return 0
    limit = args.limit or 25
    for r in rows[:limit]:
        out.write(f"{r['claim_id']}  {r['status']:<10} {r['family']:<15} {r['event_time'][:10]}  "
                  f"{r['statement'][:150]}\n")
    out.write(f"-- {len(rows)} {args.status} claim(s)" + (f", showing {limit}" if len(rows) > limit else "") + "\n")
    return 0


def closure_check(store: Store, ticker: str, family: str, out) -> int:
    from collections import Counter
    from .verify import verify_claim
    mine = [c for c in store.claims() if c["subject"] == ticker and c["family"] == family]
    counts = Counter(c["status"] for c in mine)
    active = [c for c in store.active_claims(_now()) if c["subject"] == ticker and c["family"] == family]
    per_period = Counter(c["event_time"] for c in active)
    results = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        out.write(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else "") + "\n")

    out.write(f"{ticker} {family}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
              + f"; all historical records {len(mine)}\n")
    check("no pending claims remain", counts.get("pending", 0) == 0, f"{counts.get('pending', 0)} pending")
    check("exactly one active claim per event_time", bool(active) and all(v == 1 for v in per_period.values()),
          f"{len(active)} active over {len(per_period)} periods")
    check("only approved claims are graph-eligible", all(c["status"] == "approved" for c in active))
    check("every superseded claim has an approved successor in its chain",
          all(any(d["status"] == "approved" and c["claim_id"] in store.ancestors(d["claim_id"]) for d in mine)
              for c in mine if c["status"] == "superseded"))
    if family == "INVENTORY":
        check("all active claims use 'ending-inventory coverage'",
              all("ending-inventory coverage" in c["statement"] for c in active))
    versions = {"INVENTORY": "inventory_v2", "PRICING_MARGIN": "margin_v2", "CAPACITY_CAPEX": "capex_v2"}
    if family in versions:
        check(f"all active claims are {versions[family]} with structured calculation",
              bool(active) and all((c.get("calculation") or {}).get("version") == versions[family] for c in active),
              "no active claims" if not active else "")
    for c in active:
        v = verify_claim(store, c)
        ok = all(v.get(k) is True for k in ("calculation_reproduces", "inputs_match_recomputation",
                                            "no_quarantined_inputs", "available_at_equals_latest_input"))
        check(f"{c['claim_id']} {c['event_time']} checks", ok, ", ".join(
            f"{k}={v.get(k)}" for k in ("calculation_reproduces", "inputs_match_recomputation",
                                         "no_quarantined_inputs", "available_at_equals_latest_input")))
    closed = all(results)
    out.write(f"-- {family} {'CLOSED' if closed else 'NOT CLOSED'}\n")
    return 0 if closed else 1


def run(args) -> int:
    store = Store(args.data)
    out = sys.stdout
    c = args.live_cmd
    if c == "ingest":
        client = EdgarClient()
        res = ingest(store, client, args.ticker, args.since, tuple(args.forms.split(",")),
                     log=lambda s: print(s, file=sys.stderr))
        res["candidates_created"] = extract_all(store, args.ticker.upper(), args.since)
        out.write(json.dumps(res, indent=2) + "\n")
    elif c == "status":
        out.write(json.dumps(store.stats(), indent=2) + "\n")
    elif c == "list":
        return _review(store, args, out)
    elif c == "lifecycle-sync":
        out.write(f"{store.sync_supersession()} predecessor claim(s) moved to superseded\n")
    elif c == "closure-check":
        return closure_check(store, args.ticker.upper(), args.family, out)
    elif c == "review":
        return _review(store, args, out)
    elif c == "show":
        r = store.claim(args.claim_id)
        if not r:
            print("no unique claim with that id prefix", file=sys.stderr)
            return 1
        r["history"] = [dict(h) for h in store.history(r["claim_id"])]
        out.write(json.dumps(r, indent=2) + "\n")
    elif c in ("approve", "reject"):
        ids = list(args.claim_ids)
        if args.family:
            ids += [r["claim_id"] for r in store.claims(status="pending") if r["family"] == args.family]
        if not ids:
            print("give claim ids or --family", file=sys.stderr)
            return 1
        for cid in ids:
            r = store.claim(cid)
            if not r:
                print(f"skip {cid}: not found / ambiguous", file=sys.stderr)
                continue
            store.review(r["claim_id"], "approved" if c == "approve" else "rejected", args.by, args.note)
            out.write(f"{c.rstrip('e')}ed {r['claim_id']}: {r['statement'][:100]}\n")
    elif c == "dossier":
        rel = load_relationships(REL_DIR / f"{args.ticker.upper()}.json")
        out.write(live_dossier.render(store, rel, args.ticker.upper(), args.as_of or _now()) + "\n")
    elif c == "audit":
        from .audit import render_audit, table_fingerprint
        out.write(render_audit(store, args.as_of, args.periods.split(",") if args.periods else None) + "\n")
        out.write(f"\ntable fingerprint: {table_fingerprint(store, args.as_of)}\n")
    elif c in ("source-check", "reconcile"):
        from .audit import source_check
        rows = source_check(store, args.as_of)
        if args.periods:
            rows = [r for r in rows if r["quarter_end"] in args.periods.split(",")]
        for r in rows:
            out.write(f"{r['level']:<15} {r['metric']:<18} {r['quarter_end']}  {r['concept'][:28]:<28} "
                      f"{r['value_millions']:>9}M  {r['accession']}  {r['fact']}  | {r['context']}\n")
        counts = {lv: sum(r["level"] == lv for r in rows) for lv in
                  ("STATEMENT_LINE_MATCH", "LINE_ITEM_MATCH", "LABEL_ONLY", "NUMBER_ONLY", "MISSING",
                   "NOT_INGESTED")}
        out.write(f"-- numeric source-presence check: {counts}. This is a proximity heuristic; statement, column and "
                  f"line-item placement still require a manual read of each filing.\n")
        return 1 if counts["MISSING"] else 0
    elif c == "signals":
        rel = load_relationships(REL_DIR / f"{args.ticker.upper()}.json")
        as_of = args.as_of or _now()
        g = build(store, rel, as_of)
        inv = Investigation(g, args.ticker.upper(), date.fromisoformat(as_of[:10]))
        out.write(json.dumps(inv.signals, indent=2) + "\n")
        if not inv.signals:
            print("no packets: packets need approved claims on verified edges", file=sys.stderr)
    elif c == "rel":
        path = REL_DIR / f"{args.ticker.upper()}.json"
        if args.rel_cmd == "list":
            rel = load_relationships(path)
            names = {e["id"]: e["name"] for e in rel["entities"]}
            for e in rel["edges"]:
                out.write(f"{e['id']}  {'VERIFIED  ' if e['verified'] else 'unverified'}  {names[e['source']]} "
                          f"-{e['relationship']}-> {names[e['target']]}  s={e['strength']}  {e.get('notes', '')}\n")
        else:
            e = verify_edge(path, args.edge_id, args.by, getattr(args, "evidence", ""),
                            verified=args.rel_cmd == "verify")
            out.write(json.dumps(e, indent=1) + "\n")
    return 0
