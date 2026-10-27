# BNK9 agent knob

Binepad BNK9 (3×3 keys + push knob) as a control surface for coding
agents on macOS: six interchangeable session keys, accept / cancel /
slash-command, and a knob that scrolls or sets the agent's effort level.

**Requirements:** a Binepad BNK9, macOS with Terminal.app (other
terminals work for ACCEPT/CANCEL only), and Python 3.9 or newer. Works
with Claude Code, Codex and Hermes through their hooks, and with any
other terminal program as a plain session.

This is a personal project. It is not affiliated with or endorsed by
Binepad, Anthropic, OpenAI or the makers of any agent it talks to.

```
        ( KNOB )      rotate = scroll · press = effort mode (3 s)
┌────────┬────────┬────────┐
│ slot 1 │ slot 2 │ slot 3 │   colour = whatever is running in it
├────────┼────────┼────────┤
│ slot 4 │ slot 5 │ slot 6 │   claude·codex·hermes·ollama·openclaw·other
├────────┼────────┼────────┤
│ ACCEPT │ CANCEL │ /usage │   green · red · purple
└────────┴────────┴────────┘
```

Key LEDs: **dim** = free slot, **solid** = agent working, **slow breathe**
= finished, your turn, **fast flash** = blocked waiting for approval.
Breathing and flashing mean different things on purpose: a fade is an
invitation, a flash is a blockage. Whole pad
breathes dimly when the daemon isn't running.

Signalling is a request for attention, so it ends when the attention
arrives: tapping the key (which focuses that session) turns it solid, as
does responding. That dismissal sticks — the minute-by-minute idle nudge
will not undo it, and only something genuinely new re-arms it, such as an
approval request or the next completed turn. Merely having the window in
front of you does not count: a turn usually ends while you are sitting
there, so treating that as acknowledgement would cancel the signal before
it could be read. Otherwise it keeps going until you deal with it; set
`input_flash_secs` to a number of seconds to have it give up by itself.

The six keys are interchangeable. A key is not tied to an agent — you
assign a session to it, and it takes that session's colour:

| | |
|---|---|
| claude | orange |
| codex | azure |
| hermes | teal |
| ollama | white |
| openclaw | amber |
| anything else | pink |

**Tap** an unassigned (dim) key to start a fresh Claude Code session in
a new Terminal tab (`spawn_command`; set `spawn_in_tab` to `false` for a
window). Tap an assigned key to focus its session. Close
the window to free the key again.

**Hold** a key for 800 ms to assign the terminal session you are already
working in — any kind, not just Claude. Assigned sessions get the same
status colours as spawned ones.

The type is read from the terminal's foreground job leader, so it stays
right even though agents host each other's helper processes (Claude Code
runs a codex MCP server), and it updates if you quit one agent and start
another in the same window. Only the program name counts, looking
through interpreters such as `node` or `python`: `vim CLAUDE.md` or a
shell in `~/claude-projects` is not an agent.

Slots are tracked by the **tty** of their session, never by window
title. Agents rewrite terminal titles constantly, so a title marker
does not survive; the tty cannot be overwritten and dies with the
session, which is also how a closed window frees its slot.

Terminal's tab list occasionally comes back short. So that a live
session is not unassigned by mistake, the daemon asks `ps` whether
anything still holds that tty before freeing a key, and requires two
consecutive misses. A short list is logged as a warning rather than acted
on.

ACCEPT sends Return and CANCEL sends Escape into any terminal window
(`accept_apps` in the config; add `"iTerm2"` or `"Ghostty"` if you move
off Terminal.app). They stay inert when a non-terminal app is frontmost,
so a stray Return cannot land in mail or chat.

The purple key types a slash command into the agent session you are
looking at — `/usage` on Claude, `/status` on Codex (whose `/usage` is a
12-month summary rather than current limits) — set per agent type under
`session_commands`, so it can be any command you reach for often. It toggles: press again to
dismiss the panel.

Both the purple key and the effort knob decide what to send from the
kind of session in front of you, set in `session_commands`. They fire
into an agent prompt whether or not it is assigned to a key, and into a
plain shell never — which is the point of the check. Claude takes
`/effort <level>` directly. Codex has no argument form for reasoning
level, so the knob opens `/model` and then drives the chooser with a
scripted `effort_sequence`: `key:<name>` presses a key, `wait:<secs>`
lets the interface catch up, anything else is typed, and `{level}` is
substituted. Being keystroke choreography it depends on the chooser's
layout, so adjust that list if a Codex update rearranges it.

`level_map` gives the steps for each level, since one level can need
several presses. For Codex the route is `/model` → `2` (All models) →
Enter (keeps the model you are on) → the reasoning list, where a digit
selects outright: 1 Low, 2 Medium, 3 High, 4 Extra high, 5 More
reasoning… and behind that, 1 Max and 2 Ultra.

Digits matter here. The highlight starts on your *current* level, so
pressing Enter just re-picks it — and any step that lands after the
chooser has closed gets typed into the prompt and submitted as a stray
message. Selecting by digit avoids both.

Knob: rotation is a native mouse wheel, clockwise scrolling down (works
everywhere, daemon or not). The stock knob spins freely with little
detent, so `encoder_divider` sets how many pulses each action needs —
`scroll` for wheel ticks, `effort` for gauge steps. Raising `effort`
makes choosing a level deliberate rather than a flick. Scrolling
defaults to 1 so it stays responsive, the gauge to 3; the firmware
accepts 1 to 16 and applies it live, so tuning needs a daemon restart
but no reflash. macOS scrolls whatever sits under the
pointer rather than the focused window, so selecting a slot moves the
pointer into that window — otherwise the knob would keep scrolling
wherever the mouse was left. Set `warp_cursor_on_focus` to `false` to
stop the pad touching your pointer. Pressing it enters effort mode for ~3 s: the
top keys become a six-step gauge, and you can either rotate the knob or
press the key for the step you want. While the gauge is showing, the
agent keys select a level rather than launching anything.

| Step | Applied to a launched Claude session |
|---|---|
| low / medium / high / xhigh | `--effort <level>` |
| max | `--effort max` (CLI-only; `settings.json` rejects it); the six agent keys run a flowing rainbow |
| ultracode | `--settings '{"ultracode":true}'` — xhigh plus standing dynamic-workflow orchestration; the six agent keys pulse hard in purple, bottom row untouched |

Choosing a level does two things: it becomes the launch flag for the
next session you start from the pad, **and** it retunes the session you
are looking at, as described above.

The level is the daemon's own state (`~/.bnk9/state.json`); your global
`~/.claude/settings.json` is never modified.

## Install

1. **Firmware** — built by GitHub Actions (`.github/workflows/build.yml`).
   Download the `bnk9_agentknob` artifact, then: unplug the pad, hold the
   knob down, plug it back in; a `RPI-RP2` drive appears; drag
   `binepad_bnk9_agentknob.uf2` onto it. The pad reboots into the new
   firmware. (Recovery is the same procedure with any other firmware.)
2. **Host** — `host/install.sh`, then run the daemon once in the
   foreground to collect the macOS permission prompts (Automation for
   Terminal and System Events; approve them), then
   `host/install.sh --launchd` to install autostart.
3. **Accessibility** — required for ACCEPT/CANCEL, which send real key
   events. Without it the log shows *"osascript is not allowed to send
   keystrokes"*. System Settings → Privacy & Security → Accessibility →
   add **Terminal** (covers foreground runs) and, for the launchd agent,
   `.venv/bin/python3` (Cmd-Shift-G in the file picker to type the path).
   The venv is deliberately built with `--copies` so this grants only our
   own interpreter, not the system-wide Python. Be aware that the grant
   covers *anything* run with that interpreter, not just the daemon, so
   don't use this venv for other scripts. If the installer reports that
   it fell back to symlinks, the grant would cover the system Python
   instead; install a standalone Python (python.org, Homebrew or `uv`),
   or point `BNK9_PYTHON` at one, and re-run it.
4. **Hermes consent** — Hermes asks once, in-session, before running the
   newly registered shell hooks (its `hooks_auto_accept` is off by
   design). Accept when prompted.
5. **Codex hook trust** — Codex skips new or changed hooks until they are
   trusted: run `/hooks` inside Codex once and approve them. Trust is
   recorded against the hook's hash, so re-approve after editing them.
   Note the event names are PascalCase in config (`Stop`,
   `PermissionRequest`) even though the binary carries them in
   snake_case, and that `notify` must be a **root-level** key in
   `config.toml` — under a table it is silently ignored.

### Uninstall

`host/install.sh --uninstall` stops the launchd agent and removes every
hook the installer added to `~/.claude/settings.json`,
`~/.codex/hooks.json`, `~/.codex/config.toml` and Hermes's
`config.yaml`, backing each file up first. It leaves `~/.bnk9` and the
venv for you to delete, and the Accessibility grant to remove in System
Settings.

The installer looks for Hermes at `~/.hermes`; set `HERMES_PYTHON` and
`HERMES_CONFIG` if yours lives elsewhere.

## Configuration

`~/.bnk9/config.json` holds only the settings you change; everything
else comes from the defaults, which the daemon writes to
`~/.bnk9/config.default.json` on every start for reference (editing
that copy does nothing). Nested tables merge key by key, so
`{"session_colors": {"claude": [255, 60, 0]}}` changes one colour and
keeps the rest. A `config.json` from an older version that holds a full
copy of the defaults still works, but it pins those old values; delete
the keys you never changed to pick up new defaults.

Settings include `session_colors` per agent
type, `free_color`, `spawn_command` and `spawn_cwd` for the tap-to-start
session, plus brightness, `accept_apps`, `hold_ms` and the bottom-row
colours.
`{effort_args}` in a command is replaced with the knob's launch flags
(`{effort}` still works for the bare level name). The knob's six steps
are `effort_steps`, so you can rename, reorder or drop them; a step's
optional `anim` (`rainbow`, `ultracode` or `none`) replaces its gauge
with a firmware animation on the six agent keys.
`host/bnk9-run` remains available as a PTY wrapper that infers
waiting-for-input from output silence, for agents with no hook system;
put it in front of a command in `spawn_command` to use it. Restart the
daemon after editing
(`launchctl kickstart -k gui/$UID/local.bnk9d`).

## How state reporting works

| Agent | Mechanism | Events |
|---|---|---|
| Claude Code | hooks in `~/.claude/settings.json` | UserPromptSubmit→running, Stop→input, SessionEnd→ended, Notification→waiting unless its message is the 60s idle nudge, which is→input |
| Codex | hooks in `~/.codex/hooks.json`, plus `notify` in `config.toml` as a fallback | UserPromptSubmit→running, Stop→input, PermissionRequest→waiting, SessionEnd→ended |
| Hermes | shell hooks in `~/.hermes/config.yaml` | pre_llm_call→running, post_llm_call→input, pre_approval_request→waiting, post_approval_response→running |
| Ollama | `bnk9-run` PTY wrapper | output flowing→running, >1.2 s silence→input |

All of them call `host/bnk9ctl`. Sessions launched from the pad carry
`BNK9_SLOT` and name their slot directly. Sessions you started yourself
never inherited it, so `bnk9ctl` walks up the process tree to the
agent's controlling tty (hooks run detached, but the agent still holds
the terminal) and the daemon maps that tty to a slot — which is what
makes an adopted session flash like a spawned one. Reports from
terminals the pad does not track resolve to nothing and are dropped, so
unrelated sessions stay unaffected.

## Files

| Path | Purpose |
|---|---|
| `keyboards/binepad/bnk9/keymaps/agentknob/` | QMK keymap (raw HID protocol in `keymap.c` header comment) |
| `host/bnk9d.py` | daemon: HID ↔ Terminal.app ↔ unix socket |
| `host/bnk9ctl` | hook client (fast, silent) |
| `host/bnk9-run` | PTY activity wrapper for hook-less agents |
| `host/install.sh` | venv, hook wiring, launchd, `--uninstall` |
| `host/wire_hooks.py` | adds and removes the hook entries (called by `install.sh`) |
| `~/.bnk9/` | config, effort state, socket, log |

## Licence

GPL-2.0-or-later; see `LICENSE`. The firmware is built on
[QMK](https://qmk.fm), which is GPL-licensed, and the host side uses the
same licence so the whole project is under one set of terms.
