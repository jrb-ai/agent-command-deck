#!/usr/bin/env python3
# Copyright 2026 Jon Regan-Beasley
# SPDX-License-Identifier: GPL-2.0-or-later
"""bnk9d - host daemon for the Binepad BNK9 agent knob.

Talks to the pad over raw HID (custom QMK firmware, see keyboards/...),
maps the six agent keys to Terminal.app windows via AppleScript, drives
the per-key LEDs, and listens on a unix socket for state reports from
agent hooks (bnk9ctl) and the PTY wrapper (bnk9-run).

Key ids from firmware: 0=knob push, 1-6 agent keys, 7 accept, 8 cancel,
9 action (types a configurable slash command). LED indices 0-8 are keys 1-9 in reading order.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import hid

VENDOR_ID = 0x4249
PRODUCT_ID = 0x4E39
USAGE_PAGE = 0xFF60
USAGE = 0x61
REPORT_LEN = 32

BNK9_DIR = Path.home() / ".bnk9"
CONFIG_PATH = BNK9_DIR / "config.json"
DEFAULTS_PATH = BNK9_DIR / "config.default.json"
STATE_PATH = BNK9_DIR / "state.json"
SOCKET_PATH = BNK9_DIR / "bnk9d.sock"
LOG_PATH = BNK9_DIR / "bnk9d.log"

LED_OFF, LED_SOLID, LED_FLASH_SLOW, LED_FLASH_FAST, LED_BREATHE = 0, 1, 2, 3, 4
ANIM_NONE, ANIM_ULTRACODE, ANIM_RAINBOW = 0, 1, 2
ANIM_BY_NAME = {"none": ANIM_NONE, "ultracode": ANIM_ULTRACODE,
                "rainbow": ANIM_RAINBOW}

DEFAULT_CONFIG = {
    # DEBUG also logs hook reports from terminals no key is watching,
    # which is how you tell "the hook never fired" from "it fired and
    # the daemon ignored it".
    "log_level": "INFO",
    "brightness": 160,
    "idle_dim": 0.12,
    # Knob gauge, one step per top key. Applied when a session is
    # launched, via {effort_args} in the slot command. "max" is CLI-only
    # (settings.json rejects it) and ultracode is session-scoped, which
    # is why these go on the command line rather than into settings.
    "effort_steps": [
        {"label": "low", "effort": "low"},
        {"label": "medium", "effort": "medium"},
        {"label": "high", "effort": "high"},
        {"label": "xhigh", "effort": "xhigh"},
        {"label": "max", "effort": "max", "anim": "rainbow"},
        {"label": "ultracode", "effort": "xhigh", "ultracode": True,
         "anim": "ultracode"},
    ],
    "effort_gauge_color": [80, 140, 255],
    # Apps ACCEPT/CANCEL may type into. Add your terminal here if you
    # switch away from Terminal.app (e.g. "iTerm2", "Ghostty").
    "accept_apps": ["Terminal"],
    "hold_ms": 800,
    # Knob pulses required per action (1-16). Scrolling wants to stay
    # responsive, so it runs undivided; the effort gauge is a choice
    # rather than a movement, so it takes three pulses per step.
    "encoder_divider": {"scroll": 1, "effort": 3},
    # Move the pointer into a window when the pad focuses it, so the
    # knob scrolls the session you just selected.
    "warp_cursor_on_focus": True,
    # Seconds before "finished, your turn" stops signalling by itself.
    # 0 means it keeps breathing until you press the key or respond,
    # which is the point of the indicator.
    "input_flash_secs": 0,
    "accept_color": [0, 200, 0],
    "cancel_color": [220, 0, 0],
    "action_color": [150, 0, 255],
    # Every key is generic. Its colour comes from whatever is actually
    # running in the session assigned to it.
    "session_colors": {
        "claude":   [255, 110, 0],
        "codex":    [0, 150, 255],
        "hermes":   [0, 230, 140],
        "ollama":   [230, 230, 230],
        "openclaw": [255, 210, 0],
        "other":    [255, 0, 130],
    },
    "free_color": [90, 90, 110],
    # Tapping an unassigned key starts this.
    "spawn_command": "claude {effort_args}",
    "spawn_cwd": "~",
    # Keep every agent in one Terminal window, as tabs.
    "spawn_in_tab": True,
    # Slash commands per session type. A template without {level} sets
    # nothing by itself - Codex has no argument form for reasoning level,
    # so /model opens its chooser and you pick from there. null means the
    # agent has no equivalent at all and the pad stays out of the way.
    "session_commands": {
        "claude": {"action": "/usage", "effort": "/effort {level}"},
        "codex": {
            # /usage is a 12-month summary; /status shows the limits that
            # actually matter mid-session.
            "action": "/status",
            # Codex has no argument form for reasoning level, so the
            # chooser is opened and then driven. Tune this list if the
            # chooser's layout differs: "key:<name>" presses a key,
            # "wait:<secs>" pauses, anything else is typed.
            "effort": "/model",
            # Drives the chooser: /model opens "Select Model", where 2 is
            # "All models"; Enter there keeps the model you are already
            # on; that lands on the reasoning list. Digits select outright
            # on these lists - no Enter, which is what makes it safe. The
            # highlight starts on the current level, so a digit is used
            # rather than arrow counts.
            # Each chooser screen renders in about 10ms, so these waits
            # only cover keystroke delivery, not the interface.
            "effort_sequence": [
                "wait:0.3", "2",
                "wait:0.25", "key:enter",
                "wait:0.3", "{level}",
            ],
            # 1 Low, 2 Medium, 3 High, 4 Extra high, 5 More reasoning…
            # and behind that submenu, 1 Max and 2 Ultra.
            "level_map": {
                "low": ["1"], "medium": ["2"], "high": ["3"],
                "xhigh": ["4"],
                "max": ["5", "wait:0.25", "1"],
                "ultracode": ["5", "wait:0.25", "2"],
            },
        },
        "openclaw": {"action": "/usage", "effort": None},
    },
}

log = logging.getLogger("bnk9d")


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_quartz():
    """ApplicationServices, for moving the mouse pointer."""
    path = ctypes.util.find_library("ApplicationServices")
    if path is None:
        return None
    try:
        lib = ctypes.cdll.LoadLibrary(path)
    except OSError:
        return None
    lib.CGWarpMouseCursorPosition.argtypes = [_CGPoint]
    lib.CGWarpMouseCursorPosition.restype = ctypes.c_int
    return lib


_quartz = _load_quartz()


# --------------------------------------------------------------------------
# AppleScript helpers
# --------------------------------------------------------------------------

def osascript(script: str, timeout: int = 15) -> str | None:
    """Run an AppleScript, returning stdout or None on failure."""
    try:
        proc = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("osascript timed out")
        return None
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        if "1002" in stderr or "not allowed to send keystrokes" in stderr:
            log.error(
                "Accessibility permission missing: grant it to the program "
                "running this daemon (System Settings > Privacy & Security > "
                "Accessibility). Foreground runs need Terminal; the launchd "
                "agent needs %s", sys.executable)
        elif stderr:
            log.warning("osascript failed: %s", stderr)
        return None
    return proc.stdout.strip()


def osa_quote(text: str) -> str:
    """Escape a string for embedding in an AppleScript double-quoted literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def slot_marker(slot: int) -> str:
    return f"BNK9-{slot}"


def terminal_spawn(shell_cmd: str, title: str,
                   in_tab: bool = True) -> str | None:
    """Run `shell_cmd` in Terminal and return its tty.

    Opens a tab in the existing window by default, so every agent lives
    in one Terminal. A bare `do script` always makes a new window, so
    the tab is created with Cmd-T and the command sent into it.
    """
    quoted = osa_quote(shell_cmd)
    if not in_tab:
        script = f'''
        tell application "Terminal"
            activate
            set newTab to do script "{quoted}"
            set custom title of newTab to "{osa_quote(title)}"
            return tty of newTab
        end tell'''
        return osascript(script) or None
    # Terminal has no AppleScript verb for "new tab", so the tab is made
    # with Cmd-T (Terminal must be frontmost first, hence the delay after
    # activate). Whether that yields a tab or a window is governed by the
    # macOS "prefer tabs" setting, not by us. Either way the busy check
    # matters: if no new session appeared, the selected tab is still
    # running an agent and the command must not be typed into it.
    script = f'''
    tell application "Terminal" to activate
    delay 0.3
    tell application "System Events"
        tell process "Terminal" to keystroke "t" using command down
    end tell
    delay 0.5
    tell application "Terminal"
        set newTab to selected tab of front window
        if busy of newTab then
            set newTab to do script "{quoted}"
        else
            do script "{quoted}" in newTab
        end if
        set custom title of newTab to "{osa_quote(title)}"
        return tty of newTab
    end tell'''
    return osascript(script) or None


def frontmost_tty() -> str | None:
    """tty of the Terminal session you are currently working in."""
    script = '''
    tell application "System Events"
        set p to name of first application process whose frontmost is true
    end tell
    if p is not "Terminal" then return ""
    tell application "Terminal"
        try
            return tty of selected tab of front window
        end try
    end tell
    return ""'''
    return osascript(script) or None


def live_ttys() -> set[str] | None:
    """Every open Terminal tty, or None if the query failed."""
    script = '''
    tell application "Terminal"
        set out to ""
        set failed to false
        repeat with w in windows
            try
                repeat with t in tabs of w
                    set out to out & (tty of t) & linefeed
                end repeat
            on error
                set failed to true
            end try
        end repeat
        if failed then return "PARTIAL"
        return out
    end tell'''
    out = osascript(script)
    # A window closing mid-enumeration used to yield a short list, which
    # read as "those sessions are gone" and freed live slots.
    if out is None or out == "PARTIAL":
        return None
    return {line.strip() for line in out.splitlines() if line.strip()}


def focus_tty(tty: str) -> None:
    """Bring the tab owning `tty` to the front, selecting it in its window."""
    script = f'''
    tell application "Terminal"
        repeat with w in windows
            try
                repeat with t in tabs of w
                    if tty of t is "{osa_quote(tty)}" then
                        set selected tab of w to t
                        set index of w to 1
                        activate
                        return "ok"
                    end if
                end repeat
            end try
        end repeat
    end tell
    return "no"'''
    osascript(script)


def front_window_center() -> tuple[float, float] | None:
    """Centre of Terminal's front window, in screen coordinates."""
    script = '''
    tell application "Terminal"
        try
            set b to bounds of front window
            set cx to ((item 1 of b) + (item 3 of b)) / 2
            set cy to ((item 2 of b) + (item 4 of b)) / 2
            return (cx as text) & "," & (cy as text)
        end try
    end tell
    return ""'''
    out = osascript(script)
    if not out or "," not in out:
        return None
    try:
        x, y = (float(v) for v in out.split(",", 1))
    except ValueError:
        return None
    return x, y


def warp_cursor(x: float, y: float) -> bool:
    """Move the mouse pointer, so scroll events land on this window.

    macOS delivers scroll wheel events to whatever sits under the
    pointer, not to the focused window, so the knob would otherwise keep
    scrolling whichever window the mouse was left over.
    """
    if _quartz is None:
        return False
    try:
        _quartz.CGWarpMouseCursorPosition(_CGPoint(x, y))
    except OSError:
        return False
    return True


SESSION_PATTERNS = (
    ("openclaw", "openclaw"),
    ("claude", "claude"),
    ("codex", "codex"),
    ("hermes", "hermes"),
    ("ollama", "ollama"),
)

# Programs that run the agent as a script argument rather than being it,
# e.g. "node /opt/homebrew/bin/claude" for an npm install.
INTERPRETERS = {"node", "nodejs", "bun", "deno", "python", "python3",
                "env", "npx", "uv", "uvx", "pipx"}
# Words that sit between a runner and the program, as in "uv run hermes".
RUNNER_VERBS = {"run", "exec", "x", "tool"}


def classify_command(args: str) -> str:
    """Which agent a command line is running, judged by the program alone.

    Only the program name counts, never its arguments: "vim CLAUDE.md" or
    a shell in ~/claude-projects is not an agent, and typing a slash
    command plus Return into one of those is exactly what the check is
    there to prevent. Interpreters and the bnk9-run wrapper are looked
    through to the program they run.
    """
    words = args.split()
    i = 0
    while i < len(words):
        name = os.path.basename(words[i]).lower()
        if name.replace(".", "").isdigit():
            # A versioned install run by path, ".../claude/versions/2.1.0":
            # the program is named by the directory above "versions".
            name = os.path.basename(os.path.dirname(
                os.path.dirname(words[i]))).lower()
        if name == "bnk9-run" and "--" in words[i:]:
            i = words.index("--", i) + 1
            continue
        if name in INTERPRETERS or name.startswith("python3."):
            # Skip the interpreter's own options, any "VAR=value" given
            # to env, and a runner's verb, to reach the program itself.
            i += 1
            while i < len(words) and (words[i].startswith("-")
                                      or "=" in words[i]):
                i += 1
            if i < len(words) and words[i] in RUNNER_VERBS:
                i += 1
            continue
        for pattern, kind in SESSION_PATTERNS:
            if pattern in name:
                return kind
        return "other"
    return "other"


def live_session_types() -> dict[str, str]:
    """Map each terminal's tty to the kind of agent running in it.

    Keyed off the foreground job leader - the process whose pid equals
    its process-group id, with a "+" in its state flags. Agents host
    each other's helpers (Claude Code runs a codex MCP server), so
    matching any process on the tty mislabels the session.
    """
    try:
        out = subprocess.run(["ps", "-Ao", "tty=,pid=,pgid=,stat=,args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not read process list: %s", exc)
        return {}
    types = {}
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        tty, pid, pgid, stat, args = parts
        if tty == "??" or pid != pgid or "+" not in stat:
            continue
        types[f"/dev/{tty}"] = classify_command(args)
    return types


def tty_has_processes(tty: str) -> bool:
    """Whether anything still has `tty` as its controlling terminal.

    A second opinion on whether a session is really gone. Terminal's own
    tab list occasionally comes back short, and acting on that alone
    unassigns keys whose sessions are still very much alive.
    """
    name = tty.rsplit("/", 1)[-1]
    try:
        out = subprocess.run(["ps", "-t", name, "-o", "pid="],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # an unanswered question is not evidence of death
    return bool(out.strip())


def frontmost_app() -> str:
    """Name of the frontmost application process, or "" if unknown."""
    script = '''
    tell application "System Events"
        return name of first application process whose frontmost is true
    end tell'''
    return osascript(script) or ""


def send_key_code(code: int) -> None:
    osascript(f'tell application "System Events" to key code {code}')


# Re-entrant: a scripted sequence holds it across several keystrokes
# while the helpers it calls take it again.
_type_lock = threading.RLock()

KEY_CODES = {"enter": 36, "esc": 53, "tab": 48, "space": 49,
             "up": 126, "down": 125, "left": 123, "right": 124}


def type_text(text: str) -> bool:
    """Type `text` into the frontmost app without pressing Return."""
    script = f'''
    tell application "System Events"
        keystroke "{osa_quote(text)}"
    end tell
    return "ok"'''
    with _type_lock:
        return osascript(script) == "ok"


def run_sequence(steps: list[str], level_steps: list[str] | str = ()) -> None:
    """Play a scripted series of keystrokes into the frontmost app.

    Steps are "key:<name>" for a named key ("key:<name>*<n>" to repeat
    it), "wait:<seconds>" to let the interface catch up, or literal text
    to type. A step of "{level}" expands to `level_steps`, which is a
    list so one level can need several presses.
    Held under one lock so nothing interleaves midway through.
    """
    if isinstance(level_steps, str):
        level_steps = [level_steps]
    expanded = []
    for step in steps:
        expanded.extend(level_steps if step == "{level}" else [step])
    with _type_lock:
        for step in expanded:
            if step.startswith("key:"):
                name, _, times = step[4:].partition("*")
                code = KEY_CODES.get(name)
                if code is None:
                    log.warning("unknown key in sequence: %s", step)
                    continue
                try:
                    count = max(1, min(int(times), 30)) if times else 1
                except ValueError:
                    count = 1
                for _ in range(count):
                    send_key_code(code)
                    time.sleep(0.04)
            elif step.startswith("wait:"):
                try:
                    time.sleep(min(float(step[5:]), 3.0))
                except ValueError:
                    log.warning("bad wait in sequence: %s", step)
            else:
                type_text(step)


def type_line(text: str) -> bool:
    """Type `text` into the frontmost app and press Return.

    Serialised: typing is not atomic, so two concurrent callers would
    interleave into one line ("/effort ultracode/effort max").
    """
    script = f'''
    tell application "System Events"
        keystroke "{osa_quote(text)}"
        delay 0.15
        key code 36
    end tell
    return "ok"'''
    with _type_lock:
        return osascript(script) == "ok"


# --------------------------------------------------------------------------
# Effort level (pad-local; applied at launch, never written to global settings)
# --------------------------------------------------------------------------

def read_effort_idx(default: int) -> int:
    try:
        return int(json.loads(STATE_PATH.read_text())["effort_idx"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return default


def write_effort_idx(idx: int) -> None:
    try:
        STATE_PATH.write_text(json.dumps({"effort_idx": idx}) + "\n")
    except OSError as exc:
        log.warning("could not persist effort level: %s", exc)


def step_anim(step: dict) -> int:
    """Overlay animation a gauge step asks for.

    An explicit "anim" wins; otherwise the two steps that are special
    enough to warrant one are inferred, so configs written before this
    existed still light up.
    """
    name = step.get("anim")
    if name is not None:
        return ANIM_BY_NAME.get(name, ANIM_NONE)
    if step.get("ultracode"):
        return ANIM_ULTRACODE
    if step.get("effort") == "max":
        return ANIM_RAINBOW
    return ANIM_NONE


def effort_args(step: dict) -> str:
    """Command-line arguments that put a Claude session at this step."""
    args = f"--effort {step['effort']}"
    if step.get("ultracode"):
        args += " --settings " + shlex.quote(json.dumps({"ultracode": True}))
    return args


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------

class Daemon:
    # Slot states -> LED behaviour
    STATE_LED = {
        "empty":   ("dim",  LED_SOLID),
        "running": ("full", LED_SOLID),
        "input":   ("full", LED_BREATHE),
        "waiting": ("full", LED_FLASH_FAST),
        "ended":   (None,   LED_OFF),
    }

    def __init__(self, config: dict):
        self.cfg = config
        self.device = None
        self.hid_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.slot_state = {n: "empty" for n in range(1, 7)}
        # Slots are identified by the tty of their terminal session, not
        # by window title: agents rewrite titles constantly, so a title
        # marker does not survive.
        self.slot_tty: dict[int, str] = {}
        # What each assigned slot is running, for its colour.
        self.slot_type: dict[int, str] = {}
        self.effort_steps = config["effort_steps"][:6]
        self.effort_idx = read_effort_idx(min(3, len(self.effort_steps) - 1))
        self.effort_mode = False
        self.effort_dirty = False
        self.effort_seq = 0
        self.action_held = False
        self.hold_lock = threading.Lock()
        self.hold_timers: dict[int, threading.Timer] = {}
        self.hold_consumed: set[int] = set()
        self.anim = ANIM_NONE
        # Consecutive polls in which a slot's session looked gone.
        self.missing: dict[int, int] = {}
        self.input_timers: dict[int, threading.Timer] = {}
        # Slots whose current attention state you have already seen.
        # Acknowledged slots render solid instead of flashing.
        self.ack: set[int] = set()
        # Terminals currently showing the action command's panel, so the
        # key can dismiss what it opened.
        self.action_open: set[str] = set()
        self.stop = threading.Event()

    # ---- HID ----------------------------------------------------------

    def open_device(self) -> bool:
        for info in hid.enumerate(VENDOR_ID, PRODUCT_ID):
            if info["usage_page"] == USAGE_PAGE and info["usage"] == USAGE:
                try:
                    dev = hid.device()
                    dev.open_path(info["path"])
                    self.device = dev
                    log.info("connected to BNK9 raw HID")
                    return True
                except OSError as exc:
                    log.warning("open failed: %s", exc)
        return False

    def send_report(self, payload: bytes) -> None:
        report = bytes([0x00]) + payload.ljust(REPORT_LEN, b"\x00")
        with self.hid_lock:
            if self.device is None:
                return
            try:
                self.device.write(report)
            except OSError as exc:
                log.warning("HID write failed: %s", exc)
                self.drop_device()

    def drop_device(self) -> None:
        if self.device is not None:
            try:
                self.device.close()
            except OSError:
                pass
            self.device = None

    def set_led(self, idx: int, rgb, mode: int) -> None:
        r, g, b = (int(c) for c in rgb)
        self.send_report(bytes([0x10, idx, r, g, b, mode]))

    # ---- LED state rendering -------------------------------------------

    def dim(self, rgb):
        f = self.cfg["idle_dim"]
        return [int(c * f) for c in rgb]

    def push_slot_led(self, slot: int) -> None:
        if self.effort_mode and slot <= 6:
            return  # gauge overlay owns the top keys
        state = self.slot_state[slot]
        if state == "empty":
            color = self.cfg["free_color"]
        else:
            kind = self.slot_type.get(slot, "other")
            color = self.cfg["session_colors"].get(
                kind, self.cfg["session_colors"]["other"])
        level, mode = self.STATE_LED[state]
        if slot in self.ack and mode in (LED_FLASH_SLOW, LED_FLASH_FAST,
                                         LED_BREATHE):
            mode = LED_SOLID
        if level is None:
            self.set_led(slot - 1, (0, 0, 0), LED_OFF)
        else:
            self.set_led(slot - 1, self.dim(color) if level == "dim" else color, mode)

    def push_bottom_row(self) -> None:
        self.set_led(6, self.cfg["accept_color"], LED_SOLID)
        self.set_led(7, self.cfg["cancel_color"], LED_SOLID)
        mode = LED_FLASH_FAST if self.action_held else LED_SOLID
        self.set_led(8, self.cfg["action_color"], mode)

    def set_anim(self, anim: int) -> None:
        if anim != self.anim:
            self.anim = anim
            self.send_report(bytes([0x22, anim]))

    def push_gauge(self) -> None:
        # Some steps hand the six agent keys to a firmware animation
        # instead of drawing a gauge on them.
        anim = step_anim(self.effort_steps[self.effort_idx])
        if anim != ANIM_NONE:
            self.set_anim(anim)
            return
        self.set_anim(ANIM_NONE)
        color = self.cfg["effort_gauge_color"]
        for i in range(6):
            if i >= len(self.effort_steps):
                self.set_led(i, (0, 0, 0), LED_OFF)
            else:
                self.set_led(i, color if i <= self.effort_idx else self.dim(color),
                             LED_SOLID)

    def push_all(self) -> None:
        self.send_report(bytes([0x20, self.cfg["brightness"]]))
        div = self.cfg["encoder_divider"]
        self.send_report(bytes([0x23,
                                max(1, min(16, int(div.get("scroll", 1)))),
                                max(1, min(16, int(div.get("effort", 1))))]))
        self.anim = -1  # a reconnected pad has forgotten its overlay
        if self.effort_mode:
            self.push_gauge()
        else:
            self.set_anim(ANIM_NONE)
            for slot in range(1, 7):
                self.push_slot_led(slot)
        self.push_bottom_row()

    # ---- slot actions ---------------------------------------------------

    def slot_pressed(self, slot: int) -> None:
        with self.state_lock:
            state = self.slot_state[slot]
            tty = self.slot_tty.get(slot)
            if state == "empty":
                # Claim before the (slow) AppleScript call so a double
                # press can't spawn two windows.
                self.slot_state[slot] = "running"
        if state != "empty":
            if tty is None:
                # Occupied but untracked: either a spawn is still in
                # flight or we lost the window. Either way a second
                # session is the last thing wanted here.
                log.info("slot %d is busy but untracked; ignoring press", slot)
                return
            self.acknowledge(slot)
            focus_tty(tty)
            self.point_at_front_window()
            return
        self.push_slot_led(slot)
        step = self.effort_steps[self.effort_idx]
        command = (self.cfg["spawn_command"]
                   .replace("{effort_args}", effort_args(step))
                   .replace("{effort}", step["effort"]))
        cwd = str(Path(self.cfg["spawn_cwd"]).expanduser())
        shell_cmd = (f"export BNK9_SLOT={slot}; "
                     f"cd {shlex.quote(cwd)}; {command}")
        # A first guess at the colour; refresh_types corrects it once
        # the program is actually running.
        kind = classify_command(command)
        log.info("spawning slot %d: %s (%s)", slot, kind, step["label"])
        tty = terminal_spawn(shell_cmd, f"{slot_marker(slot)} · {kind}",
                             in_tab=self.cfg["spawn_in_tab"])
        if tty is None:
            log.error("spawn failed for slot %d", slot)
            self.set_state(slot, "empty")
            return
        with self.state_lock:
            self.slot_tty[slot] = tty
            self.slot_type[slot] = kind
        self.point_at_front_window()
        self.push_slot_led(slot)

    def set_state(self, slot: int, state: str, reminder: bool = False) -> None:
        """Record a slot's state.

        A `reminder` is the periodic "still waiting on you" nudge rather
        than something new happening, so it leaves a dismissal you have
        already made in place.
        """
        if state not in self.STATE_LED:
            log.warning("unknown state %r for slot %d", state, slot)
            return
        with self.state_lock:
            if self.slot_state[slot] != state:
                log.info("slot %d: %s -> %s", slot, self.slot_state[slot], state)
            self.slot_state[slot] = state
            if state == "empty":
                self.slot_tty.pop(slot, None)
                self.slot_type.pop(slot, None)
            timer = self.input_timers.pop(slot, None)
            # Something new is a new thing to notice, so it flashes
            # again; a mere reminder is not.
            if not reminder:
                self.ack.discard(slot)
                self.action_open.discard(self.slot_tty.get(slot))
            secs = self.cfg["input_flash_secs"]
            if state == "input" and secs:
                self.input_timers[slot] = threading.Timer(
                    secs, self.settle_input, args=(slot,))
                self.input_timers[slot].start()
        if timer is not None:
            timer.cancel()
        self.push_slot_led(slot)

    def resolve_slot(self, msg: dict) -> int | None:
        """Which slot a hook report belongs to.

        Pad-launched sessions name their slot directly. Everything else
        is matched on the tty the hook ran under, which is how adopted
        sessions report state despite never inheriting BNK9_SLOT.
        Reports from untracked terminals resolve to nothing and are
        dropped.
        """
        tty = msg.get("tty")
        with self.state_lock:
            if tty:
                match = next((s for s, t in self.slot_tty.items() if t == tty),
                             None)
                if match is not None:
                    return match
            if "slot" not in msg:
                if tty:
                    log.debug("report for untracked terminal %s: %s",
                              tty, msg.get("state"))
                return None
            try:
                slot = int(msg["slot"])
            except (TypeError, ValueError):
                return None
            if not 1 <= slot <= 6:
                return None
            known = self.slot_tty.get(slot)
            if known is None:
                # A free slot stays free: old sessions keep exporting the
                # slot that launched them, and one of those must not be
                # able to claim a key you have not pressed.
                if self.slot_state[slot] == "empty":
                    log.debug("report for free slot %d (%s): %s",
                              slot, tty, msg.get("state"))
                    return None
                # Occupied but untracked means the window query came back
                # short earlier. Re-learn rather than strand the session.
                if tty:
                    self.slot_tty[slot] = tty
                    log.info("slot %d re-matched to %s", slot, tty)
                return slot
            if tty and known != tty:
                # An older session still exporting a slot that has since
                # been pointed somewhere else.
                return None
            return slot

    def settle_input(self, slot: int) -> None:
        """Stop the "your turn" flash once it has had time to be noticed."""
        with self.state_lock:
            self.input_timers.pop(slot, None)
            if self.slot_state[slot] != "input":
                return
            self.ack.add(slot)
        self.push_slot_led(slot)

    def acknowledge(self, slot: int) -> None:
        """Pressing a slot's key means you have seen it: stop flashing.

        Deliberately not triggered by merely having the window in front
        of you. A turn usually ends while you are sitting in that
        window, so auto-acknowledging on focus cancelled the signal
        before it could be read. Responding ends it anyway, by moving
        the slot to "running".
        """
        with self.state_lock:
            if slot in self.ack:
                return
            self.ack.add(slot)
            timer = self.input_timers.pop(slot, None)
        if timer is not None:
            timer.cancel()
        self.push_slot_led(slot)

    # ---- key events ------------------------------------------------------

    def on_key(self, key_id: int, pressed: bool) -> None:
        if key_id == 9:
            self.action_held = pressed
            self.push_bottom_row()
            if pressed:
                threading.Thread(target=self.action_key, daemon=True).start()
            return
        if 1 <= key_id <= 6:
            self.on_slot_key(key_id, pressed)
            return
        if not pressed:
            return
        if key_id == 7:
            threading.Thread(target=self.guarded_key, args=(36,),
                             daemon=True).start()
        elif key_id == 8:
            threading.Thread(target=self.guarded_key, args=(53,),
                             daemon=True).start()

    def on_slot_key(self, slot: int, pressed: bool) -> None:
        """Agent keys: pick an effort step, or tap to open / hold to adopt."""
        if pressed:
            if self.effort_mode:
                # Mark the press as consumed: selecting a level clears
                # effort mode, so without this the release would no
                # longer look like an effort press and would launch.
                with self.hold_lock:
                    self.hold_consumed.add(slot)
                self.select_effort(slot - 1)
                return
            timer = threading.Timer(self.cfg["hold_ms"] / 1000,
                                    self.hold_fired, args=(slot,))
            with self.hold_lock:
                self.hold_timers[slot] = timer
                self.hold_consumed.discard(slot)
            timer.start()
            return
        # Release: cancel the pending hold; if it already fired, the tap
        # action must not also run.
        with self.hold_lock:
            timer = self.hold_timers.pop(slot, None)
            consumed = slot in self.hold_consumed
            self.hold_consumed.discard(slot)
        if timer is not None:
            timer.cancel()
        if not consumed:
            threading.Thread(target=self.slot_pressed, args=(slot,),
                             daemon=True).start()

    def hold_fired(self, slot: int) -> None:
        """Hold threshold reached: adopt the frontmost terminal window."""
        with self.hold_lock:
            self.hold_timers.pop(slot, None)
            self.hold_consumed.add(slot)
        self.adopt(slot)

    def select_effort(self, idx: int) -> None:
        """Pick an effort step with an agent key, then close the gauge.

        Closing it immediately matters: the gauge would otherwise stay up
        for a few seconds and swallow the very next key press as another
        effort selection, so launching an agent straight after choosing a
        level would silently reset that level to the launch key's step.
        """
        if not 0 <= idx < len(self.effort_steps):
            return
        self.effort_idx = idx
        self.effort_seq += 1
        self.effort_dirty = False
        write_effort_idx(idx)
        log.info("effort for new sessions -> %s", self.effort_steps[idx]["label"])
        self.effort_mode = False
        self.set_anim(ANIM_NONE)
        for slot in range(1, 7):
            self.push_slot_led(slot)
        threading.Thread(
            target=self.send_slash, args=("effort", self.effort_seq),
            kwargs={"level": self.effort_steps[idx]["label"]},
            daemon=True).start()

    def point_at_front_window(self) -> None:
        """Put the pointer in the front window so the knob scrolls it."""
        if not self.cfg["warp_cursor_on_focus"]:
            return
        center = front_window_center()
        if center is None:
            return
        if not warp_cursor(*center):
            log.warning("could not move the pointer; knob will scroll "
                        "whatever window it is already over")

    def frontmost_session(self, why: str) -> tuple[str | None, str]:
        """The terminal you are looking at, and what is running in it.

        Eligibility comes from what the terminal is actually running,
        not from whether it is assigned to a key: a slash command is
        safe to type into an agent prompt and nowhere else, so a plain
        shell or a password prompt reports as "other" and gets nothing.
        """
        tty = frontmost_tty()
        if tty is None:
            log.info("%s skipped: no frontmost Terminal session", why)
            return None, "other"
        kind = live_session_types().get(tty)
        if kind is None:
            with self.state_lock:
                slot = next((s for s, t in self.slot_tty.items() if t == tty),
                            None)
                kind = self.slot_type.get(slot, "other")
        return tty, kind

    def command_for(self, kind: str, key: str) -> str | None:
        """The slash command `key` maps to for this kind of session."""
        return (self.cfg["session_commands"].get(kind) or {}).get(key)

    def send_slash(self, key: str, seq: int | None = None, **fields) -> None:
        """Type a slash command into the session you are looking at.

        `seq` guards against a stale effort level: spinning the knob
        several steps queues several of these, and only the level you
        settled on should reach the session.
        """
        tty, kind = self.frontmost_session(key)
        if tty is None:
            return
        template = self.command_for(kind, key)
        if not template:
            log.info("%s: a %s session has no equivalent command", key, kind)
            return
        if seq is not None and seq != self.effort_seq:
            log.info("%s superseded before it was sent", key)
            return
        spec = self.cfg["session_commands"].get(kind) or {}
        command = template.format(**fields)
        level_steps = spec.get("level_map", {}).get(fields.get("level"),
                                                    fields.get("level", ""))
        if not type_line(command):
            log.warning("failed to send %s to %s", command, tty)
            return
        sequence = spec.get(f"{key}_sequence")
        if sequence:
            # The agent has no argument form, so the chooser it just
            # opened has to be driven to reach the level.
            run_sequence(sequence, level_steps)
            log.info("sent %s and drove its chooser to %s on %s (%s)",
                     command, fields.get("level"), tty, kind)
        elif key == "effort" and "{level}" not in template:
            log.info("opened %s on %s (%s): pick the level there",
                     command, tty, kind)
        else:
            log.info("sent %s to %s (%s)", command, tty, kind)

    def action_key(self) -> None:
        """Toggle the action command: run it, press again to dismiss it."""
        tty, kind = self.frontmost_session("action")
        if tty is None:
            return
        command = self.command_for(kind, "action")
        if not command:
            log.info("action: a %s session has no equivalent command", kind)
            return
        with self.state_lock:
            showing = tty in self.action_open
            if showing:
                self.action_open.discard(tty)
            else:
                self.action_open.add(tty)
        if showing:
            send_key_code(53)  # Escape dismisses the panel
            log.info("dismissed %s on %s", command, tty)
        elif type_line(command):
            log.info("sent %s to %s (%s)", command, tty, kind)
        else:
            with self.state_lock:
                self.action_open.discard(tty)
            log.warning("failed to send %s to %s", command, tty)

    def adopt(self, slot: int) -> None:
        """Bind the terminal session you are already working in to a slot."""
        tty = frontmost_tty()
        if tty is None:
            log.warning("adopt ignored: no frontmost Terminal session "
                        "(Terminal.app must be the active app)")
            return
        kind = live_session_types().get(tty, "other")
        with self.state_lock:
            # Taking a session from another slot leaves that one free.
            for other, other_tty in list(self.slot_tty.items()):
                if other_tty == tty and other != slot:
                    self.slot_tty.pop(other)
                    self.slot_type.pop(other, None)
                    self.slot_state[other] = "empty"
                    log.info("slot %d released %s", other, tty)
            self.slot_tty[slot] = tty
            self.slot_type[slot] = kind
        # The running agent never inherited BNK9_SLOT; its hooks still
        # report the tty they run under, which now maps to this slot.
        log.info("slot %d adopted %s (%s)", slot, tty, kind)
        self.set_state(slot, "running")
        self.point_at_front_window()
        self.push_all()

    def guarded_key(self, code: int) -> None:
        """Send Return/Escape, but only into a terminal application.

        Scoping this to terminals (rather than to windows the pad itself
        spawned) means it also works in sessions you started yourself,
        while still refusing to fire a stray Return into mail or chat.
        """
        app = frontmost_app()
        if app in self.cfg["accept_apps"]:
            with self.state_lock:
                self.action_open.clear()  # Return/Escape dismisses it too
            send_key_code(code)
        else:
            log.info("accept/cancel ignored: frontmost app is %r, not in %s",
                     app or "unknown", self.cfg["accept_apps"])

    def on_rotate(self, clockwise: bool) -> None:
        if not self.effort_mode:
            return
        idx = self.effort_idx + (1 if clockwise else -1)
        self.effort_idx = max(0, min(len(self.effort_steps) - 1, idx))
        self.effort_seq += 1
        self.effort_dirty = True
        self.push_gauge()

    def on_knob_mode(self, mode: int) -> None:
        self.effort_mode = mode == 1
        if self.effort_mode:
            self.push_gauge()
        else:
            if self.effort_dirty:
                write_effort_idx(self.effort_idx)
                self.effort_dirty = False
                threading.Thread(
                    target=self.send_slash, args=("effort", self.effort_seq),
                    kwargs={"level": self.effort_steps[self.effort_idx]["label"]},
                    daemon=True).start()
            self.set_anim(ANIM_NONE)
            for slot in range(1, 7):
                self.push_slot_led(slot)

    # ---- threads ---------------------------------------------------------

    def hid_loop(self) -> None:
        last_ping = 0.0
        while not self.stop.is_set():
            if self.device is None:
                if self.open_device():
                    self.push_all()
                else:
                    self.stop.wait(3)
                    continue
            now = time.monotonic()
            if now - last_ping > 2:
                self.send_report(bytes([0x7F]))
                last_ping = now
            try:
                data = self.device.read(REPORT_LEN, timeout_ms=250)
            except OSError:
                log.warning("HID read failed; reconnecting")
                self.drop_device()
                continue
            if not data:
                continue
            kind = data[0]
            if kind == 0x01:
                self.on_key(data[1], bool(data[2]))
            elif kind == 0x02:
                self.on_rotate(bool(data[1]))
            elif kind == 0x03:
                self.on_knob_mode(data[1])

    def socket_loop(self) -> None:
        SOCKET_PATH.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        server.listen(16)
        server.settimeout(1)
        while not self.stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            with conn:
                # A client that connects and never writes must not stall
                # every report queued behind it.
                conn.settimeout(0.5)
                try:
                    msg = json.loads(conn.recv(4096).decode())
                    slot = self.resolve_slot(msg)
                    if slot is not None:
                        self.set_state(slot, msg["state"],
                                       reminder=bool(msg.get("reminder")))
                except (ValueError, KeyError, UnicodeDecodeError,
                        AttributeError, OSError) as exc:
                    log.warning("bad socket message: %s", exc)
        server.close()
        SOCKET_PATH.unlink(missing_ok=True)

    def poll_loop(self) -> None:
        """Free slots whose terminal session has gone away."""
        while not self.stop.wait(3):
            with self.state_lock:
                tracked = {s: t for s, t in self.slot_tty.items()
                           if self.slot_state[s] != "empty"}
            if not tracked:
                continue
            # One AppleScript call for every slot, rather than one each.
            alive = live_ttys()
            if alive is None:
                continue  # query failed; assume nothing closed
            for slot, tty in tracked.items():
                if tty in alive:
                    self.missing.pop(slot, None)
                    continue
                if tty_has_processes(tty):
                    log.warning("slot %d: %s absent from Terminal's tab list "
                                "but still running; keeping it", slot, tty)
                    self.missing.pop(slot, None)
                    continue
                # Two clean misses before letting a key go, so one bad
                # answer cannot unassign a session you are using.
                misses = self.missing.get(slot, 0) + 1
                self.missing[slot] = misses
                if misses >= 2:
                    log.info("slot %d session %s closed", slot, tty)
                    self.set_state(slot, "empty")
                    self.missing.pop(slot, None)
            self.refresh_types()

    def refresh_types(self) -> None:
        """Re-colour a slot if its terminal is now running something else."""
        types = live_session_types()
        if not types:
            return
        changed = []
        with self.state_lock:
            for slot, tty in self.slot_tty.items():
                kind = types.get(tty, "other")
                if self.slot_type.get(slot) != kind:
                    self.slot_type[slot] = kind
                    changed.append((slot, kind))
        for slot, kind in changed:
            log.info("slot %d is now running %s", slot, kind)
            self.push_slot_led(slot)

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.hid_loop, daemon=True),
            threading.Thread(target=self.socket_loop, daemon=True),
            threading.Thread(target=self.poll_loop, daemon=True),
        ]
        for t in threads:
            t.start()
        log.info("bnk9d running (effort=%s)",
                 self.effort_steps[self.effort_idx]["label"])
        try:
            while not self.stop.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()
            with self.hold_lock:
                for timer in self.hold_timers.values():
                    timer.cancel()
                self.hold_timers.clear()
            self.send_report(bytes([0x21]))  # LEDs off, overlay cleared
            self.drop_device()


def merge_config(base: dict, override: dict) -> dict:
    """`override` on top of `base`, merging nested tables key by key.

    A config that sets one session colour keeps the defaults for the
    rest, rather than losing every colour it did not mention.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config() -> dict:
    """Defaults, overlaid with whatever ~/.bnk9/config.json changes.

    config.json holds only your overrides, so improvements to the
    defaults reach you on upgrade. The full set is written alongside as
    config.default.json for reference; editing that file does nothing.
    """
    BNK9_DIR.mkdir(mode=0o700, exist_ok=True)
    defaults = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        DEFAULTS_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    except OSError as exc:
        log.warning("could not write %s: %s", DEFAULTS_PATH, exc)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text("{}\n")
        log.info("created %s; copy settings into it from %s to change them",
                 CONFIG_PATH, DEFAULTS_PATH.name)
        return defaults
    try:
        override = json.loads(CONFIG_PATH.read_text())
    except ValueError as exc:
        log.error("%s is not valid JSON (%s); fix it and restart",
                  CONFIG_PATH, exc)
        raise SystemExit(1)
    if not isinstance(override, dict):
        log.error("%s must contain a JSON object", CONFIG_PATH)
        raise SystemExit(1)
    return merge_config(defaults, override)


def main() -> None:
    BNK9_DIR.mkdir(mode=0o700, exist_ok=True)
    handlers = [logging.FileHandler(LOG_PATH)]
    if sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    level = getattr(logging, str(config.get("log_level", "INFO")).upper(),
                    logging.INFO)
    logging.getLogger().setLevel(level)
    daemon = Daemon(config)
    signal.signal(signal.SIGTERM, lambda *_: daemon.stop.set())
    daemon.run()


if __name__ == "__main__":
    main()
