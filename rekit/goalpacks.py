"""Goalpacks — goals that run on the rekit loop (E6, first slice).

A **goalpack is a folder** (like a skill), not a ``.yaml``: a ``GOALPACK.md``
frontmatter file next to a ``system-prompt.md`` and a ``renderer.py``. It declares
a goal, the capabilities it wants, and — crucially — **its own report renderer**.
The ledger holds only the generic findings/artifact substrate; each goalpack owns
the shape it folds those findings into. There is no shared ``report_model``.

::

    <goalpack>/
      GOALPACK.md        # frontmatter: name, title, goal, requestedCapabilities, renderer
      system-prompt.md   # the brain's full system prompt for this goal
      renderer.py        # def <fn>(project, goalpack, summary) -> dict

``GOALPACK.md`` frontmatter (parsed by the existing :mod:`rekit.skills.frontmatter`
parser — no PyYAML, consistency with ``SKILL.md``):

* ``name`` — unique id (defaults to the folder name).
* ``title`` — human label.
* ``goal`` — the one-line goal text handed to the loop.
* ``requestedCapabilities`` — a list of capabilities the goalpack wants scoped in.
* ``renderer`` — ``renderer:render_report`` → module file ``renderer.py``, function
  ``render_report``. The bare form ``render_report`` also works (module defaults to
  ``renderer``).

Discovery scans **builtin** ``rekit/goalpacks/*/GOALPACK.md`` (shipped with the
package) and **user** ``$REKIT_HOME/goalpacks/*/GOALPACK.md``. A user goalpack of
the same name shadows a builtin one.

A goalpack *runs* via :func:`run_goalpack`, which drives :func:`rekit.loop.run`
with the goalpack's system prompt / goal / requested capabilities, then calls the
goalpack's own ``renderer(project, goalpack, summary)`` over the ledger findings and
returns the rendered report.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .harness.base import HarnessAdapter
from .harness.tiers import CHEAP
from .human.channel import HumanChannel
from .ledger.project import Project
from .loop import loop as _loop
from .skills import frontmatter, home as _home
from .skills.scoping import Policy

#: The frontmatter file that marks a folder as a goalpack.
GOALPACK_FILE = "GOALPACK.md"

#: The brain's full system prompt lives in this sibling file.
SYSTEM_PROMPT_FILE = "system-prompt.md"

#: Default module (file stem) a bare ``renderer:`` value resolves against.
DEFAULT_RENDERER_MODULE = "renderer"


@dataclass(frozen=True)
class Goalpack:
    """A parsed goalpack folder: its identity, the goal + requested capabilities it
    declares, the brain's system prompt, and its own report renderer.

    ``renderer`` is a resolved callable ``(project, goalpack, loop_summary) -> dict``
    — the goalpack owns the shape it folds the ledger's generic findings into.
    """

    name: str
    title: str
    goal: str
    requested_capabilities: tuple[str, ...]
    system_prompt: str
    renderer: Callable[..., dict[str, Any]]
    dir: Path


def discover_goalpacks(environ: dict | None = None) -> list[Goalpack]:
    """Every goalpack: builtin ``rekit/goalpacks`` + user ``$REKIT_HOME/goalpacks``.

    Scans ``<root>/*/GOALPACK.md`` under both roots. A missing root yields nothing;
    a folder whose ``GOALPACK.md`` fails to parse (or whose renderer won't import) is
    skipped rather than sinking discovery. A **user** goalpack shadows a builtin one
    of the same name (user roots are scanned last and win the name).
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
) -> dict[str, Any]:
    """Run ``goalpack`` on the ralph loop, then render its own report.

    Bridges the loop to the goalpack's renderer: drives :func:`rekit.loop.run` with
    the goalpack's ``system_prompt`` / ``goal`` / ``requested_capabilities`` (so the
    loop scopes exactly the capabilities the goal asked for), then hands the
    :class:`~rekit.loop.LoopSummary` and the ``project`` to
    ``goalpack.renderer(project, goalpack, summary)``. The renderer reads the ledger's
    generic findings — the shared substrate — and folds them into the goalpack's own
    report shape, which this returns unchanged.
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
    return goalpack.renderer(project, goalpack, summary)


# -- internals --------------------------------------------------------------


def _builtin_root() -> Path:
    """``rekit/goalpacks`` shipped inside the package."""
    return Path(__file__).resolve().parent / "goalpacks"


def _user_root(environ: dict | None = None) -> Path:
    """``$REKIT_HOME/goalpacks`` — user-authored goalpacks (zero install)."""
    return _home.rekit_home(environ) / "goalpacks"


def _load_from_dir(directory: Path) -> Goalpack:
    """Parse one goalpack folder into a :class:`Goalpack`.

    Reads ``GOALPACK.md`` frontmatter, loads the sibling ``system-prompt.md``, and
    resolves the ``renderer`` by importing ``renderer.py`` from the folder and
    getting the named function.
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

    renderer = _resolve_renderer(directory, str(meta.get("renderer") or "").strip())

    return Goalpack(
        name=name,
        title=title,
        goal=goal,
        requested_capabilities=requested,
        system_prompt=system_prompt,
        renderer=renderer,
        dir=directory,
    )


def _resolve_renderer(directory: Path, spec: str) -> Callable[..., dict[str, Any]]:
    """Resolve a ``renderer`` spec to a callable by importing a file from the folder.

    ``spec`` is ``"<module>:<function>"`` (e.g. ``renderer:render_report``) or a bare
    ``"<function>"`` (module defaults to ``renderer``). The module file is imported
    from ``<directory>/<module>.py`` via :func:`importlib.util.spec_from_file_location`
    — no package install, so a user goalpack's renderer loads with zero setup.
    """
    module_stem, _, func_name = spec.partition(":")
    module_stem = module_stem.strip() or DEFAULT_RENDERER_MODULE
    func_name = func_name.strip()
    if not func_name:
        # Bare form: the whole spec was the function name; module defaults.
        func_name = module_stem
        module_stem = DEFAULT_RENDERER_MODULE
    if not func_name:
        raise ValueError("goalpack renderer spec is empty")

    module_path = directory / f"{module_stem}.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"renderer module not found: {module_path}")

    # A stable, unique module name so two goalpacks' renderers never collide.
    mod_name = f"_rekit_goalpack_{directory.name}_{module_stem}"
    import_spec = importlib.util.spec_from_file_location(mod_name, module_path)
    if import_spec is None or import_spec.loader is None:
        raise ImportError(f"cannot load renderer module: {module_path}")
    module = importlib.util.module_from_spec(import_spec)
    import_spec.loader.exec_module(module)

    fn = getattr(module, func_name, None)
    if not callable(fn):
        raise AttributeError(
            f"renderer {func_name!r} not found (or not callable) in {module_path}"
        )
    return fn


def _as_tuple(value: Any) -> tuple[str, ...]:
    """Coerce a frontmatter scalar/list value to a tuple of non-empty strings."""
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    return (str(value).strip(),)
