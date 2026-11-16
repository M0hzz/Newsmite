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
    p.add_argument("--family")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--status", default="pending")
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
    p = ls.add_parser("reconcile", help="find each input fact's value in the filed statement text")
    p.add_argument("ticker")
    p.add_argument("--as-of", default=None)
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
    elif c == "review":
        rows = [r for r in store.claims(status=args.status) if not args.family or r["family"] == args.family]
        for r in rows[: args.limit]:
            out.write(f"{r['claim_id']}  {r['family']:<15} {r['event_time'][:10]}  {r['statement'][:150]}\n")
        out.write(f"-- {len(rows)} {args.status} claim(s)" + (f", showing {args.limit}" if len(rows) > args.limit
                                                               else "") + "\n")
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
            out.write(f"{c}d {r['claim_id']}: {r['statement'][:100]}\n")
    elif c == "dossier":
        rel = load_relationships(REL_DIR / f"{args.ticker.upper()}.json")
        out.write(live_dossier.render(store, rel, args.ticker.upper(), args.as_of or _now()) + "\n")
    elif c == "audit":
        from .audit import render_audit, table_fingerprint
        out.write(render_audit(store, args.as_of, args.periods.split(",") if args.periods else None) + "\n")
        out.write(f"\ntable fingerprint: {table_fingerprint(store, args.as_of)}\n")
    elif c == "reconcile":
        from .audit import reconcile
        rows = reconcile(store, args.as_of)
        for r in rows:
            flag = {True: "FOUND  ", False: "MISSING", None: "n/a    "}[r["found_in_filing"]]
            out.write(f"{flag} {r['metric']:<18} {r['quarter_end']}  {r['value_millions']:>9}M  {r['accession']}  "
                      f"{r['fact']}  | {r['context']}\n")
        bad = [r for r in rows if r["found_in_filing"] is False]
        out.write(f"-- {len(rows)} input facts checked, {len(bad)} not found in their filing text\n")
        return 1 if bad else 0
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
