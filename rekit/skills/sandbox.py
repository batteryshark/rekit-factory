"""Run a skill under a restrictive macOS Seatbelt sandbox (E5 — design decision D3).

Skills run on UNTRUSTED input — a hostile ``.apk``/``.dex``/native binary is
attacker-controlled data, and a skill's parser is a code-execution surface. So we
contain the subprocess: **no network** (blocks exfil / callbacks) and filesystem
**writes confined** to the skill's output dir plus a private temp dir. The tool can
still *read* what it needs (allow-default for reads); it just can't phone home or
scribble outside its sandbox. Runs are **timeout-bounded** so a hostile input
can't wedge the loop.

This is a clean re-implementation of parallax's
``prlx_transform_decompile.sandbox`` (D3) — nothing is imported from parallax;
rekit depends on nothing outside stdlib.

On a non-macOS host, or if ``sandbox-exec`` is missing, we degrade to running the
tool *without* the profile. The no-network + confined-writes guarantee is the
point, so losing it is **visible** (:func:`available` is False and :func:`sandboxed`
stamps ``sandboxed: false``), never silent — callers decide whether to proceed on
untrusted input.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path


def available() -> bool:
    """Whether the real Seatbelt sandbox can be applied on this host.

    True only on macOS with ``sandbox-exec`` present. Everywhere else the runner
    degrades to an unsandboxed subprocess (and says so via :func:`sandboxed`).
    """
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


def _q(text: str) -> str:
    """Quote a path for a Seatbelt profile string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(writable: list[str]) -> str:
    """A Seatbelt profile: allow reads/exec, deny all network, deny writes except
    to the explicitly-listed subpaths (output + temp) and the harmless devices a
    subprocess/JVM touches.

    ``allow default`` keeps interpreters/dyld working; the denies claw back the two
    capabilities that matter for untrusted input — network and out-of-sandbox
    writes.
    """
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound)",
        "(deny network-inbound)",
        "(allow network-outbound (remote unix-socket))",  # loopback IPC only
        "(deny file-write*)",
    ]
    for path in writable:
        lines.append(f"(allow file-write* (subpath {_q(path)}))")
    # Devices a shell / interpreter / JVM legitimately writes to.
    for dev in ("/dev/null", "/dev/dtracehelper", "/dev/tty", "/dev/stdout", "/dev/stderr"):
        lines.append(f"(allow file-write* (literal {_q(dev)}))")
    return "\n".join(lines) + "\n"


def _user_scratch_dirs() -> list[str]:
    """The per-user temp/cache dirs an interpreter/JVM writes to regardless of
    ``TMPDIR`` (``java.io.tmpdir`` defaults here; the JVM class cache lives here).

    Confining to exactly these two ``getconf`` dirs — not all of ``/var/folders`` —
    keeps the sandbox tight while letting real tools run. (``os.confstr`` lacks
    these names on macOS CPython, so we shell out to ``getconf``.)
    """
    out: list[str] = []
    for name in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        try:
            result = subprocess.run(
                ["getconf", name], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = (result.stdout or "").strip()
        if value and os.path.isdir(value):
            out.append(str(Path(value).resolve()))
    return out


def run(
    cmd: list[str],
    *,
    out_dir: str | os.PathLike,
    timeout: float = 900.0,
    env: dict | None = None,
    extra_writable: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` sandboxed, writing only under ``out_dir`` (+ a private temp).

    Returns the :class:`subprocess.CompletedProcess` (stdout/stderr captured as
    text). Raises :class:`subprocess.TimeoutExpired` if the tool runs past
    ``timeout`` — a hostile input can't wedge the loop. The private temp dir is
    always cleaned up.

    On a host without Seatbelt (:func:`available` False) the same command runs
    *unsandboxed*; the loss of containment is reported by :func:`sandboxed`, not
    hidden.
    """
    out_dir = str(Path(out_dir).resolve())
    os.makedirs(out_dir, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(prefix="rekit-skill-")
    run_env = dict(os.environ if env is None else env)
    run_env["TMPDIR"] = tmp_dir
    writable = [out_dir, tmp_dir, *_user_scratch_dirs(), *(extra_writable or [])]
    if available():
        full = ["sandbox-exec", "-p", _profile(writable), *cmd]
    else:
        full = list(cmd)
    try:
        return subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
            cwd=out_dir,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def sandboxed(note: str = "") -> dict:
    """A provenance stamp recording whether a run was actually contained.

    Attached to emitted artifacts / the run result so downstream can see the
    untrusted work ran sandboxed — or, on an unsupported host, that it did **not**
    and containment was lost.
    """
    return {"sandboxed": available(), "host": platform.system(), "note": note}
