#!/usr/bin/env python3
"""Carry hand-edited settings from an old ktc-filament folder into an
overrides file, for setup.sh's one-time move to the git clone.

    python3 tools/migrate_settings.py --old BACKUP_DIR --new REPO_DIR --out FILE

For each module file it compares every single-line option (variable_*,
[save_variables] filename, ...) between the old copy and the repo copy.
A value that differs is a setting you changed, and is written to FILE under
its [section] header (Klipper merges sections with the same name, and FILE is
included after the modules, so its values win). Version markers are skipped.

Edits to macro code (gcode: blocks) cannot be carried over automatically.
The script detects them by comparing the old file, with setting values
masked, against the released versions, and lists the file so you can compare
it by hand. Exit code: 0 = done, 2 = done but some files need a manual look.
Only the standard library is used (runs on the Pi's system python3).
"""
import argparse
import datetime
import hashlib
import os
import re
import sys

FILES = ("filament/filament_macros.cfg", "orbiter/orbiter_addon.cfg",
         "usage/usage_addon.cfg")
SKIP = re.compile(r"^(variable_module_version|variable_requires_\w+|description)$")
OPT = re.compile(r"^([A-Za-z0-9_.\-]+)\s*[:=](.*)$")

# sha256 of each released file with setting values masked (see _masked).
# Lets the script tell "only settings changed" from "macro code was edited".
RELEASED = {
    "filament/filament_macros.cfg": {
        "0.1.0": "8d2f7dadf28f54299d0c2d3a9577a43bd9b74e923bce189739ce9a2f88c637d6", "0.2.0": "57fc0ad257fa8de8fc337aa6938d7554e25c283a09036fd024847cad1e842217"},
    "orbiter/orbiter_addon.cfg": {
        "0.1.0": "3e1a6c631a2f3a5a348e13e060d81883cdb12fe925b35eab63a0f47f00c478ef",
        "0.2.0": "3e1a6c631a2f3a5a348e13e060d81883cdb12fe925b35eab63a0f47f00c478ef",
        "0.2.7": "bfe1bb4dcf55d39180e6c5d171f8fdf28ae98ad5d685d53613a4adb70d466365"},
    "usage/usage_addon.cfg": {
        "0.2.0": "82bad8a3ef52a4aa5a453a781b1a8bc623cba0e747f5edbd745a50c4b87073d9"},
}


def _value(raw):
    # Klipper (configparser) treats "#" or ";" after whitespace as a comment
    return re.split(r"\s[#;]", raw, maxsplit=1)[0].strip()


def parse(text):
    """{section: {option: (value, multiline)}} in file order."""
    out, sec, last = {}, None, None
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if line[0] in " \t":
            if sec is not None and last is not None:
                v, _ = out[sec][last]
                out[sec][last] = (v + "\n" + s, True)
            continue
        if s.startswith("["):
            sec = s[1:s.index("]")].strip() if "]" in s else s[1:]
            out.setdefault(sec, {})
            last = None
            continue
        m = OPT.match(line)
        if m and sec is not None:
            last = m.group(1).lower()
            out[sec][last] = (_value(m.group(2)), False)
    for sec in out:
        for k, (v, multi) in list(out[sec].items()):
            if multi:
                out[sec][k] = (v, True)
    return out


def _masked(text):
    keep = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s.strip() or s.strip()[0] in "#;":
            continue
        m = OPT.match(s)
        if m and _value(m.group(2)):
            s = m.group(1) + ":"
        keep.append(s)
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()


def compare(old_text, new_text):
    old, new = parse(old_text), parse(new_text)
    changes, dropped = [], []
    for sec, opts in old.items():
        for k, (v, multi) in opts.items():
            if multi or SKIP.match(k):
                continue
            if sec not in new or k not in new[sec]:
                dropped.append((sec, k, v))
            elif new[sec][k][1] is False and new[sec][k][0] != v:
                changes.append((sec, k, v, new[sec][k][0]))
    return changes, dropped


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", required=True, help="old ktc-filament folder (backup)")
    ap.add_argument("--new", required=True, help="repo folder")
    ap.add_argument("--out", help="overrides file to append to (omit for a dry run)")
    ap.add_argument("--hash", action="store_true", help="print masked hashes of --new and exit")
    a = ap.parse_args(argv)

    if a.hash:
        for f in FILES:
            p = os.path.join(a.new, f)
            if os.path.isfile(p):
                print(f, _masked(open(p, encoding="utf-8").read()))
        return 0

    block, manual = [], []
    for f in FILES:
        po, pn = os.path.join(a.old, f), os.path.join(a.new, f)
        if not (os.path.isfile(po) and os.path.isfile(pn)):
            continue
        old_text = open(po, encoding="utf-8", errors="replace").read()
        new_text = open(pn, encoding="utf-8", errors="replace").read()
        changes, dropped = compare(old_text, new_text)
        if _masked(old_text) not in RELEASED.get(f, {}).values():
            manual.append(f)
        if changes:
            block.append("\n# from your old %s" % f)
            sec_done = None
            for sec, k, v, newv in changes:
                if sec != sec_done:
                    block.append("[%s]" % sec)
                    sec_done = sec
                block.append("%s: %s    # default %s" % (k, v, newv))
                print("  carried over: [%s] %s: %s (default %s)" % (sec, k, v, newv))
        for sec, k, v in dropped:
            print("  not carried (no longer in the module): [%s] %s: %s" % (sec, k, v))

    if block and a.out:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(a.out, "a", encoding="utf-8") as fh:
            fh.write("\n# ---- carried over by setup.sh on %s ----" % stamp)
            fh.write("\n".join(block) + "\n")
    if not block:
        print("  no changed settings found, nothing to carry over")
    for f in manual:
        print("  NOTE: your old %s has edits beyond the settings (macro code or"
              " options). They were NOT carried over; compare it with the backup by hand." % f)
    return 2 if manual else 0


if __name__ == "__main__":
    sys.exit(main())
