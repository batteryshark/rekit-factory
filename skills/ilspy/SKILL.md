---
name: ilspy
capability: decompile
accepts: [binary/pe]
emits: [source/csharp]
tier: executes-untrusted
host: ilspycmd (.NET SDK required)
env: ILSPY_HOME
paths: [bin]
keywords: [dotnet, ilspy, cil, assembly, pe, decompile, csharp]
run: scripts/decompile.sh
---

# ilspy — .NET assembly (PE) → C# source

`ilspycmd` (the CLI of ILSpy) decompiles managed .NET assemblies — `.dll`/`.exe`
that carry CIL bytecode — back into readable C#. rekit uses it to turn an opaque
managed binary into source a goal (mcd, understand) can analyze.

This is a **host-tool** skill: it runs an external decompiler on untrusted input,
so its tier is `executes-untrusted` — the runner gates it through the human
channel and executes it under a Seatbelt sandbox.

## Installing the tool

`ilspycmd` is a **dotnet global tool**, so it needs the **.NET SDK/runtime**
(`dotnet` on PATH — https://dotnet.microsoft.com/download). The binary is **not
committed** — it's installed onto your machine (gitignored). Run:

```sh
scripts/fetch.sh            # dotnet tool install ilspycmd into the resolved bin dir
scripts/fetch.sh 8.2.0.7535 # optional: pin a specific version
```

`fetch.sh` installs the launcher into the shared `$REKIT_HOME/bin` when that dir is
resolvable (so one install serves every skill), else into the skill's own `bin/`.
Either way host-gating finds it.

Resolution order at run time: `ILSPY_HOME` → `$REKIT_HOME/bin` / skill `bin/` →
`PATH`. Point `ILSPY_HOME` at an existing install (`$ILSPY_HOME/ilspycmd` or
`$ILSPY_HOME/bin/ilspycmd` must exist), or put `ilspycmd` on your `PATH`.

Without `dotnet`, `fetch.sh` exits non-zero and the skill stays unavailable — the
runner degrades it to an "install ilspycmd" lead instead of crashing.

## How rekit drives it

`scripts/decompile.sh <assembly.dll|.exe> <out_dir>` wraps the invocation the skill
uses:

```sh
ilspycmd <assembly> -o <out_dir> -p
```

`-p` writes a `.csproj` project (a tree of `.cs`), `-o` sets the output dir. The
runner executes this under a macOS Seatbelt sandbox: no network, writes confined to
`<out_dir>`. The input assembly is treated as untrusted — a malicious PE can't
exfiltrate or escape.

## Output

A tree of `.cs` files under `<out_dir>`, classified `source/csharp`, re-entering
the ledger so the goal analyzes the recovered code.
