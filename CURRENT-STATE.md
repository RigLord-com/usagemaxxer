# UsageMaxxer - Current State

Status as of **2026-07-15**: **v1.0.1 published** (v1.0.0 was the first
public release; security remediation complete).

This document records current product and release state. It is not a development
diary, incident history, or full technical specification.

## Product

UsageMaxxer is a Windows system-tray widget for subscription usage on Claude
Code and Codex. It displays provider-reported usage as instrument gauges and
shows only detected, enabled providers.

- Claude displays Session and Weekly windows when reported.
- Codex currently displays Weekly only; its short window is no longer reported.
- Gauges and the tray icon use green, amber, and red utilization states.
- Settings support provider toggles and start-on-login.
- The app polls approximately every five minutes and degrades per provider.
- The standalone Windows executable is built with PyInstaller.

## Data And Credentials

- Claude usage: `https://api.anthropic.com/api/oauth/usage`.
- Codex usage: `https://chatgpt.com/backend-api/wham/usage`.
- Credentials are read from each provider's local CLI credential file.
- Claude's expired OAuth token may be refreshed and written back to Claude's
  credential file. Codex credentials are never modified.
- Tokens are not logged or sent to third parties.
- Both usage endpoints are undocumented and may change or stop working without
  notice.

## Verified

- Claude percentages use the provider's explicit `limits[]` values.
- Codex's reported remaining percentage is converted to percentage used.
- Missing provider windows are omitted rather than shown as zero.
- Credential refresh merges current data, preserves Windows ACLs, rejects stale
  external updates, and keeps a valid rotated token usable after a write error.
- Authenticated redirects and oversized provider responses are rejected.
- Revoked Claude access tokens receive one refresh-and-retry attempt.
- Last-known values remain visible and marked stale after transient failures.
- Provider detection, settings, start-on-login, single-instance behavior, tray
  rendering, and graceful degradation are implemented.
- Ten regression tests, release-version validation, locked dependency
  resolution, and a clean temporary PyInstaller build pass.

## Known Limitations

- Provider endpoints are undocumented and unsupported.
- Scoped or additional usage windows are not displayed.
- Alerts are passive color changes; there are no toast notifications.
- Windows is the only supported platform.
- The executable is unsigned and may trigger Windows SmartScreen.
- Cross-process credential coordination remains inherently limited because the
  external Claude CLI does not share the widget's lock protocol.

## Release State

v1.0.0 and v1.0.1 are published on GitHub Releases with a downloadable
`.exe`, checksum, license, and third-party notices.

The release workflow uses hash-locked dependencies and SHA-pinned Actions,
runs tests and version validation, builds the executable, publishes a checksum,
creates a provenance attestation, and verifies the downloaded artifact. It
prepares a draft release; publishing the draft remains a manual step.

See [`README.md`](README.md) for installation and trust details, and
[`SPEC.md`](SPEC.md) for the original pre-build design reference.
