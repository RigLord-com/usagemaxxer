"""Dual-provider usage tray widget: Claude Code + Codex.

Shows session (5h) and weekly window utilization for both providers in the
Windows system tray, with a click-to-open panel. Reads the credentials each
CLI already stores locally; the CLIs themselves keep those files refreshed.

Data sources (both undocumented — code defensively):
  Claude Code:  GET https://api.anthropic.com/api/oauth/usage
                Bearer token from ~/.claude/.credentials.json
  Codex:        GET https://chatgpt.com/backend-api/wham/usage
                Bearer token + account id from ~/.codex/auth.json

Usage:
  python usage_widget.py --once         # print both snapshots and exit
  pythonw usage_widget.py               # run the tray widget
  python usage_widget.py --install-startup   # auto-start on login
"""

import json
import os
import sys
import threading
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 300  # 5 minutes
HOME = Path.home()

# ---------------------------------------------------------------- normalized shape


@dataclass
class Window:
    label: str          # "Session" | "Weekly" | scoped e.g. "Weekly (Opus)"
    percent: float      # 0-100 used
    resets_at: datetime | None = None


@dataclass
class Snapshot:
    provider: str                     # "Claude Code" | "Codex"
    ok: bool = False
    error: str = ""
    plan: str = ""
    windows: list[Window] = field(default_factory=list)
    credits: str = ""                 # human line, e.g. "$0.00 / $20.00 extra usage"
    fetched_at: datetime | None = None

    def window(self, label_prefix: str) -> Window | None:
        for w in self.windows:
            if w.label.startswith(label_prefix):
                return w
        return None


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, (int, float)):  # Codex sends unix epoch seconds
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- fetchers


def fetch_claude() -> Snapshot:
    snap = Snapshot(provider="Claude Code")
    try:
        creds_path = HOME / ".claude" / ".credentials.json"
        oauth = json.loads(creds_path.read_text(encoding="utf-8"))["claudeAiOauth"]
        snap.plan = str(oauth.get("subscriptionType", "")).title()
        payload = _get_json(
            "https://api.anthropic.com/api/oauth/usage",
            {
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "Accept": "application/json",
                "User-Agent": "usage-widget",
            },
        )
        # Prefer five_hour/seven_day objects; fall back to limits[] if absent.
        mapping = (
            ("five_hour", "Session"),
            ("seven_day", "Weekly"),
            ("seven_day_opus", "Weekly (Opus)"),
            ("seven_day_sonnet", "Weekly (Sonnet)"),
        )
        for key, label in mapping:
            win = payload.get(key) or {}
            util = win.get("utilization")
            if util is None:
                continue
            pct = float(util) * 100 if float(util) <= 1 else float(util)
            snap.windows.append(Window(label, pct, _parse_dt(win.get("resets_at"))))
        if not snap.windows:
            for lim in payload.get("limits") or []:
                if lim.get("percent") is None:
                    continue
                label = {"session": "Session", "weekly_all": "Weekly"}.get(
                    lim.get("kind"), str(lim.get("kind"))
                )
                snap.windows.append(
                    Window(label, float(lim["percent"]), _parse_dt(lim.get("resets_at")))
                )
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
        snap.error = "not logged in (no ~/.claude/.credentials.json)"
    except urllib.error.HTTPError as e:
        snap.error = f"HTTP {e.code} — token may be expired; open Claude Code to refresh"
    except Exception as e:  # noqa: BLE001 — surface anything in the UI
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
        plan = str(payload.get("plan_type") or "").replace("_", " ").title()
        snap.plan = plan
        rate = payload.get("rate_limit") or {}
        for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
            win = rate.get(key) or {}
            used = win.get("used_percent")
            if used is None:
                continue
            snap.windows.append(Window(label, float(used), _parse_dt(win.get("reset_at"))))
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
        snap.error = "not logged in (no ~/.codex/auth.json)"
    except urllib.error.HTTPError as e:
        snap.error = f"HTTP {e.code} — token may be expired; run codex once to refresh"
    except Exception as e:  # noqa: BLE001
        snap.error = str(e)
    snap.fetched_at = datetime.now(timezone.utc)
    return snap


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}


# ---------------------------------------------------------------- formatting


def _fmt_reset(dt: datetime | None) -> str:
    if dt is None:
        return ""
    delta = dt - datetime.now(timezone.utc)
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "resetting…"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {mins}m"
    return f"resets in {mins}m"


def severity_color(pct: float) -> str:
    if pct >= 90:
        return "#e5484d"  # red
    if pct >= 70:
        return "#f5a524"  # amber
    return "#46a758"      # green


def snapshot_text(snap: Snapshot) -> str:
    lines = [f"{snap.provider}" + (f"  ({snap.plan})" if snap.plan else "")]
    if not snap.ok:
        lines.append(f"  unavailable: {snap.error}")
        return "\n".join(lines)
    for w in snap.windows:
        reset = _fmt_reset(w.resets_at)
        lines.append(f"  {w.label:<16} {w.percent:5.1f}%   {reset}")
    if snap.credits:
        lines.append(f"  {snap.credits}")
    return "\n".join(lines)


# ---------------------------------------------------------------- tray app


class WidgetApp:
    def __init__(self):
        self.snapshots: dict[str, Snapshot] = {}
        self.lock = threading.Lock()
        self.icon = None
        self.root = None          # tk root (hidden)
        self.panel = None         # tk Toplevel
        self._stop = threading.Event()

    # ---- data

    def refresh(self):
        for key, fn in FETCHERS.items():
            snap = fn()
            with self.lock:
                self.snapshots[key] = snap
        if self.icon is not None:
            self.icon.icon = self._draw_icon()
            self.icon.title = self._tooltip()

    def _poll_loop(self):
        while not self._stop.wait(POLL_SECONDS):
            self.refresh()

    def _tooltip(self) -> str:
        parts = []
        with self.lock:
            for key, name in (("claude", "CC"), ("codex", "Codex")):
                snap = self.snapshots.get(key)
                if snap and snap.ok:
                    s = snap.window("Session")
                    w = snap.window("Weekly")
                    parts.append(
                        f"{name} {s.percent:.0f}%"
                        + (f"/{w.percent:.0f}%w" if w else "")
                    )
                else:
                    parts.append(f"{name} —")
        return " · ".join(parts)[:127]  # Windows tooltip limit

    # ---- tray icon rendering

    def _draw_icon(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        with self.lock:
            pairs = [self.snapshots.get("claude"), self.snapshots.get("codex")]
        for i, snap in enumerate(pairs):
            x0 = 6 + i * 32
            x1 = x0 + 20
            d.rectangle([x0, 4, x1, 60], fill=(255, 255, 255, 40))
            if snap and snap.ok:
                sess = snap.window("Session")
                pct = min(max(sess.percent, 0), 100) if sess else 0
                top = 60 - int(56 * pct / 100)
                d.rectangle([x0, top, x1, 60], fill=severity_color(pct))
            else:
                d.rectangle([x0, 28, x1, 36], fill="#8b8d98")  # gray dash
        return img

    # ---- panel (tkinter)

    def _toggle_panel(self):
        self.root.after(0, self._toggle_panel_main)

    def _toggle_panel_main(self):
        import tkinter as tk

        if self.panel is not None and self.panel.winfo_exists():
            self.panel.destroy()
            self.panel = None
            return
        p = tk.Toplevel(self.root)
        self.panel = p
        p.title("Usage")
        p.attributes("-topmost", True)
        p.resizable(False, False)
        p.configure(bg="#1c1c1f", padx=14, pady=10)

        def row(parent, label, pct, reset):
            f = tk.Frame(parent, bg="#1c1c1f")
            f.pack(fill="x", pady=2)
            tk.Label(f, text=label, width=14, anchor="w", bg="#1c1c1f",
                     fg="#b0b4ba", font=("Segoe UI", 9)).pack(side="left")
            bar = tk.Frame(f, bg="#2e3035", width=120, height=10)
            bar.pack(side="left", padx=4)
            bar.pack_propagate(False)
            fill = tk.Frame(bar, bg=severity_color(pct), height=10,
                            width=max(2, int(120 * min(pct, 100) / 100)))
            fill.pack(side="left")
            tk.Label(f, text=f"{pct:.0f}%", width=4, anchor="e", bg="#1c1c1f",
                     fg="#eceef0", font=("Segoe UI", 9, "bold")).pack(side="left")
            tk.Label(f, text=reset, anchor="w", bg="#1c1c1f",
                     fg="#6c7078", font=("Segoe UI", 8)).pack(side="left", padx=6)

        with self.lock:
            snaps = [self.snapshots.get("claude"), self.snapshots.get("codex")]
        for snap in snaps:
            if snap is None:
                continue
            hdr = tk.Frame(p, bg="#1c1c1f")
            hdr.pack(fill="x", pady=(8, 2))
            tk.Label(hdr, text=snap.provider, bg="#1c1c1f", fg="#eceef0",
                     font=("Segoe UI", 10, "bold")).pack(side="left")
            if snap.plan:
                tk.Label(hdr, text=snap.plan, bg="#1c1c1f", fg="#6c7078",
                         font=("Segoe UI", 8)).pack(side="left", padx=6)
            if snap.ok:
                for w in snap.windows:
                    row(p, w.label, w.percent, _fmt_reset(w.resets_at))
                if snap.credits:
                    tk.Label(p, text=snap.credits, bg="#1c1c1f", fg="#6c7078",
                             anchor="w", font=("Segoe UI", 8)).pack(fill="x")
            else:
                tk.Label(p, text=snap.error, bg="#1c1c1f", fg="#f5a524",
                         anchor="w", wraplength=280, justify="left",
                         font=("Segoe UI", 8)).pack(fill="x")
        ts = max(
            (s.fetched_at for s in snaps if s and s.fetched_at),
            default=None,
        )
        if ts:
            tk.Label(p, text=f"updated {ts.astimezone().strftime('%H:%M')}",
                     bg="#1c1c1f", fg="#4a4d55", font=("Segoe UI", 7)).pack(
                fill="x", pady=(8, 0))
        # Position bottom-right AFTER layout so the real width is known
        # (positioning first clips the reset labels off-screen).
        p.update_idletasks()
        sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
        p.geometry(f"+{sw - p.winfo_reqwidth() - 24}+{sh - p.winfo_reqheight() - 90}")
        p.bind("<FocusOut>", lambda e: (p.destroy(), setattr(self, "panel", None)))

    # ---- lifecycle

    def _refresh_clicked(self):
        threading.Thread(target=self.refresh, daemon=True).start()

    def _quit(self):
        self._stop.set()
        if self.icon:
            self.icon.stop()
        self.root.after(0, self.root.destroy)

    def run(self):
        import tkinter as tk
        import pystray

        self.refresh()  # first fetch before showing anything

        self.root = tk.Tk()
        self.root.withdraw()

        menu = pystray.Menu(
            pystray.MenuItem("Show usage", lambda: self._toggle_panel(), default=True),
            pystray.MenuItem("Refresh now", lambda: self._refresh_clicked()),
            pystray.MenuItem("Quit", lambda: self._quit()),
        )
        self.icon = pystray.Icon(
            "usage-widget", self._draw_icon(), self._tooltip(), menu
        )
        self.icon.run_detached()

        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.mainloop()


# ---------------------------------------------------------------- startup install


def install_startup():
    startup = (
        Path(os.environ["APPDATA"])
        / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    cmd = startup / "UsageWidget.cmd"
    cmd.write_text(f'@start "" "{pythonw}" "{script}"\n', encoding="utf-8")
    print(f"Installed: {cmd}")


# ---------------------------------------------------------------- entry


def main():
    if "--install-startup" in sys.argv:
        install_startup()
        return
    if "--once" in sys.argv:
        for fn in FETCHERS.values():
            print(snapshot_text(fn()))
            print()
        return
    WidgetApp().run()


if __name__ == "__main__":
    main()
