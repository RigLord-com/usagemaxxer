# Contributing to UsageMaxxer

This is a small, personal tool shared in the open. Improvements are welcome —
especially from people who actually run coding agents all day and want the
gauges to be sharper.

## The honest state of things

Read [`CURRENT-STATE.md`](CURRENT-STATE.md) first. It's a candid snapshot of
what works, what's fragile, and why. The core caveat: **both usage endpoints are
undocumented** and reverse-engineered from what each CLI calls internally. They
can change or break with no notice. That's inherent to the idea, not a bug to
fix.

## Getting it running

```sh
pip install --require-hashes -r requirements.txt
python usagemaxxer.py --once   # prints both usage snapshots as text — the fastest way to test a change
pythonw usagemaxxer.py         # runs the tray widget
```

The whole app is one file: [`usagemaxxer.py`](usagemaxxer.py). The network layer
is ~150 lines (`fetch_claude`, `_refresh_claude_oauth`, `fetch_codex`).

## Good things to work on

- **Another provider.** The pattern is: read the CLI's local credential file,
  call its usage endpoint, normalize to percent-used. Gemini CLI, Cursor, etc.
- **macOS / Linux.** Currently Windows-only (tray + startup are Win32).
- **Endpoint resilience.** Better handling for when a provider changes its
  response shape.
- **Toast alerts** when you cross a threshold (currently passive: color only).

## Opening a pull request

1. Fork the repo, branch off `main`.
2. Make your change; test it with `python usagemaxxer.py --once`.
3. Open a PR describing what you changed and how you verified it.

No CLA, no process overhead. If it makes the gauges more accurate or more useful,
it's welcome.

## Security note

Never commit a credential file (`.credentials.json`, `auth.json`) or any token.
The app reads both files and may safely rotate Claude's OAuth token in its own
credential file at runtime; neither file should ever enter this repo.
