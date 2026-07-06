"""E7.0 / E7.3 tests: ``rekit serve`` — the transport over the read-model.

Proves the local HTTP surface:

* the pure router :func:`handle` serves the fleet, a single project view, the
  client HTML, and 404s cleanly;
* ``POST /api/answer`` appends to a project's inbox — the writeback that unblocks
  a waiting channel (verified by the pending count dropping to zero);
* a real socket round-trip over :func:`make_server` returns live JSON;
* the notifier's pure core reports only *new* pending decisions.

Hermetic via a temp ``REKIT_HOME``; the socket test uses an ephemeral port. Pure
stdlib (``urllib``).
"""

import contextlib
import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit.human import pending_questions, post_question  # noqa: E402
from rekit.lab.server import (  # noqa: E402
    handle,
    make_server,
    new_notifications,
)
from rekit.ledger import open_project, projects_root  # noqa: E402
from rekit.ledger.runlog import RunLog  # noqa: E402


@contextlib.contextmanager
def _temp_home():
    saved = os.environ.get("REKIT_HOME")
    os.environ["REKIT_HOME"] = tempfile.mkdtemp(prefix="rekit-home-")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("REKIT_HOME", None)
        else:
            os.environ["REKIT_HOME"] = saved


def _project(name, *, pending=None):
    ws = tempfile.mkdtemp(prefix="rekit-target-")
    target = Path(ws) / name
    target.write_bytes(b"bytes-" + name.encode())
    p = open_project(str(target))
    RunLog(p.dir).run_started(goal="goal " + name, harness="mock", tier="cheap", max_rounds=4)
    if pending:
        post_question(p.dir, pending[0], pending[1], pending[2] if len(pending) > 2 else None)
    return p


def _body(resp):
    status, ctype, payload = resp
    return status, json.loads(payload) if "json" in ctype else payload


# -- pure router ------------------------------------------------------------

def test_fleet_route():
    with _temp_home():
        _project("app.apk")
        status, data = _body(handle("GET", "/api/fleet", root=projects_root()))
        assert status == 200
        assert len(data["fleet"]) == 1 and data["health"]["total"] == 1
        assert data["fleet"][0]["run"]["harness"] == "mock"


def test_project_route():
    with _temp_home():
        p = _project("firmware.bin")
        status, data = _body(handle("GET", f"/api/project?id={p.id}", root=projects_root()))
        assert status == 200 and data["id"] == p.id


def test_project_route_404():
    with _temp_home():
        status, data = _body(handle("GET", "/api/project?id=nope", root=projects_root()))
        assert status == 404


def test_answer_route_unblocks():
    with _temp_home():
        p = _project("libx.so", pending=("confirm", "allow ghidra?"))
        assert len(pending_questions(p.dir)) == 1
        qid = pending_questions(p.dir)[0]["id"]
        body = json.dumps({"projectId": p.id, "questionId": qid, "value": "yes"}).encode()
        status, data = _body(handle("POST", "/api/answer", body, root=projects_root()))
        assert status == 200 and data["ok"] is True
        assert pending_questions(p.dir) == []          # unblocked


def test_answer_bad_request():
    with _temp_home():
        status, _ = _body(handle("POST", "/api/answer", b'{"projectId":"x"}', root=projects_root()))
        assert status == 400                            # missing questionId


def test_root_serves_client_html():
    status, ctype, payload = handle("GET", "/")
    assert status == 200 and "text/html" in ctype
    assert payload.strip().lower().startswith(b"<!doctype")


def test_unknown_route_404():
    status, _ = _body(handle("GET", "/nope"))
    assert status == 404


# -- real socket round-trip -------------------------------------------------

def test_live_server_serves_and_answers():
    with _temp_home():
        p = _project("SwissBank.exe", pending=("confirm", "allow?"))
        qid = pending_questions(p.dir)[0]["id"]
        httpd = make_server("127.0.0.1", 0, root=projects_root())
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/fleet", timeout=3) as r:
                data = json.loads(r.read())
            assert len(data["fleet"]) == 1
            # answer over the wire → the pending decision clears
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/answer",
                data=json.dumps({"projectId": p.id, "questionId": qid, "value": "yes"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=3) as r:
                assert json.loads(r.read())["ok"] is True
            assert pending_questions(p.dir) == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(3)


# -- notifier core ----------------------------------------------------------

def test_new_notifications_reports_only_fresh():
    views = [{"id": "proj-a", "pending": [{"id": "q1", "question": "allow?"}]}]
    seen: set = set()
    first = new_notifications(seen, views)
    assert first == [("proj-a", "allow?")]
    # Second pass over the same pending yields nothing new.
    assert new_notifications(seen, views) == []


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    failures = []
    for test in ALL_TESTS:
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures.append((test.__name__, exc))
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    if failures:
        print(f"\n{len(failures)} failed, {len(ALL_TESTS) - len(failures)} passed")
        return 1
    print(f"\nall {len(ALL_TESTS)} server tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
