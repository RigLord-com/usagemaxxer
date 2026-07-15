"""Redlined — glance at how close your AI coding agents are to the limit.

An instrument-cluster usage widget for Claude Code + Codex. Analog gauges with
a needle and a red zone show your session (5h) and weekly window utilization,
live in the Windows system tray. It reads the same local login each CLI already
stores, so the numbers are the real ones — no manual calibration.

  codingatredline.com

Data sources (both undocumented — code defensively):
  Claude Code:  GET https://api.anthropic.com/api/oauth/usage
                Bearer token from ~/.claude/.credentials.json
  Codex:        GET https://chatgpt.com/backend-api/wham/usage
                Bearer token + account id from ~/.codex/auth.json

Usage:
  python redlined.py --once            # print both snapshots as text
  pythonw redlined.py                  # run the tray widget
  python redlined.py --install-startup # auto-start on login
"""

import json
import math
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

# Instrument palette
BG = "#0d0e10"          # dashboard black
FACE = "#16171b"        # dial face
RIM = "#2a2c31"         # dial rim
TICK = "#5c6068"        # tick marks
NEEDLE = "#f4f5f7"      # needle (turns red in the red zone)
INK = "#f7f7f8"         # digital readout
MUTED = "#7c828b"       # labels
FAINT = "#4a4e56"       # footer
GREEN = "#3ba55d"
AMBER = "#f5a524"
RED = "#e5484d"
REDLINE_START = 88      # % where the red zone begins

# ---------------------------------------------------------------- normalized shape


@dataclass
class Window:
    label: str
    percent: float
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
                "User-Agent": "redlined",
            },
        )
        mapping = (
            ("five_hour", "Session"),
            ("seven_day", "Weekly"),
            ("seven_day_opus", "Opus"),
            ("seven_day_sonnet", "Sonnet"),
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
        snap.error = f"HTTP {e.code} — open Claude Code to refresh login"
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
        snap.error = f"HTTP {e.code} — run codex once to refresh login"
    except Exception as e:  # noqa: BLE001
        snap.error = str(e)
    snap.fetched_at = datetime.now(timezone.utc)
    return snap


FETCHERS = {"claude": fetch_claude, "codex": fetch_codex}


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
    if pct >= REDLINE_START:
        return RED
    if pct >= 70:
        return AMBER
    return GREEN


def _gauge_angle(pct: float) -> float:
    """Map 0-100% to the sweep angle (math degrees, CCW from +x).

    270-degree sweep: 0% at lower-left (225 deg), over the top, 100% at
    lower-right (-45 deg)."""
    return 225.0 - min(max(pct, 0.0), 100.0) / 100.0 * 270.0


def snapshot_text(snap: Snapshot) -> str:
    lines = [snap.provider + (f"  ({snap.plan})" if snap.plan else "")]
    if not snap.ok:
        return "\n".join(lines + [f"  unavailable: {snap.error}"])
    for w in snap.windows:
        r = _fmt_reset(w.resets_at)
        lines.append(f"  {w.label:<10} {w.percent:5.1f}%   resets in {r}" if r
                     else f"  {w.label:<10} {w.percent:5.1f}%")
    if snap.credits:
        lines.append(f"  {snap.credits}")
    return "\n".join(lines)


# ---------------------------------------------------------------- tray app


class WidgetApp:
    def __init__(self):
        self.snapshots: dict[str, Snapshot] = {}
        self.lock = threading.Lock()
        self.icon = None
        self.root = None
        self.panel = None
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

    def _max_session(self) -> float:
        worst = 0.0
        with self.lock:
            for snap in self.snapshots.values():
                if snap and snap.ok:
                    s = snap.window("Session")
                    if s:
                        worst = max(worst, s.percent)
        return worst

    def _tooltip(self) -> str:
        parts = []
        with self.lock:
            for key, name in (("claude", "CC"), ("codex", "Codex")):
                snap = self.snapshots.get(key)
                if snap and snap.ok:
                    s = snap.window("Session")
                    w = snap.window("Weekly")
                    parts.append(
                        f"{name} {s.percent:.0f}%" + (f"/{w.percent:.0f}%w" if w else "")
                    )
                else:
                    parts.append(f"{name} —")
        return ("Redlined · " + " · ".join(parts))[:127]

    # ---- tray icon: a single needle gauge showing the worst session %

    def _draw_icon(self):
        from PIL import Image, ImageDraw

        S = 64
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, cy, r = 32, 38, 25
        bbox = [cx - r, cy - r, cx + r, cy + r]
        pct = self._max_session()

        # PIL arcs are clockwise from 3 o'clock; convert our math angle.
        def pil(p):
            a = (-_gauge_angle(p)) % 360
            return a

        # background sweep 0->100
        a0, a1 = pil(0), pil(100)
        if a1 < a0:
            a1 += 360
        d.arc(bbox, a0, a1, fill=RIM, width=6)
        # red zone at the top end
        ra0, ra1 = pil(REDLINE_START), pil(100)
        if ra1 < ra0:
            ra1 += 360
        d.arc(bbox, ra0, ra1, fill=RED, width=6)

        # needle
        th = math.radians(_gauge_angle(pct))
        nx = cx + (r - 4) * math.cos(th)
        ny = cy - (r - 4) * math.sin(th)
        ncol = RED if pct >= REDLINE_START else NEEDLE
        d.line([cx, cy, nx, ny], fill=ncol, width=4)
        d.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], fill=NEEDLE)
        return img

    # ---- canvas gauge (tkinter)

    @staticmethod
    def _draw_canvas_gauge(cv, cx, cy, r, pct, label, reset):
        # dial face + rim
        cv.create_oval(cx - r - 7, cy - r - 7, cx + r + 7, cy + r + 7,
                       fill=FACE, outline=RIM, width=1)
        # colored zones
        for a, b, col in ((0, 70, GREEN), (70, REDLINE_START, AMBER),
                          (REDLINE_START, 100, RED)):
            start = _gauge_angle(a)
            extent = _gauge_angle(b) - _gauge_angle(a)
            cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=start,
                          extent=extent, style="arc", outline=col, width=5)
        # ticks
        for p in (0, 25, 50, 75, 100):
            th = math.radians(_gauge_angle(p))
            x1, y1 = cx + (r - 9) * math.cos(th), cy - (r - 9) * math.sin(th)
            x2, y2 = cx + r * math.cos(th), cy - r * math.sin(th)
            cv.create_line(x1, y1, x2, y2, fill=TICK, width=2)
        # needle
        th = math.radians(_gauge_angle(pct))
        nx, ny = cx + (r - 7) * math.cos(th), cy - (r - 7) * math.sin(th)
        ncol = RED if pct >= REDLINE_START else NEEDLE
        cv.create_line(cx, cy, nx, ny, fill=ncol, width=3, capstyle="round")
        cv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=NEEDLE, outline="")
        # digital readout + labels
        cv.create_text(cx, cy + r - 8, text=f"{pct:.0f}%", fill=INK,
                       font=("Consolas", 13, "bold"))
        cv.create_text(cx, cy + r + 16, text=label.upper(), fill=MUTED,
                       font=("Segoe UI", 8, "bold"))
        cv.create_text(cx, cy + r + 30, text=(f"resets {reset}" if reset else ""),
                       fill=FAINT, font=("Segoe UI", 7))

    def _toggle_panel(self):
        self.root.after(0, self._toggle_panel_main)

    def _toggle_panel_main(self):
        import tkinter as tk

        if self.panel is not None and self.panel.winfo_exists():
            self.panel.destroy()
            self.panel = None
            return

        W, H = 540, 292
        p = tk.Toplevel(self.root)
        self.panel = p
        p.overrideredirect(True)          # frameless — reads like an instrument panel
        p.attributes("-topmost", True)
        cv = tk.Canvas(p, width=W, height=H, bg=BG, highlightthickness=1,
                       highlightbackground=RIM)
        cv.pack()

        # header
        cv.create_text(18, 18, text="REDLINED", anchor="w", fill=INK,
                       font=("Segoe UI Semibold", 13, "bold"))
        cv.create_text(118, 20, text="coding at redline", anchor="w", fill=FAINT,
                       font=("Segoe UI", 8))
        cv.create_line(0, 34, W, 34, fill=RIM)

        # gauge layout: 4 gauges in a row, provider labels above pairs
        centers = [95, 218, 341, 464]
        cy = 150
        r = 44
        cv.create_line(W / 2, 44, W / 2, H - 30, fill=RIM)  # divider between providers

        with self.lock:
            snaps = [("claude", self.snapshots.get("claude")),
                     ("codex", self.snapshots.get("codex"))]

        for pair_idx, (_, snap) in enumerate(snaps):
            gx = centers[pair_idx * 2: pair_idx * 2 + 2]
            label_x = (gx[0] + gx[1]) / 2
            if snap is None:
                continue
            title = snap.provider + (f"  ·  {snap.plan}" if snap.plan else "")
            cv.create_text(label_x, 52, text=title, fill=MUTED,
                           font=("Segoe UI Semibold", 9, "bold"))
            if snap.ok:
                for i, wname in enumerate(("Session", "Weekly")):
                    w = snap.window(wname)
                    if w is None:
                        continue
                    self._draw_canvas_gauge(cv, gx[i], cy, r, w.percent,
                                            wname, _fmt_reset(w.resets_at))
                if snap.credits:
                    cv.create_text(label_x, H - 40, text=snap.credits, fill=FAINT,
                                   font=("Segoe UI", 8))
            else:
                cv.create_text(label_x, cy, text=snap.error, fill=AMBER, width=200,
                               justify="center", font=("Segoe UI", 8))

        # footer
        with self.lock:
            ts = max((s.fetched_at for s in self.snapshots.values()
                      if s and s.fetched_at), default=None)
        if ts:
            cv.create_text(W - 12, H - 12, anchor="e",
                           text=f"updated {ts.astimezone().strftime('%H:%M')}",
                           fill=FAINT, font=("Segoe UI", 7))
        cv.create_text(12, H - 12, anchor="w", text="click tray icon to close",
                       fill=FAINT, font=("Segoe UI", 7))

        # position bottom-right, above the taskbar
        p.update_idletasks()
        sw, sh = p.winfo_screenwidth(), p.winfo_screenheight()
        p.geometry(f"{W}x{H}+{sw - W - 24}+{sh - H - 60}")
        p.bind("<Button-1>", lambda e: (p.destroy(), setattr(self, "panel", None)))
        p.focus_force()
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

        self.refresh()
        self.root = tk.Tk()
        self.root.withdraw()

        menu = pystray.Menu(
            pystray.MenuItem("Show gauges", lambda: self._toggle_panel(), default=True),
            pystray.MenuItem("Refresh now", lambda: self._refresh_clicked()),
            pystray.MenuItem("Quit", lambda: self._quit()),
        )
        self.icon = pystray.Icon("redlined", self._draw_icon(), self._tooltip(), menu)
        self.icon.run_detached()
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self.root.mainloop()


# ---------------------------------------------------------------- startup install


def install_startup():
    startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
               / "Start Menu" / "Programs" / "Startup")
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    cmd = startup / "Redlined.cmd"
    cmd.write_text(f'@start "" "{pythonw}" "{script}"\n', encoding="utf-8")
    print(f"Installed: {cmd}")


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
