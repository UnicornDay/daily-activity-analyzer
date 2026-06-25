"""
Daily Activity Analyzer — passively tracks computer activity with full
window context and provides AI-powered summaries like the recorder app's
"Analyze" feature.

Tracks: foreground window titles, app switches, clicks, keystrokes.
Generates: "What you did today" narrative + productivity suggestions.
"""

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import json
import os
import sys
import time
import threading
import traceback
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

try:
    from pynput import mouse, keyboard
except ImportError:
    print("Missing pynput. Install: py -3 -m pip install pynput")
    sys.exit(1)

try:
    import win32gui, win32api, win32process
except ImportError:
    print("Missing pywin32. Install: py -3 -m pip install pywin32")
    sys.exit(1)

import tkinter as tk
from tkinter import ttk, scrolledtext

DATA_DIR = Path.home() / "activity_logs" / "productivity"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_AI_MODEL = "ministral-3:3b"

# ─── Global state (thread-safe) ──────────────────────────────────────

_lock = threading.Lock()
_recording_event = threading.Event()

state = {
    "date": date.today().isoformat(),
    "start_time": "",
    "end_time": "",
    "sessions": [],       # {app, title, start, end, duration_secs}
    "total_clicks": 0,
    "total_keystrokes": 0,
    "total_scrolls": 0,
    "peak_apps": {},      # {app: total_seconds}
    "hourly": defaultdict(lambda: {"clicks": 0, "keystrokes": 0, "scrolls": 0}),
}

# Runtime tracking (always active even when not recording)
_current_app = "Idle"
_current_title = ""
_current_start = time.time()

# ─── Window tracking ────────────────────────────────────────────────

def _get_active_window_detailed():
    try:
        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return "Idle", ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
            exe = win32process.GetModuleFileNameEx(handle, 0)
            win32api.CloseHandle(handle)
            app = os.path.basename(exe).replace(".exe", "")
        except Exception:
            app = _fallback_app(title)
        return app, title.strip()
    except Exception:
        return "Idle", ""

def _fallback_app(title):
    t = title.lower()
    if not t:
        return "Unknown"
    known = {
        "Chrome": ["chrome", "- google", "google"],
        "Firefox": ["firefox", "mozilla"],
        "Edge": ["edge", "microsoft edge", " - bing"],
        "VS Code": ["visual studio code", "vscode", "code -"],
        "Slack": ["slack"],
        "Teams": ["teams", "microsoft teams"],
        "Terminal": ["terminal", "powershell", "cmd.exe", "command prompt", "wsl"],
        "Outlook": ["outlook", "mail"],
        "Office": ["word", "excel", "powerpoint", "onenote"],
        "Notepad": ["notepad", "notepad++"],
        "Spotify": ["spotify"],
        "Explorer": ["explorer", "file explorer"],
    }
    for cat, triggers in known.items():
        if any(t in title.lower() for t in triggers):
            return cat
    return title[:30]

def _window_poller():
    global _current_app, _current_title, _current_start
    while True:
        try:
            app, title = _get_active_window_detailed()
            if not app:
                app = "Idle"
            now = time.time()
            with _lock:
                # Always update current tracking state
                if app != _current_app:
                    if _recording_event.is_set():
                        dur = now - _current_start
                        state["sessions"].append({
                            "app": _current_app,
                            "title": _current_title,
                            "start": datetime.fromtimestamp(_current_start).isoformat(),
                            "end": datetime.fromtimestamp(now).isoformat(),
                            "duration_secs": round(dur, 1),
                        })
                        state["peak_apps"][_current_app] = state["peak_apps"].get(_current_app, 0) + dur
                    _current_app = app
                    _current_title = title
                    _current_start = now
                elif title and title != _current_title:
                    if _recording_event.is_set():
                        dur = now - _current_start
                        state["sessions"].append({
                            "app": _current_app,
                            "title": _current_title,
                            "start": datetime.fromtimestamp(_current_start).isoformat(),
                            "end": datetime.fromtimestamp(now).isoformat(),
                            "duration_secs": round(dur, 1),
                        })
                    _current_title = title
                    _current_start = now
            time.sleep(3)
        except Exception:
            time.sleep(3)

# ─── Input tracking ─────────────────────────────────────────────────

_click_cb = None
_key_cb = None
_scroll_cb = None

def _on_click(x, y, button, pressed):
    if pressed:
        if _click_cb:
            _click_cb()
def _on_scroll(x, y, dx, dy):
    if _scroll_cb:
        _scroll_cb()
def _on_key_press(key):
    if _key_cb:
        _key_cb()

# ─── Save / Load ───────────────────────────────────────────────────

def _today_path():
    return DATA_DIR / f"{date.today().isoformat()}.json"

def save_today():
    p = _today_path()
    with _lock:
        data = {
            "date": state["date"],
            "start_time": state["start_time"],
            "end_time": datetime.now().isoformat(),
            "total_clicks": state["total_clicks"],
            "total_keystrokes": state["total_keystrokes"],
            "total_scrolls": state["total_scrolls"],
            "sessions": state["sessions"],
            "peak_apps": dict(state["peak_apps"]),
            "hourly": {k: dict(v) for k, v in state["hourly"].items()},
        }
    with open(p, "w") as f:
        json.dump(data, f, indent=2)

def load_day(d):
    p = DATA_DIR / f"{d}.json"
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None

def list_days():
    return sorted([f.stem for f in DATA_DIR.glob("*.json")], reverse=True)

# ─── AI analysis ───────────────────────────────────────────────────

def analyze_day_titles(days_data):
    lines = []
    for day in days_data:
        when = day.get("date", "?")
        st = day.get("start_time", "")[11:19] if day.get("start_time") else "?"
        et = day.get("end_time", "")[11:19] if day.get("end_time") else "?"
        lines.append(f"--- {when} ({st} - {et}) ---")
        sessions = day.get("sessions", [])
        if sessions:
            for s in sessions:
                app = s.get("app", "?")
                title = s.get("title", "") or "(no title)"
                dur = s.get("duration_secs", 0)
                if dur < 5:
                    continue
                mins = int(dur // 60)
                secs_r = int(dur % 60)
                if mins:
                    lines.append(f"  [{mins}m {secs_r}s] {app}: {title}")
                else:
                    lines.append(f"  [{secs_r}s] {app}: {title}")
        clicks = day.get("total_clicks", 0)
        keys = day.get("total_keystrokes", 0)
        lines.append(f"  * Totals: {clicks} clicks, {keys} keystrokes\n")
    return "\n".join(lines)

def get_suggestions(days_data, model_name=None):
    model = model_name or DEFAULT_AI_MODEL
    if not days_data:
        return ("Not enough data yet.\n\n"
                "Record at least 15-30 minutes of activity, "
                "then try again.")

    activity_log = analyze_day_titles(days_data)

    prompt = (
        "You are a productivity analyst. A user recorded their computer "
        "activity with timestamps, app names, and window titles.\n\n"
        "Here is what they did:\n\n"
        f"{activity_log}\n\n"
        "Provide a two-part response:\n\n"
        "== WHAT YOU DID TODAY ==\n"
        "Write 2-4 sentences summarizing their day in narrative form. "
        "Mention specific apps and topics. For example:\n"
        "'You started the morning coding in VS Code (working on a Python project), "
        "then switched to Chrome to research technical topics. After lunch you "
        "spent time in Slack and Outlook before ending the day with more coding.'\n\n"
        "== SUGGESTIONS ==\n"
        "Then give 2-3 specific, actionable suggestions to improve productivity. "
        "Reference their actual activities. Use bullet points."
    )
    try:
        import ollama
        full = ""
        for chunk in ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        ):
            piece = chunk.get("message", {}).get("content", "")
            if piece:
                full += piece
        return full.strip() or "No analysis generated."
    except ImportError:
        return "Ollama not installed.\n\nInstall: https://ollama.com/download\nThen: ollama pull " + model
    except Exception as e:
        msg = str(e).lower()
        if "connection" in msg or "refused" in msg or "11434" in msg:
            return "Ollama not running.\n\nStart: ollama serve"
        if "model" in msg:
            return f"Model '{model}' not installed.\n\nRun: ollama pull {model}"
        return f"Error: {e}"

# ─── GUI ───────────────────────────────────────────────────────────

class ProductivityMonitorApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Daily Activity Analyzer")
        self.root.geometry("1000x640")
        self.root.minsize(850, 500)
        self._save_running = True
        self._suggestion_running = False
        self._timer_id = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Wire up input callbacks
        global _click_cb, _key_cb, _scroll_cb
        _click_cb = self._on_click_event
        _key_cb = self._on_key_event
        _scroll_cb = self._on_scroll_event

        # Start background threads (listen always, but only record when _recording_event is set)
        threading.Thread(target=_window_poller, daemon=True).start()
        mouse.Listener(on_click=_on_click, on_scroll=_on_scroll, daemon=True).start()
        keyboard.Listener(on_press=_on_key_press, daemon=True).start()

        # Periodic save
        threading.Thread(target=self._periodic_save, daemon=True).start()

        self._update_timer()
        self.set_status("Press Start to begin recording your activity")

    def _get_available_model(self):
        """Query Ollama for installed models, return first available or default."""
        try:
            import subprocess
            result = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                if len(lines) > 1:
                    names = [l.split()[0] for l in lines[1:]]
                    self._all_models = names
                    return names[0]
        except Exception:
            pass
        self._all_models = []
        return DEFAULT_AI_MODEL

    def _build_ui(self):
        # ── Toolbar ──
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill=tk.X)

        self.start_btn = ttk.Button(top, text="▶ Start Recording", command=self._start_recording)
        self.start_btn.pack(side=tk.LEFT, padx=(0,4))
        self.stop_btn = ttk.Button(top, text="■ Stop", command=self._stop_recording, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)

        ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)

        self.rec_indicator = ttk.Label(top, text="●", foreground="gray", font=("Segoe UI", 12))
        self.rec_indicator.pack(side=tk.LEFT, padx=(0,2))
        self.timer_var = tk.StringVar(value="00:00")
        ttk.Label(top, textvariable=self.timer_var, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0,10))

        ttk.Button(top, text="Analyze My Day", command=self._run_suggestions).pack(side=tk.LEFT, padx=4)
        ttk.Label(top, text="  Model:").pack(side=tk.LEFT, padx=(8,2))
        self.model_var = tk.StringVar(value=self._get_available_model())
        self.model_combo = ttk.Combobox(top, textvariable=self.model_var, width=20, state="readonly")
        self.model_combo["values"] = self._all_models if hasattr(self, "_all_models") else []
        self.model_combo.pack(side=tk.LEFT, padx=2)

        ttk.Button(top, text="Quit", command=self._quit).pack(side=tk.RIGHT, padx=(4,0))

        # ── Main: timeline + analysis ──
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        left_frame = ttk.LabelFrame(main, text="Activity Log", padding=4)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0,4))
        self.timeline_text = scrolledtext.ScrolledText(left_frame, wrap=tk.WORD, font=("Consolas", 9), height=14, relief=tk.FLAT)
        self.timeline_text.pack(fill=tk.BOTH, expand=True)

        right_frame = ttk.LabelFrame(main, text="AI Analysis", padding=4)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(4,0))
        self.analysis_text = scrolledtext.ScrolledText(right_frame, wrap=tk.WORD, font=("Segoe UI", 10), height=14, relief=tk.FLAT)
        self.analysis_text.pack(fill=tk.BOTH, expand=True)

        # ── Stats row ──
        stats_frame = ttk.Frame(self.root, padding=4)
        stats_frame.pack(fill=tk.X)
        self.stats_var = tk.StringVar(value="")
        ttk.Label(stats_frame, textvariable=self.stats_var, font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=4)

        # ── Status bar ──
        self.status_var = tk.StringVar()
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=4).pack(fill=tk.X, side=tk.BOTTOM)

        # Placeholder
        self.timeline_text.insert(tk.END, "Press ▶ Start Recording to begin tracking your activity.\n\nThe analyzer will capture window titles and timelines.")
        self.analysis_text.insert(tk.END, "Click \"Analyze My Day\" after recording to get an\nAI summary of what you did and productivity suggestions.")

    # ── Recording control ──────────────────────────────────────────

    def _toggle_recording(self, start):
        if start:
            self._recording_start_time = time.time()
            with _lock:
                _recording_event.set()
                state["sessions"] = []
                state["total_clicks"] = 0
                state["total_keystrokes"] = 0
                state["total_scrolls"] = 0
                state["peak_apps"] = {}
                state["hourly"] = defaultdict(lambda: {"clicks": 0, "keystrokes": 0, "scrolls": 0})
                state["start_time"] = datetime.now().isoformat()
                global _current_app, _current_title, _current_start
                now = time.time()
                app, title = _get_active_window_detailed()
                _current_app = app or "Idle"
                _current_title = title or ""
                _current_start = now
            self.start_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
            self.rec_indicator.configure(foreground="red")
            self.timeline_text.delete(1.0, tk.END)
            self.timeline_text.insert(tk.END, "Recording... activity will appear here as it's tracked.\n")
            self.analysis_text.delete(1.0, tk.END)
            self.analysis_text.insert(tk.END, "Analysis available after stopping.")
            self.stats_var.set("")
            self.set_status("Recording started — the analyzer is tracking your activity")
        else:
            with _lock:
                _recording_event.clear()
                state["end_time"] = datetime.now().isoformat()
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)
            self.rec_indicator.configure(foreground="gray")
            save_today()
            self._refresh()
            self.set_status("Recording stopped — data saved")

    def _start_recording(self):
        self._toggle_recording(True)

    def _stop_recording(self):
        self._toggle_recording(False)

    # ── Timer ──────────────────────────────────────────────────────

    def _update_timer(self):
        if _recording_event.is_set():
            elapsed = int(time.time() - self._recording_start_time)
            mins = elapsed // 60
            secs = elapsed % 60
            self.timer_var.set(f"{mins:02d}:{secs:02d}")
        else:
            self.timer_var.set("--:--")
        self._timer_id = self.root.after(1000, self._update_timer)

    # ── Callbacks from tracking threads ────────────────────────────

    def _on_click_event(self):
        with _lock:
            if not _recording_event.is_set():
                return
            state["total_clicks"] += 1
            h = str(datetime.now().hour)
            state["hourly"][h]["clicks"] += 1

    def _on_key_event(self):
        with _lock:
            if not _recording_event.is_set():
                return
            state["total_keystrokes"] += 1
            h = str(datetime.now().hour)
            state["hourly"][h]["keystrokes"] += 1

    def _on_scroll_event(self):
        with _lock:
            if not _recording_event.is_set():
                return
            state["total_scrolls"] += 1
            h = str(datetime.now().hour)
            state["hourly"][h]["scrolls"] += 1

    # ── UI helpers ──────────────────────────────────────────────────

    def set_status(self, msg):
        self.status_var.set(msg)

    def _on_close(self):
        self._quit()

    def _quit(self):
        self._save_running = False
        if self._timer_id:
            self.root.after_cancel(self._timer_id)
        save_today()
        self.root.destroy()

    def _periodic_save(self):
        while self._save_running:
            save_today()
            time.sleep(60)

    def _refresh(self):
        if _recording_event.is_set():
            return
        save_today()
        day = load_day(date.today().isoformat())
        self.timeline_text.delete(1.0, tk.END)
        t = self.timeline_text

        if not day:
            t.insert(tk.END, "No activity recorded today.\nPress ▶ Start Recording to begin.")
            self.stats_var.set("")
            self.set_status("No data — press Start to record")
            return

        clicks = day.get("total_clicks", 0)
        keys = day.get("total_keystrokes", 0)
        scrolls = day.get("total_scrolls", 0)
        sessions = day.get("sessions", [])
        apps = day.get("peak_apps", {})
        hourly = day.get("hourly", {})

        st = day.get("start_time", "")
        et = day.get("end_time", "")
        st_str = st[11:19] if st else "?"
        et_str = et[11:19] if et else "?"
        self.stats_var.set(f"Session: {st_str} - {et_str}  |  {len(sessions)} sessions · {clicks} clicks · {keys} keys · {scrolls} scrolls")

        if sessions:
            t.insert(tk.END, "── Activity Timeline ──\n\n")
            for s in sessions:
                app = s.get("app", "?")
                title = s.get("title", "")
                dur = s.get("duration_secs", 0)
                if dur < 3:
                    continue
                mins = int(dur // 60)
                secs_r = int(dur % 60)
                ts = s.get("start", "")[11:19]
                label = f"{ts} {app}"
                if mins:
                    t.insert(tk.END, f"  [{mins:2d}m {secs_r:2d}s] {label}\n")
                else:
                    t.insert(tk.END, f"  [{secs_r:2d}s] {label}\n")
                if title:
                    t.insert(tk.END, f"          {title[:80]}\n")
            t.insert(tk.END, "\n")

        if apps:
            t.insert(tk.END, "── Top Apps ──\n\n")
            for app, secs in sorted(apps.items(), key=lambda x: -x[1])[:10]:
                m = int(secs // 60)
                h = m // 60; mr = m % 60
                bar = "▓" * min(m // 2, 30)
                if h:
                    t.insert(tk.END, f"  {app:15s} {h:2d}h {mr:2d}m {bar}\n")
                else:
                    t.insert(tk.END, f"  {app:15s} {mr:2d}m {bar}\n")

        if hourly:
            t.insert(tk.END, "\n── Hourly Activity ──\n\n")
            for h in sorted(hourly.keys(), key=lambda x: int(x)):
                d = hourly[h]
                c = d.get("clicks", 0)
                k = d.get("keystrokes", 0)
                s = d.get("scrolls", 0)
                bar = "█" * min((c + k + s) // 5, 40)
                t.insert(tk.END, f"  {h}:00 {bar} ({c}c {k}k {s}s)\n")

        self.set_status(f"Stopped at {datetime.now().strftime('%H:%M:%S')} — {len(sessions)} sessions recorded")

    def _run_suggestions(self):
        if _recording_event.is_set():
            self.set_status("Stop recording first before analyzing")
            return
        if self._suggestion_running:
            self.set_status("Already generating...")
            return
        self._suggestion_running = True
        self.analysis_text.delete(1.0, tk.END)
        self.analysis_text.insert(tk.END, "Analyzing your day... (10-30 seconds)\n\n")
        self.analysis_text.update()
        model = self.model_var.get().strip() or DEFAULT_AI_MODEL

        days = list_days()
        days_data = []
        for d in days[:5]:
            day = load_day(d)
            if day and day.get("sessions"):
                days_data.append(day)

        threading.Thread(target=self._do_analysis, args=(days_data, model), daemon=True).start()

    def _do_analysis(self, days_data, model):
        try:
            result = get_suggestions(days_data, model_name=model)
        except Exception as e:
            result = f"Error: {e}\n{traceback.format_exc()}"
        self.root.after(0, lambda: self._show_analysis(result))

    def _show_analysis(self, text):
        self.analysis_text.delete(1.0, tk.END)
        self.analysis_text.insert(tk.END, text)
        self._suggestion_running = False
        self.set_status("Analysis complete")

    def run(self):
        self.root.mainloop()

if __name__ == "__main__":
    app = ProductivityMonitorApp()
    app.run()
