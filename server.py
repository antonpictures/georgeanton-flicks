#!/usr/bin/env python3
"""Georgeanton Flicks server — the shelf that georgeanton.com serves.

    python3 server.py            # http://127.0.0.1:8099
    PORT=8099 python3 server.py

Routes:
    GET  /                 the site
    GET  /api/catalog      {"movies":[…], "built":ts, "sources":n}
    GET  /api/health       quick liveness + library counts
    POST /api/refresh      run the curator in the background (Alice's hands)
    GET  /<file>           static files from this directory

The server never proxies video: playback is a YouTube embed, so the bandwidth
and the takedown risk stay with YouTube, and the shelf stays up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "movies_catalog.json"
PORT = int(os.environ.get("PORT", "8099"))
PY = "/Users/ioanganton/.local/share/autoclip/venv/bin/python"

_refresh_lock = threading.Lock()
_refresh_state: dict = {"running": False, "started": 0, "finished": 0, "last_log": ""}


def catalog_payload() -> dict:
    try:
        rows = json.loads(CATALOG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        rows = []
    rows.sort(key=lambda m: (-(m.get("year") or 0), -(m.get("added_at") or 0)))
    sources = len({m.get("source") for m in rows if m.get("source")})
    return {
        "movies": rows,
        "count": len(rows),
        "playable": sum(1 for m in rows if m.get("status") == "ok"),
        "sources": sources,
        "built": int(CATALOG.stat().st_mtime) if CATALOG.exists() else 0,
    }


def run_curator() -> None:
    with _refresh_lock:
        if _refresh_state["running"]:
            return
        _refresh_state.update(running=True, started=time.time())
    try:
        p = subprocess.run([PY, str(HERE / "curator.py")], cwd=str(HERE),
                           capture_output=True, text=True, timeout=3600)
        _refresh_state["last_log"] = (p.stdout or "")[-4000:]
    except Exception as e:  # noqa: BLE001
        _refresh_state["last_log"] = f"curator failed: {e}"
    finally:
        _refresh_state.update(running=False, finished=time.time())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE), **kw)

    def log_message(self, fmt, *args):  # keep the log quiet but present
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/catalog":
            return self._json(catalog_payload())
        if path == "/api/health":
            p = catalog_payload()
            return self._json({"ok": True, "count": p["count"], "playable": p["playable"],
                               "refresh": _refresh_state})
        if path == "/api/refresh":
            return self._json({"refresh": _refresh_state})
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0] == "/api/refresh":
            if _refresh_state["running"]:
                return self._json({"started": False, "reason": "already running",
                                   "refresh": _refresh_state}, 202)
            threading.Thread(target=run_curator, daemon=True).start()
            return self._json({"started": True})
        self.send_error(404)


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Georgeanton Flicks → http://127.0.0.1:{PORT}  ({HERE})", flush=True)
    if CATALOG.exists():
        p = catalog_payload()
        print(f"library: {p['count']} films · {p['playable']} playable · "
              f"{p['sources']} uploaders", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
