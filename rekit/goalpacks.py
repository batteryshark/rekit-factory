"""Goalpacks — goals that run on the rekit loop (E6).

A **goalpack is a folder** (like a skill), not a ``.yaml``: a ``GOALPACK.md``
frontmatter file next to a ``system-prompt.md`` and — **optionally** — a
``renderer.py``. It declares a goal and the capabilities it wants. Reporting is
**optional**: a goalpack MAY declare a report renderer; if it does, rekit renders
the report as a terminal step and records it as a ``report/*`` **artifact** in the
ledger. If it doesn't (an *act*-goal that produces a patch, not a report), the goal
simply produces findings/artifacts with no report.

::

    <goalpack>/
      GOALPACK.md        # frontmatter: name, title, goal, requestedCapabilities, [renderer], [markdown]
      system-prompt.md   # the brain's full system prompt for this goal
      renderer.py        # optional: def <fn>(project, goalpack, summary) -> dict
                         #           (+ optional def render_markdown(report) -> str)

``GOALPACK.md`` frontmatter (parsed by the existing :mod:`rekit.skills.frontmatter`
parser — no PyYAML, consistency with ``SKILL.md``):

* ``name`` — unique id (defaults to the folder name).
* ``title`` — human label.
* ``goal`` — the one-line goal text handed to the loop.
* ``requestedCapabilities`` — a list of capabilities the goalpack wants scoped in.
* ``renderer`` — *optional* ``renderer:render_report`` → module file ``renderer.py``,
  function ``render_report``. The bare form ``render_report`` also works (module
  defaults to ``renderer``). **Omit it** (and ship no ``renderer.py``) for a goalpack
  that produces no report — that is valid and yields ``renderer = None``.
* ``markdown`` — *optional* companion to ``renderer``: a
  ``(report: dict) -> str`` function that renders the structured report to a
  human-readable form. Same spec form as ``renderer``. If absent, rekit looks for a
  ``render_markdown`` function alongside the renderer; if that too is absent, the
  report is persisted as JSON only.

The **renderer/markdown contract** (deliberately simple so existing callers keep
working):

* the *renderer* returns the structured report ``dict`` — unchanged;
* the *markdown* function (if any) takes that same ``dict`` and returns a string.

Discovery scans **builtin** ``rekit/goalpacks/*/GOALPACK.md`` (shipped with the
package) and **user** ``$REKIT_HOME/goalpacks/*/GOALPACK.md``. A user goalpack of
the same name shadows a builtin one.

A goalpack *runs* via :func:`run_goalpack`, which drives :func:`rekit.loop.run`
with the goalpack's system prompt / goal / requested capabilities, then — **only if
a renderer is declared** — folds the ledger findings into the report, persists it as
``report.json`` (+ ``report.md`` when a markdown function is present) under the
project's report area, and records those files as ``report/json`` / ``report/markdown``
ledger artifacts. Either way it returns a :class:`GoalpackResult`.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .harness.base import HarnessAdapter
from .harness.tiers import CHEAP
from .human.channel import HumanChannel
from .ledger.artifacts import Artifact, from_path
from .ledger.project import Project
from .loop import loop as _loop
from .skills import frontmatter, home as _home
from .skills.scoping import Policy

#: The frontmatter file that marks a folder as a goalpack.
GOALPACK_FILE = "GOALPACK.md"

#: The brain's full system prompt lives in this sibling file.
SYSTEM_PROMPT_FILE = "system-prompt.md"

#: Default module (file stem) a bare ``renderer:`` / ``markdown:`` value resolves against.
DEFAULT_RENDERER_MODULE = "renderer"

#: Function looked up in the renderer module for the markdown rendering when the
#: frontmatter declares no explicit ``markdown:`` spec.
DEFAULT_MARKDOWN_FUNC = "render_markdown"

#: Artifact kinds a persisted report is recorded under in the ledger.
REPORT_JSON_KIND = "report/json"
REPORT_MARKDOWN_KIND = "report/markdown"


@dataclass(frozen=True)
class Goalpack:
    """A parsed goalpack folder: its identity, the goal + requested capabilities it
    declares, the brain's system prompt, and — optionally — its own report renderer.

    ``renderer`` is a resolved callable ``(project, goalpack, loop_summary) -> dict``
    (the goalpack owns the shape it folds the ledger's generic findings into), or
    ``None`` when the goalpack declares no report. ``render_markdown`` is an optional
    ``(report: dict) -> str`` companion that turns that dict into a readable form.
    """

    name: str
    title: str
    goal: str
    requested_capabilities: tuple[str, ...]
    system_prompt: str
    renderer: Callable[..., dict[str, Any]] | None
    dir: Path
    render_markdown: Callable[[dict[str, Any]], str] | None = None


@dataclass(frozen=True)
class GoalpackResult:
    """The outcome of running a goalpack on the loop.

    ``report`` is the structured report dict when the goalpack declared a renderer,
    else ``None`` (an *act*-goal). ``report_artifacts`` are the ``report/*`` ledger
    artifacts the report was persisted as (empty when there is no report).
    ``findings`` is always the ledger's generic finding substrate; ``summary`` is the
    loop's :class:`~rekit.loop.LoopSummary`.

    ``.report`` keeps existing callers/tests easy: it is the report dict directly.
    """

    report: dict[str, Any] | None
    report_artifacts: list[Artifact] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    summary: Any = None


def discover_goalpacks(environ: dict | None = None) -> list[Goalpack]:
    """Every goalpack: builtin ``rekit/goalpacks`` + user ``$REKIT_HOME/goalpacks``.

    Scans ``<root>/*/GOALPACK.md`` under both roots. A missing root yields nothing;
    a folder whose ``GOALPACK.md`` fails to parse (or whose declared renderer won't
    import) is skipped rather than sinking discovery. A **user** goalpack shadows a
    builtin one of the same name (user roots are scanned last and win the name).
    """
    by_name: dict[str, Goalpack] = {}
    # Builtin first, then user — so a user goalpack of the same name shadows it.
    for root in (_builtin_root(), _user_root(environ)):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            manifest = child / GOALPACK_FILE
            if not manifest.is_file():
                continue
            try:
                gp = _load_from_dir(child)
            except Exception:
                continue
            by_name[gp.name] = gp
    return [by_name[n] for n in sorted(by_name)]


def load_goalpack(name: str, environ: dict | None = None) -> Goalpack:
    """Load a single goalpack by ``name`` (user shadows builtin). Raises
    :class:`KeyError` if no goalpack with that name is discoverable."""
    for gp in discover_goalpacks(environ):
        if gp.name == name:
            return gp
    raise KeyError(f"no goalpack named {name!r}")


def run_goalpack(
    project: Project,
    goalpack: Goalpack,
    adapter: HarnessAdapter,
    *,
    channel: HumanChannel | None = None,
    policy: Policy | None = None,
    registry: Any = None,
    tier: str = CHEAP,
    max_rounds: int = 8,
) -> GoalpackResult:
    """Run ``goalpack`` on the ralph loop; report only if the goalpack asks for one.

    Always drives :func:`rekit.loop.run` with the goalpack's ``system_prompt`` /
    ``goal`` / ``requested_capabilities`` (so the loop scopes exactly the
    capabilities the goal asked for). Then:

    * **renderer declared** → call it to fold the ledger's generic findings into the
      goalpack's own report shape, persist that as a content-addressed ``report/*``
      ledger artifact (``report.json`` + optional ``report.md``), and return a
      :class:`GoalpackResult` carrying the report dict and its artifacts.
    * **no renderer** → return a :class:`GoalpackResult` with ``report=None`` and no
      report artifacts — just the ledger substrate (findings). This is the
      *act*-goal case: reporting is optional.
    """
    summary = _loop.run(
        project,
        goalpack.goal,
        adapter,
        system_prompt=goalpack.system_prompt,
        requested_capabilities=list(goalpack.requested_capabilities),
        channel=channel,
        policy=policy,
        registry=registry,
        tier=tier,
        max_rounds=max_rounds,
    )

    findings = project.ledger.findings()

    if goalpack.renderer is None:
        # An act-style goalpack: no report, just the ledger substrate.
        return GoalpackResult(
            report=None,
            report_artifacts=[],
            findings=findings,
            summary=summary,
        )

    report = goalpack.renderer(project, goalpack, summary)
    markdown = None
    if goalpack.render_markdown is not None:
        markdown = goalpack.render_markdown(report)

    artifacts = _persist_report(project, goalpack, report, markdown, findings)
    return GoalpackResult(
        report=report,
        report_artifacts=artifacts,
        findings=findings,
        summary=summary,
    )


# -- internals --------------------------------------------------------------


def _persist_report(
    project: Project,
    goalpack: Goalpack,
    report: dict[str, Any],
    markdown: str | None,
    findings: list[dict],
) -> list[Artifact]:
    """Write ``report.json`` (+ ``report.md``) and record them as ``report/*``
    ledger artifacts.

    The report is a **derived artifact** among many in the ledger: content-addressed,
    so re-running a goalpack that produces byte-identical report files is a no-op
    (``add_artifact`` dedupes on the content hash). Provenance meta (the goalpack name
    and finding count) rides on each artifact.
    """
    reports_dir = project.dir / "reports" / goalpack.name
    reports_dir.mkdir(parents=True, exist_ok=True)

    meta = {"goalpack": goalpack.name, "findingCount": len(findings)}
    artifacts: list[Artifact] = []

    json_path = reports_dir / "report.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    json_art = from_path(json_path, kind=REPORT_JSON_KIND, meta=dict(meta))
    project.add_artifact(json_art)
    artifacts.append(json_art)

    if markdown is not None:
        md_path = reports_dir / "report.md"
        md_path.write_text(markdown, encoding="utf-8")
        md_art = from_path(md_path, kind=REPORT_MARKDOWN_KIND, meta=dict(meta))
        project.add_artifact(md_art)
        artifacts.append(md_art)

    return artifacts


def _builtin_root() -> Path:
    """``rekit/goalpacks`` shipped inside the package."""
    return Path(__file__).resolve().parent / "goalpacks"


def _user_root(environ: dict | None = None) -> Path:
    """``$REKIT_HOME/goalpacks`` — user-authored goalpacks (zero install)."""
    return _home.rekit_home(environ) / "goalpacks"


def _load_from_dir(directory: Path) -> Goalpack:
    """Parse one goalpack folder into a :class:`Goalpack`.

    Reads ``GOALPACK.md`` frontmatter, loads the sibling ``system-prompt.md``, and —
    if a ``renderer`` is declared — resolves it (and an optional ``markdown``
    function) by importing ``renderer.py`` from the folder. A goalpack with no
    ``renderer:`` in frontmatter and no ``renderer.py`` gets ``renderer = None``.
    """
    manifest = directory / GOALPACK_FILE
    meta, _body = frontmatter.parse(manifest.read_text(encoding="utf-8"))

    name = str(meta.get("name") or "").strip() or directory.name
    title = str(meta.get("title") or "").strip() or name
    goal = str(meta.get("goal") or "").strip()
    requested = _as_tuple(meta.get("requestedCapabilities"))

    system_prompt = ""
    prompt_path = directory / SYSTEM_PROMPT_FILE
    if prompt_path.is_file():
        system_prompt = prompt_path.read_text(encoding="utf-8")

    renderer, render_markdown = _resolve_reporting(
        directory,
        str(meta.get("renderer") or "").strip(),
        str(meta.get("markdown") or "").strip(),
    )

    return Goalpack(
        name=name,
        title=title,
        goal=goal,
        requested_capabilities=requested,
        system_prompt=system_prompt,
        renderer=renderer,
        dir=directory,
        render_markdown=render_markdown,
    )


def _resolve_reporting(
    directory: Path, renderer_spec: str, markdown_spec: str
) -> tuple[Callable[..., dict[str, Any]] | None, Callable[[dict[str, Any]], str] | None]:
    """Resolve the optional ``renderer`` and ``markdown`` callables for a goalpack.

    Reporting is optional: with no ``renderer:`` frontmatter **and** no
    ``renderer.py`` on disk, this returns ``(None, None)`` — a valid goalpack that
    produces no report. When a renderer is present, an accompanying markdown function
    is resolved from the explicit ``markdown:`` spec, else from a ``render_markdown``
    function in the renderer module if one exists (else ``None`` → JSON-only report).
    """
    default_module = directory / f"{DEFAULT_RENDERER_MODULE}.py"

    # No renderer declared and no default module → this goalpack produces no report.
    if not renderer_spec and not default_module.is_file():
        return None, None

    renderer = _resolve_callable(directory, renderer_spec, DEFAULT_RENDERER_MODULE)

    render_markdown: Callable[[dict[str, Any]], str] | None = None
    if markdown_spec:
        render_markdown = _resolve_callable(directory, markdown_spec, DEFAULT_RENDERER_MODULE)
    else:
        # Fall back to a render_markdown in the renderer module, if present.
        render_markdown = _optional_callable(directory, DEFAULT_RENDERER_MODULE, DEFAULT_MARKDOWN_FUNC)

    return renderer, render_markdown


def _resolve_callable(directory: Path, spec: str, default_module: str) -> Callable[..., Any]:
    """Resolve a ``"<module>:<function>"`` (or bare ``"<function>"``) spec to a
    callable by importing a file from the goalpack folder.

    The module file is imported from ``<directory>/<module>.py`` via
    :func:`importlib.util.spec_from_file_location` — no package install, so a user
    goalpack's renderer loads with zero setup. Raises if the spec cannot be resolved.
    """
    module_stem, _, func_name = spec.partition(":")
    module_stem = module_stem.strip() or default_module
    func_name = func_name.strip()
    if not func_name:
        # Bare form: the whole spec was the function name; module defaults.
        func_name = module_stem
        module_stem = default_module
    if not func_name:
        raise ValueError("goalpack callable spec is empty")

    module = _import_module(directory, module_stem)
    fn = getattr(module, func_name, None)
    if not callable(fn):
        raise AttributeError(
            f"{func_name!r} not found (or not callable) in {directory / (module_stem + '.py')}"
        )
    return fn


def _optional_callable(directory: Path, module_stem: str, func_name: str) -> Callable[..., Any] | None:
    """Best-effort lookup of ``func_name`` in ``<directory>/<module_stem>.py``.

    Returns the callable if the module imports and defines it, else ``None`` — used
    for the optional ``render_markdown`` companion so its absence is simply "no
    markdown", never an error.
    """
    module_path = directory / f"{module_stem}.py"
    if not module_path.is_file():
        return None
    try:
        module = _import_module(directory, module_stem)
    except Exception:
        return None
    fn = getattr(module, func_name, None)
    return fn if callable(fn) else None


def _import_module(directory: Path, module_stem: str):
    """Import ``<directory>/<module_stem>.py`` under a stable, unique module name so
    two goalpacks' renderers never collide."""
    module_path = directory / f"{module_stem}.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"renderer module not found: {module_path}")
    mod_name = f"_rekit_goalpack_{directory.name}_{module_stem}"
    import_spec = importlib.util.spec_from_file_location(mod_name, module_path)
    if import_spec is None or import_spec.loader is None:
        raise ImportError(f"cannot load renderer module: {module_path}")
    module = importlib.util.module_from_spec(import_spec)
    import_spec.loader.exec_module(module)
    return module


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a frontmatter scalar/list value to a tuple of non-empty strings."""
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value).strip(),)
