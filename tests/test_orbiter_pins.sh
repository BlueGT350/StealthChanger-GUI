#!/usr/bin/env bash
# Orbiter pins survive a git update: edited pins move into the user file,
# the clone file does not define them again, and a second run does not
# overwrite the user file. Uses Klipper's config reader when it is available.
#   bash tests/test_orbiter_pins.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
PASS=0; FAIL=0
ok() { if eval "$2"; then PASS=$((PASS+1)); echo "  ok    $1"; else FAIL=$((FAIL+1)); echo "  FAIL  $1"; fi; }

KLIPPY=""
for c in "${KLIPPER_PATH:-}" /tmp/klipper/klippy "$HOME/klipper/klippy"; do
  [ -n "$c" ] && [ -f "$c/configfile.py" ] && KLIPPY=$c && break
done

# Old tree: the current repo, but the addon still contains the button blocks
# the way v0.2.6 did. That is the file a user would have edited.
mkdir -p "$T/old"
cp -a "$REPO/." "$T/old/src"
rm -rf "$T/old/src/.git"
python3 - "$T/old/src/orbiter/orbiter_addon.cfg" "$REPO/orbiter/orbiter_pins.cfg" <<'PY'
import sys
addon, pins = sys.argv[1:]
text = open(addon, encoding="utf-8").read()
body = []
keep = False
for line in open(pins, encoding="utf-8"):
    if line.startswith("[gcode_button"):
        keep = True
    if keep:
        body.append(line)
open(addon, "a", encoding="utf-8").write("\n" + "".join(body))
PY
git -C "$T/old/src" init -q -b main
git -C "$T/old/src" add -A
git -C "$T/old/src" -c user.name=t -c user.email=t@t commit -qm old
git clone -q "$T/old/src" "$T/work"

# User edited one pin. Not committed: this is what makes Update Manager say dirty.
sed -i 's/\^nhkT0:gpio13/^nhkT0:gpio99/' "$T/work/orbiter/orbiter_addon.cfg"
mkdir -p "$T/home/printer_data/config"
cat > "$T/home/printer_data/config/printer.cfg" <<'CFG'
[include ktc-filament/filament/filament_macros.cfg]
[include ktc-filament/orbiter/orbiter_addon.cfg]

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# [tool T0]
#*# gcode_x_offset = 0.125
CFG
cp "$T/home/printer_data/config/printer.cfg" "$T/printer.before"
python3 - "$T/home/printer_data/config/printer.cfg" <<'PY' > "$T/tail.before"
import re, sys
raw=open(sys.argv[1],"rb").read()
m=re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
sys.stdout.buffer.write(raw[m.start():])
PY

echo "1. migrate edited pins out of a dirty clone"
OUT=$(env HOME="$T/home" python3 "$REPO/tools/migrate_orbiter_pins.py" \
  --clone "$T/work" --config "$T/home/printer_data/config" \
  --backup-dir "$T/home/stealthchanger-backups/mig" --home "$T/home" 2>&1)
RC=$?
ok "migrate exits 0" '[ $RC -eq 0 ]'
ok "reports MIGRATED and a backup" 'grep -q "^MIGRATED$" <<<"$OUT" && grep -q "^BACKUP " <<<"$OUT"'
ok "user file has the edited pin" 'grep -q "pin: \\^nhkT0:gpio99" "$T/home/printer_data/config/ktc-filament-orbiter.cfg"'
ok "backup has the edited pin" 'grep -q "gpio99" "$T/home/stealthchanger-backups/mig/orbiter_addon.cfg"'
ok "clone addon matches git again (Update Manager can pull)" 'git -C "$T/work" diff --quiet -- orbiter/orbiter_addon.cfg'
ok "SAVE_CONFIG bytes unchanged and the pin include was added" \
  'python3 - "$T/home/printer_data/config/printer.cfg" "$T/tail.before" <<'"'"'PY'"'"'
import re, sys
raw=open(sys.argv[1],"rb").read()
m=re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
open(sys.argv[2],"rb").read()==raw[m.start():] or sys.exit(1)
text=raw[:m.start()].decode()
sys.exit(0 if "[include ktc-filament-orbiter.cfg]" in text else 1)
PY'
cp -a "$T/home/printer_data/config/ktc-filament-orbiter.cfg" "$T/user-once"

echo "2. second run does not overwrite"
OUT=$(env HOME="$T/home" python3 "$REPO/tools/migrate_orbiter_pins.py" \
  --clone "$T/work" --config "$T/home/printer_data/config" \
  --backup-dir "$T/home/stealthchanger-backups/mig2" --home "$T/home" 2>&1)
ok "second run keeps the file" 'grep -q "^KEPT$" <<<"$OUT" && cmp -s "$T/user-once" "$T/home/printer_data/config/ktc-filament-orbiter.cfg"'

echo "3. update removes clone pins; re-running migration keeps the user file"
# The published update deletes the button blocks from the clone. Pins were
# already copied out by the migrate step above (what setup.sh does). The
# pull itself does not migrate; a later setup.sh run must leave the file.
cp "$REPO/orbiter/orbiter_addon.cfg" "$T/old/src/orbiter/orbiter_addon.cfg"
cp "$REPO/orbiter/orbiter_pins.cfg" "$T/old/src/orbiter/orbiter_pins.cfg"
git -C "$T/old/src" add -A
git -C "$T/old/src" -c user.name=t -c user.email=t@t commit -qm new
git -C "$T/work" pull --quiet
ok "after pull the clone defines no button pins" '! grep -q "^\[gcode_button" "$T/work/orbiter/orbiter_addon.cfg"'
ok "edited pin still in the user file" 'grep -q "pin: \\^nhkT0:gpio99" "$T/home/printer_data/config/ktc-filament-orbiter.cfg" && cmp -s "$T/user-once" "$T/home/printer_data/config/ktc-filament-orbiter.cfg"'
OUT=$(env HOME="$T/home" python3 "$REPO/tools/migrate_orbiter_pins.py" \
  --clone "$T/work" --config "$T/home/printer_data/config" \
  --backup-dir "$T/home/stealthchanger-backups/after" --home "$T/home" 2>&1)
ok "setup.sh migrate after the update does not overwrite" 'grep -q "^KEPT$" <<<"$OUT" && cmp -s "$T/user-once" "$T/home/printer_data/config/ktc-filament-orbiter.cfg"'

echo "4. Klipper sees each pin once"
if [ -n "$KLIPPY" ]; then
  python3 - "$KLIPPY" "$T/home/printer_data/config/printer.cfg" "$T/work" "$T/home/printer_data/config/ktc-filament-orbiter.cfg" <<'PY'
import importlib.util, os, sys
klippy, printer, clone, user = sys.argv[1:]
# Point the includes at this sandbox: printer.cfg names ktc-filament/...
# which is not linked here, so build a tiny printer that includes the two files.
cfg = os.path.join(os.path.dirname(printer), "pins-check.cfg")
open(cfg, "w").write("[include %s]\n[include %s]\n" % (
    os.path.join(clone, "orbiter/orbiter_addon.cfg"), user))
spec = importlib.util.spec_from_file_location("klippy_configfile", os.path.join(klippy, "configfile.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
fc = mod.ConfigFileReader().build_fileconfig_with_includes(open(cfg).read(), cfg)
pins = []
names = []
for sec in fc.sections():
    if sec.split()[0] == "gcode_button":
        names.append(sec)
        pins.append(fc.get(sec, "pin"))
addon = open(os.path.join(clone, "orbiter/orbiter_addon.cfg")).read().splitlines()
if any(line.startswith("[gcode_button") for line in addon):
    sys.exit(2)
if len(pins) != len(set(pins)) or len(names) != len(set(names)):
    sys.exit(3)
if "^nhkT0:gpio99" not in pins:
    sys.exit(4)
if pins.count("^nhkT0:gpio13") or pins.count("^nhkT0:gpio99") != 1:
    sys.exit(5)
PY
  krc=$?
  ok "Klipper config reader: one gpio99 pin, none left in the clone" '[ $krc -eq 0 ]'
else
  echo "  skip  Klipper config reader (no KLIPPER_PATH)"
fi

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
