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
from .readmodel import fleet, health, project_view

#: Default port — 7358 is "REKT" on a phone keypad, and chosen to avoid clashing
#: with common local dashboards (e.g. opencode-ensemble on 4747). Override with --port.
DEFAULT_PORT = 7358
DEFAULT_HOST = "127.0.0.1"


def _json(status: int, obj: Any) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8")


def _projects_base(root: str | Path | None) -> Path:
    return Path(root) if root is not None else projects_root()


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
        return _json(200, project_view(d))

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
</style></head>
<body>
<header>
  <span class="wm">RE<span class="k">KIT</span></span>
  <span style="font-family:var(--mono);font-size:11px;color:var(--ink3)">MISSION CONTROL</span>
  <div class="sp"></div>
  <div class="legend" id="legend"></div>
  <div class="live"><i></i><span id="tick">live</span></div>
</header>
<main>
  <div id="decisions"></div>
  <h2>Fleet</h2>
  <div class="grid" id="fleet"><div class="empty">connecting…</div></div>
</main>
<script>
const $=id=>document.getElementById(id);
const PILL={running:'p-running',blocked:'p-blocked',suspended:'p-suspended',done:'p-done',idle:'p-idle',failed:'p-failed'};
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function answer(pid,qid,val){
  await fetch('/api/answer',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({projectId:pid,questionId:qid,value:val})});
  poll();
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
  return `<div class="card s-${v.status}"><span class="stripe"></span>
    <div class="ct"><div class="nm">${esc(v.id)}</div><span class="pill ${PILL[v.status]||'p-idle'}">${esc(v.status)}</span></div>
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
async function poll(){
  try{const r=await fetch('/api/fleet');render(await r.json());
    $('tick').textContent='live · '+new Date().toLocaleTimeString();
  }catch(e){$('tick').textContent='reconnecting…';}
}
poll();setInterval(poll,1500);
</script>
</body></html>
"""
