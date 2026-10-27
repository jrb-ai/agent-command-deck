#!/bin/bash
# Copyright 2026 Jon Regan-Beasley
# SPDX-License-Identifier: GPL-2.0-or-later
# Install the BNK9 agent knob host side.
#
#   ./install.sh              venv, runtime dir, hook wiring for claude/codex/hermes
#   ./install.sh --launchd    additionally install + start the launchd agent
#                             (run the daemon in the foreground once first, so
#                             macOS permission prompts appear in front of you)
#   ./install.sh --uninstall  stop the launchd agent and remove every hook
#                             this script added (each file is backed up first)
#
# Environment overrides:
#   BNK9_PYTHON     interpreter to build the venv from
#   HERMES_PYTHON   Hermes's own interpreter (needs PyYAML)
#   HERMES_CONFIG   Hermes's config.yaml
set -euo pipefail

MODE="${1:-}"
case "$MODE" in
    ""|--launchd|--uninstall) ;;
    *) echo "usage: $0 [--launchd | --uninstall]" >&2; exit 2 ;;
esac

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CTL="$REPO/host/bnk9ctl"
PY="$REPO/.venv/bin/python3"
LABEL="local.bnk9d"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HERMES_PY="${HERMES_PYTHON:-$HOME/.hermes/hermes-agent/venv/bin/python}"
HERMES_CFG="${HERMES_CONFIG:-$HOME/.hermes/config.yaml}"

stop_agent() {
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    # bootstrap races the teardown of the previous job, so wait it out
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
        sleep 0.5
    done
}

wire() {  # wire <install|uninstall> <agent> [config]
    "$PY" "$REPO/host/wire_hooks.py" "$1" "$2" "$PY" "$CTL" "${@:3}"
}

echo "==> repo: $REPO"

if [ "$MODE" = "--uninstall" ]; then
    if [ ! -x "$PY" ]; then
        echo "ERROR: $PY is missing; nothing was installed from this checkout" >&2
        exit 1
    fi
    if [ -f "$PLIST" ]; then
        stop_agent
        rm -f "$PLIST"
        echo "==> launchd agent removed"
    fi
    wire uninstall claude
    wire uninstall codex
    if [ -x "$HERMES_PY" ] && [ -f "$HERMES_CFG" ]; then
        "$HERMES_PY" "$REPO/host/wire_hooks.py" uninstall hermes "$PY" "$CTL" "$HERMES_CFG"
    fi
    echo
    echo "Left in place: ~/.bnk9 (config and log) and $REPO/.venv."
    echo "Delete them by hand if you are done with the pad. If you granted"
    echo "Accessibility to $PY, remove it from System Settings too."
    exit 0
fi

# 1. Python venv with hidapi
# --copies (not symlinks) so the interpreter is a real file inside this
# repo: macOS Accessibility permission is then scoped to our binary
# rather than to the system-wide Python it would otherwise resolve to.
# Apple's bundled python refuses --copies, so prefer any standalone build.
if [ ! -f "$PY" ] || [ -L "$PY" ]; then
    rm -rf "$REPO/.venv"
    for CAND in ${BNK9_PYTHON:+"$BNK9_PYTHON"} \
                "$HOME"/.local/share/uv/python/cpython-3.*/bin/python3 \
                /Library/Frameworks/Python.framework/Versions/3.*/bin/python3 \
                /opt/homebrew/bin/python3 \
                /usr/local/bin/python3; do
        [ -x "$CAND" ] || continue
        if "$CAND" -m venv --copies "$REPO/.venv" 2>/dev/null; then
            echo "==> created venv --copies from $("$CAND" -V) at $CAND"
            break
        fi
        rm -rf "$REPO/.venv"
    done
    if [ ! -f "$PY" ]; then
        echo "==> no interpreter supports --copies; falling back to symlinks"
        echo "    (Accessibility will then apply to the system Python, not just us)"
        /usr/bin/python3 -m venv "$REPO/.venv"
    fi
fi
"$PY" -m pip install --quiet --upgrade pip hidapi
echo "==> venv ready ($("$PY" -c 'import hid; print("hidapi ok")'))"

# 2. Runtime dir + executables
mkdir -p "$HOME/.bnk9"
chmod 700 "$HOME/.bnk9"
chmod +x "$CTL" "$REPO/host/bnk9-run" "$REPO/host/bnk9d.py"

# 3. Agent hooks. Each calls bnk9ctl through the venv's interpreter, so
#    nothing depends on which python3 an agent finds on its PATH.
wire install claude
if [ -d "$HOME/.codex" ]; then
    wire install codex
else
    echo "==> codex: no ~/.codex; skipping"
fi
# Hermes's config is YAML, so it is edited with Hermes's own interpreter,
# which has PyYAML.
if [ -x "$HERMES_PY" ] && [ -f "$HERMES_CFG" ]; then
    "$HERMES_PY" "$REPO/host/wire_hooks.py" install hermes "$PY" "$CTL" "$HERMES_CFG"
else
    echo "==> hermes: not found; skipping (set HERMES_PYTHON / HERMES_CONFIG if it lives elsewhere)"
fi

# 4. launchd agent (optional)
if [ "$MODE" = "--launchd" ]; then
    mkdir -p "$(dirname "$PLIST")"
    # plistlib writes the plist, so paths need no XML escaping.
    "$PY" - "$PLIST" "$LABEL" "$PY" "$REPO/host/bnk9d.py" "$HOME/.bnk9/bnk9d.log" <<'PLISTEOF'
import plistlib, sys
plist, label, python, daemon, log = sys.argv[1:6]
with open(plist, "wb") as f:
    plistlib.dump({"Label": label,
                   "ProgramArguments": [python, daemon],
                   "RunAtLoad": True, "KeepAlive": True,
                   "StandardOutPath": log, "StandardErrorPath": log}, f)
PLISTEOF
    stop_agent
    if launchctl bootstrap "gui/$(id -u)" "$PLIST"; then
        echo "==> launchd agent installed and started"
    else
        echo "ERROR: bootstrap failed; retry with:"
        echo "    launchctl bootstrap gui/$(id -u) \"$PLIST\""
        exit 1
    fi
    echo
    echo "If ACCEPT/CANCEL do nothing, add this binary to System Settings >"
    echo "Privacy & Security > Accessibility (Cmd-Shift-G in the file picker):"
    echo "    $PY"
else
    echo
    echo "Next: run the daemon once in the foreground to collect the macOS"
    echo "permission prompts (Automation -> Terminal / System Events):"
    echo "    \"$PY\" \"$REPO/host/bnk9d.py\""
    echo "then re-run:  $0 --launchd"
fi
