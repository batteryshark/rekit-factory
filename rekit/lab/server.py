"""``rekit serve`` — the local transport over the lab read-model (E7.0 / E7.3).

A dependency-light HTTP server (stdlib :mod:`http.server` only) that turns the
pure read-model into a live Mission Control:

- ``GET  /``              → the browser client (polls the API, renders the fleet).
- ``GET  /api/fleet``     → ``{fleet: [...view...], health: {...}}``.
- ``GET  /api/project?id`` → one project's full :func:`~.readmodel.project_view`.
- ``POST /api/answer``    → append an answer to a project's inbox — the writeback
  that unblocks a waiting :class:`~rekit.human.inbox.LedgerHumanChannel`.

The routing is a **pure function**, :func:`handle` — status/content-type/bytes for
a ``(method, path, body)`` — so it unit-tests without a socket; the
:class:`http.server.BaseHTTPRequestHandler` is a thin shell over it. A best-effort
background **notifier** fires a desktop notification when a new decision appears
(the "wait + notify" half of the design), guarded so it never sinks the server.

Observe (fleet / project) works for any run — even one launched from the plain
CLI — because it only reads ``$REKIT_HOME``. Launching / stopping runs from the UI
is E7.4 and rides on the same server later.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..human import inbox as _inbox
from ..ledger.home import projects_root
from .readmodel import fleet, health, project_detail, reap_stale

#: Default port — 7358 is "REKT" on a phone keypad, and chosen to avoid clashing
#: with common local dashboards (e.g. opencode-ensemble on 4747). Override with --port.
DEFAULT_PORT = 7358
DEFAULT_HOST = "127.0.0.1"


def _json(status: int, obj: Any) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8")


def _projects_base(root: str | Path | None) -> Path:
    return Path(root) if root is not None else projects_root()


_SUPERVISOR = None


def _supervisor():
    """The process-wide run supervisor (E7.4) — lazily built so importing the
    server never hard-depends on the launch machinery."""
    global _SUPERVISOR
    if _SUPERVISOR is None:
        from .supervisor import Supervisor
        _SUPERVISOR = Supervisor()
    return _SUPERVISOR


def handle(method: str, path: str, body: bytes = b"", *,
           root: str | Path | None = None) -> tuple[int, str, bytes]:
    """Route one request to ``(status, content_type, payload)`` — pure and testable.

    ``root`` overrides the projects dir (tests point it at a fixture); otherwise the
    read-model honours ``$REKIT_HOME``.
    """
    base = _projects_base(root)
    parsed = urlsplit(path)
    route = parsed.path
    query = parse_qs(parsed.query)

    if method == "GET" and route in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", CLIENT_HTML.encode("utf-8")

    if method == "GET" and route == "/api/fleet":
        views = fleet(base)
        return _json(200, {"fleet": views, "health": health(views)})

    if method == "GET" and route == "/api/project":
        pid = (query.get("id") or [""])[0]
        d = base / pid
        if not pid or not (d / "project.json").exists():
            return _json(404, {"error": "no such project"})
        return _json(200, project_detail(d))

    if method == "POST" and route == "/api/answer":
        try:
            data = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return _json(400, {"error": "invalid JSON body"})
        pid = data.get("projectId")
        qid = data.get("questionId")
        if not pid or not qid:
            return _json(400, {"error": "projectId and questionId are required"})
        d = base / pid
        if not (d / "project.json").exists():
            return _json(404, {"error": "no such project"})
        _inbox.answer(d, qid, data.get("value"))
        return _json(200, {"ok": True})

    if method == "POST" and route == "/api/run":
        try:
            data = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return _json(400, {"error": "invalid JSON body"})
        target, goal = data.get("target"), data.get("goal")
        if not target or not goal:
            return _json(400, {"error": "target and goal are required"})
        try:
            pid = _supervisor().launch(
                target, goal,
                harness=data.get("harness", "mock"),
                tier=data.get("tier", "cheap"),
                max_rounds=int(data.get("maxRounds", 8)),
                tools=data.get("tools") or None,
            )
        except Exception as exc:  # noqa: BLE001 — surface a clean error to the UI.
            return _json(500, {"error": f"launch failed: {exc}"})
        return _json(200, {"ok": True, "id": pid})

    if method == "POST" and route == "/api/stop":
        try:
            data = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return _json(400, {"error": "invalid JSON body"})
        pid = data.get("projectId")
        if not pid:
            return _json(400, {"error": "projectId is required"})
        return _json(200, {"ok": _supervisor().stop(pid)})

    if method == "GET" and route == "/api/skills":
        from .catalog import skills_catalog
        return _json(200, skills_catalog())

    if method == "GET" and route == "/api/harnesses":
        from .catalog import harnesses
        return _json(200, {"harnesses": harnesses()})

    return _json(404, {"error": "not found"})


# -- desktop notifications (best-effort) ------------------------------------


def new_notifications(seen: set[str], views: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Pure: given ids already notified and the current fleet, return the
    ``(project_id, question_text)`` pairs that are newly pending — and mutate
    ``seen`` to include them. Drives the notifier; unit-testable without firing."""
    fresh: list[tuple[str, str]] = []
    for v in views:
        for q in v.get("pending", []):
            key = f"{v['id']}:{q['id']}"
            if key not in seen:
                seen.add(key)
                fresh.append((v["id"], q.get("question", "a decision")))
    return fresh


def _desktop_notify(title: str, body: str) -> None:
    """Fire an OS notification, best-effort — never raises."""
    try:
        if sys.platform == "darwin":
            script = f'display notification {json.dumps(body)} with title {json.dumps(title)}'
            subprocess.run(["osascript", "-e", script], check=False,
                           capture_output=True, timeout=5)
        elif sys.platform.startswith("linux"):
            subprocess.run(["notify-send", title, body], check=False,
                           capture_output=True, timeout=5)
    except Exception:  # noqa: BLE001 — notifications are never load-bearing.
        pass


def _notifier_loop(base: Path, stop: threading.Event, *, interval: float = 2.0) -> None:
    seen: set[str] = set()
    # Seed with what is already pending so we only notify on *new* decisions.
    try:
        new_notifications(seen, fleet(base))
    except Exception:  # noqa: BLE001
        pass
    while not stop.wait(interval):
        try:
            reap_stale(base)  # also sweep zombies mid-session (idempotent)
            for pid, question in new_notifications(seen, fleet(base)):
                _desktop_notify(f"rekit · {pid} needs you", question)
        except Exception:  # noqa: BLE001
            pass


# -- the HTTP shell ---------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def _run(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        root = getattr(self.server, "rekit_root", None)
        status, ctype, payload = handle(method, self.path, body, root=root)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._run("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._run("POST")

    def log_message(self, *args: Any) -> None:  # keep the console quiet
        pass


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
                root: str | Path | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the threaded HTTP server, bound to a projects
    ``root`` (default: ``$REKIT_HOME/projects``). ``port=0`` picks a free port."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True  # request threads never keep the process alive
    httpd.rekit_root = root  # type: ignore[attr-defined]
    return httpd


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
          notify: bool = True) -> int:
    """Run Mission Control until interrupted. Returns a process exit code."""
    try:
        httpd = make_server(host, port)
    except OSError as exc:
        # Almost always "address already in use" — a clear message beats a traceback.
        print(f"rekit: could not start on {host}:{port} — {exc}")
        print(f"  something else is already using port {port}.")
        print(f"  → pick another port:   rekit serve --port {port + 1}")
        print(f"  → or see what's there:  lsof -nP -i :{port}")
        return 1
    base = _projects_base(None)
    reaped = reap_stale(base)  # clear zombie 'running' cards from a dead session
    if reaped:
        print(f"  reaped {len(reaped)} stale run(s) left by a previous session")
    stop = threading.Event()
    if notify:
        threading.Thread(target=_notifier_loop, args=(base, stop),
                         daemon=True).start()
    # Run the server on a background thread so the main thread can own Ctrl-C.
    # (shutdown() must be called from a *different* thread than serve_forever, or
    # it deadlocks — hence this split, not serve_forever() in the main thread.)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://{host}:{httpd.server_address[1]}"
    print(f"rekit lab — Mission Control at {url}")
    print(f"  reading {base}")
    print("  Ctrl-C to stop")
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        stop.set()
        httpd.shutdown()      # safe: serve_forever runs in server_thread
        httpd.server_close()
        server_thread.join(timeout=2)
    return 0


# -- the browser client (self-contained; polls the API) ---------------------

CLIENT_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>rekit · Mission Control</title>
<style>
:root{--bg:#080d17;--panel:#0e1626;--panel2:#121d31;--line:#1d2a44;--line2:#2a3b5c;
--ink:#d8e2f2;--ink2:#9fb0cc;--ink3:#647694;--green:#46e08a;--teal:#35d6c3;--violet:#a78bfa;
--amber:#f5b13d;--red:#f25563;--blue:#5aa8f2;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--sans);
background-image:linear-gradient(#0e1628 1px,transparent 1px),linear-gradient(90deg,#0e1628 1px,transparent 1px);
background-size:44px 44px}
header{display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--line);
background:rgba(10,17,32,.85);backdrop-filter:blur(6px);position:sticky;top:0;z-index:5}
.wm{font-family:var(--mono);font-weight:700;font-size:16px}.wm .k{color:var(--green)}
.sp{flex:1}.legend{display:flex;gap:14px;font-family:var(--mono);font-size:12px;color:var(--ink2)}
.legend i{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:5px}
.live{display:flex;align-items:center;gap:7px;font-family:var(--mono);font-size:11px;color:var(--green)}
.live i{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 1.5s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
main{padding:22px 24px 60px;max-width:1400px}
h2{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--ink3);margin:26px 0 12px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:15px}
.card{position:relative;background:linear-gradient(180deg,var(--panel),#0a1120);border:1px solid var(--line);
border-radius:10px;padding:15px 16px;overflow:hidden}
.stripe{position:absolute;left:0;top:0;bottom:0;width:3px}
.s-running .stripe{background:var(--green)}.s-blocked .stripe{background:var(--amber)}
.s-suspended .stripe{background:var(--violet)}.s-done .stripe{background:var(--blue)}
.s-idle .stripe{background:#43536f}.s-failed .stripe{background:var(--red)}
.ct{display:flex;align-items:flex-start;gap:10px;margin-bottom:10px}
.nm{font-family:var(--mono);font-weight:600;font-size:14px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.09em;padding:3px 9px;border-radius:99px;white-space:nowrap}
.p-running{background:rgba(70,224,138,.14);color:var(--green)}.p-blocked{background:rgba(245,177,61,.14);color:var(--amber)}
.p-suspended{background:rgba(167,139,250,.14);color:var(--violet)}.p-done{background:rgba(90,168,242,.14);color:var(--blue)}
.p-idle{background:var(--panel2);color:var(--ink3)}.p-failed{background:rgba(242,85,99,.14);color:var(--red)}
.goal{color:var(--ink2);font-size:12.5px;line-height:1.4;margin-bottom:11px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:11px}
.tag{font-family:var(--mono);font-size:10.5px;color:var(--ink2);border:1px solid var(--line);border-radius:6px;padding:3px 7px;background:var(--bg)}
.tag .k{color:var(--ink3)}
.counters{display:grid;grid-template-columns:repeat(5,1fr);gap:7px}
.c{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:6px 8px}
.c .v{font-family:var(--mono);font-weight:700;font-size:15px}.c .l{font-family:var(--mono);font-size:8.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-top:4px}
.c.f .v{color:var(--violet)}.c.l2 .v{color:var(--amber)}.c.d .v{color:var(--teal)}.c.s .v{color:var(--green)}.c.$ .v{color:var(--blue)}
.decisions .card{border-color:rgba(245,177,61,.35)}
.dq{font-size:14px;margin:2px 0 12px;line-height:1.4}
.acts{display:flex;gap:8px;flex-wrap:wrap}
button{font-family:var(--sans);cursor:pointer;border:0;border-radius:8px;padding:9px 14px;font-weight:600;font-size:13px}
.b-green{background:linear-gradient(180deg,#4fe895,#31c274);color:#04160c}
.b-violet{background:linear-gradient(180deg,#b8a3ff,#8b72e8);color:#120a2e}
.b-ghost{background:var(--panel2);color:var(--ink2);border:1px solid var(--line2)}
.b-deny{background:rgba(242,85,99,.08);color:#f5868f;border:1px solid rgba(242,85,99,.4)}
.src{font-family:var(--mono);font-size:11px;color:var(--ink3);margin-bottom:8px}
.src b{color:var(--ink2)}
.empty{color:var(--ink3);font-family:var(--mono);font-size:13px;padding:40px 0;text-align:center}
.ask{width:100%;background:var(--bg);border:1px solid var(--line2);border-radius:8px;color:var(--ink);font-family:var(--sans);padding:10px;margin-bottom:8px}
.card.clk{cursor:pointer;transition:.15s}.card.clk:hover{border-color:var(--line2);transform:translateY(-2px)}
.detail-head{display:flex;align-items:center;gap:12px;margin:4px 0 4px;flex-wrap:wrap}
.back{font-family:var(--mono);font-size:12px;color:var(--ink2);border:1px solid var(--line2);border-radius:7px;padding:7px 12px;background:var(--panel2)}
.back:hover{color:var(--ink)}
.dtitle{font-family:var(--mono);font-weight:600;font-size:18px}
.dgoal{color:var(--ink2);font-size:13px;margin:0 0 14px;line-height:1.4}
.kv{display:flex;gap:14px;font-family:var(--mono);font-size:12px;color:var(--ink3);flex-wrap:wrap;margin-bottom:16px}
.kv b{color:var(--ink)}.kv .teal{color:var(--teal)}
.tabs{display:flex;gap:3px;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:4px;margin-bottom:16px;width:fit-content}
.tab{font-family:var(--mono);font-size:12px;color:var(--ink3);padding:7px 14px;border-radius:6px;background:none;display:flex;gap:6px;align-items:center}
.tab.on{background:#17233b;color:var(--green)}.tab .n{background:var(--bg);border-radius:99px;padding:0 6px;font-size:10px;color:var(--ink3)}
.panel{background:linear-gradient(180deg,var(--panel),#0a1120);border:1px solid var(--line);border-radius:10px;padding:15px;margin-bottom:14px}
.panel h3{font-family:var(--mono);font-size:10.5px;text-transform:uppercase;letter-spacing:.13em;color:var(--ink3);margin:0 0 12px}
.find{display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.find:last-child{border-bottom:0}
.find .sev{width:5px;border-radius:3px;background:var(--violet);flex-shrink:0}
.find .ft{color:var(--ink);font-size:13px;line-height:1.4}.find .fp{font-family:var(--mono);font-size:10.5px;color:var(--ink3);margin-top:3px}
.lead{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font-size:12.5px}.lead:last-child{border-bottom:0}
.lead .lc{font-family:var(--mono);color:var(--amber)}.lead .lr{font-family:var(--mono);font-size:11px;color:var(--ink3);margin-left:auto}
.art{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:12px;padding:4px 0;color:var(--ink2)}.art .k{color:var(--ink3);font-size:10px;margin-left:auto;text-transform:uppercase}
.stream{background:var(--bg);border:1px solid var(--line);border-radius:10px;font-family:var(--mono);font-size:12px;max-height:540px;overflow:auto;padding:6px 0}
.ev{display:flex;gap:11px;padding:4px 13px;border-left:2px solid transparent}.ev:hover{background:var(--panel)}
.ev .ts{color:#43536f;flex-shrink:0}.ev .ty{flex-shrink:0;width:104px;font-size:10px;text-transform:uppercase;letter-spacing:.04em}.ev .m{color:var(--ink2);min-width:0}
.ev.run{border-left-color:var(--green)}.ev.run .ty{color:var(--green)}.ev.ledger .ty{color:var(--teal)}
.empty2{color:var(--ink3);font-size:12.5px;padding:8px 0}
.nav{font-family:var(--mono);font-size:12px;color:var(--ink3);background:none;border:0;padding:6px 11px;border-radius:6px;cursor:pointer}
.nav:hover{color:var(--ink)}.nav.on{color:var(--green);background:#17233b}
.newrun-btn{font-family:var(--sans);font-weight:600;font-size:13px;background:linear-gradient(180deg,#4fe895,#31c274);color:#04160c;border:0;border-radius:8px;padding:8px 14px;cursor:pointer}
.newrun-btn:hover{filter:brightness(1.06)}
.stop-btn{font-family:var(--mono);font-size:11px;color:#f5868f;background:rgba(242,85,99,.08);border:1px solid rgba(242,85,99,.35);border-radius:6px;padding:4px 9px;cursor:pointer}
.stop-btn:hover{background:rgba(242,85,99,.16)}
.form{max-width:720px;display:flex;flex-direction:column;gap:18px;margin-top:6px}
.field label{display:block;font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--ink3);margin-bottom:8px}
.field .hint{text-transform:none;letter-spacing:0;color:#43536f;font-size:11px;margin-left:8px}
.field input,.field textarea{width:100%;background:var(--bg);border:1px solid var(--line2);border-radius:8px;color:var(--ink);font-family:var(--sans);font-size:14px;padding:11px 13px;outline:0}
.field textarea{min-height:70px;resize:vertical;line-height:1.5}
.field input:focus,.field textarea:focus{border-color:var(--green)}
.field input.mono{font-family:var(--mono);font-size:13px}
.row{display:flex;gap:12px;flex-wrap:wrap}.row>.field{flex:1;min-width:150px}
.seg{display:flex;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:3px;width:fit-content;flex-wrap:wrap}
.seg button{font-family:var(--mono);font-size:12px;color:var(--ink3);background:none;border:0;padding:7px 13px;border-radius:6px;cursor:pointer}
.seg button.on{background:#17233b;color:var(--green)}
.expl{font-size:12px;color:var(--ink3);margin:6px 0 0;line-height:1.5}
.capgrp{border:1px solid var(--line);border-radius:9px;background:var(--bg);margin-bottom:8px;overflow:hidden}
.capgrp .h{display:flex;align-items:center;gap:9px;padding:10px 12px;cursor:pointer}.capgrp .h:hover{background:var(--panel2)}
.capgrp .cn{font-family:var(--mono);font-size:12.5px;color:var(--ink);font-weight:600}.capgrp .cc{font-family:var(--mono);font-size:11px;color:var(--ink3);margin-left:auto}
.capgrp .b{display:none;padding:4px 12px 10px;flex-direction:column;gap:6px}.capgrp.open .b{display:flex}
.skrow{display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--line);border-radius:7px;font-family:var(--mono);font-size:12px;cursor:pointer}
.skrow.on{border-color:#2ea86a;background:rgba(70,224,138,.05)}
.skrow .chk{width:16px;height:16px;border-radius:4px;border:1.5px solid var(--line2);flex-shrink:0;display:grid;place-items:center;color:transparent}.skrow.on .chk{background:var(--green);border-color:var(--green);color:#04160c}
.skrow .sn{color:var(--ink)}.skrow .meta{margin-left:auto;display:flex;gap:7px;align-items:center}
.ttier{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;padding:2px 7px;border-radius:5px}
.ttier.ro{background:rgba(70,224,138,.14);color:var(--green)}.ttier.gated{background:rgba(245,177,61,.14);color:var(--amber)}.ttier.na{background:rgba(242,85,99,.1);color:var(--red)}
.launch{align-self:flex-start;font-family:var(--sans);font-weight:700;font-size:14px;background:linear-gradient(180deg,#4fe895,#31c274);color:#04160c;border:0;border-radius:9px;padding:12px 22px;cursor:pointer}
.launch:hover{filter:brightness(1.06)}
.fbtn{font-family:var(--mono);font-size:10.5px;color:var(--ink3);border:1px solid var(--line);border-radius:6px;padding:3px 9px;background:none;cursor:pointer;margin-right:6px}.fbtn.on{color:var(--green);border-color:#2ea86a}
.hrow{display:flex;align-items:center;gap:11px;padding:11px 13px;border:1px solid var(--line);border-radius:8px;background:var(--bg);margin-bottom:8px}
.hrow .hn{font-family:var(--mono);font-size:13px;color:var(--ink)}.hrow .hd{font-size:12px;color:var(--ink3);margin-top:2px}
.hrow .hs{margin-left:auto;font-family:var(--mono);font-size:10px;text-transform:uppercase;letter-spacing:.06em;padding:3px 8px;border-radius:5px}
.hs.available{background:rgba(70,224,138,.14);color:var(--green)}.hs.unconfigured{background:rgba(245,177,61,.14);color:var(--amber)}.hs.planned{background:var(--panel2);color:var(--ink3)}
</style></head>
<body>
<header>
  <span class="wm">RE<span class="k">KIT</span></span>
  <button class="nav" id="nav-fleet" onclick="showFleet()">Fleet</button>
  <button class="nav" id="nav-skills" onclick="showSkills()">Skills</button>
  <div class="sp"></div>
  <div class="legend" id="legend"></div>
  <button class="newrun-btn" onclick="showNewRun()">+ New Run</button>
  <div class="live"><i></i><span id="tick">live</span></div>
</header>
<main>
  <div id="fleet-view">
    <div id="decisions"></div>
    <h2>Fleet</h2>
    <div class="grid" id="fleet"><div class="empty">connecting…</div></div>
  </div>
  <div id="detail-view" style="display:none"></div>
  <div id="newrun-view" style="display:none"></div>
  <div id="skills-view" style="display:none"></div>
</main>
<script>
const $=id=>document.getElementById(id);
const PILL={running:'p-running',blocked:'p-blocked',suspended:'p-suspended',done:'p-done',idle:'p-idle',failed:'p-failed'};
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function answer(pid,qid,val){
  await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({projectId:pid,questionId:qid,value:val})});
  refresh();
}
function decisionControls(pid,q){
  const k=q.kind;
  if(k==='confirm'){return `<div class="acts">
    <button class="b-green" onclick="answer('${pid}','${q.id}','yes')">Allow &amp; run</button>
    <button class="b-deny" onclick="answer('${pid}','${q.id}','no')">Deny</button></div>`;}
  if(k==='tool'){const t=(q.extra&&q.extra.tool)||'the tool';return `<div class="acts">
    <button class="b-violet" onclick="answer('${pid}','${q.id}','install')">Install ${esc(t)} &amp; resume</button>
    <button class="b-ghost" onclick="answer('${pid}','${q.id}','manual')">Install manually</button>
    <button class="b-ghost" onclick="answer('${pid}','${q.id}','skip')">Skip &amp; accept the gap</button></div>`;}
  if(k==='present_choices'){return `<div class="acts">`+(q.options||[]).map(o=>
    `<button class="b-ghost" onclick="answer('${pid}','${q.id}',${JSON.stringify(o)})">${esc(o)}</button>`).join('')+`</div>`;}
  // ask
  return `<textarea class="ask" id="a-${q.id}" placeholder="type your answer…"></textarea>
    <div class="acts"><button class="b-green" onclick="answer('${pid}','${q.id}',document.getElementById('a-${q.id}').value)">Send</button></div>`;
}
function renderDecisions(fleet){
  const pend=[];fleet.forEach(v=>(v.pending||[]).forEach(q=>pend.push([v,q])));
  if(!pend.length){$('decisions').innerHTML='';return;}
  $('decisions').innerHTML=`<h2>Decisions — ${pend.length} awaiting you</h2><div class="grid decisions">`+
    pend.map(([v,q])=>`<div class="card s-${q.kind==='tool'?'suspended':'blocked'}"><span class="stripe"></span>
      <div class="src">from <b>${esc(v.id)}</b> · ${esc(q.kind)}</div>
      <div class="dq">${esc(q.question)}</div>${decisionControls(v.id,q)}</div>`).join('')+`</div>`;
}
function card(v){
  const r=v.run||{},c=r.counters||{},cost=r.cost||{};
  return `<div class="card clk s-${v.status}" onclick="openProject('${v.id}')"><span class="stripe"></span>
    <div class="ct"><div class="nm">${esc(v.id)}</div>${v.status==='running'?`<button class="stop-btn" onclick="event.stopPropagation();stopRun('${v.id}')">stop</button>`:''}<span class="pill ${PILL[v.status]||'p-idle'}">${esc(v.status)}</span></div>
    <div class="goal">${esc(r.goal||'—')}</div>
    <div class="meta"><span class="tag"><span class="k">R</span> ${r.round||0}/${r.maxRounds||0}</span>
      <span class="tag">${esc(r.tier||'—')}</span>
      <span class="tag"><span class="k">${esc(r.harness||'—')}</span> ${esc(r.model||'')}</span></div>
    <div class="counters">
      <div class="c f"><div class="v">${c.findings||0}</div><div class="l">find</div></div>
      <div class="c l2"><div class="v">${c.leads||0}</div><div class="l">leads</div></div>
      <div class="c d"><div class="v">${c.derivations||0}</div><div class="l">deriv</div></div>
      <div class="c s"><div class="v">${c.skillRuns||0}</div><div class="l">skills</div></div>
      <div class="c $"><div class="v">$${(cost.usd||0).toFixed(2)}</div><div class="l">spend</div></div>
    </div></div>`;
}
function render(data){
  const f=data.fleet||[],h=data.health||{};
  $('legend').innerHTML=[['running','var(--green)'],['blocked','var(--amber)'],['suspended','var(--violet)'],['done','var(--blue)']]
    .map(([k,c])=>`<span><i style="background:${c}"></i>${h[k]||0} ${k}</span>`).join('');
  renderDecisions(f);
  $('fleet').innerHTML=f.length?f.map(card).join(''):'<div class="empty">no projects yet — start a run from the CLI</div>';
}
let view='fleet',currentId=null,currentTab='overview',lastDetail=null,actFilter='all';
let nr={harness:'mock',tier:'cheap',rounds:8,openCaps:new Set(),target:'',goal:''};
function tick(){$('tick').textContent='live · '+new Date().toLocaleTimeString();}
function fail(){$('tick').textContent='reconnecting…';}
function refresh(){if(view==='detail')return pollDetail();if(view==='fleet')return pollFleet();}
const VIEWS=['fleet-view','detail-view','newrun-view','skills-view'];
function hideAll(){VIEWS.forEach(id=>$(id).style.display='none');}
function navHL(){['fleet','skills'].forEach(n=>{const b=$('nav-'+n);if(b)b.classList.toggle('on',view===n);});}
function openProject(id){view='detail';currentId=id;currentTab='overview';actFilter='all';lastDetail=null;
  hideAll();const dv=$('detail-view');dv.style.display='';dv.innerHTML='<div class="empty">loading…</div>';navHL();pollDetail();}
function showFleet(){view='fleet';currentId=null;hideAll();$('fleet-view').style.display='';navHL();pollFleet();}
function showSkills(){view='skills';hideAll();$('skills-view').style.display='';navHL();renderSkills();}
function showNewRun(){view='newrun';hideAll();$('newrun-view').style.display='';navHL();renderNewRun();}
function setTab(t){currentTab=t;if(lastDetail)renderDetail(lastDetail);}
function setActFilter(f){actFilter=f;if(lastDetail)renderDetail(lastDetail);}
async function stopRun(id){try{await fetch('/api/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({projectId:id})});}catch(e){}refresh();}
async function pollFleet(){try{const r=await fetch('/api/fleet');render(await r.json());tick();}catch(e){fail();}}
async function pollDetail(){if(!currentId)return;try{
    const r=await fetch('/api/project?id='+encodeURIComponent(currentId));const v=await r.json();
    if(v.error){showFleet();return;}renderDetail(v);tick();
  }catch(e){fail();}}
function counter(v,color,label){return `<div class="c"><div class="v" style="color:${color}">${v}</div><div class="l">${label}</div></div>`;}
function renderDetail(v){
  lastDetail=v;
  const r=v.run||{},c=r.counters||{},cost=r.cost||{};
  const pend=v.pending||[],find=v.findings||[],leads=v.leads||[],arts=v.artifacts||[],evs=v.events||[];
  const tabBtn=(id,label,n)=>`<button class="tab ${currentTab===id?'on':''}" onclick="setTab('${id}')">${label}${n!=null?`<span class="n">${n}</span>`:''}</button>`;
  let pane='';
  if(currentTab==='overview'){
    pane=`<div class="counters" style="max-width:660px">`+
      counter(c.findings||0,'var(--violet)','findings')+counter(c.leads||0,'var(--amber)','leads')+
      counter(c.derivations||0,'var(--teal)','derived')+counter(c.skillRuns||0,'var(--green)','skills')+
      counter('$'+(cost.usd||0).toFixed(2),'var(--blue)','spend')+`</div>`;
    if(pend.length) pane+=`<div class="panel"><h3>Decisions — ${pend.length} awaiting you</h3>`+
      pend.map(q=>`<div style="margin-bottom:12px"><div class="dq">${esc(q.question)}</div>${decisionControls(v.id,q)}</div>`).join('')+`</div>`;
  } else if(currentTab==='ledger'){
    pane=`<div class="panel"><h3>Findings · ${find.length}</h3>`+
      (find.length?find.map(f=>`<div class="find"><div class="sev"></div><div><div class="ft">${esc(f.note||f.text||f.summary||'finding')}</div>${(f.artifactPath||f.artifact)?`<div class="fp">${esc(f.artifactPath||f.artifact)}</div>`:''}</div></div>`).join(''):'<div class="empty2">no findings yet</div>')+`</div>`;
    pane+=`<div class="panel"><h3>Install leads · ${leads.length}</h3>`+
      (leads.length?leads.map(l=>`<div class="lead"><span class="lc">${esc(l.capability)}</span><span style="color:var(--ink3)">for ${esc(l.kind)}</span>${(l.requires&&l.requires.length)?`<span class="lr">needs ${esc(l.requires.join(', '))}</span>`:''}</div>`).join(''):'<div class="empty2">none</div>')+`</div>`;
    pane+=`<div class="panel"><h3>Artifacts · ${arts.length}</h3>`+
      (arts.length?arts.map(a=>`<div class="art">${a.isTree?'▸':'◦'} ${esc(a.path||a.id)}${a.analyzed?' <span style="color:var(--green)">✓</span>':''}<span class="k">${esc(a.kind)}</span></div>`).join(''):'<div class="empty2">none</div>')+`</div>`;
  } else {
    const filtered=evs.filter(e=>actFilter==='all'?true:actFilter==='findings'?e.type==='finding_recorded':e.source===actFilter);
    pane=`<div style="margin-bottom:10px">${['all','run','ledger','findings'].map(f=>`<button class="fbtn ${actFilter===f?'on':''}" onclick="setActFilter('${f}')">${f}</button>`).join('')}</div>
      <div class="stream">`+
      (filtered.length?filtered.map(e=>`<div class="ev ${e.source}"><span class="ts">${(e.ts||'').slice(11,19)}</span><span class="ty">${esc(e.type)}</span><span class="m">${esc(e.msg)}</span></div>`).join(''):'<div class="empty2" style="padding:14px">no events</div>')+`</div>`;
  }
  $('detail-view').innerHTML=`
    <div class="detail-head"><button class="back" onclick="showFleet()">← Fleet</button>
      <span class="dtitle">${esc(v.id)}</span><span class="pill ${PILL[v.status]||'p-idle'}">${esc(v.status)}</span>${v.status==='running'?`<button class="stop-btn" onclick="stopRun('${v.id}')">stop</button>`:''}</div>
    <div class="dgoal">${esc(r.goal||'—')}</div>
    <div class="kv"><span>R <b>${r.round||0}/${r.maxRounds||0}</b></span><span>tier <b>${esc(r.tier||'—')}</b></span>
      <span>${esc(r.harness||'—')} <b class="teal">${esc(r.model||'')}</b></span><span>spend <b>$${(cost.usd||0).toFixed(2)}</b></span></div>
    <div class="tabs">${tabBtn('overview','Overview')}${tabBtn('ledger','Ledger',find.length)}${tabBtn('activity','Activity',evs.length)}</div>
    <div class="pane">${pane}</div>`;
}
async function renderNewRun(){
  let harn=[],cat={capabilities:[]};
  try{harn=(await (await fetch('/api/harnesses')).json()).harnesses||[];}catch(e){}
  try{cat=await (await fetch('/api/skills')).json();}catch(e){}
  const harnOpts=harn.filter(h=>h.status!=='planned').map(h=>`<button class="${nr.harness===h.name?'on':''}" onclick="setHarness('${h.name}')">${esc(h.name)}${h.status==='unconfigured'?' ·cfg?':''}</button>`).join('');
  const caps=(cat.capabilities||[]).map(c=>{
    const open=nr.openCaps.has(c.capability);
    return `<div class="capgrp ${open?'open':''}"><div class="h" onclick="toggleCap('${c.capability}')"><span class="cn">${esc(c.capability)}</span><span class="cc">${c.skills.length}</span></div>
      <div class="b">${c.skills.map(s=>`<div class="skrow"><span class="sn">${esc(s.name)}</span><div class="meta"><span class="ttier ${s.available?(s.tier==='read-only'?'ro':'gated'):'na'}">${s.available?esc(s.tier):'not installed'}</span></div></div>`).join('')}</div></div>`;
  }).join('');
  $('newrun-view').innerHTML=`
    <div class="detail-head"><button class="back" onclick="showFleet()">← Fleet</button><span class="dtitle">New Run</span></div>
    <div class="dgoal">point rekit at a target, hand it a goal — it forages under a gate</div>
    <div class="form">
      <div class="field"><label>Target <span class="hint">a path on this machine</span></label><input class="mono" id="nr-target" placeholder="~/targets/renderer.dll" value="${esc(nr.target)}"></div>
      <div class="field"><label>Goal</label><textarea id="nr-goal" placeholder="Explain the graphics pipeline and write a patch to force windowed mode.">${esc(nr.goal)}</textarea></div>
      <div class="row">
        <div class="field"><label>Harness</label><div class="seg">${harnOpts||'<button class="on">mock</button>'}</div></div>
        <div class="field"><label>Tier floor</label><div class="seg">${['cheap','beefy'].map(t=>`<button class="${nr.tier===t?'on':''}" onclick="setTier('${t}')">${t}</button>`).join('')}</div></div>
        <div class="field"><label>Max rounds</label><input class="mono" id="nr-rounds" style="max-width:100px" value="${nr.rounds}"></div>
      </div>
      <div class="field"><label>Your rack <span class="hint">rekit auto-scopes these per round from the target's kinds — no need to pre-pick</span></label>${caps||'<div class="empty2">no skills discovered</div>'}</div>
      <button class="launch" onclick="submitRun()">▸ Launch run</button>
    </div>`;
}
function nrCapture(){const t=$('nr-target'),g=$('nr-goal'),r=$('nr-rounds');if(t)nr.target=t.value;if(g)nr.goal=g.value;if(r)nr.rounds=r.value;}
function toggleCap(c){nrCapture();nr.openCaps.has(c)?nr.openCaps.delete(c):nr.openCaps.add(c);renderNewRun();}
function setHarness(h){nrCapture();nr.harness=h;renderNewRun();}
function setTier(t){nrCapture();nr.tier=t;renderNewRun();}
async function submitRun(){
  nrCapture();
  const target=(nr.target||'').trim(),goal=(nr.goal||'').trim();
  if(!target||!goal){alert('Target and goal are both required.');return;}
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({target,goal,harness:nr.harness,tier:nr.tier,maxRounds:parseInt(nr.rounds)||8})});
    const d=await r.json();
    if(d.error){alert('Launch failed: '+d.error);return;}
    nr={harness:nr.harness,tier:nr.tier,rounds:8,openCaps:new Set(),target:'',goal:''};
    showFleet();
  }catch(e){alert('Launch failed.');}
}
async function renderSkills(){
  let cat={capabilities:[],total:0},harn=[];
  try{cat=await (await fetch('/api/skills')).json();}catch(e){}
  try{harn=(await (await fetch('/api/harnesses')).json()).harnesses||[];}catch(e){}
  const caps=(cat.capabilities||[]).map(c=>`<div class="capgrp open"><div class="h"><span class="cn">${esc(c.capability)}</span><span class="cc">${c.skills.length}</span></div>
    <div class="b">${c.skills.map(s=>`<div class="skrow"><span class="sn">${esc(s.name)}</span><div class="meta"><span style="color:var(--ink3);font-size:11px">accepts ${esc((s.accepts||[]).join(', ')||'any')}</span><span class="ttier ${s.available?(s.tier==='read-only'?'ro':'gated'):'na'}">${s.available?esc(s.tier):'not installed'}</span></div></div>`).join('')}</div></div>`).join('');
  const hs=harn.map(h=>`<div class="hrow"><div><div class="hn">${esc(h.name)}</div><div class="hd">${esc(h.description)}</div></div><span class="hs ${h.status}">${esc(h.status)}</span></div>`).join('');
  $('skills-view').innerHTML=`<h2>Skills · ${cat.total||0} in the rack</h2>${caps||'<div class="empty2">none discovered</div>'}
    <h2 style="margin-top:26px">Harnesses</h2>${hs}`;
}
refresh();setInterval(refresh,1500);
</script>
</body></html>
"""
