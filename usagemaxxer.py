"""UsageMaxxer — Coding at Redline.

Glance at how close your AI coding agents are to the limit.

A Windows system-tray instrument cluster for Claude Code + Codex. Analog gauges
with a needle and a red zone show your Session (~5h) and Weekly (7-day) plan
utilization, live in the tray. It reads the same local login each CLI already
stores and calls that provider's own usage endpoint, so the numbers are the
real ones on any plan tier. The only file it ever writes is Claude's own
credentials file, and only to refresh an expired token in place. Race-car
aesthetic. Part of RigLord.

Data sources (both undocumented — code defensively):
  Claude Code:  GET https://api.anthropic.com/api/oauth/usage
                Bearer token from ~/.claude/.credentials.json. (A long-lived
                `claude setup-token` was tried and reverted — confirmed live
                that it lacks the scope this endpoint requires, so it 403s
                every time. The on-disk token is the only one that works.
                Only the terminal CLI ever rewrites that file — the desktop
                app authenticates separately and never touches it — so when
                the stored token expires the widget refreshes it itself via
                the OAuth refresh_token grant and writes the result back;
                see _refresh_claude_oauth.)
  Codex:        GET https://chatgpt.com/backend-api/wham/usage
                Bearer token + account id from ~/.codex/auth.json

Usage:
  python usagemaxxer.py --once            # print both snapshots as text
  pythonw usagemaxxer.py                  # run the tray widget
  python usagemaxxer.py --install-startup # auto-start on login
  python usagemaxxer.py --uninstall-startup
"""

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "UsageMaxxer"
APP_ID = "Usagemaxxer"  # stable identity: config dir, mutex, startup .cmd — do not
                        # re-case, or existing installs would orphan their config
TAGLINE_LEAD = "Coding at "   # tagline, drawn in two colors in the masthead
TAGLINE_HOT = "REDLINE"
VERSION = "1.0.0"
POLL_SECONDS = 300  # 5 minutes
HOME = Path.home()
MAX_RESPONSE_BYTES = 1024 * 1024  # Usage payloads are small; never buffer an unbounded response.

# Instrument palette
BG = "#0d0e10"          # dashboard black
FACE = "#16171b"        # dial face
RIM = "#2a2c31"         # dial rim
TICK = "#767b85"        # tick marks (brightened for contrast)
NEEDLE = "#f4f5f7"      # needle (turns red in the red zone)
INK = "#ffffff"         # digital readout (max contrast)
LABEL = "#c7ccd4"       # row / provider labels (brightened)
MUTED = "#9aa0aa"       # secondary labels (brightened)
FAINT = "#6b7079"       # footer / reset times (brightened)
GREEN = "#3ec16a"
AMBER = "#f5a524"
RED = "#f0393e"
DISC = "#1b1c20"        # icon disc body
RIMLIGHT = "#e9eaed"    # icon rim highlight (reads on dark taskbars)

# Alert thresholds (fixed for v1): amber >= 70%, red >= 90%.
AMBER_AT = 70
RED_AT = 90

# ---------------------------------------------------------------- normalized shape


# Window kinds, in display order. `kind` is the stable identity we key on;
# `label` is only for display. Keying by kind (not a label prefix) is what lets
# a provider expose any subset of windows — Codex now offers "weekly" only.
WINDOW_ORDER = {"session": 0, "weekly": 1}


@dataclass
class Window:
    kind: str          # "session" | "weekly"
    label: str         # display text, e.g. "Session" / "Weekly"
    percent: float     # 0–100, clamped at construction-time (see _pct)
    resets_at: datetime | None = None


@dataclass
class Snapshot:
    provider: str
    ok: bool = False
    error: str = ""
    plan: str = ""
    windows: list[Window] = field(default_factory=list)
    credits: str = ""
    fetched_at: datetime | None = None
    auth_error: bool = False  # 401/403 → needs "log in to refresh", not a stale keep
    stale: bool = False       # showing last-known values after a transient failure

    def window(self, kind: str) -> Window | None:
        for w in self.windows:
            if w.kind == kind:
                return w
        return None

    def sort_windows(self) -> None:
        self.windows.sort(key=lambda w: WINDOW_ORDER.get(w.kind, 99))


def _pct(value) -> float:
    """Normalize any provider's percentage to a clamped 0–100 float. Every
    Window.percent goes through here so no downstream code ever has to guess
    whether a number is a fraction or a percent again."""
    try:
        return min(max(float(value), 0.0), 100.0)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):  # Codex sends unix epoch seconds
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Authenticated requests must never forward credentials to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _get_json(url: str, headers: dict, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers)
    with _NO_REDIRECT_OPENER.open(req, timeout=15) as resp:
        # Defense in depth: reject an unexpected final scheme or origin even if a
        # future handler changes redirect behavior.
        requested = urllib.parse.urlsplit(url)
        actual = urllib.parse.urlsplit(resp.geturl())
        if actual.scheme != "https" or actual.netloc != requested.netloc:
            raise urllib.error.URLError("unexpected redirect target")
        body = resp.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds size limit")
        return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- fetchers
# Proven against live Claude Code + Codex accounts (2026-07-11). Carried
# verbatim from design-history/v2-gauges — do not re-derive the endpoints.


# The OAuth client id Claude Code's own login flow uses. Needed for the
# refresh_token grant below. Undocumented, same as the usage endpoint.
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"

# After a failed refresh, don't retry until this (epoch seconds). Only touched
# from the poll thread. Keeps a revoked/rotated-away refresh token from
# hammering the endpoint every poll; the CLI may rewrite the file in the
# meantime, and we re-read it fresh each poll.
_claude_refresh_backoff_until = 0.0
_claude_refresh_lock = threading.Lock()
_claude_pending_refresh: tuple[Path, str, dict] | None = None
_claude_last_refresh_error = ""


def _replace_credential_file(tmp_path: Path, creds_path: Path) -> None:
    """Replace a credential file without widening its permissions or Windows ACL."""
    if os.name == "nt":
        # ReplaceFileW keeps the original file's security descriptor (DACL),
        # unlike os.replace(), which installs the temp file's inherited ACL.
        import ctypes
        if not ctypes.windll.kernel32.ReplaceFileW(str(creds_path), str(tmp_path), None, 0, None, None):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        shutil.copystat(creds_path, tmp_path)
        os.replace(tmp_path, creds_path)


def _persist_claude_oauth(creds_path: Path, expected_refresh_token: str,
                          updated_oauth: dict) -> bool:
    """Merge a rotated token into the latest credentials without clobbering CLI data.

    A changed refresh token means another writer won the race. Do not overwrite it.
    """
    latest_text = creds_path.read_text(encoding="utf-8")
    latest = json.loads(latest_text)
    latest_oauth = latest.get("claudeAiOauth") or {}
    if latest_oauth.get("refreshToken") != expected_refresh_token:
        return False
    merged_oauth = dict(latest_oauth)
    for key in ("accessToken", "refreshToken", "expiresAt"):
        if key in updated_oauth:
            merged_oauth[key] = updated_oauth[key]
    latest["claudeAiOauth"] = merged_oauth
    fd, tmp_name = tempfile.mkstemp(prefix=f".{creds_path.name}.", suffix=".tmp", dir=creds_path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(latest, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        # Check once more immediately before the atomic replacement. This avoids
        # overwriting a CLI rewrite that landed while we prepared the temp file.
        if creds_path.read_text(encoding="utf-8") != latest_text:
            return False
        _replace_credential_file(tmp_path, creds_path)
        return True
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _current_claude_oauth(creds_path: Path) -> dict | None:
    try:
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = creds.get("claudeAiOauth") or {}
        return oauth if oauth.get("accessToken") else None
    except (OSError, ValueError, TypeError):
        return None


def _refresh_claude_oauth(creds_path: Path, creds: dict) -> dict | None:
    """Mint a fresh Claude access token from the stored refreshToken and write
    the whole credentials file back (atomically), so the CLI benefits too.
    Returns the updated claudeAiOauth dict, or None if refresh isn't possible
    right now (no refresh token, backing off, or the grant failed).

    Why the widget does this itself: the terminal CLI refreshes
    .credentials.json, but the desktop app authenticates separately and never
    touches that file — so for a desktop-app-only user the on-disk token expires
    ~8h after their last CLI run and stays dead (root-caused 2026-07-14,
    CURRENT-STATE.md issue #3). Waiting for someone else to refresh it means
    waiting forever; an app has to keep its own data source alive.
    """
    global _claude_refresh_backoff_until
    global _claude_last_refresh_error
    with _claude_refresh_lock:
        now = datetime.now(timezone.utc).timestamp()
        global _claude_pending_refresh
        if _claude_pending_refresh and _claude_pending_refresh[0] == creds_path:
            _, expected_refresh_token, pending_oauth = _claude_pending_refresh
            try:
                if _persist_claude_oauth(creds_path, expected_refresh_token, pending_oauth):
                    _claude_pending_refresh = None
                else:
                    # The CLI rotated credentials after our read. Its newer file
                    # wins; do not keep retrying a stale write on later polls.
                    _claude_pending_refresh = None
                    return _current_claude_oauth(creds_path)
                return pending_oauth
            except Exception:
                # Keep the valid in-memory token and retry persistence next poll.
                _claude_last_refresh_error = "could not save refreshed Claude credentials"
                return pending_oauth
        # Respect the rate-limit backoff after a failed grant, including after a
        # usage endpoint 401/403.
        if now < _claude_refresh_backoff_until:
            return None
        oauth = creds.get("claudeAiOauth") or {}
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            _claude_last_refresh_error = "Claude refresh token is missing"
            return None
        try:
            tok = _get_json(
                CLAUDE_OAUTH_TOKEN_URL,
                {"Content-Type": "application/json", "User-Agent": APP_ID},
                json.dumps({
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLAUDE_OAUTH_CLIENT_ID,
                }).encode("utf-8"),
            )
            updated_oauth = dict(oauth)
            updated_oauth["accessToken"] = tok["access_token"]
            if tok.get("refresh_token"):
                updated_oauth["refreshToken"] = tok["refresh_token"]
            updated_oauth["expiresAt"] = int((now + float(tok.get("expires_in", 28800))) * 1000)
            _claude_pending_refresh = (creds_path, refresh_token, updated_oauth)
            try:
                if _persist_claude_oauth(creds_path, refresh_token, updated_oauth):
                    _claude_pending_refresh = None
                else:
                    _claude_pending_refresh = None
                    _claude_last_refresh_error = "Claude credentials changed during refresh"
                    return _current_claude_oauth(creds_path)
            except Exception:
                # The rotation succeeded even though disk persistence did not.
                # Use this access token now and retry the saved write next poll.
                _claude_last_refresh_error = "could not save refreshed Claude credentials"
            return updated_oauth
        except urllib.error.HTTPError as e:
            _claude_refresh_backoff_until = now + 1800
            _claude_last_refresh_error = f"Claude refresh rejected (HTTP {e.code})"
            return None
        except (OSError, ValueError, KeyError, TypeError):
            _claude_refresh_backoff_until = now + 1800
            _claude_last_refresh_error = "Claude refresh response or credential file was invalid"
            return None


def fetch_claude() -> Snapshot:
    snap = Snapshot(provider="Claude Code")
    global _claude_last_refresh_error
    try:
        # NOTE: a long-lived CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`)
        # was tried here and reverted — confirmed live that it 403s this
        # endpoint with permission_error "does not meet scope requirement
        # user:profile". That token is scoped for running Claude Code itself,
        # not for reading account usage, so it can never work here. The on-disk
        # token is the only one with the right scope for this endpoint.
        creds_path = HOME / ".claude" / ".credentials.json"
        creds = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = creds["claudeAiOauth"]
        snap.plan = str(oauth.get("subscriptionType", "")).title()
        # An expired token never fixes itself when a desktop-app user does not
        # run the terminal CLI for days, so
        # refresh it ourselves. (Don't fire the usage request with a dead
        # token: repeated 401s get us rate-limited.) A 60s margin keeps a
        # token from dying mid-flight.
        exp = oauth.get("expiresAt")
        if isinstance(exp, (int, float)) and exp / 1000 <= datetime.now(timezone.utc).timestamp() + 60:
            oauth = _refresh_claude_oauth(creds_path, creds)
            if oauth is None:
                snap.error = _claude_last_refresh_error or "Waiting for refresh ..."
                snap.auth_error = True
                snap.fetched_at = datetime.now(timezone.utc)
                return snap
        token = oauth["accessToken"]

        headers = {
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
            "User-Agent": APP_ID,
        }
        try:
            payload = _get_json("https://api.anthropic.com/api/oauth/usage", headers)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            # An access token can be revoked before its local expiry. Refresh
            # once and retry; never turn this into an unbounded auth loop.
            oauth = _refresh_claude_oauth(creds_path, creds)
            if oauth is None:
                raise
            headers["Authorization"] = f"Bearer {oauth['accessToken']}"
            payload = _get_json("https://api.anthropic.com/api/oauth/usage", headers)
        # Source of truth: the `limits` array. Each entry carries an explicit
        # `percent` (already 0–100), so we never have to guess fraction-vs-
        # percent. This is the fix for the old five_hour/seven_day heuristic,
        # which read `utilization` and multiplied by 100 when it was <= 1 —
        # wrong, because `utilization: 1.0` means 1%, not 100%. v1 shows
        # Session + Weekly only (Opus/Sonnet scoped weeklies deferred).
        limits_map = {"session": ("session", "Session"),
                      "weekly_all": ("weekly", "Weekly")}
        for lim in payload.get("limits") or []:
            mapped = limits_map.get(lim.get("kind"))
            if not mapped or lim.get("percent") is None:
                continue
            kind, label = mapped
            snap.windows.append(
                Window(kind, label, _pct(lim["percent"]), _parse_dt(lim.get("resets_at")))
            )
        # Fallback for older/leaner payloads with no `limits` array. Here
        # `utilization` is likewise already a percent (1.0 → 1%), so take it
        # as-is — no ×100.
        if not snap.windows:
            for key, kind, label in (("five_hour", "session", "Session"),
                                     ("seven_day", "weekly", "Weekly")):
                win = payload.get(key) or {}
                util = win.get("utilization")
                if util is None:
                    continue
                snap.windows.append(
                    Window(kind, label, _pct(util), _parse_dt(win.get("resets_at")))
                )
        snap.sort_windows()
        extra = payload.get("extra_usage") or {}
        if extra.get("is_enabled"):
            used = extra.get("used_credits")
            limit = extra.get("monthly_limit")
            places = int(extra.get("decimal_places") or 2)
            if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
                snap.credits = (
                    f"Extra usage: ${used / 10**places:.2f} / ${limit / 10**places:.2f}"
                )
        snap.ok = bool(snap.windows)
        if not snap.ok:
            snap.error = "no usage windows in response"
    except FileNotFoundError:
        snap.error = "log in to Claude Code to connect"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            snap.error = "Waiting for refresh ..."
            snap.auth_error = True
        else:
            snap.error = f"HTTP {e.code} — retrying"
    except urllib.error.URLError:
        snap.error = "offline — can't reach Anthropic"
    except Exception as e:  # noqa: BLE001
        snap.error = str(e)
    snap.fetched_at = datetime.now(timezone.utc)
    return snap


def fetch_codex() -> Snapshot:
    snap = Snapshot(provider="Codex")
    try:
        auth = json.loads((HOME / ".codex" / "auth.json").read_text(encoding="utf-8"))
        tokens = auth.get("tokens") or {}
        headers = {
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        account_id = str(tokens.get("account_id") or "").strip()
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        payload = _get_json("https://chatgpt.com/backend-api/wham/usage", headers)
        snap.plan = str(payload.get("plan_type") or "").replace("_", " ").title()
        rate = payload.get("rate_limit") or {}
        # Codex now exposes a Weekly window only — its former ~5h window is gone
        # (secondary_window comes back null). We don't hardcode that: we read
        # whatever windows are present and label each by its actual duration
        # (>= 1 day → Weekly, else Session), because "primary" vs "secondary"
        # isn't a stable Session/Weekly assignment. In practice this yields a
        # single Weekly window today; if Codex ever restores a short window it
        # will simply appear as a second gauge with no code change.
        #
        # A null slot is skipped, not shown as 0%. (This was tried the other way
        # and reverted: confirmed live that Weekly kept climbing across many
        # polls while the null slot never populated — a genuinely-fresh window
        # would have. So null means "not reported", never "0% used".)
        for key in ("primary_window", "secondary_window"):
            win = rate.get(key)
            if not win:
                continue
            # `used_percent` is misnamed: it's the percent REMAINING, not used.
            # Confirmed by the account owner comparing the widget directly to
            # Codex's own app — this inversion is exactly what made the two
            # disagree. Flip it so the gauge shows % used like every other
            # window. (If a future payload ever really is "used", this is the
            # one line to revisit.)
            remaining = win.get("used_percent")
            if remaining is None:
                continue
            used = 100.0 - _pct(remaining)
            window_secs = win.get("limit_window_seconds")
            if isinstance(window_secs, (int, float)) and window_secs >= 86400:
                kind, label = "weekly", "Weekly"
            else:
                kind, label = "session", "Session"
            # Codex reset_at is a unix epoch int, not ISO — _parse_dt handles both.
            snap.windows.append(Window(kind, label, _pct(used), _parse_dt(win.get("reset_at"))))
        snap.sort_windows()
        credits = payload.get("credits") or {}
        if credits.get("has_credits"):
            bal = credits.get("balance")
            if isinstance(bal, (int, float)):
                snap.credits = f"Credits balance: ${float(bal):.2f}"
            elif credits.get("unlimited"):
                snap.credits = "Credits balance: unlimited"
        snap.ok = bool(snap.windows)
        if not snap.ok:
            snap.error = "no usage windows in response"
    except FileNotFoundError:
        snap.error = "log in to OpenAI to connect"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            snap.error = "log in — run codex to refresh"
            snap.auth_error = True
        else:
            snap.error = f"HTTP {e.code} — retrying"
    except urllib.error.URLError:
        snap.error = "offline — can't reach ChatGPT"
    except Exception as e:  # noqa: BLE001
        snap.error = str(e)
    snap.fetched_at = datetime.now(timezone.utc)
    return snap


# ---------------------------------------------------------------- provider detection
# Is each CLI installed / logged in? Drives Settings status + graceful
# degradation. Returns one of: "ready", "logged out", "not installed".


def detect_claude() -> str:
    base = HOME / ".claude"
    creds = base / ".credentials.json"
    try:
        if creds.exists():
            oauth = json.loads(creds.read_text(encoding="utf-8")).get("claudeAiOauth")
            if oauth and oauth.get("accessToken"):
                return "ready"
        return "logged out" if base.exists() else "not installed"
    except Exception:  # noqa: BLE001 — a malformed file just means "log in again"
        return "logged out"


def detect_codex() -> str:
    base = HOME / ".codex"
    auth = base / "auth.json"
    try:
        if auth.exists():
            tokens = (json.loads(auth.read_text(encoding="utf-8")).get("tokens") or {})
            if tokens.get("access_token"):
                return "ready"
        return "logged out" if base.exists() else "not installed"
    except Exception:  # noqa: BLE001
        return "logged out"


# Provider registry — add a provider = add one entry (fetcher + detector).
# The whole UI renders whatever is enabled and detected.
PROVIDERS = [
    {"key": "claude", "name": "Claude", "fetch": fetch_claude, "detect": detect_claude},
    {"key": "codex", "name": "OpenAI", "fetch": fetch_codex, "detect": detect_codex},
]
PROVIDER_BY_KEY = {p["key"]: p for p in PROVIDERS}


# ---------------------------------------------------------------- config store


def _config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(HOME)
    return Path(base) / APP_ID


def _config_path() -> Path:
    return _config_dir() / "config.json"


def default_config() -> dict:
    return {
        "enabled": {p["key"]: True for p in PROVIDERS},
        "start_on_login": False,
    }


def load_config() -> dict:
    cfg = default_config()
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt config → sensible defaults
        return cfg
    if isinstance(raw.get("enabled"), dict):
        for key in cfg["enabled"]:
            if key in raw["enabled"]:
                cfg["enabled"][key] = bool(raw["enabled"][key])
    if isinstance(raw.get("start_on_login"), bool):
        cfg["start_on_login"] = raw["start_on_login"]
    return cfg


def save_config(cfg: dict) -> None:
    try:
        d = _config_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 — never crash the widget over a config write
        pass


# ---------------------------------------------------------------- helpers


def _fmt_reset(dt: datetime | None) -> str:
    if dt is None:
        return ""
    secs = int((dt - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "resetting"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def zone_color(pct: float) -> str:
    if pct >= RED_AT:
        return RED
    if pct >= AMBER_AT:
        return AMBER
    return GREEN


def _gauge_angle(pct: float) -> float:
    """Map 0-100% to the sweep angle (math degrees, CCW from +x).

    270-degree sweep: 0% at lower-left (225 deg), over the top, 100% at
    lower-right (-45 deg)."""
    return 225.0 - min(max(pct, 0.0), 100.0) / 100.0 * 270.0


# ---------------------------------------------------------------- gauge rendering
# One Pillow renderer draws every dial — the tray icon and each panel gauge — so
# they share an identical instrument look. Everything is drawn supersampled and
# downsampled with LANCZOS, which is what gives the anti-aliased, "real dial"
# finish that flat tkinter Canvas arcs can't.

TRACK = "#3a3d44"      # dim full-sweep track behind the value arc
_FONT_CACHE: dict = {}
_BAHNSCHRIFT = "C:/Windows/Fonts/bahnschrift.ttf"  # DIN-style face Windows ships


def _hex(color: str) -> tuple:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def _pil_font(size: int, weight: str = "SemiBold"):
    """Bahnschrift at a chosen variable-font weight, cached; falls back to
    Pillow's default face if Bahnschrift isn't present (non-Windows / stripped
    box)."""
    key = (size, weight)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    from PIL import ImageFont
    font = None
    try:
        font = ImageFont.truetype(_BAHNSCHRIFT, size)
        try:  # Bahnschrift is a variable font — pick the weight if we can
            font.set_variation_by_name(weight)
        except Exception:  # noqa: BLE001 — static build or unsupported axis
            pass
    except Exception:  # noqa: BLE001 — Bahnschrift missing (non-Windows / stripped)
        for alt in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
            try:
                font = ImageFont.truetype(alt, size)
                break
            except Exception:  # noqa: BLE001
                continue
    if font is None:
        # Last resort. load_default(size) keeps the readout legible on Pillow
        # >= 10.1; the sizeless default (a tiny bitmap face) is the final floor.
        try:
            font = ImageFont.load_default(size)
        except Exception:  # noqa: BLE001
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _pil_angle(pct: float) -> float:
    """Our math angle (CCW from +x) → PIL's clockwise-from-3-o'clock degrees."""
    return (-_gauge_angle(pct)) % 360


def render_gauge(pct: float, r: int, stale: bool = False, scale: int = 3):
    """A single analog dial as an RGBA image ~2r wide: dim track, zone-colored
    value arc, thin outer zone ring, ticks, a tapered needle, and a large
    Bahnschrift %/USED readout baked in. `stale` dims the whole dial."""
    from PIL import Image, ImageDraw

    pct = min(max(float(pct), 0.0), 100.0)
    col = zone_color(pct)
    pad = 5
    box = (r + pad) * 2
    S = box * scale
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = S / 2
    R = r * scale

    def arc(radius, p0, p1, color, width):
        a0, a1 = _pil_angle(p0), _pil_angle(p1)
        if a1 < a0:
            a1 += 360
        d.arc([c - radius, c - radius, c + radius, c + radius],
              a0, a1, fill=color, width=max(1, int(width)))

    # face disc
    d.ellipse([c - R, c - R, c + R, c + R], fill=FACE, outline=RIM, width=scale)
    # dim full track, then the bright value arc 0→pct on top of it
    track_r = R - 6 * scale
    arc(track_r, 0, 100, TRACK, 7 * scale)
    if pct > 0:
        arc(track_r, 0, pct, col, 7 * scale)
    # thin outer zone ring for context (green / amber / red bands)
    ring_r = R - 1 * scale
    for a, b, zc in ((0, AMBER_AT, GREEN), (AMBER_AT, RED_AT, AMBER), (RED_AT, 100, RED)):
        arc(ring_r, a, b, zc, 2 * scale)
    # ticks: minor every 10%, major at 0/25/50/75/100
    for p in range(0, 101, 5):
        major = p % 25 == 0
        if not major and p % 10:
            continue
        th = math.radians(_gauge_angle(p))
        outer = track_r + 3 * scale
        inner = track_r - (7 * scale if major else 4 * scale)
        d.line([c + inner * math.cos(th), c - inner * math.sin(th),
                c + outer * math.cos(th), c - outer * math.sin(th)],
               fill=TICK, width=(2 if major else 1) * scale)
    # tapered needle (polygon: wide at hub → point at tip) + short counterweight
    th = math.radians(_gauge_angle(pct))
    perp = th + math.pi / 2
    ncol = RED if pct >= RED_AT else NEEDLE
    tip = (c + (track_r - 2 * scale) * math.cos(th), c - (track_r - 2 * scale) * math.sin(th))
    half = 3.5 * scale
    bl = (c + half * math.cos(perp), c - half * math.sin(perp))
    br = (c - half * math.cos(perp), c + half * math.sin(perp))
    tail = (c - 11 * scale * math.cos(th), c + 11 * scale * math.sin(th))
    d.polygon([tip, bl, tail, br], fill=ncol)
    # hub
    hub = 6 * scale
    d.ellipse([c - hub, c - hub, c + hub, c + hub], fill=NEEDLE, outline=RIM, width=scale)
    # large % + USED readout, lower half of the dial
    pf = _pil_font(int(21 * scale), "SemiBold")
    txt = f"{pct:.0f}%"
    tb = d.textbbox((0, 0), txt, font=pf)
    ty = c + R * 0.34
    d.text((c - (tb[2] - tb[0]) / 2, ty), txt, font=pf, fill=col)
    uf = _pil_font(int(7 * scale), "Regular")
    ub = d.textbbox((0, 0), "USED", font=uf)
    d.text((c - (ub[2] - ub[0]) / 2, ty + (tb[3] - tb[1]) + 6 * scale), "USED",
           font=uf, fill=_hex(FAINT))

    out = img.resize((box, box), Image.LANCZOS)
    if stale:  # showing last-known values → dim the whole dial
        alpha = out.getchannel("A").point(lambda a: int(a * 0.42))
        out.putalpha(alpha)
    return out


def snapshot_text(snap: Snapshot) -> str:
    lines = [snap.provider + (f"  ({snap.plan})" if snap.plan else "")]
    if not snap.ok:
        return "\n".join(lines + [f"  unavailable: {snap.error}"])
    for w in snap.windows:
        r = _fmt_reset(w.resets_at)
        lines.append(f"  {w.label:<10} {w.percent:5.1f}% used   resets in {r}" if r
                     else f"  {w.label:<10} {w.percent:5.1f}% used")
    if snap.credits:
        lines.append(f"  {snap.credits}")
    return "\n".join(lines)


# ---------------------------------------------------------------- startup install


def _startup_dir() -> Path:
    base = os.environ.get("APPDATA") or str(HOME)
    return (Path(base) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs" / "Startup")


def _startup_cmd_path() -> Path:
    return _startup_dir() / f"{APP_ID}.cmd"


def install_startup() -> None:
    cmd = _startup_cmd_path()
    if getattr(sys, "frozen", False):  # packaged .exe
        target = f'start "" "{Path(sys.executable).resolve()}"'
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        script = Path(__file__).resolve()
        target = f'start "" "{pythonw}" "{script}"'
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text(f"@echo off\n{target}\n", encoding="utf-8")


def uninstall_startup() -> None:
    try:
        _startup_cmd_path().unlink()
    except FileNotFoundError:
        pass


def startup_installed() -> bool:
    return _startup_cmd_path().exists()


# ---------------------------------------------------------------- single instance


def resource_path(rel: str) -> Path:
    """Path to a bundled resource, whether running from source or a PyInstaller
    --onefile build (which unpacks to sys._MEIPASS)."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent
    return Path(base) / rel


def _apply_window_icon(win) -> None:
    try:
        ico = resource_path("assets/app.ico")
        if ico.exists():
            win.iconbitmap(default=str(ico))
    except Exception:  # noqa: BLE001 — cosmetic only
        pass


def acquire_single_instance() -> bool:
    """True if we're the only instance. Windows named mutex; best-effort."""
    try:
        import ctypes
        # use_last_error so get_last_error() reliably reflects CreateMutexW.
        # The handle is intentionally left open for the process lifetime so the
        # named mutex stays alive; the OS releases it when the process exits.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW(None, True, f"Global\\{APP_ID}_singleton")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 — non-Windows / no ctypes: don't block launch
        return True


def set_dpi_awareness() -> None:
    """Tell Windows we render at native resolution, so it doesn't bitmap-stretch
    (and re-blur) the panel on a scaled display, and so screen-size queries
    return real pixels. Best-effort; harmless where unsupported."""
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
        except Exception:  # noqa: BLE001 — pre-8.1 fallback
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 — non-Windows / no ctypes
        pass


# ---------------------------------------------------------------- tray app


class WidgetApp:
    def __init__(self):
        self.config = load_config()
        self.snapshots: dict[str, Snapshot] = {}
        self.lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self.icon = None
        self.root = None
        self.panel = None
        self.settings = None
        self.panel_available = True
        self._stop = threading.Event()
        # Provider detection (a credential-file read per provider) is cached for
        # the poll cycle in refresh(), so active_keys()/_tooltip() don't touch
        # disk on every call. Settings reads detection live for fresh status.
        self._detect_cache: dict[str, str] = {}

    # ---- provider selection

    def _refresh_detection(self) -> None:
        """Detect every provider once and cache it for this poll cycle."""
        self._detect_cache = {p["key"]: p["detect"]() for p in PROVIDERS}

    def active_keys(self) -> list[str]:
        """Providers that take a panel column: enabled AND logged in ("ready").
        A not-installed or logged-out provider never gets a column — its status
        lives in Settings instead (spec: no error tile in the panel). A ready
        provider whose token has since expired still gets a column and shows
        "log in to refresh" from the fetch, which the spec does want surfaced.

        Reads the per-cycle detection cache; falls back to a live pass only if
        nothing has populated it yet (before the first refresh)."""
        detect = self._detect_cache or {p["key"]: p["detect"]() for p in PROVIDERS}
        return [p["key"] for p in PROVIDERS
                if self.config["enabled"].get(p["key"], True) and detect.get(p["key"]) == "ready"]

    # ---- data

    def refresh(self):
        # Menu clicks, settings changes, and the poller share one in-flight fetch.
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            self._refresh_detection()  # one detection pass per cycle; active_keys() reads the cache
            active = self.active_keys()
            for key in active:
                new = PROVIDER_BY_KEY[key]["fetch"]()
                with self.lock:
                    prev = self.snapshots.get(key)
                    if not new.ok and prev is not None and prev.ok:
                        # Keep the last-known good reading rather than blanking the
                        # gauges after a transient failure.
                        prev.stale = True
                        prev.auth_error = new.auth_error
                        if new.auth_error:
                            prev.error = new.error
                    else:
                        self.snapshots[key] = new
            # Drop snapshots for providers no longer active (disabled / logged out).
            with self.lock:
                for key in list(self.snapshots):
                    if key not in active:
                        self.snapshots.pop(key, None)
            if self.icon is not None:
                try:
                    self.icon.icon = self._draw_icon()
                    self.icon.title = self._tooltip()
                except Exception:  # noqa: BLE001 — a pystray/Win32 hiccup
                    pass
        finally:
            self._refresh_lock.release()

    def _poll_loop(self):
        while not self._stop.wait(POLL_SECONDS):
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 — one bad poll must never end the loop
                pass

    def _worst_util(self) -> float:
        """Worst-case utilization across every enabled provider's windows —
        drives the tray icon color."""
        worst = 0.0
        with self.lock:
            for snap in self.snapshots.values():
                if snap and snap.ok:
                    for w in snap.windows:
                        worst = max(worst, w.percent)
        return worst

    def _tooltip(self) -> str:
        parts = []
        keys = self.active_keys()  # disk-backed detection — keep it out of the lock
        with self.lock:
            for key in keys:
                snap = self.snapshots.get(key)
                name = PROVIDER_BY_KEY[key]["name"].split()[0]
                if snap and snap.ok:
                    s = snap.window("session")
                    w = snap.window("weekly")
                    bits = []
                    if s is not None:
                        bits.append(f"{s.percent:.0f}% used")
                    if w is not None:
                        bits.append(f"{w.percent:.0f}% used wk")
                    parts.append(f"{name} " + "/".join(bits) if bits else f"{name} —")
                else:
                    parts.append(f"{name} —")
        body = " · ".join(parts) if parts else "no providers"
        return (f"{APP_NAME} · " + body)[:127]

    # ---- tray icon: filled disc + zone-colored fill arc, legible on any taskbar

    def _draw_icon(self):
        from PIL import Image, ImageDraw

        # Drawn 4× and downsampled with LANCZOS for a crisp tray glyph. Keeps
        # the proven contrast treatment: a filled dark disc reads on LIGHT
        # taskbars, a bright rim reads on DARK ones.
        sc = 4
        S = 64
        SS = S * sc
        img = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, cy, r = SS // 2, SS // 2, 27 * sc
        pct = self._worst_util()

        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DISC, outline=RIMLIGHT, width=3 * sc)

        def arc(radius, p0, p1, color, width):
            a0, a1 = _pil_angle(p0), _pil_angle(p1)
            if a1 < a0:
                a1 += 360
            d.arc([cx - radius, cy - radius, cx + radius, cy + radius],
                  a0, a1, fill=color, width=width)

        # fixed green/amber/red zone track at full thickness -- always all
        # three colors, doesn't fill/change with pct. The needle alone shows
        # the current reading, like a real gauge's colored zones + pointer.
        track_r = r - 8 * sc
        for a, b, zc in ((0, AMBER_AT, GREEN), (AMBER_AT, RED_AT, AMBER), (RED_AT, 100, RED)):
            arc(track_r, a, b, zc, 7 * sc)

        # needle + hub, always white for contrast against any zone color
        th = math.radians(_gauge_angle(pct))
        d.line([cx, cy, cx + (r - 11 * sc) * math.cos(th), cy - (r - 11 * sc) * math.sin(th)],
               fill=NEEDLE, width=4 * sc)
        d.ellipse([cx - 5 * sc, cy - 5 * sc, cx + 5 * sc, cy + 5 * sc], fill=NEEDLE)
        return img.resize((S, S), Image.LANCZOS)

    # ---- panel: one vertical "band" per provider, each a self-contained dial
    # cluster. Bands (not a shared Session/Weekly grid) are what let providers
    # differ — Claude shows two dials, Codex shows one centered Weekly dial —
    # without any cell ever looking empty or misaligned.

    # sub-label under each dial, by window kind
    _WIN_SUB = {"session": "~5h", "weekly": "7d"}

    PANEL_W = 348  # snug: sized so the "PLAN USAGE … updated HH:MM" row is the widest element

    def _fonts(self):
        """Pick Bahnschrift (the instrument-cluster face Windows ships) with a
        Segoe UI fallback, and cache the tk Font objects for this session."""
        if getattr(self, "_font_cache", None):
            return self._font_cache
        import tkinter.font as tkfont
        fams = set(tkfont.families(self.root))
        semi = "Bahnschrift SemiBold" if "Bahnschrift SemiBold" in fams else "Segoe UI Semibold"
        body = "Bahnschrift" if "Bahnschrift" in fams else "Segoe UI"
        f = {
            "title": tkfont.Font(family=semi, size=11),
            "name": tkfont.Font(family=semi, size=12),
            "chip": tkfont.Font(family=semi, size=8),
            "label": tkfont.Font(family=semi, size=9),
            "sub": tkfont.Font(family=body, size=8),
            "note": tkfont.Font(family=body, size=8),
            "msg": tkfont.Font(family=body, size=10),
            "updated": tkfont.Font(family=body, size=10),
            "reset": tkfont.Font(family=body, size=9),
        }
        # brand wordmark — Agency FB (the condensed motorsport face Windows
        # ships with Office) in bold italic for a speedometer feel. Falls back
        # to Bahnschrift's condensed cut, then the panel's semibold face, if
        # Agency FB isn't installed on an end-user machine.
        if "Agency FB" in fams:
            brand_family, brand_weight, brand_slant = "Agency FB", "bold", "italic"
        elif "Bahnschrift SemiBold Condensed" in fams:
            brand_family, brand_weight, brand_slant = "Bahnschrift SemiBold Condensed", "normal", "italic"
        else:
            brand_family, brand_weight, brand_slant = semi, "normal", "roman"
        # Sized to a comfortable fraction of the panel — deliberately NOT
        # spanning the full width — then nudged down only if it would overrun.
        brand = tkfont.Font(family=brand_family, weight=brand_weight,
                            slant=brand_slant, size=30)
        target = self.PANEL_W - 96
        size = max(16, min(30, int(30 * target / max(1, brand.measure(APP_NAME)))))
        brand.configure(size=size)
        while size > 16 and brand.measure(APP_NAME) > target:
            size -= 1
            brand.configure(size=size)
        f["brand"] = brand
        f["tag"] = tkfont.Font(family=semi, size=11)
        self._font_cache = f
        return f

    def _toggle_panel(self):
        if self.root:
            self.root.after(0, self._toggle_panel_main)

    def _toggle_panel_main(self):
        import tkinter as tk
        from PIL import ImageTk

        if self.panel is not None and self.panel.winfo_exists():
            self.panel.destroy()
            self.panel = None
            return

        keys = self.active_keys()
        with self.lock:
            snaps = [(k, self.snapshots.get(k)) for k in keys]
        f = self._fonts()

        # geometry
        PANEL_W = self.PANEL_W
        R = 52
        DIAL_BOX = (R + 5) * 2            # 114
        # masthead: wordmark + tagline, sized from real font metrics
        MAST_PAD = 14
        brand_h = f["brand"].metrics("linespace")
        tag_h = f["tag"].metrics("linespace")
        MAST_H = MAST_PAD + brand_h + 4 + tag_h + MAST_PAD
        APP_HEADER_H = 66                  # taller → more air around the header divider
        FOOTER_H = 14
        BAND_HEADER_H = 30
        DIAL_LABEL_H = 46
        BAND_PAD = 16                      # more breathing room above/below band dividers
        GAP = 56                          # between the two Claude dials
        RESET_GAP = 24                    # label row -> reset row

        def band_height(snap):
            # credits render in the band header (right side), not the body, so
            # every band is the same height regardless of a credits line.
            return BAND_PAD * 2 + BAND_HEADER_H + DIAL_BOX + DIAL_LABEL_H

        if snaps:
            H = MAST_H + APP_HEADER_H + sum(band_height(s) for _, s in snaps) + FOOTER_H
        else:
            H = MAST_H + APP_HEADER_H + 120 + FOOTER_H
        W = PANEL_W

        p = tk.Toplevel(self.root)
        self.panel = p
        self._panel_imgs = []             # hold PhotoImage refs or tk GCs them
        p.title(APP_NAME)
        p.overrideredirect(True)          # frameless — reads like an instrument panel
        p.attributes("-topmost", True)
        cv = tk.Canvas(p, width=W, height=H, bg=BG, highlightthickness=1,
                       highlightbackground=RIM)
        cv.pack()

        # top edge strip echoes the tray icon: worst-case zone color
        worst = self._worst_util()
        cv.create_rectangle(0, 0, W, 3, fill=zone_color(worst), outline="")

        # masthead — UsageMaxxer wordmark spanning the width, tagline under it
        # with REDLINE picked out in red, then a separator
        cv.create_text(W / 2, MAST_PAD + brand_h / 2, text=APP_NAME,
                       fill=INK, font=f["brand"])
        tag_cy = MAST_PAD + brand_h + 4 + tag_h / 2
        lead_w = f["tag"].measure(TAGLINE_LEAD)
        tag_x = (W - lead_w - f["tag"].measure(TAGLINE_HOT)) / 2
        cv.create_text(tag_x, tag_cy, text=TAGLINE_LEAD, anchor="w",
                       fill=MUTED, font=f["tag"])
        cv.create_text(tag_x + lead_w, tag_cy, text=TAGLINE_HOT, anchor="w",
                       fill=RED, font=f["tag"])
        cv.create_line(0, MAST_H, W, MAST_H, fill=GREEN)

        # app header — title with "updated HH:MM" right after it, centered as one group
        with self.lock:
            ts = max((s.fetched_at for s in self.snapshots.values()
                      if s and s.fetched_at), default=None)
        title_txt = "PLAN USAGE"
        title_w = f["title"].measure(title_txt)
        header_cy = MAST_H + APP_HEADER_H / 2
        if ts:
            updated_txt = f"updated {ts.astimezone().strftime('%H:%M')}"
            updated_w = f["updated"].measure(updated_txt)
            title_gap = 24
            start_x = (W - (title_w + title_gap + updated_w)) / 2
            cv.create_text(start_x, header_cy, text=title_txt, anchor="w",
                           fill=MUTED, font=f["title"])
            cv.create_text(start_x + title_w + title_gap, header_cy, text=updated_txt,
                           anchor="w", fill=GREEN, font=f["updated"])
        else:
            cv.create_text(W / 2, header_cy, text=title_txt, fill=MUTED, font=f["title"])
        cv.create_line(0, MAST_H + APP_HEADER_H, W, MAST_H + APP_HEADER_H, fill=GREEN)

        if not snaps:
            cv.create_text(W / 2, MAST_H + APP_HEADER_H + 60,
                           text="No connected providers.\n\n"
                           "Log into Claude Code or Codex, then open\n"
                           "the tray icon → Settings.",
                           fill=MUTED, justify="center", font=f["msg"])

        y = MAST_H + APP_HEADER_H
        for bi, (key, snap) in enumerate(snaps):
            bh = band_height(snap)
            if bi > 0:
                cv.create_line(16, y, W - 16, y, fill=GREEN)

            # band header: provider name + plan chip (left), stale/credits (right)
            hy = y + BAND_PAD + 12
            name = PROVIDER_BY_KEY[key]["name"]
            cv.create_text(20, hy, text=name, anchor="w", fill=INK, font=f["name"])
            nx = 20 + f["name"].measure(name)
            if snap and snap.plan:
                plan = snap.plan.upper()
                pw = f["chip"].measure(plan)
                cx0 = nx + 12
                cv.create_rectangle(cx0, hy - 9, cx0 + pw + 14, hy + 9,
                                    fill=FACE, outline=RIM)
                cv.create_text(cx0 + 7 + pw / 2, hy, text=plan, fill=LABEL, font=f["chip"])
            if snap and snap.ok and snap.stale:
                # concise header note (the full fetch message is longer than the
                # header can hold without colliding with the plan chip)
                note = "⚠ log in to refresh" if snap.auth_error else "last known · reconnecting"
                cv.create_text(W - 20, hy, anchor="e", text=note,
                               fill=AMBER, font=f["note"])

            dial_top = y + BAND_PAD + BAND_HEADER_H
            dial_cy = dial_top + DIAL_BOX / 2
            label_y = dial_top + DIAL_BOX + 16

            if snap and snap.ok:
                wins = snap.windows
                total = len(wins) * DIAL_BOX + (len(wins) - 1) * GAP
                startx = (W - total) / 2
                for i, w in enumerate(wins):
                    dcx = startx + DIAL_BOX / 2 + i * (DIAL_BOX + GAP)
                    img = ImageTk.PhotoImage(render_gauge(w.percent, R, stale=snap.stale))
                    self._panel_imgs.append(img)
                    cv.create_image(dcx, dial_cy, image=img)
                    sub = self._WIN_SUB.get(w.kind, "")
                    cv.create_text(dcx, label_y, text=f"{w.label.upper()}  ·  {sub}",
                                   fill=LABEL, font=f["label"])
                    reset = _fmt_reset(w.resets_at)
                    if reset:
                        cv.create_text(dcx, label_y + RESET_GAP, text=f"↻ {reset}",
                                       fill=FAINT, font=f["reset"])
            else:
                msg = snap.error if snap else "loading…"
                cv.create_text(W / 2, dial_cy, text=msg, fill=AMBER,
                               width=W - 60, justify="center", font=f["msg"])

            y += bh

        p.update_idletasks()
        sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
        p.geometry(f"{W}x{H}+{sw - W - 24}+{sh - H - 60}")
        cv.bind("<Button-1>", lambda e: self._close_panel())
        p.focus_force()
        p.bind("<FocusOut>", lambda e: self._close_panel())
        # FocusOut isn't reliably delivered to a frameless (overrideredirect)
        # window, so give Escape as a guaranteed way to dismiss it too.
        p.bind("<Escape>", lambda e: self._close_panel())

    def _close_panel(self):
        if self.panel is not None:
            try:
                self.panel.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.panel = None

    # ---- settings window

    def _open_settings(self):
        if self.root:
            self.root.after(0, self._open_settings_main)

    def _open_settings_main(self):
        import tkinter as tk

        if self.settings is not None and self.settings.winfo_exists():
            self.settings.lift()
            self.settings.focus_force()
            return

        s = tk.Toplevel(self.root)
        self.settings = s
        s.title(f"{APP_NAME} — Settings")
        s.configure(bg=BG)
        s.attributes("-topmost", True)
        s.resizable(False, False)
        _apply_window_icon(s)

        tk.Label(s, text="PROVIDERS", bg=BG, fg=LABEL,
                 font=("Segoe UI Semibold", 10, "bold")).grid(
                     row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(16, 6))

        status_text = {"ready": "logged in", "logged out": "installed — logged out",
                       "not installed": "not installed"}
        status_col = {"ready": GREEN, "logged out": AMBER, "not installed": FAINT}
        self._enabled_vars = {}
        row = 1
        for pv in PROVIDERS:
            st = pv["detect"]()
            var = tk.BooleanVar(value=self.config["enabled"].get(pv["key"], True))
            self._enabled_vars[pv["key"]] = var
            cb = tk.Checkbutton(
                s, text=pv["name"], variable=var, bg=BG, fg=INK, selectcolor=TRACK,
                activebackground=BG, activeforeground=INK, font=("Segoe UI", 10),
                command=lambda k=pv["key"]: self._on_toggle_provider(k))
            cb.grid(row=row, column=0, sticky="w", padx=16, pady=3)
            tk.Label(s, text=status_text.get(st, st), bg=BG, fg=status_col.get(st, MUTED),
                     font=("Segoe UI", 9)).grid(row=row, column=1, sticky="w", padx=12)
            row += 1

        tk.Frame(s, bg=RIM, height=1).grid(row=row, column=0, columnspan=2,
                                           sticky="ew", padx=16, pady=(12, 8))
        row += 1

        self._startup_var = tk.BooleanVar(value=self.config.get("start_on_login", False))
        tk.Checkbutton(
            s, text="Start automatically on login", variable=self._startup_var, bg=BG,
            fg=INK, selectcolor=TRACK, activebackground=BG, activeforeground=INK,
            font=("Segoe UI", 10), command=self._on_toggle_startup).grid(
                row=row, column=0, columnspan=2, sticky="w", padx=16, pady=3)
        row += 1

        tk.Label(s, text="Reads your local CLI login and keeps Claude's token fresh in place.\n"
                 "Nothing leaves your PC except requests to each provider's own API.",
                 bg=BG, fg=FAINT, justify="left", font=("Segoe UI", 8)).grid(
                     row=row, column=0, columnspan=2, sticky="w", padx=16, pady=(10, 4))
        row += 1
        tk.Label(s, text=f"{APP_NAME} v{VERSION} · by RigLord", bg=BG, fg=FAINT,
                 font=("Segoe UI", 8)).grid(row=row, column=0, columnspan=2,
                                            sticky="w", padx=16, pady=(0, 14))

        s.update_idletasks()
        sw, sh = s.winfo_screenwidth(), s.winfo_screenheight()
        ww, wh = s.winfo_width(), s.winfo_height()
        s.geometry(f"+{(sw - ww) // 2}+{(sh - wh) // 2}")
        s.protocol("WM_DELETE_WINDOW", self._close_settings)

    def _close_settings(self):
        if self.settings is not None:
            try:
                self.settings.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.settings = None

    def _on_toggle_provider(self, key: str):
        self.config["enabled"][key] = bool(self._enabled_vars[key].get())
        save_config(self.config)
        self._close_panel()
        threading.Thread(target=self.refresh, daemon=True).start()

    def _on_toggle_startup(self):
        on = bool(self._startup_var.get())
        self.config["start_on_login"] = on
        save_config(self.config)
        try:
            if on:
                install_startup()
            else:
                uninstall_startup()
        except OSError:  # never let a startup-file write error escape the Tk callback
            pass

    # ---- lifecycle

    def _refresh_clicked(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def _quit(self):
        self._stop.set()
        if self.icon:
            self.icon.stop()
        if self.root:
            self.root.after(0, self.root.destroy)

    def run(self):
        import pystray

        set_dpi_awareness()  # crisp panel on scaled displays; real pixel queries

        # reconcile the start-on-login setting with what's actually installed
        if self.config.get("start_on_login") and not startup_installed():
            try:
                install_startup()
            except OSError:
                pass

        self.refresh()
        try:
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
            _apply_window_icon(self.root)
        except Exception:  # noqa: BLE001 - UI initialization is optional.
            # pystray's Windows backend can keep the status icon alive even
            # when the current Python distribution lacks Tcl/Tk.
            self.root = None
            self.panel_available = False

        menu = pystray.Menu(
            pystray.MenuItem("Show gauges", lambda: self._toggle_panel(), default=True,
                             visible=lambda item: self.panel_available),
            pystray.MenuItem("Refresh now", lambda: self._refresh_clicked()),
            pystray.MenuItem("Settings…", lambda: self._open_settings()),
            pystray.MenuItem("Quit", lambda: self._quit()),
        )
        self.icon = pystray.Icon(APP_ID, self._draw_icon(), self._tooltip(), menu)
        threading.Thread(target=self._poll_loop, daemon=True).start()
        if self.root:
            self.icon.run_detached()
            self.root.mainloop()
        else:
            # On Windows, pystray's main-thread event loop is more reliable
            # than a detached icon loop when there is no Tk main loop.
            self.icon.run()


# ---------------------------------------------------------------- entrypoint


def main():
    if "--install-startup" in sys.argv:
        install_startup()
        print(f"Installed: {_startup_cmd_path()}")
        return
    if "--uninstall-startup" in sys.argv:
        uninstall_startup()
        print("Removed start-on-login.")
        return
    if "--once" in sys.argv:
        for pv in PROVIDERS:
            print(f"[{pv['detect']()}]")
            print(snapshot_text(pv["fetch"]()))
            print()
        return
    if not acquire_single_instance():
        return  # already running
    WidgetApp().run()


if __name__ == "__main__":
    main()
