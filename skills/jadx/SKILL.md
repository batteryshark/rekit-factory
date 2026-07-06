---
name: jadx
capability: decompile
accepts: [archive/apk, binary/dex]
emits: [source/java]
tier: executes-untrusted
host: jadx (JVM required)
env: JADX_HOME
paths: [bin]
keywords: [android, dalvik, apk, dex, decompile, java]
run: scripts/decompile.sh
---

# jadx — Android APK/DEX → Java source

jadx decompiles Dalvik bytecode (`.dex`, and the `classes*.dex` inside an `.apk`)
back into readable Java. rekit uses it to turn an opaque Android binary into
source a goal (mcd, understand) can actually analyze.

This is a **host-tool** skill: it runs an external decompiler on untrusted input,
so its tier is `executes-untrusted` — the runner gates it through the human
channel and executes it under a Seatbelt sandbox.

## Installing the tool

The binary is **not committed** — it's fetched onto your machine (gitignored). Run:

```sh
scripts/fetch.sh            # downloads the latest pinned jadx release
```

`fetch.sh` installs the launcher into the shared `$REKIT_HOME/bin` when that dir is
resolvable (so one install serves every skill), else into the skill's own `bin/`.
Either way host-gating finds it.

Resolution order at run time: `JADX_HOME` → `$REKIT_HOME/bin` / skill `bin/` →
`PATH`. Point `JADX_HOME` at an existing install (`$JADX_HOME/bin/jadx` must
exist), or put `jadx` on your `PATH`.

Requires a JVM (`java` on PATH). Check with `java -version`.

## How rekit drives it

`scripts/decompile.sh <input> <out_dir>` wraps the invocation the skill uses:

```sh
jadx --no-res --no-debug-info -q -d <out_dir> <input>
```

The runner executes this under a macOS Seatbelt sandbox: no network, writes
confined to `<out_dir>`. The input APK/DEX is treated as untrusted — a malicious
archive can't exfiltrate or escape.

## Output

A tree of `.java` files under `<out_dir>/sources/`, classified `source/java`,
re-entering the ledger so the goal analyzes the recovered code.
