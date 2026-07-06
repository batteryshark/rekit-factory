"""rekit.skills — filesystem skill discovery, scoping, and a searchable registry (E3).

Skills are the tools: ``SKILL.md`` + ``scripts/`` folders (each with its own uv
venv) discovered by convention under ``$REKIT_HOME/skills`` — no pip, no entry
points. Dropping a folder makes it available with zero install.

Two halves work together. Scoping is the passive half: rekit computes
``(kinds present in the ledger) ∩ (capabilities the goalpack requested)``,
filtered by trust tier, and hands the harness *only* that set each turn — so the
brain never scans the rack, identically across harnesses. The registry is the
active half: ``find_skills(intent)`` searches SKILL.md description/keywords so the
agent can reach for an instrument when it notices something with no special
artifact kind. Mirrors ToolSearch.

Filled by epic E3. No logic here yet (E0 scaffold).
"""
