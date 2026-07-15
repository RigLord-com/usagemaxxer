# UsageMaxxer — Spec

A Windows system-tray widget that shows, at a glance, how close you are to the
usage limits on the AI coding tools you're logged into. Race-car instrument
aesthetic (analog gauges, needle, red zone). Reads the login token each CLI
already stores locally, and calls that provider's own usage endpoint. Open
source. Part of the RigLord ecosystem.

Status: **finalized for build** (2026-07-11) — this is the original pre-build
spec, kept for reference. It was built as a **v1.0.0 release candidate** but has
not been publicly released. For what was implemented — and where it diverged from this spec
— see [`CURRENT-STATE.md`](CURRENT-STATE.md). Two decisions below evolved during
the build: the panel became **per-provider bands**, not the aligned Session/Weekly
grid described under "Layout"; and **Codex now reports a Weekly window only** (its
~5h `secondary_window` was retired upstream). The widget also now writes Claude's
credential file to refresh an expired token in place — so it is no longer strictly
read-only.

---

## The core idea

- Tracks **subscription plan usage** (session / weekly windows), NOT API-key billing.
- Reads **utilization %** from each provider, which is already plan-relative — so it
  works on any plan tier (Pro, Max, Plus…) without us hardcoding limits.
- **Provider-agnostic by design:** providers are a registry; the UI renders whatever
  is enabled and detected. Adding a provider later = add a fetcher + registry entry,
  no UI rewrite.

---

## DECIDED

**Name & identity**
- Product name: **UsageMaxxer** (tagline "Coding at Redline"). Folder stays generic
  (`Usage-Widget`) so a name change never forces a move. (Originally specced as
  "Usagemaxxer"; recased at release.)
- Lives under the RigLord project. **No formal RigLord branding
  in-app for v1** — at most a small "by RigLord" line in the About box. The widget
  stands on the race-car aesthetic.

**Platform & stack**
- Windows system-tray app. Python (pystray + Pillow + tkinter), stdlib HTTP fetchers.
- Packaged as a **standalone `.exe`** (PyInstaller) — end users need no Python.

**Providers**
- **v1: Claude Code + Codex** (both proven — local token → clean usage endpoint).
- **Phase 2: kept open** — architecture accepts arbitrary future providers. Known
  candidates Grok/xAI and OpenCode Go are *web-dashboard* providers (no clean local
  usage endpoint) — deferred, not designed-in yet.
- **Windows shown: Session + Weekly only.** (Claude's Opus/Sonnet scoped weeklies are
  NOT shown in v1.)

**Data / fetchers (proven, carry forward verbatim)**
- Claude Code: `GET https://api.anthropic.com/api/oauth/usage`; Bearer from
  `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`); headers
  `anthropic-beta: oauth-2025-04-20`, `anthropic-version: 2023-06-01`.
  Windows: `five_hour` → Session, `seven_day` → Weekly. `extra_usage` = credit balance.
- Codex: `GET https://chatgpt.com/backend-api/wham/usage`; Bearer +
  `ChatGPT-Account-Id` from `~/.codex/auth.json`. Windows: `primary_window` → Session,
  `secondary_window` → Weekly. Note `reset_at` is a unix epoch int, not ISO.
- Both endpoints are undocumented → code defensively; each provider row fails soft
  without taking down the others.
- Utilization comes back as a %/fraction already relative to the user's plan.

**Layout**
- **Grid instrument cluster.** Columns = providers (toggleable), rows = window type
  (Session on top, Weekly below), **aligned across providers** so you can compare a
  row at a glance. Grows sideways as providers are added.
- Race-car feel: analog gauge, needle, green→amber→red zones, red zone = "redline."
- **Fixes required from prior iterations:**
  - Tray icon must be clearly visible on BOTH light and dark taskbars (prior gauge
    outline was invisible — needs a filled/high-contrast treatment).
  - Large high-contrast digital `%` as the primary readout; labels + reset-times
    bigger and brighter than the v2-gauges version.

**Alerts — passive only (kept simple for v1)**
- Gauge + tray icon color shifts green→amber→red by utilization.
- Thresholds fixed: **amber ≥ 70%, red ≥ 90%.**
- **No toast/popup notifications in v1** (deferred — see Non-goals). No alert state
  tracking needed.

**Settings window** (tray right-click → Settings)
- Toggle each provider on/off, with live **status** (detected / not logged in / off).
- Start-on-login checkbox.
- Persisted to `%APPDATA%\Usagemaxxer\config.json`.
- (Deliberately minimal — no threshold or notification config in v1.)

**Productization (what makes it a real release, not a script)**
- **Graceful per-provider detection & degradation:** auto-detect which CLIs are
  installed / logged-in; render only those; an absent or logged-out provider shows its
  status in Settings, never an error tile in the panel.
- Expired/invalid token → clear "log in to refresh" message, no crash.
- Never crash on offline/network error; keep last-known values with a stale marker.
- First-run: detect providers, show a one-line trust note (reads local login,
  read-only, link to source), apply sensible defaults.
- App identity: name, `.exe` icon, About/version. Single-instance (don't launch twice).

**Distribution**
- **Open source**, MIT. **Unsigned** for v1 — document the SmartScreen
  "More info → Run anyway" step. Public repo under the **`RigLord-com`** GitHub org
  ([RigLord-com/usagemaxxer](https://github.com/RigLord-com/usagemaxxer)), with
  Releases carrying the `.exe`.
- README: what it does, the trust story (read-only token access, here's the exact
  code), install, SmartScreen note.

**Runtime**
- Poll every ~5 min; re-read credential files each poll (CLIs keep them refreshed).

---

## Build components

1. **Fetcher layer** — provider registry + normalized `Snapshot` (variable windows).
   Carry from prior version (`design-history/v2-gauges/redlined.py`).
2. **Provider detection** — is each CLI installed / logged in? drives Settings status
   and graceful degradation.
3. **Config store** — load/save `%APPDATA%\Usagemaxxer\config.json`, defaults,
   forward-compatible.
4. **Tray icon renderer** — contrast-fixed race-car gauge, visible on light + dark
   taskbars; color reflects worst-case utilization.
5. **Panel/grid renderer** — instrument cluster; enabled providers × aligned window
   rows (Session/Weekly); large high-contrast `%`.
6. **Settings window** — provider toggles + status, start-on-login.
7. **Lifecycle** — poll loop, start-on-login install/uninstall, single-instance, quit.
8. **Packaging** — PyInstaller spec, `.exe` icon, version stamp.
9. **Repo/release** — README (trust + SmartScreen), LICENSE, GitHub Release.

---

## Non-goals (v1)

- **Toast/popup notifications** (passive color only in v1; toasts are an easy phase-2 add).
- API-key / dollar billing tracking (subscription plan windows only).
- Grok, OpenCode Go (phase-2 web-dashboard providers).
- Opus/Sonnet scoped weekly windows.
- Auto-update.
- macOS / Linux (Windows first).
- Cross-device sync.

---

## Reference

- Working fetcher + gauge code and two frozen prior UIs live in `design-history/`
  (`v1-bars/`, `v2-gauges/`). The v2 gauges are the visual starting point;
  fix icon contrast + text contrast per above.
