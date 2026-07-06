---
name: ghidra
capability: decompile
accepts: [binary/native]
emits: [source/c]
tier: executes-untrusted
host: analyzeHeadless (JVM required)
env: GHIDRA_HOME
paths: [bin]
keywords: [ghidra, native, elf, macho, pe, decompile, c, reverse-engineering]
run: scripts/decompile.sh
---

# ghidra — native binary (ELF/Mach-O/PE) → C source

Ghidra's headless analyzer decompiles native code (ELF, Mach-O, native PE) back
into C-like pseudocode. rekit uses it to turn an opaque compiled binary into
source a goal (mcd, understand) can actually analyze.

This is a **host-tool** skill: it runs an external decompiler on untrusted input,
so its tier is `executes-untrusted` — the runner gates it through the human
channel and executes it under a Seatbelt sandbox.

## Installing the tool

Ghidra is **not committed** — it's large and fetched onto your machine
(gitignored). Run:

```sh
scripts/fetch.sh            # downloads the pinned Ghidra release, drops analyzeHeadless
```

`fetch.sh` defaults to the pinned 11.3.2 release asset; because the GitHub asset
name carries a build-date suffix that changes, you may pass the full download URL
as `$1` if the default 404s. It extracts into `dist/` and writes a thin
`analyzeHeadless` wrapper (into the shared `$REKIT_HOME/bin` when resolvable, else
the skill's own `bin/`) that `exec`s the real launcher under
`dist/ghidra_*/support/`.

Resolution order at run time: `GHIDRA_HOME` → `$REKIT_HOME/bin` / skill `bin/` →
`PATH`. Point `GHIDRA_HOME` at an existing install
(`$GHIDRA_HOME/support/analyzeHeadless` must exist), or put `analyzeHeadless` on
your `PATH`.

Requires a JVM (`java` on PATH). Check with `java -version`.

## How rekit drives it

`scripts/decompile.sh <binary> <out_dir>` wraps the invocation the skill uses:

```sh
analyzeHeadless <out_dir>/project rekit -import <binary> \
    -scriptPath scripts -postScript DecompileToC.java <out_dir>/decompiled.c \
    -deleteProject
```

`DecompileToC.java` is a GhidraScript that decompiles every function via
`DecompInterface` and appends the C to the output file. The runner executes this
under a macOS Seatbelt sandbox: no network, writes confined to `<out_dir>` (the
Ghidra project is created there). The input binary is treated as untrusted —
native analysis is a large parser surface, so a malicious binary can't exfiltrate
or escape.

## Output

C source (`<out_dir>/decompiled.c`) collected as a `tree`, classified `source/c`,
re-entering the ledger so the goal analyzes the recovered code.
