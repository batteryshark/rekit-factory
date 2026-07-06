---
name: dex2jar
capability: dex-to-jar
accepts: [binary/dex, archive/apk]
emits: [archive/jar]
tier: executes-untrusted
host: d2j-dex2jar.sh (JVM required)
env: DEX2JAR_HOME
paths: [bin]
keywords: [android, dalvik, dex, apk, jar, dex2jar, bridge]
run: scripts/decompile.sh
---

# dex2jar — Android DEX/APK → Java `.jar`

dex2jar rewrites Dalvik bytecode (`.dex`, and the `classes*.dex` inside an `.apk`)
into standard JVM `.class` files packed in a `.jar`. It's a **bridge**: the `.jar`
it emits is what jadx accepts to reach readable Java, so rekit chains
`.dex`/`.apk` → dex2jar → `.jar` → jadx → source.

This is a **host-tool** skill: it runs an external converter on untrusted input,
so its tier is `executes-untrusted` — the runner gates it through the human
channel and executes it under a Seatbelt sandbox. Its capability is `dex-to-jar`
(distinct from `decompile`): it produces an intermediate `archive/jar`, not
source.

## Installing the tool

The binary is **not committed** — it's fetched onto your machine (gitignored). Run:

```sh
scripts/fetch.sh            # downloads the pinned dex2jar release
```

`fetch.sh` installs the launcher into the shared `$REKIT_HOME/bin` when that dir is
resolvable (so one install serves every skill), else into the skill's own `bin/`.
Either way host-gating finds it.

Resolution order at run time: `DEX2JAR_HOME` → `$REKIT_HOME/bin` / skill `bin/` →
`PATH`. Point `DEX2JAR_HOME` at an existing install
(`$DEX2JAR_HOME/d2j-dex2jar.sh` or `$DEX2JAR_HOME/bin/d2j-dex2jar.sh` must exist),
or put `d2j-dex2jar.sh` on your `PATH`.

Requires a JVM (`java` on PATH). Check with `java -version`.

## How rekit drives it

`scripts/decompile.sh <input> <out_dir>` wraps the invocation the skill uses:

```sh
d2j-dex2jar.sh -o <out_dir>/classes.jar <input>
```

The runner executes this under a macOS Seatbelt sandbox: no network, writes
confined to `<out_dir>`. The input DEX/APK is treated as untrusted — a malicious
file can't exfiltrate or escape.

## Output

A single `classes.jar` under `<out_dir>`, classified `archive/jar`, re-entering the
ledger so jadx decompiles it into Java source.
