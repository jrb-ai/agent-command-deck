#!/usr/bin/env python3
# Copyright 2026 Jon Regan-Beasley
# SPDX-License-Identifier: GPL-2.0-or-later
"""wire_hooks - add or remove bnk9ctl in each agent's hook config.

Called by install.sh; not meant to be run by hand.

  wire_hooks.py install|uninstall claude|codex|hermes PYTHON CTL [CONFIG]

PYTHON and CTL are the interpreter and bnk9ctl paths the hooks should
call. Every file is backed up next to itself before it is changed, and
an entry counts as ours if its command mentions "bnk9ctl", which is how
both installing twice and uninstalling find what is already there.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

MARKER = "bnk9ctl"
STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


def backup(path: Path) -> str:
    target = path.with_name(f"{path.name}.bak-bnk9-{STAMP}")
    shutil.copy2(path, target)
    return target.name


def write(path: Path, text: str) -> str:
    """Write `text` to `path`, backing up what was there first."""
    note = f"backup: {backup(path)}" if path.exists() else "new file"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return note


def load_json(path: Path, what: str) -> dict | None:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text())
    except ValueError:
        print(f"WARN: {path} is not valid JSON; leaving {what} alone")
        return None
    if not isinstance(doc, dict):
        print(f"WARN: {path} is not a JSON object; leaving {what} alone")
        return None
    return doc


def table(doc: dict, key: str) -> dict:
    """doc[key] as a dict, replacing a missing or null value."""
    value = doc.get(key)
    if not isinstance(value, dict):
        value = doc[key] = {}
    return value


def ours(hook: dict) -> bool:
    return MARKER in str(hook.get("command") or "")


def command(python: str, ctl: str, state: str) -> str:
    return f"{shlex.quote(python)} {shlex.quote(ctl)} {state}"


# ---- Claude Code and Codex share a hooks layout --------------------------
#   {"hooks": {"Event": [{"hooks": [{"type": "command", ...}]}]}}

def add_nested(hooks: dict, wanted: dict) -> bool:
    changed = False
    for event, hook in wanted.items():
        entries = hooks.get(event) or []
        hooks[event] = entries
        if any(ours(h) for e in entries for h in e.get("hooks", [])):
            continue
        entries.append({"hooks": [hook]})
        changed = True
    return changed


def remove_nested(hooks: dict) -> bool:
    changed = False
    for event in list(hooks):
        kept = []
        for entry in hooks[event] or []:
            inner = [h for h in entry.get("hooks", []) if not ours(h)]
            if len(inner) != len(entry.get("hooks", [])):
                changed = True
                if not inner:
                    continue
                entry = dict(entry, hooks=inner)
            kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return changed


def claude(action: str, python: str, ctl: str) -> None:
    path = Path.home() / ".claude" / "settings.json"
    settings = load_json(path, "Claude Code hooks")
    if settings is None:
        return
    hooks = table(settings, "hooks")
    if action == "install":
        wanted = {event: {"type": "command",
                          "command": command(python, ctl, state)}
                  for event, state in (("UserPromptSubmit", "running"),
                                       ("Notification", "waiting"),
                                       ("Stop", "input"),
                                       ("SessionEnd", "ended"))}
        changed = add_nested(hooks, wanted)
    else:
        changed = remove_nested(hooks)
        if not hooks:
            del settings["hooks"]
    if not changed:
        print(f"==> claude: nothing to {action}")
        return
    note = write(path, json.dumps(settings, indent=2) + "\n")
    print(f"==> claude: hooks {action}ed in {path} ({note})")


def codex_hooks(action: str, python: str, ctl: str) -> None:
    path = Path.home() / ".codex" / "hooks.json"
    doc = load_json(path, "Codex hooks")
    if doc is None:
        return
    if action == "install" and not doc:
        doc = {"description": "BNK9 agent knob status reporting"}
    hooks = table(doc, "hooks")
    if action == "install":
        # Codex caps SessionEnd at 3 s; event names are PascalCase.
        wanted = {event: {"type": "command",
                          "command": command(python, ctl, state),
                          "timeout": timeout}
                  for event, state, timeout in (
                      ("UserPromptSubmit", "running", 5),
                      ("Stop", "input", 5),
                      ("PermissionRequest", "waiting", 5),
                      ("SessionEnd", "ended", 3))}
        changed = add_nested(hooks, wanted)
    else:
        changed = remove_nested(hooks)
    if not changed:
        print(f"==> codex: no hooks to {action}")
        return
    note = write(path, json.dumps(doc, indent=2) + "\n")
    print(f"==> codex: hooks {action}ed in {path} ({note})")
    if action == "install":
        print("    run /hooks inside Codex once to trust them")


def codex_notify(action: str, python: str, ctl: str) -> None:
    """The `notify` program, a fallback for Codex builds without hooks.

    It must be a root-level key: appended to the end of the file it
    lands inside whatever table happens to be last, where Codex silently
    ignores it.
    """
    path = Path.home() / ".codex" / "config.toml"
    if not path.exists():
        if action == "install":
            print(f"==> codex: no {path}; skipping notify (hooks suffice)")
        return
    lines = path.read_text().splitlines()
    if action == "uninstall":
        kept = [line for line in lines
                if not (line.lstrip().startswith("notify") and MARKER in line)]
        if len(kept) == len(lines):
            return
        note = write(path, "\n".join(kept) + "\n")
        print(f"==> codex: notify removed from {path} ({note})")
        return
    if any(MARKER in line for line in lines):
        print("==> codex: notify already present")
        return
    if any(line.lstrip().startswith("notify") for line in lines):
        print(f"WARN: {path} already defines 'notify'; leaving it alone")
        return
    # JSON string literals are valid TOML basic strings.
    entry = "notify = [%s, %s, \"--codex\"]" % (json.dumps(python),
                                                json.dumps(ctl))
    first_table = next((i for i, line in enumerate(lines)
                        if line.lstrip().startswith("[")), len(lines))
    while first_table and not lines[first_table - 1].strip():
        first_table -= 1
    lines.insert(first_table, entry)
    note = write(path, "\n".join(lines) + "\n")
    print(f"==> codex: notify added to {path} ({note})")


def hermes(action: str, python: str, ctl: str, cfg: str) -> None:
    import yaml  # run with Hermes's own interpreter, which has PyYAML

    path = Path(cfg)
    config = yaml.safe_load(path.read_text()) or {}
    hooks = table(config, "hooks")
    changed = False
    if action == "install":
        for event, state in (("pre_llm_call", "running"),
                             ("post_llm_call", "input"),
                             ("pre_approval_request", "waiting"),
                             ("post_approval_response", "running")):
            entries = hooks.get(event) or []
            hooks[event] = entries
            if any(ours(e) for e in entries):
                continue
            entries.append({"command": command(python, ctl, state),
                            "timeout": 5})
            changed = True
    else:
        for event in list(hooks):
            entries = hooks[event] or []
            kept = [e for e in entries if not ours(e)]
            if len(kept) != len(entries):
                changed = True
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
        if not hooks:
            del config["hooks"]
    if not changed:
        print(f"==> hermes: nothing to {action}")
        return
    note = write(path, yaml.safe_dump(config, sort_keys=False))
    print(f"==> hermes: hooks {action}ed in {path} ({note})")


def main() -> int:
    try:
        action, agent, python, ctl = sys.argv[1:5]
    except ValueError:
        print(__doc__, file=sys.stderr)
        return 2
    if action not in ("install", "uninstall"):
        print(__doc__, file=sys.stderr)
        return 2
    if agent == "claude":
        claude(action, python, ctl)
    elif agent == "codex":
        codex_hooks(action, python, ctl)
        codex_notify(action, python, ctl)
    elif agent == "hermes" and len(sys.argv) > 5:
        hermes(action, python, ctl, sys.argv[5])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
