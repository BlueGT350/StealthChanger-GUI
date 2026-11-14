#!/usr/bin/env bash
# StealthChanger-GUI uninstall (run on the printer's Pi as your normal user).
#
# Reverses setup.sh and gui/install.sh. Safe to run again: a second run finds
# nothing left to undo and does not restart Klipper or Moonraker.
#
#   bash uninstall.sh              prompt before the firmware restart
#   bash uninstall.sh --yes        restart without asking
#   bash uninstall.sh --dry-run    print every action, change nothing
#   bash uninstall.sh --purge      also remove ~/StealthChanger-GUI
#
# What it does:
#  1. backs up printer.cfg, moonraker.conf and variables-related files to
#     ~/stealthchanger-backups/<time>/ (it does not delete the variable files)
#  2. removes exact [include ktc-filament/...] lines from printer.cfg
#     (the SAVE_CONFIG #*# block is copied back byte for byte)
#  3. removes the [update_manager stealthchanger-gui] section from moonraker.conf
#  4. runs gui/install.sh --uninstall (nginx site ktc-gui and ~/ktc-gui)
#  5. removes the ~/printer_data/config/ktc-filament symlink only
#  6. leaves the git clone unless --purge is given
#  7. if something changed, firmware-restarts Klipper and restarts Moonraker
#
# The deploy key (~/.ssh/stealthchanger_gui_deploy) is left in place.
# [include ktc-filament-overrides.cfg] is left in place; that file is only
# backed up. SC_* variables exist so tests can point this at a fake home.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: uninstall.sh [--dry-run] [--yes] [--purge]

Reverses setup.sh and gui/install.sh. Safe to run twice.

  --dry-run   print every action, change nothing
  --yes       firmware-restart Klipper and restart Moonraker without asking
  --purge     also remove ~/StealthChanger-GUI (default: keep the clone)
  -h, --help  show this help

With a terminal and without --yes, it asks before restarting. With no
terminal and without --yes, it does not restart. A run that changes nothing
does not restart.
EOF
}

DRY=0
PURGE=0
YES=0
CHANGED=0
BACKED=" "

note_change() { CHANGED=1; }

backup() {  # backup FILE (content, even if FILE is a symlink)
  local src=$1 real base dest
  [ -e "$src" ] || [ -L "$src" ] || return 0
  real=$(readlink -f "$src" 2>/dev/null || true)
  [ -n "$real" ] && [ -f "$real" ] || { info "not a file, not backed up: $src"; return 0; }
  case "$BACKED" in *" $real "*) return 0 ;; esac
  BACKED="$BACKED$real "
  base=$(basename "$real")
  dest=$BACKUP/$base
  if [ "$DRY" != 1 ] && [ -e "$dest" ]; then
    dest=$BACKUP/${base}.$(basename "$(dirname "$real")")
  fi
  if [ "$DRY" = 1 ]; then
    info "would back up $src -> $dest"
    return 0
  fi
  mkdir -p "$BACKUP"
  cp -a "$real" "$dest"
  info "backed up $src -> $dest"
}

list_variables() {  # files under HOME: *variables* in config, overrides, save_variables filename
  python3 - "$CONFIG" "$PRINTER_CFG" <<'PY'
import os, re, sys
config, printer = sys.argv[1], sys.argv[2]
home = os.path.realpath(os.path.expanduser("~"))
found = []

def add(path):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(config, path)
    try:
        path = os.path.realpath(path)
    except OSError:
        return
    if not (path == home or path.startswith(home + os.sep)):
        return
    if not os.path.isfile(path) or path in found:
        return
    found.append(path)

if os.path.isdir(config):
    for name in sorted(os.listdir(config)):
        if "variables" in name.lower() or name == "ktc-filament-overrides.cfg":
            add(os.path.join(config, name))

if printer and os.path.isfile(printer):
    raw = open(printer, "rb").read()
    cut = re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
    head = raw[:cut.start()] if cut else raw
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    in_sv = False
    for line in text.splitlines():
        logical = line.split("#", 1)[0].split(";", 1)[0].strip()
        if logical.startswith("[") and logical.endswith("]"):
            in_sv = logical.replace(" ", "").lower() == "[save_variables]"
            continue
        if not in_sv:
            continue
        m = re.match(r"filename:\s*(\S+)", logical)
        if m:
            add(m.group(1))
for path in found:
    print(path)
PY
}

edit_cfg() {  # edit_cfg printer|moonraker FILE
  local kind=$1 file=$2 out line
  if [ ! -f "$file" ]; then
    info "no $file"
    return 0
  fi
  out=$(python3 - "$kind" "$file" "$DRY" <<'PY'
import os, re, sys, tempfile
kind, path, dry = sys.argv[1], sys.argv[2], sys.argv[3]
raw = open(path, "rb").read()
cut = re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
if cut:
    head_b, tail_b = raw[:cut.start()], raw[cut.start():]
else:
    head_b, tail_b = raw, b""
try:
    head = head_b.decode("utf-8")
except UnicodeDecodeError:
    sys.stderr.write("not utf-8: %s\n" % path)
    sys.exit(1)

def logical(line):
    return line.split("#", 1)[0].split(";", 1)[0].strip()

lines = head.splitlines(keepends=True)
new_lines = []
removed_lines = []
removed = False
if kind == "printer":
    inc = re.compile(r"^\[include[ \t]+(?:ktc-filament/\S+|ktc-filament-orbiter\.cfg)[ \t]*\]$")
    for line in lines:
        if inc.match(logical(line)):
            removed = True
            removed_lines.append(logical(line))
        else:
            new_lines.append(line)
    unchanged_note = "no [include ktc-filament/...] lines"
else:
    header = re.compile(r"^\[update_manager[ \t]+stealthchanger-gui\]$")
    # A following section header. A slash means an [include path], which is
    # not a section, so it is removed with the update_manager block.
    section = re.compile(r"^\[[^\]/]+\]$")
    i = 0
    while i < len(lines):
        if header.match(logical(lines[i])):
            removed = True
            removed_lines.append(lines[i].rstrip("\r\n"))
            i += 1
            while i < len(lines) and not section.match(logical(lines[i])):
                body = lines[i].rstrip("\r\n")
                if body.strip():
                    removed_lines.append(body)
                i += 1
            continue
        new_lines.append(lines[i])
        i += 1
    unchanged_note = "[update_manager stealthchanger-gui] already absent"

if removed and dry != "1":
    new_head = "".join(new_lines).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)) or ".", prefix=".uninstall-")
    try:
        os.write(fd, new_head + tail_b)
        os.close(fd)
        fd = None
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
        os.replace(tmp, path)
        tmp = None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp and os.path.exists(tmp):
            os.remove(tmp)

print("status=" + ("changed" if removed else "unchanged"))
if removed:
    for line in removed_lines:
        print("line=" + line)
else:
    print("note=" + unchanged_note)
print("save=" + ("kept" if tail_b else "none"))
PY
) || die "could not update $file (left unchanged)"
  while IFS= read -r line; do
    case "$line" in
      status=changed) note_change ;;
      status=unchanged) ;;
      line=*)
        if [ "$DRY" = 1 ]; then info "would remove ${line#line=} from $file"
        else info "removed ${line#line=} from $file"; fi ;;
      note=*) info "${line#note=}" ;;
      save=kept) info "SAVE_CONFIG block in $file left unchanged" ;;
      save=none) ;;
      "") ;;
      *) info "$line" ;;
    esac
  done <<< "$out"
}

gui_uninstall() {
  local dest="$HOME/ktc-gui"
  local avail="$NGINX_DIR/sites-available/ktc-gui"
  local enabled="$NGINX_DIR/sites-enabled/ktc-gui"
  local present=0
  if [ -e "$dest" ] || [ -L "$dest" ] || [ -e "$avail" ] || [ -L "$avail" ] \
     || [ -e "$enabled" ] || [ -L "$enabled" ] || [ -e "${avail}.bak" ] || [ -L "${avail}.bak" ]; then
    present=1
  fi
  if [ "$present" = 0 ]; then
    info "nginx site ktc-gui and $dest already gone"
    return 0
  fi
  note_change
  if [ "$DRY" = 1 ]; then
    info "would run: sudo rm -f $enabled $avail ${avail}.bak"
    if command -v nginx >/dev/null 2>&1; then
      info "would run: sudo nginx -t && sudo systemctl reload nginx"
    fi
    if [ -L "$dest" ]; then info "would remove symlink $dest -> $(readlink "$dest")"
    elif [ -d "$dest" ]; then info "would remove directory $dest"
    else info "would remove $dest"; fi
    return 0
  fi
  [ -f "$INSTALL_SH" ] || die "gui/install.sh not found ($INSTALL_SH). Run this from the StealthChanger-GUI clone."
  bash "$INSTALL_SH" --uninstall
}

remove_symlink() {
  local link="$LINK"
  if [ -L "$link" ]; then
    note_change
    if [ "$DRY" = 1 ]; then info "would remove symlink $link -> $(readlink "$link")"
    else rm "$link"; info "removed symlink $link"; fi
  elif [ -e "$link" ]; then
    info "$link is a real folder, not a symlink. Left it in place."
  else
    info "no symlink $link"
  fi
}

clone_is_inside_home() {
  local real home
  real=$(readlink -f "$CLONE" 2>/dev/null || true)
  home=$(readlink -f "$HOME")
  case "$real" in
    "$home"|"$home/"|"") return 1 ;;
    "$home"/*) return 0 ;;
    *) return 1 ;;
  esac
}

check_purge() {
  [ "$PURGE" = 1 ] || return 0
  [ -e "$CLONE" ] || [ -L "$CLONE" ] || return 0
  if ! clone_is_inside_home; then
    die "refusing to purge $CLONE (it resolves outside $HOME)"
  fi
}

purge_clone() {
  if [ "$PURGE" != 1 ]; then
    if [ -e "$CLONE" ] || [ -L "$CLONE" ]; then
      if [ "$DRY" = 1 ]; then info "would keep $CLONE (pass --purge to remove the clone)"
      else info "keeping $CLONE (pass --purge to remove the clone)"; fi
    else
      info "no clone at $CLONE"
    fi
    return 0
  fi
  if [ ! -e "$CLONE" ] && [ ! -L "$CLONE" ]; then
    info "clone already gone: $CLONE"
    return 0
  fi
  # check_purge already refused a path outside HOME; repeat so a later edit cannot skip it
  clone_is_inside_home || die "refusing to purge $CLONE (it resolves outside $HOME)"
  note_change
  if [ "$DRY" = 1 ]; then info "would remove $CLONE"; return 0; fi
  rm -rf "$CLONE"
  info "removed $CLONE"
}

api() {  # api GET|POST PATH -> body on stdout, non-zero on failure
  python3 - "$1" "$MOONRAKER_URL$2" <<'PY'
import sys, urllib.request
m, url = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, method=m, data=b"" if m == "POST" else None)
try:
    print(urllib.request.urlopen(req, timeout=10).read().decode())
except Exception as e:
    print(e, file=sys.stderr)
    sys.exit(1)
PY
}

ask_yes() {
  local prompt=$1 ans=""
  [ "$YES" = 1 ] && return 0
  if [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
    read -r -p "$prompt" ans </dev/tty || true
  elif [ -t 0 ]; then
    read -r -p "$prompt" ans || true
  else
    info "No terminal, so Klipper and Moonraker were not restarted."
    info "Re-run with --yes, or run FIRMWARE_RESTART in the console and restart Moonraker."
    return 1
  fi
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
  esac
  info "Not restarting. Run FIRMWARE_RESTART in the console, then restart Moonraker, when you want to."
  return 1
}

do_restart() {
  local ks="" _
  if api POST /printer/firmware_restart >/dev/null; then
    info "Klipper firmware restart sent"
    for _ in $(seq 1 30); do
      ks=$(api GET /printer/info 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' 2>/dev/null || true)
      if [ "$ks" = ready ] || [ "$ks" = error ]; then break; fi
      sleep 1
    done
    case "${ks:-}" in
      ready) info "Klipper is ready" ;;
      error) info "Klipper reports an error. Old files are in ${BACKUP:-the backup folder}" ;;
      *) info "Klipper is still starting (state: ${ks:-?})" ;;
    esac
  else
    info "Klipper firmware restart failed. Run FIRMWARE_RESTART in the console."
  fi
  if api POST /server/restart >/dev/null; then info "Moonraker restarting"
  else info "Moonraker restart failed. Run: sudo systemctl restart moonraker"; fi
}

restart_services() {
  if [ "$CHANGED" != 1 ]; then
    info "nothing changed, no restart needed"
    return 0
  fi
  if [ "$DRY" = 1 ]; then
    info "would restart Klipper (firmware restart, POST /printer/firmware_restart) and Moonraker (POST /server/restart)"
    return 0
  fi
  if ! ask_yes "Restart Klipper (firmware restart) and Moonraker now? [y/N] "; then
    return 0
  fi
  do_restart
}

main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY=1; shift ;;
      --purge) PURGE=1; shift ;;
      --yes|-y) YES=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) printf '\nERROR: unknown option: %s\n' "$1" >&2; exit 2 ;;
    esac
  done

  [ "$(id -u)" -ne 0 ] || die "run this as your normal user (the one Klipper runs as), not root or sudo."
  command -v python3 >/dev/null || die "python3 is missing"

  local DATA LINK CONFIG n STAMP f vars
  DATA=${SC_PRINTER_DATA:-$HOME/printer_data}
  CONFIG=$DATA/config
  LINK=$CONFIG/ktc-filament
  CLONE=${SC_CLONE_DIR:-$HOME/StealthChanger-GUI}
  NGINX_DIR=${KTC_NGINX_DIR:-/etc/nginx}
  MOONRAKER_URL=${SC_MOONRAKER_URL:-http://127.0.0.1:7125}
  PRINTER_CFG=$CONFIG/printer.cfg
  MOONRAKER_CONF=$CONFIG/moonraker.conf
  INSTALL_SH=$SCRIPT_DIR/gui/install.sh
  STAMP=$(date +%Y%m%d-%H%M%S)
  BACKUP=$HOME/stealthchanger-backups/$STAMP
  n=2
  while [ -e "$BACKUP" ]; do
    BACKUP=$HOME/stealthchanger-backups/${STAMP}-$n
    n=$((n+1))
  done

  say "StealthChanger-GUI uninstall"
  [ "$DRY" = 1 ] && info "dry-run: nothing will be changed"
  check_purge

  say "1. Back up printer.cfg, moonraker.conf and variables files"
  [ -f "$PRINTER_CFG" ] && backup "$PRINTER_CFG"
  [ -f "$MOONRAKER_CONF" ] && backup "$MOONRAKER_CONF"
  [ -f "$CONFIG/ktc-filament-orbiter.cfg" ] && backup "$CONFIG/ktc-filament-orbiter.cfg"
  if [ -d "$CONFIG" ] || [ -f "$PRINTER_CFG" ]; then
    vars=$(list_variables) || die "could not list variables files"
    while IFS= read -r f; do
      [ -n "$f" ] && backup "$f"
    done <<< "$vars"
  else
    info "no $CONFIG"
  fi

  say "2. printer.cfg includes"
  edit_cfg printer "$PRINTER_CFG"

  say "3. moonraker.conf update manager"
  edit_cfg moonraker "$MOONRAKER_CONF"

  say "4. GUI web page (nginx and ~/ktc-gui)"
  gui_uninstall

  say "5. ktc-filament symlink"
  remove_symlink

  say "6. Git clone and Orbiter pin file"
  purge_clone
  local orb_user="$CONFIG/ktc-filament-orbiter.cfg"
  if [ "$PURGE" = 1 ] && { [ -e "$orb_user" ] || [ -L "$orb_user" ]; }; then
    note_change
    if [ "$DRY" = 1 ]; then info "would remove $orb_user"
    else rm -f "$orb_user"; info "removed $orb_user"; fi
  elif [ -e "$orb_user" ] || [ -L "$orb_user" ]; then
    if [ "$DRY" = 1 ]; then info "would keep $orb_user (pass --purge to remove it)"
    else info "keeping $orb_user (pass --purge to remove it)"; fi
  else
    info "no $orb_user"
  fi

  say "7. Restart"
  restart_services

  say "Done"
  if [ "$DRY" = 1 ]; then info "dry-run: nothing was changed"
  elif [ -d "${BACKUP:-}" ]; then info "Backups from this run: $BACKUP"
  fi
  info "The deploy key ~/.ssh/stealthchanger_gui_deploy was left in place."
  info "ktc-filament-overrides.cfg was only backed up, not deleted."
}

main "$@"
