# rekit

The **orchestration runtime** for the Parallax reverse-engineering lab. rekit is
the kernel: the ralph loop, the persistent project **ledger**, discovery and
artifact tracking, skill loading/scoping, the human channel, and the dashboard.

## Scope

- **Runtime only.** rekit orchestrates; it does not understand code. Parsers,
  decompilers, and tree-sitter live inside *skills*, never here.
- **Harness-agnostic.** pi / claude / codex / opencode are pluggable brains
  behind one thin adapter seam (`invoke(...) -> actions`). Swap the brain without
  touching goalpacks or skills.
- **Depends on nothing** — not even parallax. rekit is a clean kernel with zero
  runtime dependencies; heavy deps live inside the skills that need them.

Plan of record: [`parallax/docs/rekit-epic.md`](../parallax/docs/rekit-epic.md).

## Layout

Subpackages mirror the architecture. At E0 they are docstring-only stubs; each is
filled by a later epic:

| Package         | Intent                                                              | Epic |
| --------------- | ------------------------------------------------------------------- | ---- |
| `rekit/ledger`  | Persistent project ledger (file protocol under `$REKIT_HOME/projects/<id>`) | E1 |
| `rekit/loop`    | The ralph loop that drives the harness against the ledger           | E2   |
| `rekit/harness` | Harness adapters — `invoke(brain, prompt, tools, ledger_context, tier)` (pi first) | E2 |
| `rekit/skills`  | Filesystem skill discovery, scoping, and a searchable registry      | E3   |
| `rekit/human`   | `ask_human` / `present_choices` channel                             | E4   |
| `rekit/cli.py`  | Minimal CLI entry point (`rekit`)                                   | E0   |

## Develop

The workspace uses [uv](https://docs.astral.sh/uv/):

```sh
uv venv
uv pip install -e .
.venv/bin/python tests/test_smoke.py    # plain-python runner
# or: .venv/bin/python -m pytest        # pytest-compatible
rekit --version
```

## Status

**E0 — scaffolding.** No orchestration logic yet; the epics above fill it in.
