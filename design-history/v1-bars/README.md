# Usage Widget v1 — design reference (superseded)

Built 2026-07-11: the first working version of the Claude Code + Codex usage
tray widget, before any branding was applied. Plain dark popup panel with
horizontal progress bars (session % and weekly % per provider, green/amber/red
by severity) and a two-column tray icon (each column a vertical fill bar).

**Status: superseded** — twice over. First replaced by "Redlined" (an analog
instrument-cluster redesign, itself now archived at
`../redlined-design-reference/`), which was then superseded by the decision to
build this as a component of the RigLord project rather than a standalone
branded product. The current product is UsageMaxxer.

**Why kept:** this is the plainest, fastest-to-read version of the UI — no
brand styling, just bars and numbers. Useful as a baseline if a future
UsageMaxxer design turns out to want something closer to "functional dashboard" than
"instrument cluster." The verified fetcher logic (Claude Code `/api/oauth/usage`
+ Codex `/wham/usage`, both reading local CLI credentials) is identical to what
carried forward into Redlined; diff this file against the Redlined one for
implementation details.

`usage_widget.py` here is the last working version, frozen as-is — not
maintained.
