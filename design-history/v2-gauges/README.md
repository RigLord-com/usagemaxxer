# Redlined — design reference (superseded)

Built 2026-07-11 as a standalone product concept: a Windows tray widget showing
Claude Code + Codex usage as an analog instrument cluster (needle gauges, red
zone past 88%, "coding at redline" tagline, domain `codingatredline.com`).

**Status: superseded.** This design informed the current UsageMaxxer build. Its
gauge-cluster UI remains reference material, while the current product identity
is UsageMaxxer under RigLord.

**Why kept:** the gauge-cluster UI (needle physics, zone coloring, tray-icon
rendering, panel layout) and the verified usage-fetcher logic (Claude Code
`/api/oauth/usage` + Codex `/wham/usage`, both reading local CLI credentials)
are still valid reference material even though the brand identity around them
isn't being carried forward as-is.

`redlined.py` here is the last working version, frozen as-is — not maintained.
