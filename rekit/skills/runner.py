"""Execute a scoped skill against a target — gated, sandboxed, ledger-folded (E5).

This is where a skill stops being metadata and actually *runs*. The ralph loop
hands :func:`run_skill` a scoped :class:`~rekit.skills.model.Skill`, a target path,
and the persistent :class:`~rekit.ledger.project.Project`; the runner walks three
gates and, if all clear, executes the skill's declared command in a sandbox and
folds whatever it produced back into the ledger:

1. **host-gate** — is the skill's host tool present? If not, the runner does not
   crash: it records an "install X" **lead** (:meth:`Project.record_lead`) and
   returns an ``unavailable`` result. A missing tool becomes a durable want, not
   an exception.
2. **tier-gate** — :func:`~rekit.human.channel.gate_skill` decides whether this
   skill's trust tier may run under the goalpack :class:`~rekit.skills.scoping.Policy`.
   Read-only auto-runs; network / executes-untrusted / destructive route through
   the :class:`~rekit.human.channel.HumanChannel`. Denied → a ``skipped`` result
   that records nothing destructive.
3. **execute** — the skill's declared **run contract** (see below) runs
   **sandboxed** (:mod:`rekit.skills.sandbox`) with writes confined to an out_dir
   under ``$REKIT_HOME/projects/<id>/cache/<skill>/…``, timeout-bounded, on
   untrusted input. Whatever appears under the out_dir is classified into
   artifacts and recorded as a **derivation** (:meth:`Project.record_derivation`),
   which also adds the revealed artifacts — so a revealed tree/file **re-enters
   the ledger** and drives the next loop round.

The run contract
----------------
A skill declares how it is invoked in its ``SKILL.md`` frontmatter::

    run: scripts/run.sh        # relative to the skill folder; default if omitted

The command is invoked as ``<run> <input> <out_dir>``: ``$1`` is the target path
(the untrusted input to consume), ``$2`` is the sandbox-writable output directory
(the *only* place it may write). If ``run:`` is omitted the runner defaults to
``scripts/run.sh`` when that file exists. A skill with neither a declared ``run``
nor a ``scripts/run.sh`` has nothing to execute and returns a ``no-run`` result.

Everything here is pure stdlib and imports the rest of rekit read-only.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..human.channel import HumanChannel, gate_skill
from ..ledger.artifacts import Artifact, from_path
from ..ledger.project import Project
from . import frontmatter, sandbox
from .model import Skill

#: Default run command when a SKILL.md omits ``run:`` (relative to the skill dir).
DEFAULT_RUN = "scripts/run.sh"

#: The status codes a :class:`RunResult` can carry.
STATUS_OK = "ok"                    # ran, outputs recorded
STATUS_UNAVAILABLE = "unavailable"  # host tool missing -> lead recorded
STATUS_SKIPPED = "skipped"          # tier-gate denied -> nothing recorded
STATUS_NO_RUN = "no-run"            # skill declares nothing to execute
STATUS_ERROR = "error"              # the run command failed / timed out


@dataclass
class RunResult:
    """The outcome of a :func:`run_skill` call — what ran, what it produced.

    ``status`` is one of the ``STATUS_*`` codes. ``outputs`` are the artifacts
    classified under the out_dir and folded into the ledger (empty unless
    ``status == "ok"``). ``provenance`` carries the sandbox stamp
    (:func:`rekit.skills.sandbox.sandboxed`). ``recorded_derivation`` is True iff a
    new derivation event was appended.
    """

    skill: str
    status: str
    detail: str = ""
    outputs: list[Artifact] = field(default_factory=list)
    out_dir: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    recorded_derivation: bool = False
    returncode: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def summary(self) -> str:
        """A one-line, brain-legible summary fed back into the next loop round."""
        head = f"skill {self.skill!r}: {self.status}"
        if self.detail:
            head += f" ({self.detail})"
        if self.status == STATUS_OK:
            head += f" — {len(self.outputs)} output(s)"
            if self.outputs:
                head += ": " + ", ".join(
                    f"{a.path} [{a.kind}]" for a in self.outputs[:8]
                )
            note = self.provenance.get("note") or ""
            if self.provenance.get("sandboxed") is False:
                head += "  (WARNING: ran UNSANDBOXED — no host containment)"
            elif note:
                head += f"  ({note})"
        return head


def cache_dir(project: Project, skill: Skill, input_hash: str) -> Path:
    """The out_dir a skill writes into: ``<project>/cache/<skill>/<input-prefix>``.

    Keyed by the input hash so a re-run over the same bytes lands in the same
    place (and the ledger's content-addressed derivation cache makes the re-run a
    no-op). Confined under the project dir so the sandbox can allow writes to
    exactly this subtree.
    """
    return project.dir / "cache" / skill.name / input_hash[:16]


def declared_run(skill: Skill) -> str | None:
    """The skill's run command per the contract: the ``run:`` frontmatter field,
    else ``scripts/run.sh`` if it exists, else ``None`` (nothing to execute).

    Read straight from ``SKILL.md`` — the ``run`` field is not on the frozen
    :class:`Skill` model (which this integration must not edit), so we re-parse the
    one field we need. Returns the command string relative to the skill folder.
    """
    declared = _run_field(skill)
    if declared:
        return declared
    if (skill.path / DEFAULT_RUN).is_file():
        return DEFAULT_RUN
    return None


def _run_field(skill: Skill) -> str | None:
    skill_md = skill.path / "SKILL.md"
    try:
        meta, _ = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
    except OSError:
        return None
    value = meta.get("run")
    if isinstance(value, (list, tuple)):
        value = " ".join(str(v) for v in value)
    value = str(value or "").strip()
    return value or None


def _resolve_command(skill: Skill, run_spec: str, target_path: str, out_dir: str) -> list[str] | None:
    """Turn the declared run spec into a concrete argv: ``<run...> <input> <out_dir>``.

    The first token is resolved relative to the skill folder when it is a bare
    relative path (``scripts/run.sh``); an absolute path or a PATH command (``python``)
    is left as-is. A relative script that exists is made executable if it is not
    already (skills authored on other machines may have lost the bit).
    """
    parts = shlex.split(run_spec)
    if not parts:
        return None
    head, rest = parts[0], parts[1:]

    candidate = Path(head)
    if not candidate.is_absolute() and ("/" in head or head.startswith(".")):
        resolved = (skill.path / candidate).resolve()
        if not resolved.is_file():
            return None
        _ensure_executable(resolved)
        head = str(resolved)

    return [head, *rest, target_path, out_dir]


def _ensure_executable(path: Path) -> None:
    try:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _classify_outputs(out_dir: Path) -> list[Artifact]:
    """Every file the skill produced under ``out_dir``, classified into artifacts.

    Each top-level entry becomes one artifact: a directory hashes/classifies as a
    ``tree`` (the whole revealed subtree re-enters the ledger as one node), a file
    as its own kind. Deterministic order by name so the recorded outputs are stable.
    """
    if not out_dir.is_dir():
        return []
    outputs: list[Artifact] = []
    for child in sorted(out_dir.iterdir(), key=lambda p: p.name):
        try:
            outputs.append(from_path(child))
        except (OSError, ValueError):
            continue
    return outputs


def run_skill(
    skill: Skill,
    target_path: str | os.PathLike,
    project: Project,
    *,
    channel: HumanChannel,
    policy,
    environ: dict | None = None,
    timeout: float = 900.0,
) -> RunResult:
    """Gate, run (sandboxed), and record a single skill against ``target_path``.

    See the module docstring for the three-gate flow. Returns a :class:`RunResult`;
    never raises for the expected failure modes (missing host tool, denied gate,
    nothing to run, a failed/timed-out subprocess) — each maps to a status the loop
    can react to. Only a truly unexpected error propagates.

    Args:
        skill: the scoped :class:`Skill` to run.
        target_path: the input the skill consumes (the untrusted target).
        project: the persistent :class:`Project` (the write API over the ledger).
        channel: the :class:`HumanChannel` the tier-gate routes through.
        policy: the goalpack :class:`~rekit.skills.scoping.Policy` (duck-typed:
            needs ``allows`` / ``is_auto`` / ``is_gated``).
        environ: environment for host resolution + the subprocess (defaults to the
            live ``os.environ``).
        timeout: per-run wall-clock bound handed to the sandbox.
    """
    env = dict(os.environ if environ is None else environ)
    target = str(Path(target_path))

    # (1) host-gate: a missing tool becomes a durable "install X" lead, not a crash.
    if not skill.available(environ=env):
        host_name = skill.host.name if skill.host else skill.name
        project.record_lead(
            skill.capability or skill.name,
            _target_kind(target),
            requires=[host_name],
            env_hints=[skill.host.env] if (skill.host and skill.host.env) else [],
            example_path=target,
        )
        return RunResult(
            skill=skill.name,
            status=STATUS_UNAVAILABLE,
            detail=f"host tool {host_name!r} not available; recorded a lead",
        )

    # (2) tier-gate: read-only auto-runs; gated tiers ask the human. Denied → skip.
    if not gate_skill(skill, policy, channel):
        return RunResult(
            skill=skill.name,
            status=STATUS_SKIPPED,
            detail=f"tier {skill.tier!r} not permitted (gate denied)",
        )

    # A skill with nothing runnable is a no-op we can report cleanly.
    run_spec = declared_run(skill)
    if run_spec is None:
        return RunResult(
            skill=skill.name,
            status=STATUS_NO_RUN,
            detail="skill declares no run command and has no scripts/run.sh",
        )

    # (3) execute sandboxed. out_dir is confined under the project cache; the
    # sandbox allows writes only there (plus a private temp).
    input_art = _input_artifact(target)
    out_dir = cache_dir(project, skill, input_art.content_hash)
    out_dir.mkdir(parents=True, exist_ok=True)

    argv = _resolve_command(skill, run_spec, target, str(out_dir))
    if argv is None:
        return RunResult(
            skill=skill.name,
            status=STATUS_NO_RUN,
            detail=f"run command {run_spec!r} does not resolve to a runnable script",
            out_dir=str(out_dir),
        )

    provenance = sandbox.sandboxed(f"skill={skill.name}")
    try:
        completed = sandbox.run(argv, out_dir=str(out_dir), timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return RunResult(
            skill=skill.name,
            status=STATUS_ERROR,
            detail=f"run timed out after {timeout:g}s",
            out_dir=str(out_dir),
            provenance=provenance,
        )
    except OSError as exc:
        return RunResult(
            skill=skill.name,
            status=STATUS_ERROR,
            detail=f"could not launch run command: {exc}",
            out_dir=str(out_dir),
            provenance=provenance,
        )

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:]
        detail = f"run exited {completed.returncode}"
        if tail:
            detail += f": {tail[0][:200]}"
        return RunResult(
            skill=skill.name,
            status=STATUS_ERROR,
            detail=detail,
            out_dir=str(out_dir),
            provenance=provenance,
            returncode=completed.returncode,
        )

    # (record) classify the outputs and fold a derivation; revealed content
    # (trees/files) re-enters the ledger via record_derivation's add_outputs.
    outputs = _classify_outputs(out_dir)
    for out in outputs:
        out.meta.update({"skill": skill.name, "provenance": dict(provenance)})
    recorded = project.record_derivation(
        skill.capability or skill.name,
        input_art,
        outputs,
        capability=skill.capability,
    )

    return RunResult(
        skill=skill.name,
        status=STATUS_OK,
        detail="" if recorded else "already derived (cache hit); outputs re-used",
        outputs=outputs,
        out_dir=str(out_dir),
        provenance=provenance,
        recorded_derivation=recorded,
        returncode=0,
    )


def _input_artifact(target: str) -> Artifact:
    """The target as an :class:`Artifact` (real hash+classify if it exists, else a
    lightweight path-addressed placeholder so a derivation is still keyed sanely)."""
    p = Path(target)
    if p.exists():
        return from_path(p)
    import hashlib

    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
    return Artifact(kind="file", content_hash=digest, path=target, meta={"placeholder": True})


def _input_hash(target: str) -> str:
    return _input_artifact(target).content_hash


def _target_kind(target: str) -> str:
    from ..ledger.artifacts import classify

    p = Path(target)
    if p.exists():
        try:
            return classify(p)
        except OSError:
            pass
    return "unknown"
