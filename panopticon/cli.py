"""Command line.

  python -m panopticon home HLCN
  python -m panopticon ask HLCN treated_as_certain
  python -m panopticon dossier HLCN > dossier.md
  python -m panopticon shock --preset export_restriction
  python -m panopticon shock --node CU --change 0.25 --label "Copper +25%"
  python -m panopticon signals HLCN --ledger out/signals.jsonl
  python -m panopticon serve --port 8000
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import seed
from .dossier import render
from .investigations import QUESTIONS, Investigation
from .shocks import Shock, propagate
from .signals import FeedbackRegistry, SignalLedger


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="panopticon")
    ap.add_argument("--as-of", default=seed.AS_OF.isoformat(), help="point-in-time date (YYYY-MM-DD)")
    ap.add_argument("--feedback", help="OptLab feedback JSON (signal priors, edge multipliers)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("home", "dossier", "contradictions", "cone"):
        sub.add_parser(name).add_argument("ticker")
    a = sub.add_parser("ask")
    a.add_argument("ticker")
    a.add_argument("question", choices=list(QUESTIONS))
    s = sub.add_parser("shock")
    s.add_argument("--preset", choices=list(seed.SHOCK_PRESETS))
    s.add_argument("--node")
    s.add_argument("--change", type=float)
    s.add_argument("--label", default="custom shock")
    s.add_argument("--json", action="store_true")
    sg = sub.add_parser("signals")
    sg.add_argument("ticker")
    sg.add_argument("--ledger")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--host", default="127.0.0.1")
    sub.add_parser("graph")
    args = ap.parse_args(argv)

    as_of = date.fromisoformat(args.as_of)
    g = seed.build()
    fb = None
    if args.feedback:
        fb = FeedbackRegistry.load(args.feedback)
        fb.apply_to_graph(g)

    out = sys.stdout
    if args.cmd == "serve":
        from .server import serve
        serve(args.host, args.port, as_of, fb)
        return 0
    if args.cmd == "graph":
        out.write(g.to_json(as_of) + "\n")
        return 0
    if args.cmd == "shock":
        if args.preset:
            node, change, label = seed.SHOCK_PRESETS[args.preset]
        elif args.node and args.change is not None:
            node, change, label = args.node, args.change, args.label
        else:
            ap.error("shock needs --preset or --node and --change")
        res = propagate(g, Shock(node, change, label), as_of)
        out.write((json.dumps(res.to_dict(), indent=2) if args.json else res.render()) + "\n")
        return 0

    inv = Investigation(g, args.ticker.upper(), as_of, fb)
    if args.cmd == "home":
        out.write(json.dumps(inv.home(), indent=2) + "\n")
    elif args.cmd == "ask":
        out.write(json.dumps(inv.answer(args.question), indent=2) + "\n")
    elif args.cmd == "dossier":
        out.write(render(inv) + "\n")
    elif args.cmd == "contradictions":
        out.write(json.dumps([c.to_dict() for c in inv.contradictions], indent=2) + "\n")
    elif args.cmd == "cone":
        out.write(json.dumps({"focal": inv.focal, "hops": inv.cone.by_hop(),
                              "scored_paths": [s.to_dict(g) for s in inv.cone.scored]}, indent=2) + "\n")
    elif args.cmd == "signals":
        if args.ledger:
            n = SignalLedger(args.ledger).append(inv.signals)
            print(f"appended {n} new packets to {args.ledger}", file=sys.stderr)
        out.write(json.dumps(inv.signals, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
