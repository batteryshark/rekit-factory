---
name: mcd
title: Malicious-code detection
goal: Is this code doing something malicious, and can I prove it?
requestedCapabilities: [unpack, decompile]
renderer: renderer:render_report
---

# mcd — malicious-code detection

The security-review goalpack: point it at an artifact and ask whether it is
doing something malicious, and whether that can be proven from the evidence.

In the old `prlx-mcd` product this ran as two passes — a deterministic scan that
*found* malicious-code shapes, then a separate model *review* pass that
adjudicated each finding. In the goalpack world **the brain is the adjudicator**:
it both finds the behaviours and, having read the code behind each, assigns a
verdict (confirm / escalate / deescalate / refute / suppress) and a response tier
(0–5). Each finding is one `FINDING:` line carrying those structured fields:

    FINDING: [sev:high conf:0.8 verdict:confirm tier:3] <title> :: <evidence/proof>

The brain uses its built-in source reading for evidence, and when the target is
packed or a binary it requests the `unpack` / `decompile` skills via
`RUN_SKILL` (the two capabilities this goalpack scopes in) before judging.

The **renderer owns the numbers.** The reviewer only *classifies*; the renderer
applies the deterministic confidence rule ported from `adjudicate.py` (confirm
keeps confidence, escalate raises, deescalate ×0.6, refute caps at 0.1, suppress
→ 0), keeps severity untouched, keeps refuted/suppressed findings in the report
(flagged) but drops them from the disposition input, and **recomputes** the
quarantine / review / clear disposition over the reviewed confidences. Engine
(brain-stated) vs reviewed values are both retained so the two are diffable.
There is no shared `report_model`; this goalpack owns its assessment shape.
