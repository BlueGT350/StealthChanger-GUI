#!/usr/bin/env python3
"""Copy Orbiter button pins out of the git clone into the user config file.

The user file is ~/printer_data/config/ktc-filament-orbiter.cfg. It is
created once and never overwritten. A clone file that still has
[gcode_button] blocks is backed up, then restored to the committed
version when that is possible, so Moonraker can update a repo whose
only local edit was those pins.

Prints machine-readable lines:
  MIGRATED   edited pins were written to the user file
  CREATED    the user file was created from the template
  KEPT       the user file already existed and was not touched
  BACKUP <path>
  RESTART    printer.cfg or a new pin file means Klipper should restart
  CLEAN      the clone's orbiter_addon.cfg matches git again

Exit 0 on success. The user file is never overwritten.
Called by setup.sh. Re-run setup.sh after an update to move pins.
"""
import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

USER_NAME = "ktc-filament-orbiter.cfg"
ADDON_REL = os.path.join("orbiter", "orbiter_addon.cfg")
TEMPLATE_REL = os.path.join("orbiter", "orbiter_pins.cfg")
PINS_INCLUDE = "ktc-filament-orbiter.cfg"
ADDON_INCLUDE = "ktc-filament/orbiter/orbiter_addon.cfg"
BUTTON = re.compile(r"^\[gcode_button\s+(\S+)\s*\]\s*$")
PIN = re.compile(r"^pin:\s*(\S+)\s*$")
TOOLS = re.compile(r"^variable_button_tools:\s*(.+)$")
INC = re.compile(r"^\[include\s+(\S+)\s*\]\s*$")
DISABLED = re.compile(r"^#\s*\[include\s+(\S+)\s*\]\s*(?:#.*)?$")
DEFAULT_PINS = {
    "T0_orbiter_button": "^nhkT0:gpio13",
    "T1_orbiter_button": "^nhkT1:gpio13",
    "T2_orbiter_button": "^nhkT2:gpio13",
    "T3_orbiter_button": "^nhkT3:gpio13",
    "T4_orbiter_button": "^nhkT4:gpio13",
    "T5_orbiter_button": "^nhkT5:gpio13",
}


def repo_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def split_save(raw):
    marker = re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
    if not marker:
        return raw, b""
    return raw[:marker.start()], raw[marker.start():]


def button_blocks(text):
    """Return (blocks, pins, button_tools or None). blocks keep their text."""
    lines = text.splitlines()
    blocks = []
    pins = {}
    tools = None
    i = 0
    while i < len(lines):
        m = BUTTON.match(lines[i].strip())
        tm = TOOLS.match(lines[i].strip()) if lines[i][:1] not in (" ", "\t") else None
        if tm and not lines[i][:1].isspace():
            tools = tm.group(1).strip()
        if not m or lines[i][:1].isspace():
            i += 1
            continue
        name = m.group(1)
        start = i
        i += 1
        pin = None
        while i < len(lines):
            if lines[i][:1] not in (" ", "\t") and lines[i].strip().startswith("["):
                break
            pm = PIN.match(lines[i].strip())
            if pm:
                pin = pm.group(1)
            i += 1
        blocks.append("\n".join(lines[start:i]).rstrip() + "\n")
        if pin:
            pins[name] = pin
    return blocks, pins, tools


def edited(pins, blocks):
    if set(pins) != set(DEFAULT_PINS):
        return True
    if any(pins[k] != DEFAULT_PINS[k] for k in pins):
        return True
    # A changed press_gcode is still an edit even when the pin matches.
    for block in blocks:
        body = [ln.strip() for ln in block.splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
        name = BUTTON.match(body[0]).group(1) if body else ""
        tool = name[1] if name.startswith("T") and len(name) > 1 and name[1].isdigit() else ""
        expect = ["[gcode_button %s]" % name, "pin: %s" % pins.get(name, ""),
                  "press_gcode:", "_ORBITER_BUTTON TOOL=%s" % tool]
        if body != expect:
            return True
    return False


def user_text_from_blocks(blocks, tools):
    lines = [
        "# ktc-filament-orbiter.cfg: YOUR Orbiter button pins.",
        "# Copied out of the git clone so an update cannot reset them.",
        "# This file is included after ktc-filament/orbiter/orbiter_addon.cfg.",
        "# setup.sh and the update migration never overwrite it.",
        "# A backup of the file these pins came from is under",
        "# ~/stealthchanger-backups/<time>/orbiter_addon.cfg",
        "",
    ]
    if tools and tools != "[0, 1, 2, 3, 4, 5]":
        lines += [
            "[gcode_macro _ORBITER_SETTINGS]",
            "variable_button_tools: %s" % tools,
            "",
        ]
    lines.append("")
    return "\n".join(lines).rstrip() + "\n" + "\n".join(b.rstrip() for b in blocks) + "\n"


def backup_file(src, backup_dir):
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, "orbiter_addon.cfg")
    if os.path.exists(dest):
        dest = os.path.join(backup_dir, "orbiter_addon.cfg.from-" + os.path.basename(os.path.dirname(src)))
    shutil.copy2(src, dest)
    return dest


def git_checkout_addon(clone):
    try:
        subprocess.run(
            ["git", "-C", clone, "checkout", "--", ADDON_REL],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def strip_buttons(path):
    text = read_text(path)
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        if BUTTON.match(lines[i].strip()) and not lines[i][:1].isspace():
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i][:1].isspace()
                                      or not lines[i].strip().startswith("[")):
                if lines[i].strip().startswith("[") and not lines[i][:1].isspace():
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    new = "\n".join(out).rstrip() + "\n"
    if new != text:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(new)
        return True
    return False


def ensure_include(printer_cfg):
    """Add the pins include after the Orbiter add-on include. Preserve #*# bytes."""
    raw = open(printer_cfg, "rb").read()
    head_b, tail_b = split_save(raw)
    text = head_b.decode("utf-8")
    lines = text.splitlines()
    active_addon = [i for i, ln in enumerate(lines) if INC.match(ln.strip()) and INC.match(ln.strip()).group(1) == ADDON_INCLUDE]
    active_pins = [i for i, ln in enumerate(lines) if INC.match(ln.strip()) and INC.match(ln.strip()).group(1) == PINS_INCLUDE]
    if not active_addon or active_pins:
        return False
    disabled = [i for i, ln in enumerate(lines) if DISABLED.match(ln.strip()) and DISABLED.match(ln.strip()).group(1) == PINS_INCLUDE]
    if disabled:
        lines[disabled[-1]] = "[include %s]" % PINS_INCLUDE
    else:
        lines.insert(active_addon[-1] + 1, "[include %s]" % PINS_INCLUDE)
    body = "\n".join(lines)
    if text.endswith("\n") or text == "":
        body += "\n"
    out = body.encode("utf-8") + tail_b
    if not out.endswith(tail_b):
        raise SystemExit("refusing to write %s: SAVE_CONFIG block would change" % printer_cfg)
    open(printer_cfg, "wb").write(out)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--clone", default="", help="git clone that contains orbiter/")
    ap.add_argument("--config", default="", help="printer config directory")
    ap.add_argument("--source", default="", help="extra orbiter_addon.cfg to read pins from")
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--home", default="")
    args = ap.parse_args(argv)
    home = args.home or os.path.expanduser("~")
    data = os.environ.get("SC_PRINTER_DATA") or os.path.join(home, "printer_data")
    config = args.config or os.path.join(data, "config")
    clone = args.clone or os.environ.get("SC_CLONE_DIR") or repo_root()
    user = os.path.join(config, USER_NAME)
    template = os.path.join(clone, TEMPLATE_REL)
    addon = os.path.join(clone, ADDON_REL)
    sources = []
    if args.source:
        sources.append(args.source)
    if os.path.isfile(addon):
        sources.append(addon)
    status = []
    restart = False
    try:
        blocks, pins, tools = [], {}, None
        chosen = ""
        for src in sources:
            if not os.path.isfile(src):
                continue
            b, p, t = button_blocks(read_text(src))
            if b:
                blocks, pins, tools, chosen = b, p, t, src
                break
        if not os.path.isfile(user):
            os.makedirs(config, exist_ok=True)
            if blocks and edited(pins, blocks):
                if not args.backup_dir:
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    args.backup_dir = os.path.join(home, "stealthchanger-backups", stamp)
                backed = backup_file(chosen, args.backup_dir)
                status.append("BACKUP " + backed)
                with open(user, "w", encoding="utf-8") as fh:
                    fh.write(user_text_from_blocks(blocks, tools))
                status.append("MIGRATED")
            elif os.path.isfile(template):
                shutil.copyfile(template, user)
                status.append("CREATED")
            elif blocks:
                with open(user, "w", encoding="utf-8") as fh:
                    fh.write(user_text_from_blocks(blocks, tools))
                status.append("CREATED")
            restart = True
        else:
            status.append("KEPT")
            # Still back up a dirty clone copy once, without touching the user file.
            if chosen and edited(pins, blocks) and chosen == addon:
                if not args.backup_dir:
                    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                    args.backup_dir = os.path.join(home, "stealthchanger-backups", stamp)
                # Only when the working copy differs from HEAD, so a second run is quiet.
                if os.path.isdir(os.path.join(clone, ".git")) or os.path.isfile(os.path.join(clone, ".git")):
                    diff = subprocess.run(
                        ["git", "-C", clone, "diff", "--quiet", "--", ADDON_REL],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if diff.returncode == 1:
                        backed = backup_file(chosen, args.backup_dir)
                        status.append("BACKUP " + backed)
        if os.path.isfile(addon) and button_blocks(read_text(addon))[0]:
            if git_checkout_addon(clone):
                status.append("CLEAN")
            elif strip_buttons(addon):
                status.append("STRIPPED")
        printer = os.path.join(config, "printer.cfg")
        if os.path.isfile(printer) and os.path.isfile(user) and ensure_include(printer):
            status.append("INCLUDE")
            restart = True
        if restart:
            status.append("RESTART")
    except Exception as exc:
        sys.stderr.write("orbiter pin migration failed: %s\n" % exc)
        return 1
    if not status:
        status.append("KEPT")
    print("\n".join(status))
    return 0


if __name__ == "__main__":
    sys.exit(main())
