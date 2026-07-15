# UsageMaxxer v1.0.0

A Windows system-tray widget for subscription usage on Claude Code and Codex,
shown as race-car style analog gauges.

## What it does

- Reads the login token each CLI already stores locally and calls that
  provider's own usage endpoint -- no configuration, no API keys.
- Shows Session (~5h) and Weekly (7-day) plan utilization as analog gauges,
  green -> amber -> red as you approach the limit.
- Auto-detects which tools you're logged into and shows only those; a missing
  or logged-out provider degrades gracefully instead of showing an error.
- A tray icon that stays legible on light and dark taskbars.

## Install

Download `UsageMaxxer.exe` below and verify its SHA-256 against
`UsageMaxxer.exe.sha256`, then run it. The app is open-source but unsigned,
so Windows SmartScreen will show a warning on first run -- click
**More info -> Run anyway**. See the
[README](https://github.com/RigLord-com/usagemaxxer#readme) for full
installation and trust details.

## Is it safe?

The widget never asks for a password or token. It reads the credential file
the CLI you already use keeps on your machine, and makes at most two HTTPS
requests per provider, both to that provider's own API. Nothing is sent to
any third party. Full details in the README.
