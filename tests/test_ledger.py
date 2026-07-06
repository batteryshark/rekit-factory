"""E1 — the persistent, event-sourced project ledger.

Proves the four acceptance criteria of card T-048:

- close + reopen a project → ledger state reloads losslessly;
- record a derivation once → a second identical derivation is a content-addressed
  no-op (zero re-derivation);
- replay the event log from scratch → reconstructs the same ledger state;
- project lifecycle — ``open_project`` / ``list_projects`` / ``resume`` round-trip.

Plain-python style like the prlx-core tests (runnable via
``python tests/test_ledger.py``) and pytest-compatible. Each test uses a temp
``REKIT_HOME`` via the env var so nothing touches ``~/.rekit``.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from rekit import ledger  # noqa: E402
from rekit.ledger import artifacts as af, project as prj  # noqa: E402


@contextlib.contextmanager
def temp_home():
    """A temp ``REKIT_HOME`` (restored afterwards) plus a temp workspace for
    building target files. Yields ``(home_path, work_path)``."""
    saved = os.environ.get("REKIT_HOME")
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as work:
        os.environ["REKIT_HOME"] = home
        try:
            yield Path(home), Path(work)
        finally:
            if saved is None:
                os.environ.pop("REKIT_HOME", None)
            else:
                os.environ["REKIT_HOME"] = saved


def _write(work: Path, rel: str, data: bytes = b"payload") -> Path:
    p = work / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# -- home + id -------------------------------------------------------------


def test_rekit_home_honours_env_var_and_defaults():
    saved = os.environ.get("REKIT_HOME")
    try:
        os.environ["REKIT_HOME"] = "/tmp/some-rekit-home"
        assert ledger.rekit_home() == Path("/tmp/some-rekit-home")
        assert ledger.projects_root() == Path("/tmp/some-rekit-home/projects")
        os.environ.pop("REKIT_HOME", None)
        assert ledger.rekit_home() == Path.home() / ".rekit"
    finally:
        if saved is None:
            os.environ.pop("REKIT_HOME", None)
        else:
            os.environ["REKIT_HOME"] = saved


def test_project_id_is_stable_per_target_path():
    a, b = "/some/one", "/some/two"
    assert prj.project_id(a) == prj.project_id(a)          # stable
    assert prj.project_id(a) != prj.project_id(b)          # per path
    assert prj.project_id(a).startswith("one-")            # readable prefix


# -- lossless close + reopen (E1.1) ----------------------------------------


def test_close_and_reopen_is_lossless():
    with temp_home() as (_home, work):
        target = _write(work, "app.bin")
        p1 = ledger.open_project(str(target))

        root = p1.root_artifact()
        p1.add_artifact(root, is_tree=False)
        # a derived tree with a couple of files inside it
        inner = work / "unpacked"
        _write(inner, "a.js", b"require('child_process')")
        _write(inner, "nested/b.py", b"print(1)\n")
        tree = af.from_path(inner, kind=af.KIND_TREE)
        p1.record_derivation("unpack-fake", root, [tree], capability="unpack")
        p1.record_lead("decompile", "binary/native",
                       requires=["ghidra"], env_hints=["GHIDRA_HOME"],
                       example_path=str(target))
        p1.record_finding(tree, {"title": "spawns a subprocess", "severity": "high"})
        p1.mark_analyzed(tree)

        before = p1.ledger.snapshot()

        # Close (drop the object) and reopen the SAME target from disk.
        del p1
        p2 = ledger.open_project(str(target))
        after = p2.ledger.snapshot()

        assert after == before, "reopened ledger state differs from before close"
        # concrete content survived, not just counts
        assert after["kinds"].get("tree") == 1
        assert len(after["derivations"]) == 1
        assert len(after["leads"]) == 1
        found = after["artifacts"][tree.content_hash]
        assert found["analyzed"] is True
        assert found["findings"][0]["title"] == "spawns a subprocess"


# -- content-addressed derivation cache: second goal re-derives nothing ----


def test_second_identical_derivation_is_a_no_op():
    with temp_home() as (_home, work):
        target = _write(work, "in.bin")
        p = ledger.open_project(str(target))
        root = p.root_artifact()

        out = af.Artifact(kind="tree", content_hash="deadbeef" * 8, path=str(work / "out"))
        first = p.record_derivation("unpack-fake", root, [out], capability="unpack")
        assert first is True
        events_after_first = len(ledger.read_events(p.ledger_path))

        # Re-deriving the SAME (transform, input hash) is a free no-op:
        # nothing recorded, no new event appended.
        second = p.record_derivation("unpack-fake", root, [out], capability="unpack")
        assert second is False
        assert len(ledger.read_events(p.ledger_path)) == events_after_first
        assert len(p.ledger.derivations) == 1

        # And it survives a reopen — a genuinely "second goal" over the same
        # target still sees the cache hit, re-deriving nothing.
        p2 = ledger.open_project(str(target))
        assert p2.record_derivation("unpack-fake", p2.root_artifact(), [out],
                                    capability="unpack") is False


def test_adding_same_artifact_twice_is_a_no_op():
    with temp_home() as (_home, work):
        target = _write(work, "dup.bin")
        p = ledger.open_project(str(target))
        art = p.root_artifact()
        assert p.add_artifact(art) is True
        assert p.add_artifact(art) is False       # content-addressed dedupe
        assert len(p.ledger.entries) == 1


# -- event replay reconstructs the ledger (E1.3) ---------------------------


def test_event_replay_reconstructs_state():
    with temp_home() as (_home, work):
        target = _write(work, "replay.bin")
        p = ledger.open_project(str(target))
        root = p.root_artifact()
        p.add_artifact(root)
        out = af.Artifact(kind="tree", content_hash="c0ffee" * 10, path=str(work / "t"))
        p.record_derivation("unpack-fake", root, [out])
        p.record_finding(out, {"title": "x"})
        p.mark_analyzed(out)

        live = p.ledger.snapshot()

        # Replay the raw log from scratch (no Project involved) -> same state.
        events = ledger.read_events(p.ledger_path)
        assert [e.seq for e in events] == list(range(1, len(events) + 1))  # monotonic
        rebuilt = ledger.replay(events).snapshot()
        assert rebuilt == live


def test_unknown_event_type_replays_as_noop():
    from rekit.ledger import events as ev
    log = [
        ev.Event(seq=1, type=ev.ARTIFACT_ADDED, ts="t",
                 payload={"artifact": af.to_dict(af.Artifact("file", "h" * 64, "/x")),
                          "isTree": False}),
        ev.Event(seq=2, type="some_future_event", ts="t", payload={"whatever": 1}),
    ]
    rebuilt = ledger.replay(log)
    assert len(rebuilt.entries) == 1        # future event ignored, not fatal
    assert rebuilt.seq == 2                  # seq still advances


# -- lifecycle: open / list / resume (E1.2) --------------------------------


def test_list_and_resume_round_trip():
    with temp_home() as (_home, work):
        t1 = _write(work, "one.bin", b"one")
        t2 = _write(work, "two.bin", b"two")
        p1 = ledger.open_project(str(t1), now="2026-01-01T00:00:00+00:00")
        p2 = ledger.open_project(str(t2), now="2026-02-02T00:00:00+00:00")
        p1.add_artifact(p1.root_artifact())
        p1.record_finding(p1.root_artifact(), {"title": "keeper"})

        listed = ledger.list_projects()
        ids = {m["id"] for m in listed}
        assert p1.id in ids and p2.id in ids
        # newest-opened first
        assert listed[0]["id"] == p2.id

        # resume p1 by id -> same target, same state (finding survives).
        resumed = ledger.resume(p1.id)
        assert resumed is not None
        assert resumed.target == p1.target
        assert resumed.ledger.snapshot() == p1.ledger.snapshot()
        assert resumed.ledger.findings()[0]["title"] == "keeper"

        assert ledger.resume("no-such-project-xxxx") is None


def test_reopen_preserves_created_at_and_advances_last_opened():
    with temp_home() as (_home, work):
        target = _write(work, "meta.bin")
        p1 = ledger.open_project(str(target), now="2026-01-01T00:00:00+00:00")
        assert p1.read_meta()["createdAt"] == "2026-01-01T00:00:00+00:00"
        p2 = ledger.open_project(str(target), now="2026-03-03T00:00:00+00:00")
        assert p2.id == p1.id
        meta = p2.read_meta()
        assert meta["createdAt"] == "2026-01-01T00:00:00+00:00"   # preserved
        assert meta["lastOpenedAt"] == "2026-03-03T00:00:00+00:00"  # advanced


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("rekit ledger tests passed")
