#!/usr/bin/env python3
"""DarkTask - a dark-mode macro recorder in the spirit of TinyTask.

Record what your mouse and keyboard do, then play it back as many times as
you like.

    F8  start / stop recording
    F9  start / stop playback

Both hotkeys work even when DarkTask is minimised and can be changed in
Options.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import traceback

# Must run before any window exists, otherwise Windows display scaling
# (125%, 150%, ...) makes recorded mouse positions land in the wrong place.
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

try:
    from pynput import keyboard, mouse
    _PYNPUT_ERROR = None
except Exception as exc:  # missing package, or no display on Linux
    keyboard = mouse = None
    _PYNPUT_ERROR = exc

__version__ = "1.0.0"
APP_NAME = "DarkTask"
FORMAT_VERSION = 1

IDLE, COUNTDOWN, RECORDING, PLAYING = "idle", "countdown", "recording", "playing"

C = {
    "bg": "#0f1115",
    "card": "#161920",
    "raised": "#1e222b",
    "hover": "#282d38",
    "border": "#2a2f3a",
    "text": "#e7e9ee",
    "muted": "#8a92a3",
    "faint": "#545b6a",
    "accent": "#8b7bff",
    "accent_hover": "#9d8fff",
    "red": "#ff4d5e",
    "red_hover": "#ff6574",
    "green": "#2ed573",
    "green_hover": "#4be08a",
    "amber": "#f5b544",
    "on_color": "#0f1115",
}

DEFAULT_SETTINGS = {
    "repeat": 1,
    "forever": False,
    "speed": 1.0,
    "loop_delay": 0.0,
    "countdown": 0,
    "record_moves": True,
    "record_keys": True,
    "always_on_top": True,
    "hide_on_play": False,
    "show_options": True,
    "hotkey_record": {"name": "f8"},
    "hotkey_play": {"name": "f9"},
}

SPEED_PRESETS = (0.5, 1, 2, 4, 10)
MODIFIER_PREFIXES = ("shift", "ctrl", "alt", "cmd")


# --------------------------------------------------------------------------
# Settings and files
# --------------------------------------------------------------------------

def config_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, path)


def load_settings() -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(os.path.join(config_dir(), "settings.json"), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in settings and type(value) is type(settings[key]):
                    settings[key] = value
                elif key in settings and isinstance(settings[key], float) and isinstance(value, int):
                    settings[key] = float(value)
                elif key in ("x", "y") and isinstance(value, int):
                    settings[key] = value
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    try:
        _write_json(os.path.join(config_dir(), "settings.json"), settings)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def key_to_data(key) -> dict | None:
    """Turn a pynput key into something JSON can store."""
    if key is None:
        return None
    if isinstance(key, keyboard.Key):
        return {"name": key.name}
    data = {}
    vk = getattr(key, "vk", None)
    char = getattr(key, "char", None)
    if vk is not None:
        data["vk"] = int(vk)
    if char is not None:
        data["char"] = char
    return data or None


def data_to_key(data, prefer_char=False):
    """Inverse of key_to_data. Returns None for keys this system doesn't have.

    The virtual-key code is replayed as a real key press, while a plain
    character can come out as a control code (Ctrl held) or be ignored by
    some apps, so the code wins unless the macro came from another OS,
    where the codes mean different keys.
    """
    if not data:
        return None
    name = data.get("name")
    if name:
        try:
            return keyboard.Key[name]
        except KeyError:
            return None
    vk, char = data.get("vk"), data.get("char")
    if vk is not None and not (prefer_char and char):
        return keyboard.KeyCode.from_vk(vk)
    if char:
        return keyboard.KeyCode.from_char(char)
    return None


def key_id(data: dict) -> str:
    """Stable identity used to pair a key press with its release."""
    if data.get("name"):
        return "n:" + data["name"]
    if data.get("vk") is not None:
        return "v:%d" % data["vk"]
    return "c:" + str(data.get("char", "")).lower()


def key_matches(key, data) -> bool:
    if key is None or not data:
        return False
    if data.get("name"):
        return isinstance(key, keyboard.Key) and key.name == data["name"]
    if isinstance(key, keyboard.Key):
        return False
    vk = getattr(key, "vk", None)
    if data.get("vk") is not None and vk is not None:
        return vk == data["vk"]
    char = getattr(key, "char", None)
    return bool(char) and bool(data.get("char")) and char.lower() == data["char"].lower()


def key_label(data) -> str:
    if not data:
        return "None"
    name = data.get("name")
    if name:
        return name.replace("_", " ").title()
    char = data.get("char")
    if char and char.isprintable() and char.strip():
        return char.upper()
    return "Key %s" % data.get("vk", "?")


def is_modifier(data) -> bool:
    return bool(data) and str(data.get("name", "")).startswith(MODIFIER_PREFIXES)


# --------------------------------------------------------------------------
# Macro
# --------------------------------------------------------------------------
#
# Events are compact lists, times in seconds from the start of the recording:
#   [t, "move",   x, y]
#   [t, "click",  x, y, button, pressed]
#   [t, "scroll", x, y, dx, dy]
#   [t, "key",    keydata, pressed]

class Macro:
    def __init__(self, events=None, duration: float = 0.0, platform: str = sys.platform):
        self.events = events or []
        last = self.events[-1][0] if self.events else 0.0
        self.duration = max(float(duration), last)
        self.platform = platform  # key codes are only portable within one OS

    @property
    def empty(self) -> bool:
        return not self.events

    def counts(self) -> dict:
        counts = {"move": 0, "click": 0, "scroll": 0, "key": 0}
        for ev in self.events:
            kind = ev[1]
            # A click or key press is counted once, not once for down and up.
            if kind in ("click", "key") and not ev[-1]:
                continue
            counts[kind] = counts.get(kind, 0) + 1
        return counts

    def summary(self) -> str:
        if self.empty:
            return "No macro yet"
        c = self.counts()
        parts = [fmt_duration(self.duration)]
        for kind, word in (("click", "click"), ("key", "key"), ("scroll", "scroll"), ("move", "move")):
            if c[kind]:
                parts.append("%s %s%s" % (f"{c[kind]:,}", word, "" if c[kind] == 1 else "s"))
        return "  ·  ".join(parts)

    def to_dict(self) -> dict:
        return {
            "app": APP_NAME,
            "version": FORMAT_VERSION,
            "platform": self.platform,
            "duration": round(self.duration, 4),
            "events": self.events,
        }

    @classmethod
    def from_dict(cls, data) -> "Macro":
        raw = data if isinstance(data, list) else data.get("events", [])
        events = []
        for ev in raw:
            try:
                t, kind = float(ev[0]), ev[1]
                if kind == "move":
                    events.append([t, kind, int(ev[2]), int(ev[3])])
                elif kind == "click":
                    events.append([t, kind, int(ev[2]), int(ev[3]), str(ev[4]), bool(ev[5])])
                elif kind == "scroll":
                    events.append([t, kind, int(ev[2]), int(ev[3]), int(ev[4]), int(ev[5])])
                elif kind == "key" and isinstance(ev[2], dict):
                    events.append([t, kind, ev[2], bool(ev[3])])
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        events.sort(key=lambda e: e[0])
        meta = data if isinstance(data, dict) else {}
        try:
            duration = float(meta.get("duration", 0))
        except (TypeError, ValueError):
            duration = 0.0
        return cls(events, duration, str(meta.get("platform", sys.platform)))

    def save(self, path: str) -> None:
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str) -> "Macro":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "%.1fs" % seconds
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return "%d:%02d" % (m, s)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------

class Recorder:
    """Captures global mouse and keyboard input into a Macro."""

    def __init__(self, record_moves=True, record_keys=True, ignore_key=None, ignore_point=None):
        self.record_moves = record_moves
        self.record_keys = record_keys
        self._ignore_key = ignore_key or (lambda key: False)
        self._ignore_point = ignore_point or (lambda x, y: False)
        self._lock = threading.Lock()
        self._listeners = []
        self.events = []
        self.running = False
        self._t0 = 0.0

    def _begin(self) -> None:
        self.events = []
        self._buttons = {}  # button name -> press position, for presses we kept
        self._keys = {}     # key id -> key data, for presses we kept
        self._last_move = None
        self._t0 = time.perf_counter()
        self.running = True

    def start(self) -> None:
        self._begin()
        self._listeners = [mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll)]
        if self.record_keys:
            self._listeners.append(keyboard.Listener(
                on_press=self._on_press, on_release=self._on_release))
        for listener in self._listeners:
            listener.start()
        for listener in self._listeners:
            # Listener.wait() never returns if the hook fails to install, so
            # give it a moment rather than letting it freeze the window.
            waiter = threading.Thread(target=listener.wait, daemon=True)
            waiter.start()
            waiter.join(2.0)

    def stop(self) -> Macro:
        self.running = False
        for listener in self._listeners:
            listener.stop()
        self._listeners = []
        end = self.elapsed()
        with self._lock:
            # Anything still held down gets released so playback never
            # leaves a stuck key or button behind.
            for name, (x, y) in self._buttons.items():
                self.events.append([end, "click", x, y, name, False])
            for data in self._keys.values():
                self.events.append([end, "key", data, False])
            self._buttons.clear()
            self._keys.clear()
            events = list(self.events)
        return Macro(events, end)

    def elapsed(self) -> float:
        return round(time.perf_counter() - self._t0, 4)

    def _add(self, *event) -> None:
        with self._lock:
            self.events.append([self.elapsed(), *event])

    # pynput passes `injected` on backends that support it; events we
    # generate ourselves during playback are never recorded.

    def _on_move(self, x, y, injected=False):
        if not self.running or injected or not self.record_moves:
            return
        pos = (int(x), int(y))
        if pos != self._last_move:
            self._last_move = pos
            self._add("move", *pos)

    def _on_click(self, x, y, button, pressed, injected=False):
        if not self.running or injected:
            return
        name = getattr(button, "name", None)
        if name is None:
            return
        x, y = int(x), int(y)
        if pressed:
            # Clicks on DarkTask itself (e.g. its Stop button) are skipped.
            if self._ignore_point(x, y):
                return
            self._buttons[name] = (x, y)
            self._add("click", x, y, name, True)
        elif self._buttons.pop(name, None) is not None:
            self._add("click", x, y, name, False)

    def _on_scroll(self, x, y, dx, dy, injected=False):
        if not self.running or injected or self._ignore_point(int(x), int(y)):
            return
        self._add("scroll", int(x), int(y), int(dx), int(dy))

    def _on_press(self, key, injected=False):
        if not self.running or injected or key is None or self._ignore_key(key):
            return
        data = key_to_data(key)
        if data is None:
            return
        self._keys[key_id(data)] = data
        self._add("key", data, True)

    def _on_release(self, key, injected=False):
        if not self.running or injected or key is None or self._ignore_key(key):
            return
        data = key_to_data(key)
        if data is None:
            return
        pressed = self._keys.pop(key_id(data), None)
        if pressed is not None:
            self._add("key", pressed, False)


# --------------------------------------------------------------------------
# Playback
# --------------------------------------------------------------------------

class _TimerResolution:
    """Ask Windows for 1 ms timer precision so playback timing is smooth."""

    def __enter__(self):
        self.active = False
        if sys.platform == "win32":
            try:
                self.active = ctypes.windll.winmm.timeBeginPeriod(1) == 0
            except Exception:
                pass
        return self

    def __exit__(self, *exc):
        if self.active:
            ctypes.windll.winmm.timeEndPeriod(1)


class Player:
    """Plays a Macro back on a background thread."""

    def __init__(self, macro: Macro, speed=1.0, repeat=1, loop_delay=0.0,
                 on_finish=None, mouse_ctl=None, keyboard_ctl=None):
        self.macro = macro
        self.speed = max(0.01, float(speed))
        self.repeat = int(repeat)  # 0 or less means forever
        self.loop_delay = max(0.0, float(loop_delay))
        self.on_finish = on_finish
        self.loop = 0
        self.error = None
        self.skipped = 0
        self._mouse = mouse_ctl
        self._kbd = keyboard_ctl
        self._stop = threading.Event()
        self._thread = None
        self._loop_started = 0.0
        self._between_loops = False
        self._held_keys = {}
        self._held_buttons = {}

    @property
    def forever(self) -> bool:
        return self.repeat <= 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="DarkTask player", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout=None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def progress(self) -> float:
        """How far through the current loop we are, 0..1."""
        if self._between_loops or self.macro.duration <= 0:
            return 1.0
        done = (time.perf_counter() - self._loop_started) * self.speed / self.macro.duration
        return min(1.0, max(0.0, done))

    def _wait_until(self, target: float) -> bool:
        """Sleep until `target`. Returns False if we were asked to stop."""
        delay = target - time.perf_counter()
        if delay > 0:
            return not self._stop.wait(delay)
        return not self._stop.is_set()

    def _run(self) -> None:
        try:
            if self._mouse is None:
                self._mouse = mouse.Controller()
            if self._kbd is None:
                self._kbd = keyboard.Controller()
            prefer_char = self.macro.platform != sys.platform
            actions = [self._prepare(ev, prefer_char) for ev in self.macro.events]
            with _TimerResolution():
                while not self._stop.is_set() and (self.forever or self.loop < self.repeat):
                    self.loop += 1
                    if not self._play_once(actions):
                        break
                    if self.loop_delay and (self.forever or self.loop < self.repeat):
                        self._between_loops = True
                        if self._stop.wait(self.loop_delay):
                            break
        except Exception as exc:
            self.error = exc
        finally:
            self._release_all()
            if self.on_finish is not None:
                self.on_finish(self)

    def _play_once(self, actions) -> bool:
        self._between_loops = False
        start = self._loop_started = time.perf_counter()
        for t, action, args in actions:
            if not self._wait_until(start + t / self.speed):
                return False
            if action is None:
                continue
            try:
                action(*args)
            except Exception:
                self.skipped += 1
        # Keep the pause at the end of the recording, like TinyTask does.
        return self._wait_until(start + self.macro.duration / self.speed)

    def _prepare(self, ev, prefer_char=False):
        t, kind = ev[0], ev[1]
        if kind == "move":
            return t, self._move, (ev[2], ev[3])
        if kind == "click":
            button = getattr(mouse.Button, ev[4], None) if mouse else None
            if button is None:
                return t, None, ()
            return t, self._click, (ev[2], ev[3], ev[4], button, ev[5])
        if kind == "scroll":
            return t, self._scroll, (ev[2], ev[3], ev[4], ev[5])
        if kind == "key":
            key = data_to_key(ev[2], prefer_char)
            if key is None:
                return t, None, ()
            return t, self._key, (key_id(ev[2]), key, ev[3])
        return t, None, ()

    def _move(self, x, y):
        self._mouse.position = (x, y)

    def _click(self, x, y, name, button, pressed):
        self._mouse.position = (x, y)
        if pressed:
            self._mouse.press(button)
            self._held_buttons[name] = button
        else:
            self._mouse.release(button)
            self._held_buttons.pop(name, None)

    def _scroll(self, x, y, dx, dy):
        self._mouse.position = (x, y)
        self._mouse.scroll(dx, dy)

    def _key(self, ident, key, pressed):
        if pressed:
            self._kbd.press(key)
            self._held_keys[ident] = key
        else:
            self._kbd.release(key)
            self._held_keys.pop(ident, None)

    def _release_all(self) -> None:
        for key in list(self._held_keys.values()):
            try:
                self._kbd.release(key)
            except Exception:
                pass
        for button in list(self._held_buttons.values()):
            try:
                self._mouse.release(button)
            except Exception:
                pass
        self._held_keys.clear()
        self._held_buttons.clear()


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class _Hoverable:
    """Hover and click behaviour shared by the custom widgets below.

    Built from Labels and Canvases rather than tk.Button because tk.Button
    ignores background colours on macOS.
    """

    def _init_hover(self, command, widgets):
        self._command = command
        self._widgets = widgets
        self._enabled = True
        self._hover = False
        for w in widgets:
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")
            w.bind("<ButtonRelease-1>", self._on_release, add="+")

    def _pointer_inside(self) -> bool:
        try:
            w = self.winfo_containing(*self.winfo_pointerxy())
        except (tk.TclError, KeyError):
            return False
        return any(w is x for x in self._widgets)

    def _on_enter(self, _event=None):
        self._hover = True
        self._paint()

    def _on_leave(self, _event=None):
        self.after_idle(self._recheck_hover)

    def _recheck_hover(self):
        self._hover = self._pointer_inside()
        self._paint()

    def _on_release(self, _event=None):
        if self._enabled and self._pointer_inside() and self._command:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        if enabled != self._enabled:
            self._enabled = enabled
            for w in self._widgets:
                w.configure(cursor="hand2" if enabled else "arrow")
            self._paint()


class FlatButton(tk.Label, _Hoverable):
    def __init__(self, parent, text, command, font, padx, pady,
                 bg=C["raised"], fg=C["text"], hover=C["hover"]):
        super().__init__(parent, text=text, font=font, padx=padx, pady=pady, cursor="hand2")
        self._colors = (bg, fg, hover)
        self._init_hover(command, [self])
        self._paint()

    def set_style(self, bg, fg, hover) -> None:
        self._colors = (bg, fg, hover)
        self._paint()

    def _paint(self):
        bg, fg, hover = self._colors
        if not self._enabled:
            self.configure(bg=C["raised"], fg=C["faint"])
        else:
            self.configure(bg=hover if self._hover else bg, fg=fg)


class BigButton(tk.Frame, _Hoverable):
    """The large Record / Play buttons: icon, title and a hotkey hint."""

    def __init__(self, parent, command, fonts, px):
        super().__init__(parent, cursor="hand2", padx=px(12), pady=px(10))
        f_icon, f_title, f_hint = fonts
        self.icon = tk.Label(self, font=f_icon, cursor="hand2")
        self.title = tk.Label(self, font=f_title, cursor="hand2")
        self.hint = tk.Label(self, font=f_hint, cursor="hand2")
        self.icon.pack(side="left")
        self.title.pack(side="left", padx=(px(6), 0))
        self.hint.pack(side="right")
        self._colors = (C["raised"], C["text"], C["hover"], C["text"], C["muted"])
        self._init_hover(command, [self, self.icon, self.title, self.hint])

    def set(self, icon, title, hint, bg, fg, hover, icon_fg, hint_fg) -> None:
        self.icon.configure(text=icon)
        self.title.configure(text=title)
        self.hint.configure(text=hint)
        self._colors = (bg, fg, hover, icon_fg, hint_fg)
        self._paint()

    def _paint(self):
        bg, fg, hover, icon_fg, hint_fg = self._colors
        if not self._enabled:
            bg, fg, icon_fg, hint_fg = C["raised"], C["faint"], C["faint"], C["faint"]
        elif self._hover:
            bg = hover
        for w in self._widgets:
            w.configure(bg=bg)
        self.title.configure(fg=fg)
        self.icon.configure(fg=icon_fg)
        self.hint.configure(fg=hint_fg)


class Toggle(tk.Canvas, _Hoverable):
    """An on/off switch."""

    def __init__(self, parent, value, command, px, bg):
        self._tw, self._th = px(36), px(20)
        super().__init__(parent, width=self._tw, height=self._th, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.value = bool(value)
        self._on_change = command
        self._init_hover(self._flip, [self])
        self._paint()

    def _flip(self):
        self.set(not self.value)
        if self._on_change:
            self._on_change(self.value)

    def set(self, value: bool) -> None:
        self.value = bool(value)
        self._paint()

    def _paint(self):
        w, h = self._tw - 1, self._th - 1
        if not self._enabled:
            track = C["border"]
        elif self.value:
            track = C["accent_hover"] if self._hover else C["accent"]
        else:
            track = C["hover"] if self._hover else C["border"]
        self.delete("all")
        self.create_oval(0, 0, h, h, fill=track, outline=track)
        self.create_oval(w - h, 0, w, h, fill=track, outline=track)
        self.create_rectangle(h / 2, 0, w - h / 2, h, fill=track, outline=track)
        pad = max(2, round(self._th * 0.15))
        d = h - 2 * pad
        x = w - pad - d if self.value else pad
        knob = C["text"] if self._enabled else C["faint"]
        self.create_oval(x, pad, x + d, pad + d, fill=knob, outline=knob)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------

class App:
    def __init__(self, root: tk.Tk, open_path: str | None = None):
        self.root = root
        self.settings = load_settings()
        self.k = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.macro = Macro()
        self.macro_name = None
        self.state = IDLE
        self.recorder = None
        self.player = None
        self.queue = queue.Queue()
        self.capturing = None       # "record" / "play" while re-binding a hotkey
        self._hotkeys_down = set()
        self._own_rect = (0, 0, 0, 0)
        self._countdown = None      # [kind, seconds_left, after_id, then]
        self._flash_until = 0.0
        self._hidden_for_play = False
        self._dialog_open = False
        self._bar_width = 0

        self._make_fonts()
        self._build()
        self._restore_position()
        self._apply_topmost()
        self._load_last_macro()
        if open_path:
            self.open_macro(open_path)
        self._refresh()

        self._hotkey_listener = keyboard.Listener(
            on_press=self._hotkey_press, on_release=self._hotkey_release)
        self._hotkey_listener.start()
        self.root.after(50, self._tick)

    def px(self, n: float) -> int:
        return int(round(n * self.k))

    # ---- UI construction ------------------------------------------------

    def _make_fonts(self):
        if sys.platform == "win32":
            family = "Segoe UI"
        else:
            family = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_title = tkfont.Font(family=family, size=12, weight="bold")
        self.f_big = tkfont.Font(family=family, size=11, weight="bold")
        self.f_icon = tkfont.Font(family=family, size=11)
        self.f_body = tkfont.Font(family=family, size=10)
        self.f_small = tkfont.Font(family=family, size=9)
        self.f_tag = tkfont.Font(family=family, size=8, weight="bold")

    def _build(self):
        px, r = self.px, self.root
        r.title(APP_NAME)
        r.configure(bg=C["bg"])
        r.resizable(False, False)
        r.protocol("WM_DELETE_WINDOW", self.quit)
        self._icon = make_icon()
        r.iconphoto(True, self._icon)

        outer = tk.Frame(r, bg=C["bg"], padx=px(14), pady=px(12))
        outer.pack(fill="both", expand=True)
        tk.Frame(outer, bg=C["bg"], width=px(330), height=0).pack()

        head = tk.Frame(outer, bg=C["bg"])
        head.pack(fill="x")
        dot = px(12)
        logo = tk.Canvas(head, width=dot, height=dot, bg=C["bg"], highlightthickness=0)
        logo.create_oval(0, 0, dot - 1, dot - 1, fill=C["red"], outline=C["red"])
        logo.pack(side="left", padx=(0, px(8)))
        tk.Label(head, text=APP_NAME, font=self.f_title, bg=C["bg"], fg=C["text"]).pack(side="left")
        self.status = tk.Label(head, font=self.f_tag, padx=px(8), pady=px(2))
        self.status.pack(side="right")

        row = tk.Frame(outer, bg=C["bg"])
        row.pack(fill="x", pady=(px(12), px(8)))
        row.columnconfigure(0, weight=1, uniform="big")
        row.columnconfigure(1, weight=1, uniform="big")
        fonts = (self.f_icon, self.f_big, self.f_small)
        self.rec_btn = BigButton(row, self.toggle_record, fonts, px)
        self.rec_btn.grid(row=0, column=0, sticky="nsew", padx=(0, px(4)))
        self.play_btn = BigButton(row, self.toggle_play, fonts, px)
        self.play_btn.grid(row=0, column=1, sticky="nsew", padx=(px(4), 0))

        tools = tk.Frame(outer, bg=C["bg"])
        tools.pack(fill="x")
        small = dict(font=self.f_small, padx=px(10), pady=px(4))
        self.open_btn = FlatButton(tools, "Open", self.open_macro, **small)
        self.save_btn = FlatButton(tools, "Save", self.save_macro, **small)
        self.clear_btn = FlatButton(tools, "Clear", self.clear_macro, **small)
        self.opts_btn = FlatButton(tools, "", self.toggle_options, **small)
        self.open_btn.pack(side="left")
        self.save_btn.pack(side="left", padx=(px(6), 0))
        self.clear_btn.pack(side="left", padx=(px(6), 0))
        self.opts_btn.pack(side="right")

        self.bar = tk.Canvas(outer, height=px(4), bg=C["raised"], highlightthickness=0)
        self.bar.pack(fill="x", pady=(px(12), px(6)))
        self.bar_fill = self.bar.create_rectangle(0, 0, 0, px(4), fill=C["accent"], width=0)
        self.bar.bind("<Configure>", lambda e: setattr(self, "_bar_width", e.width))

        self.info = tk.Label(outer, font=self.f_small, bg=C["bg"], fg=C["muted"], anchor="w")
        self.info.pack(fill="x")

        self.options = tk.Frame(outer, bg=C["card"], padx=px(12), pady=px(6),
                                highlightthickness=1, highlightbackground=C["border"])
        self._build_options(self.options)
        self._show_options(self.settings["show_options"])

        r.bind("<Configure>", self._on_configure)
        r.bind_all("<Control-o>", lambda e: self.open_macro())
        r.bind_all("<Control-s>", lambda e: self.save_macro())

    def _build_options(self, box):
        px, s = self.px, self.settings

        self._section(box, "PLAYBACK", first=True)
        self.repeat_spin = self._spin(self._row(box, "Repeat"), "repeat", 1, 99999, 1, int, "%d",
                                      suffix="times")
        self.forever_tgl = self._toggle(self._row(box, "Loop forever"), "forever", self._on_forever)

        speed_row = self._row(box, "Speed")
        self.speed_spin = self._spin(speed_row, "speed", 0.25, 100, 0.25, float, "%.2f", suffix="×",
                                     on_change=self._paint_speed_chips)
        chips = tk.Frame(box, bg=C["card"])
        chips.pack(fill="x", pady=(0, px(4)))
        self.speed_chips = []
        for value in reversed(SPEED_PRESETS):
            chip = FlatButton(chips, "%g×" % value, lambda v=value: self._set_speed(v),
                              font=self.f_small, padx=px(7), pady=px(1))
            chip.pack(side="right", padx=(px(4), 0))
            self.speed_chips.append((value, chip))
        self._paint_speed_chips()

        self._spin(self._row(box, "Delay between loops"), "loop_delay", 0, 3600, 0.5, float, "%.1f",
                   suffix="sec")
        self._spin(self._row(box, "Countdown before start"), "countdown", 0, 30, 1, int, "%d",
                   suffix="sec")

        self._section(box, "RECORD")
        self._toggle(self._row(box, "Mouse movement"), "record_moves")
        self._toggle(self._row(box, "Keyboard"), "record_keys")

        self._section(box, "WINDOW")
        self._toggle(self._row(box, "Always on top"), "always_on_top", lambda v: self._apply_topmost())
        self._toggle(self._row(box, "Hide while playing"), "hide_on_play")

        self._section(box, "HOTKEYS")
        small = dict(font=self.f_small, padx=px(10), pady=px(2))
        self.hk_record_btn = FlatButton(self._row(box, "Record / stop"), "",
                                        lambda: self._begin_capture("record"), **small)
        self.hk_record_btn.pack(side="right")
        self.hk_play_btn = FlatButton(self._row(box, "Play / stop"), "",
                                      lambda: self._begin_capture("play"), **small)
        self.hk_play_btn.pack(side="right")
        tk.Label(box, text="Click a hotkey, then press the key you want.", font=self.f_small,
                 bg=C["card"], fg=C["faint"], anchor="w").pack(fill="x", pady=(px(4), px(2)))
        self._on_forever(s["forever"])
        self._paint_hotkeys()

    def _section(self, parent, text, first=False):
        top = self.px(4) if first else self.px(12)
        tk.Label(parent, text=text, font=self.f_tag, bg=C["card"], fg=C["faint"],
                 anchor="w").pack(fill="x", pady=(top, self.px(2)))

    def _row(self, parent, label):
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=self.px(3))
        tk.Label(row, text=label, font=self.f_body, bg=C["card"], fg=C["text"]).pack(side="left")
        return row

    def _toggle(self, row, key, on_change=None):
        def changed(value):
            self.settings[key] = value
            save_settings(self.settings)
            if on_change:
                on_change(value)
        toggle = Toggle(row, self.settings[key], changed, self.px, C["card"])
        toggle.pack(side="right")
        return toggle

    def _spin(self, row, key, lo, hi, step, cast, fmt, suffix="", on_change=None):
        # Tk snaps arrow clicks to lo + n * step, so lo should sit on that grid.
        if suffix:
            tk.Label(row, text=suffix, font=self.f_small, bg=C["card"], fg=C["muted"],
                     width=5, anchor="w").pack(side="right", padx=(self.px(6), 0))
        var = tk.StringVar()
        spin = tk.Spinbox(
            row, from_=lo, to=hi, increment=step, textvariable=var, width=7, justify="center",
            format=fmt if fmt.endswith("f") else "%.0f",  # Tk only takes %f formats
            font=self.f_body, bg=C["raised"], fg=C["text"], buttonbackground=C["raised"],
            insertbackground=C["text"], selectbackground=C["accent"], selectforeground=C["on_color"],
            disabledbackground=C["raised"], disabledforeground=C["faint"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"],
            buttoncursor="hand2")
        spin.pack(side="right")
        var.set(fmt % self.settings[key])  # after creation: Spinbox resets to from_

        def changed(*_):
            try:
                value = cast(float(var.get()))
            except ValueError:
                return
            if lo <= value <= hi and value != self.settings[key]:
                self.settings[key] = value
                save_settings(self.settings)
                if on_change:
                    on_change()

        def tidy(_event=None):
            var.set(fmt % self.settings[key])

        var.trace_add("write", changed)
        spin.bind("<FocusOut>", tidy)
        spin.bind("<Return>", lambda e: (tidy(), self.root.focus_set()))
        spin.var, spin.tidy = var, tidy
        return spin

    # ---- options helpers -----------------------------------------------

    def _on_forever(self, forever):
        self.repeat_spin.configure(state="disabled" if forever else "normal")

    def _set_speed(self, value):
        self.settings["speed"] = float(value)
        save_settings(self.settings)
        self.speed_spin.tidy()
        self._paint_speed_chips()

    def _paint_speed_chips(self):
        for value, chip in self.speed_chips:
            if abs(self.settings["speed"] - value) < 1e-9:
                chip.set_style(C["accent"], C["on_color"], C["accent_hover"])
            else:
                chip.set_style(C["raised"], C["muted"], C["hover"])

    def toggle_options(self):
        self._show_options(not self.settings["show_options"])
        save_settings(self.settings)

    def _show_options(self, show):
        self.settings["show_options"] = show
        if show:
            self.options.pack(fill="x", pady=(self.px(12), 0))
            self.opts_btn.configure(text="Options  ▴")
        else:
            self.options.pack_forget()
            self.opts_btn.configure(text="Options  ▾")

    def _apply_topmost(self):
        self.root.attributes("-topmost", bool(self.settings["always_on_top"]))

    # ---- hotkeys --------------------------------------------------------

    def _hotkey_press(self, key, injected=False):
        # Runs on pynput's thread: only talk to the UI through the queue.
        if injected or key is None:
            return
        if self.capturing:
            self.queue.put(("captured", self.capturing, key_to_data(key)))
            return
        for action in ("record", "play"):
            if key_matches(key, self.settings["hotkey_" + action]):
                if action not in self._hotkeys_down:  # ignore key auto-repeat
                    self._hotkeys_down.add(action)
                    self.queue.put(("hotkey", action))
                return

    def _hotkey_release(self, key, injected=False):
        if injected or key is None:
            return
        for action in ("record", "play"):
            if key_matches(key, self.settings["hotkey_" + action]):
                self._hotkeys_down.discard(action)

    def _is_hotkey(self, key) -> bool:
        return (key_matches(key, self.settings["hotkey_record"])
                or key_matches(key, self.settings["hotkey_play"]))

    def _begin_capture(self, which):
        if self.state != IDLE:
            return
        self.capturing = None if self.capturing == which else which
        self.root.focus_set()
        self._paint_hotkeys()

    def _finish_capture(self, which, data):
        if self.capturing != which:
            return
        self.capturing = None
        other = self.settings["hotkey_" + ("play" if which == "record" else "record")]
        if data is None or data.get("name") == "esc":
            pass
        elif is_modifier(data):
            self.flash("Pick a key that isn't Shift, Ctrl, Alt or Win.")
        elif key_id(data) == key_id(other):
            self.flash("That key is already used by the other hotkey.")
        else:
            self.settings["hotkey_" + which] = data
            save_settings(self.settings)
        self._paint_hotkeys()
        self._refresh()

    def _paint_hotkeys(self):
        for which, btn in (("record", self.hk_record_btn), ("play", self.hk_play_btn)):
            if self.capturing == which:
                btn.configure(text="Press a key…")
                btn.set_style(C["accent"], C["on_color"], C["accent_hover"])
            else:
                btn.configure(text=key_label(self.settings["hotkey_" + which]))
                btn.set_style(C["raised"], C["text"], C["hover"])

    # ---- main loop ------------------------------------------------------

    def _tick(self):
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        self._update_live()
        self.root.after(50, self._tick)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "hotkey" and not self._dialog_open:
            if msg[1] == "record":
                self.toggle_record()
            else:
                self.toggle_play()
        elif kind == "captured":
            self._finish_capture(msg[1], msg[2])
        elif kind == "play_done":
            self._on_play_done(msg[1])

    def _update_live(self):
        if self.state == RECORDING and self.recorder:
            n = len(self.recorder.events)
            self._set_info("Recording  %s  ·  %s events" % (fmt_duration(self.recorder.elapsed()), f"{n:,}"))
            self._set_bar(0)
        elif self.state == PLAYING and self.player:
            p = self.player
            total = "∞" if p.forever else f"{p.repeat:,}"
            self._set_info("Playing loop %s of %s  ·  %g× speed" % (f"{p.loop:,}", total, p.speed))
            self._set_bar(p.progress())

    def _set_info(self, text, color=None):
        if time.monotonic() < self._flash_until:
            return
        self.info.configure(text=text, fg=color or C["muted"])

    def flash(self, text):
        self._flash_until = 0.0
        self._set_info(text, C["amber"])
        self._flash_until = time.monotonic() + 3.0
        self.root.after(3050, self._refresh)

    def _set_bar(self, fraction):
        self.bar.coords(self.bar_fill, 0, 0, int(self._bar_width * fraction), self.px(4))

    def _refresh(self):
        """Repaint everything that depends on the current state."""
        s, state = self.settings, self.state
        rec_hint = key_label(s["hotkey_record"])
        play_hint = key_label(s["hotkey_play"])

        if state == RECORDING:
            self.rec_btn.set("■", "Stop", rec_hint, C["red"], C["on_color"], C["red_hover"],
                             C["on_color"], C["on_color"])
        else:
            self.rec_btn.set("●", "Record", rec_hint, C["raised"], C["text"], C["hover"],
                             C["red"], C["muted"])
        if state == PLAYING:
            self.play_btn.set("■", "Stop", play_hint, C["green"], C["on_color"], C["green_hover"],
                              C["on_color"], C["on_color"])
        else:
            self.play_btn.set("▶", "Play", play_hint, C["raised"], C["text"], C["hover"],
                              C["green"], C["muted"])

        idle = state == IDLE
        self.rec_btn.set_enabled(state in (IDLE, RECORDING, COUNTDOWN))
        self.play_btn.set_enabled(state in (IDLE, PLAYING, COUNTDOWN) and not self.macro.empty)
        for btn in (self.open_btn, self.hk_record_btn, self.hk_play_btn):
            btn.set_enabled(idle)
        self.save_btn.set_enabled(idle and not self.macro.empty)
        self.clear_btn.set_enabled(idle and not self.macro.empty)

        if state == RECORDING:
            self.status.configure(text="● REC", bg=C["red"], fg=C["on_color"])
        elif state == PLAYING:
            self.status.configure(text="▶ PLAYING", bg=C["green"], fg=C["on_color"])
        elif state == COUNTDOWN:
            self.status.configure(text="STARTING IN %d" % self._countdown[1], bg=C["amber"], fg=C["on_color"])
        else:
            self.status.configure(text="READY", bg=C["raised"], fg=C["muted"])

        if idle:
            self._set_bar(0)
            if self.macro.empty:
                self._set_info("Press %s (or Record) and do your thing." % rec_hint)
            else:
                name = "%s  ·  " % self.macro_name if self.macro_name else ""
                self._set_info(name + self.macro.summary())
        elif state == COUNTDOWN:
            what = "Recording" if self._countdown[0] == "record" else "Playback"
            self._set_info("%s starts in %d…" % (what, self._countdown[1]), C["amber"])
        self._update_live()

        title = APP_NAME if not self.macro_name else "%s - %s" % (self.macro_name, APP_NAME)
        self.root.title(title)

    # ---- record / play --------------------------------------------------

    def toggle_record(self):
        if self.state == COUNTDOWN:
            self._cancel_countdown()
        elif self.state == RECORDING:
            self.stop_record()
        elif self.state == IDLE:
            self._start_countdown("record", self._start_record)

    def toggle_play(self):
        if self.state == COUNTDOWN:
            self._cancel_countdown()
        elif self.state == PLAYING:
            self.stop_play()
        elif self.state == IDLE:
            if self.macro.empty:
                self.flash("Nothing to play yet. Record something first.")
                return
            self._start_countdown("play", self._start_play)

    def _start_countdown(self, kind, then):
        self.capturing = None
        self._paint_hotkeys()
        secs = int(self.settings["countdown"])
        if secs <= 0:
            then()
            return
        self.state = COUNTDOWN
        self._countdown = [kind, secs, None, then]
        self._countdown_step()

    def _countdown_step(self):
        kind, left, _job, then = self._countdown
        if left <= 0:
            self._countdown = None
            self.state = IDLE
            then()
            return
        self._refresh()
        self._countdown[1] = left - 1
        self._countdown[2] = self.root.after(1000, self._countdown_step)

    def _cancel_countdown(self):
        if self._countdown and self._countdown[2]:
            self.root.after_cancel(self._countdown[2])
        self._countdown = None
        self.state = IDLE
        self._refresh()

    def _start_record(self):
        self._update_own_rect()
        self.recorder = Recorder(
            record_moves=self.settings["record_moves"],
            record_keys=self.settings["record_keys"],
            ignore_key=self._is_hotkey,
            ignore_point=self._in_own_window,
        )
        try:
            self.recorder.start()
        except Exception as exc:
            self.recorder = None
            self._refresh()
            messagebox.showerror(APP_NAME, "Couldn't start recording:\n\n%s" % exc)
            return
        self.state = RECORDING
        self._refresh()

    def stop_record(self):
        macro = self.recorder.stop()
        self.recorder = None
        self.state = IDLE
        if macro.empty:
            self._refresh()
            self.flash("Nothing was recorded, kept the previous macro.")
            return
        self.macro = macro
        self.macro_name = None
        self._save_last_macro()
        self._refresh()

    def _start_play(self):
        s = self.settings
        self.player = Player(
            self.macro,
            speed=s["speed"],
            repeat=0 if s["forever"] else s["repeat"],
            loop_delay=s["loop_delay"],
            on_finish=lambda p: self.queue.put(("play_done", p)),
        )
        self.state = PLAYING
        self._refresh()
        if s["hide_on_play"]:
            self._hidden_for_play = True
            self.root.iconify()
        self.player.start()

    def stop_play(self):
        if self.player:
            self.player.stop()

    def _on_play_done(self, player):
        if player is not self.player:
            return
        self.player = None
        self.state = IDLE
        if self._hidden_for_play:
            self._hidden_for_play = False
            self.root.deiconify()
            self.root.lift()
        self._refresh()
        if player.error:
            messagebox.showerror(APP_NAME, "Playback stopped because of an error:\n\n%s" % player.error)
        elif player.skipped:
            self.flash("Done. %d step%s couldn't be replayed on this computer."
                       % (player.skipped, "" if player.skipped == 1 else "s"))

    # ---- own window -----------------------------------------------------

    def _on_configure(self, event):
        if event.widget is self.root:
            self._update_own_rect()

    def _update_own_rect(self):
        r = self.root
        try:
            if r.state() == "iconic":
                self._own_rect = (0, 0, 0, 0)
                return
            if sys.platform == "win32":
                import ctypes.wintypes
                rect = ctypes.wintypes.RECT()
                hwnd = ctypes.windll.user32.GetParent(r.winfo_id())
                if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    self._own_rect = (rect.left, rect.top, rect.right, rect.bottom)
                    return
            # winfo_x/y is the outer frame (title bar included), rootx/y the inside.
            left = min(r.winfo_x(), r.winfo_rootx())
            top = min(r.winfo_y(), r.winfo_rooty())
            right = r.winfo_rootx() + r.winfo_width()
            bottom = r.winfo_rooty() + r.winfo_height()
            self._own_rect = (left, top, right, bottom)
        except tk.TclError:
            pass

    def _in_own_window(self, x, y) -> bool:
        left, top, right, bottom = self._own_rect
        return left <= x < right and top <= y < bottom

    # ---- files ----------------------------------------------------------

    def _last_macro_path(self):
        return os.path.join(config_dir(), "last_macro.json")

    def _load_last_macro(self):
        try:
            self.macro = Macro.load(self._last_macro_path())
        except (OSError, ValueError, AttributeError):
            pass

    def _save_last_macro(self):
        try:
            self.macro.save(self._last_macro_path())
        except OSError:
            pass

    def open_macro(self, path=None):
        if self.state != IDLE:
            return
        if not path:
            self._dialog_open = True
            try:
                path = filedialog.askopenfilename(
                    parent=self.root, title="Open macro",
                    filetypes=[("DarkTask macro", "*.json"), ("All files", "*.*")])
            finally:
                self._dialog_open = False
        if not path:
            return
        try:
            macro = Macro.load(path)
        except (OSError, ValueError, AttributeError) as exc:
            messagebox.showerror(APP_NAME, "Couldn't open that file:\n\n%s" % exc)
            return
        if macro.empty:
            messagebox.showwarning(APP_NAME, "That file doesn't contain any macro steps.")
            return
        self.macro = macro
        self.macro_name = os.path.basename(path)
        self._save_last_macro()
        self._refresh()

    def save_macro(self):
        if self.state != IDLE or self.macro.empty:
            return
        self._dialog_open = True
        try:
            path = filedialog.asksaveasfilename(
                parent=self.root, title="Save macro", defaultextension=".json",
                initialfile=self.macro_name or "macro.json",
                filetypes=[("DarkTask macro", "*.json"), ("All files", "*.*")])
        finally:
            self._dialog_open = False
        if not path:
            return
        try:
            self.macro.save(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Couldn't save:\n\n%s" % exc)
            return
        self.macro_name = os.path.basename(path)
        self._refresh()
        self.flash("Saved to %s" % self.macro_name)

    def clear_macro(self):
        if self.state != IDLE or self.macro.empty:
            return
        if not messagebox.askyesno(APP_NAME, "Clear the current macro?", parent=self.root):
            return
        self.macro = Macro()
        self.macro_name = None
        try:
            os.remove(self._last_macro_path())
        except OSError:
            pass
        self._refresh()

    # ---- window position / exit -----------------------------------------

    def _restore_position(self):
        x, y = self.settings.get("x"), self.settings.get("y")
        if x is None or y is None:
            return
        left, top, width, height = virtual_screen(self.root)
        if left <= x <= left + width - 80 and top <= y <= top + height - 80:
            self.root.geometry("+%d+%d" % (x, y))

    def quit(self):
        if self.recorder:
            self.recorder.stop()
        if self.player:
            self.player.stop()
            self.player.join(1.0)
        try:
            if self.root.state() == "normal":
                self.settings["x"], self.settings["y"] = self.root.winfo_x(), self.root.winfo_y()
        except tk.TclError:
            pass
        save_settings(self.settings)
        self._hotkey_listener.stop()
        self.root.destroy()


# --------------------------------------------------------------------------
# Platform bits
# --------------------------------------------------------------------------

def virtual_screen(root):
    """(left, top, width, height) covering every monitor."""
    if sys.platform == "win32":
        try:
            m = ctypes.windll.user32.GetSystemMetrics
            return m(76), m(77), m(78), m(79)
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def dark_title_bar(root):
    """Ask Windows 10/11 for a dark title bar to match the window."""
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
        # Nudge Windows into repainting the frame straight away.
        root.withdraw()
        root.deiconify()
    except Exception:
        pass


def make_icon(size=32):
    """A red record dot on a dark tile, drawn in code so there's no image file."""
    img = tk.PhotoImage(width=size, height=size)
    c = (size - 1) / 2
    for y in range(size):
        row = []
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            if d <= size * 0.28:
                row.append(C["red"])
            elif d <= size * 0.34:
                row.append("#5a2530")
            else:
                row.append(C["card"])
        img.put("{" + " ".join(row) + "}", to=(0, y))
    return img


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    root = tk.Tk()
    if _PYNPUT_ERROR is not None:
        root.withdraw()
        messagebox.showerror(
            APP_NAME,
            "DarkTask needs the 'pynput' package to see and control the mouse and keyboard.\n\n"
            "Install it with:\n    pip install pynput\n\nDetails: %s" % _PYNPUT_ERROR)
        return 1
    root.withdraw()

    def report(exc, val, tb):
        # With no console (the .exe, or pythonw) errors would vanish silently.
        messagebox.showerror(APP_NAME, "".join(traceback.format_exception(exc, val, tb))[-1500:])
    root.report_callback_exception = report

    try:
        App(root, open_path=argv[0] if argv else None)
    except Exception:
        report(*sys.exc_info())
        return 1
    root.deiconify()
    dark_title_bar(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
