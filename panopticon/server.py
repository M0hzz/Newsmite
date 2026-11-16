"""Minimal stdlib HTTP server: JSON API + single-page UI. No dependencies."""
from __future__ import annotations

import json
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import seed
from .dossier import render
from .investigations import QUESTIONS, Investigation
from .shocks import Shock, propagate
from .signals import FeedbackRegistry

WEB = Path(__file__).parent / "web"


def make_handler(as_of: date, feedback: FeedbackRegistry | None):
    g = seed.build()
    if feedback:
        feedback.apply_to_graph(g)
    cache: dict[tuple[str, str], Investigation] = {}

    def inv(ticker: str, when: date) -> Investigation:
        key = (ticker, when.isoformat())
        if key not in cache:
            cache[key] = Investigation(g, ticker, when, feedback)
        return cache[key]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code: int, body: str | bytes, ctype: str = "application/json") -> None:
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                when = date.fromisoformat(q.get("as_of", as_of.isoformat()))
                t = q.get("ticker", "HLCN").upper()
                if u.path in ("/", "/index.html"):
                    return self._send(200, (WEB / "index.html").read_bytes(), "text/html")
                if u.path == "/api/meta":
                    return self._send(200, json.dumps({
                        "as_of": as_of.isoformat(),
                        "tickers": sorted(e.attrs["ticker"] for e in g.entities.values() if e.attrs.get("ticker")),
                        "presets": {k: v[2] for k, v in seed.SHOCK_PRESETS.items()},
                        "questions": QUESTIONS}))
                if u.path == "/api/home":
                    return self._send(200, json.dumps(inv(t, when).home()))
                if u.path == "/api/ask":
                    return self._send(200, json.dumps(inv(t, when).answer(q["q"])))
                if u.path == "/api/dossier":
                    return self._send(200, render(inv(t, when)), "text/markdown")
                if u.path == "/api/signals":
                    return self._send(200, json.dumps(inv(t, when).signals))
                if u.path == "/api/shock":
                    if "preset" in q:
                        node, change, label = seed.SHOCK_PRESETS[q["preset"]]
                    else:
                        node, change, label = q["node"], float(q["change"]), q.get("label", "custom")
                    return self._send(200, json.dumps(propagate(g, Shock(node, change, label), when).to_dict()))
                return self._send(404, json.dumps({"error": "not found"}))
            except KeyError as e:
                return self._send(400, json.dumps({"error": f"unknown {e}"}))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))

    return H


def serve(host: str, port: int, as_of: date, feedback: FeedbackRegistry | None = None) -> None:
    srv = ThreadingHTTPServer((host, port), make_handler(as_of, feedback))
    print(f"Market Panopticon on http://{host}:{port}  (as of {as_of})")
    srv.serve_forever()
