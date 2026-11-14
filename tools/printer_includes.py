#!/usr/bin/env python3
"""Add or comment-out ktc-filament includes in printer.cfg.

    python3 tools/printer_includes.py PRINTER.CFG \
        --filament keep --orbiter on --usage auto \
        --overrides ktc-filament-overrides.cfg

Modes: on (ensure the include), off (comment it out), keep (leave it).
usage=auto adds the usage add-on only when some ktc-filament/ include
is on, or is being turned on.

When the Orbiter add-on include is active (or this run turns it on),
the user pin file ktc-filament-orbiter.cfg is included too. Turning
Orbiter off comments that include out. The pin file itself is not deleted.

The #*# autosave block is copied through unchanged. A removed include
is commented, not deleted, so a rerun can turn it back on.
Exit 0. Prints ADDED, REMOVED, KEPT or UNCHANGED.
"""
import argparse
import re
import sys

MODULES = {
    "filament": "ktc-filament/filament/filament_macros.cfg",
    "orbiter": "ktc-filament/orbiter/orbiter_addon.cfg",
    "pins": "ktc-filament-orbiter.cfg",
    "usage": "ktc-filament/usage/usage_addon.cfg",
}
ORDER = ["filament", "orbiter", "pins", "usage", "overrides"]
INC = re.compile(r"^\[include\s+(\S+)\s*\]\s*$")
DISABLED = re.compile(r"^#\s*\[include\s+(\S+)\s*\]\s*(?:#.*)?$")


def include_line(path):
    return "[include %s]" % path


def split_autosave(lines):
    for i, line in enumerate(lines):
        if line.startswith("#*#"):
            return lines[:i], lines[i:]
    return lines, []


def active_paths(lines):
    found = []
    for line in lines:
        m = INC.match(line.strip())
        if m:
            found.append(m.group(1))
    return found


def find_active(lines, path):
    return [i for i, line in enumerate(lines)
            if (INC.match(line.strip()) and INC.match(line.strip()).group(1) == path)]


def find_disabled(lines, path):
    out = []
    for i, line in enumerate(lines):
        m = DISABLED.match(line.strip())
        if m and m.group(1) == path:
            out.append(i)
    return out


def name_of(dest):
    for n, p in MODULES.items():
        if p == dest:
            return n
    if dest.endswith("ktc-filament-overrides.cfg") or dest == "ktc-filament-overrides.cfg":
        return "overrides"
    if dest.startswith("ktc-filament/"):
        return "usage"
    return None


def insert_at(lines, path):
    key = name_of(path)
    rank = {n: i for i, n in enumerate(ORDER)}
    my = rank.get(key, len(ORDER))
    last_before = None
    first_after = None
    for i, line in enumerate(lines):
        m = INC.match(line.strip())
        if not m:
            continue
        name = name_of(m.group(1))
        if name is None:
            continue
        if rank.get(name, 99) < my:
            last_before = i
        elif rank.get(name, 99) > my and first_after is None:
            first_after = i
    if last_before is not None:
        return last_before + 1
    if first_after is not None:
        return first_after
    return len(lines)


def apply_one(lines, path, mode):
    active = find_active(lines, path)
    disabled = find_disabled(lines, path)
    if mode == "keep":
        return lines, "kept" if active else "unchanged"
    if mode == "on":
        if active:
            return lines, "kept"
        lines = lines[:]
        if disabled:
            lines[disabled[-1]] = include_line(path)
            return lines, "added"
        lines.insert(insert_at(lines, path), include_line(path))
        return lines, "added"
    if mode == "off":
        if not active:
            return lines, "unchanged"
        lines = lines[:]
        for i in active:
            lines[i] = "# %s  # disabled by setup.sh; the file was not deleted" % include_line(path)
        return lines, "removed"
    raise SystemExit("bad mode %s" % mode)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("printer_cfg")
    ap.add_argument("--filament", choices=["on", "off", "keep"], default="keep")
    ap.add_argument("--orbiter", choices=["on", "off", "keep"], default="keep")
    ap.add_argument("--usage", choices=["on", "off", "keep", "auto"], default="auto")
    ap.add_argument("--overrides", default="", help="overrides filename, or empty to leave it")
    args = ap.parse_args()
    raw = open(args.printer_cfg, "rb").read()
    marker = re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
    if marker:
        head_b, tail_b = raw[:marker.start()], raw[marker.start():]
    else:
        head_b, tail_b = raw, b""
    text = head_b.decode("utf-8")
    nl = text.endswith("\n") or text == ""
    lines = text.split("\n")
    if lines and lines[-1] == "" and text.endswith("\n"):
        lines = lines[:-1]
    body = lines
    actions = []

    def set_mod(name, mode):
        nonlocal body
        path = MODULES[name]
        body, act = apply_one(body, path, mode)
        if act in ("added", "removed"):
            actions.append("%s %s" % (act.upper(), path))

    fil = args.filament
    if fil == "keep" and args.orbiter == "on":
        if not find_active(body, MODULES["filament"]):
            fil = "on"
    set_mod("filament", fil)
    set_mod("orbiter", args.orbiter)
    if args.orbiter == "off":
        pins = "off"
    elif args.orbiter == "on" or find_active(body, MODULES["orbiter"]):
        pins = "on"
    else:
        pins = "keep"
    set_mod("pins", pins)
    usage = args.usage
    if usage == "auto":
        has = any(p.startswith("ktc-filament/") for p in active_paths(body))
        usage = "on" if has and not find_active(body, MODULES["usage"]) else "keep"
    set_mod("usage", usage)
    if args.overrides:
        body, act = apply_one(body, args.overrides, "on")
        if act == "added":
            actions.append("ADDED " + args.overrides)
    head_out = "\n".join(body) + ("\n" if nl or body else "")
    out = head_out.encode("utf-8") + tail_b
    if out != raw:
        open(args.printer_cfg, "wb").write(out)
    if tail_b and not out.endswith(tail_b):
        sys.stderr.write("SAVE_CONFIG block was not preserved\n")
        sys.exit(1)
    if not actions:
        print("UNCHANGED")
    else:
        print(" ".join(actions))


if __name__ == "__main__":
    main()
