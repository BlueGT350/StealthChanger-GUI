#!/usr/bin/env bash
# StealthChanger-GUI setup (run on the printer's Pi as your normal user).
#
# Moves ~/printer_data/config/ktc-filament onto a git clone of
# BlueGT350/StealthChanger-GUI, so Moonraker's update manager (Mainsail >
# Machine > Update Manager, or Orca's Device tab) keeps it up to date.
# Safe to run again: every step checks first and skips what is already done.
#
#   bash setup.sh            normal run
#   bash setup.sh --no-restart   change files only, restart nothing
#
# What it does:
#  1. clones the public repo over HTTPS to ~/StealthChanger-GUI
#  2. moves the old ktc-filament folder to ~/stealthchanger-backups/<time>/,
#     carries your changed settings into ktc-filament-overrides.cfg, and links
#     ~/printer_data/config/ktc-filament -> ~/StealthChanger-GUI
#  3. points the GUI web page (nginx, port 8090) at the clone
#  4. adds [update_manager stealthchanger-gui] to moonraker.conf if missing
#  5. adds the usage add-on and overrides includes to printer.cfg if missing
#  6. restarts Klipper and Moonraker, but never while printing or paused
set -euo pipefail

REPO_SLUG=BlueGT350/StealthChanger-GUI
BRANCH=main
UM_NAME=stealthchanger-gui

# The SC_* variables exist only for testing setup.sh off the printer.
ORIGIN=${SC_TEST_ORIGIN:-https://github.com/$REPO_SLUG.git}
CLONE=${SC_CLONE_DIR:-$HOME/StealthChanger-GUI}
DATA=${SC_PRINTER_DATA:-$HOME/printer_data}
CONFIG=$DATA/config
MOONRAKER_CONF=$CONFIG/moonraker.conf
PRINTER_CFG=$CONFIG/printer.cfg
LINK=$CONFIG/ktc-filament
OVERRIDES_NAME=ktc-filament-overrides.cfg
OVERRIDES=$CONFIG/$OVERRIDES_NAME
NGINX_DIR=${KTC_NGINX_DIR:-/etc/nginx}
GUI_LINK_DEFAULT=$HOME/ktc-gui      # where gui/install.sh puts the GUI
MOONRAKER_URL=${SC_MOONRAKER_URL:-http://127.0.0.1:7125}
BACKUP=$HOME/stealthchanger-backups/$(date +%Y%m%d-%H%M%S)
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
backup() {  # backup FILE_OR_DIR  (copy, keeps the original in place)
  mkdir -p "$BACKUP"
  cp -a "$1" "$BACKUP/"
  info "backed up $1 -> $BACKUP/$(basename "$1")"
}
CFG_CHANGED=0      # Klipper needs a restart
MR_CHANGED=0       # Moonraker needs a restart

[ "$(id -u)" -ne 0 ] || die "run this as your normal user (the one Klipper runs as), not root or sudo."
for c in git python3; do
  command -v "$c" >/dev/null || die "'$c' is missing (sudo apt install $c)."
done
[ -d "$CONFIG" ] || die "$CONFIG not found. Is this the printer's Pi?"
[ -f "$MOONRAKER_CONF" ] || die "$MOONRAKER_CONF not found."

# ---------------------------------------------------------------- 1. clone
say "1. Clone $REPO_SLUG to $CLONE"
if [ -d "$CLONE/.git" ]; then
  cur=$(git -C "$CLONE" remote get-url origin 2>/dev/null || true)
  if [ "$cur" != "$ORIGIN" ]; then
    git -C "$CLONE" remote set-url origin "$ORIGIN"
    info "origin was '$cur', set to $ORIGIN"
  fi
  if [ -n "$(git -C "$CLONE" status --porcelain --untracked-files=no)" ]; then
    info "the clone has local edits; leaving it as is (Moonraker will flag it as dirty)"
  else
    if git -C "$CLONE" fetch --quiet --tags origin && git -C "$CLONE" merge --quiet --ff-only "origin/$BRANCH" 2>/dev/null; then
      info "already cloned, now at $(git -C "$CLONE" describe --tags --always)"
    else
      info "already cloned (could not fast-forward; use Update Manager)"
    fi
  fi
else
  if [ -e "$CLONE" ]; then
    mkdir -p "$BACKUP" && mv "$CLONE" "$BACKUP/" && info "moved an old $CLONE (not a git clone) to $BACKUP/"
  fi
  git clone --quiet --branch "$BRANCH" "$ORIGIN" "$CLONE"
  info "cloned, version $(git -C "$CLONE" describe --tags --always)"
fi
# nginx (www-data) serves the GUI straight from the clone
chmod o+x "$HOME" "$CLONE" 2>/dev/null || true
chmod -R o+rX "$CLONE/gui" 2>/dev/null || true

# ---------------------------------------------------------------- 2. config link
say "2. Link $LINK -> $CLONE"
if [ -L "$LINK" ]; then
  if [ "$(readlink -f "$LINK")" = "$(readlink -f "$CLONE")" ]; then
    info "already linked"
  else
    info "was a link to $(readlink "$LINK"); relinking"
    ln -sfn "$CLONE" "$LINK"; CFG_CHANGED=1
  fi
else
  if [ -e "$LINK" ]; then
    mkdir -p "$BACKUP"
    mv "$LINK" "$BACKUP/ktc-filament"
    info "moved your old folder to $BACKUP/ktc-filament"
    [ -f "$OVERRIDES" ] || NEW_OVERRIDES=1
  fi
  ln -s "$CLONE" "$LINK"; CFG_CHANGED=1
  info "linked"
fi

if [ ! -f "$OVERRIDES" ]; then
  cat > "$OVERRIDES" <<'OVR'
# ktc-filament-overrides.cfg: YOUR settings for the StealthChanger-GUI modules.
#
# ktc-filament/ is a git clone that Moonraker's Update Manager keeps current,
# so edits inside it would be lost (Mainsail opens those files read-only).
# Put any setting you want to change here instead, under the same [section]
# header. printer.cfg includes this file AFTER the modules, and Klipper merges
# sections with the same name, so list only the lines you change.
#
# Example (from ktc-filament/filament/filament_macros.cfg):
# [gcode_macro _FILAMENT_SETTINGS]
# variable_high_load_temp: 255
OVR
  info "created $OVERRIDES"
  CFG_CHANGED=1
fi
if [ "${NEW_OVERRIDES:-0}" = 1 ] || { [ -d "$BACKUP/ktc-filament" ] && [ -f "$OVERRIDES" ]; }; then
  say "   Carrying your changed settings over"
  set +e
  python3 "$CLONE/tools/migrate_settings.py" --old "$BACKUP/ktc-filament" --new "$CLONE" --out "$OVERRIDES"
  mig=$?
  set -e
  [ "$mig" -le 2 ] || die "settings carry-over failed (your old folder is safe in $BACKUP)"
fi

# ---------------------------------------------------------------- 3. GUI web page
say "3. GUI web page (nginx)"
have_tty() { [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; }
SUDO_OK=""   # "" = not tried yet, 1 = sudo works without a prompt now, 0 = no sudo
sudo_ready() {  # ask for the sudo password at most once, and only on a terminal
  if [ -z "$SUDO_OK" ]; then
    SUDO_OK=0
    if command -v sudo >/dev/null; then
      if sudo -n true 2>/dev/null; then SUDO_OK=1
      elif [ -z "${SC_NO_SUDO_PROMPT:-}" ] && have_tty; then
        info "The nginx site for the GUI is only readable by root. sudo asks for your"
        info "password once to read it (nothing is changed with sudo)."
        # shellcheck disable=SC2024  # the redirect is the prompt's terminal, on purpose
        if sudo -v </dev/tty; then SUDO_OK=1; fi
      fi
    fi
  fi
  [ "$SUDO_OK" = 1 ]
}
site_text() {  # the ktc-gui site file(s), read directly or with sudo
  local f
  for f in "$NGINX_DIR/sites-enabled/ktc-gui" "$NGINX_DIR/sites-available/ktc-gui"; do
    if [ -r "$f" ] && [ -f "$f" ]; then cat "$f"; return 0; fi
  done
  for f in "$NGINX_DIR/sites-enabled/ktc-gui" "$NGINX_DIR/sites-available/ktc-gui"; do
    if [ -e "$f" ] || [ -L "$f" ] || ! [ -r "$(dirname "$f")" ]; then
      if sudo_ready && sudo -n cat "$f" 2>/dev/null; then return 0; fi
    fi
  done
  return 1
}
roots_of() { awk '$1=="root"{v=$2; sub(/;.*/,"",v); gsub(/["'"'"']/,"",v); print v}'; }
gui_root() {  # prints "root<TAB>how it was found", or nothing
  local r
  r=$(site_text 2>/dev/null | roots_of | head -1 || true)
  if [ -n "$r" ]; then printf '%s\tthe ktc-gui nginx site\n' "$r"; return; fi
  # nginx -T dumps the whole running config: any root that serves this GUI
  if sudo_ready; then
    r=$(sudo -n nginx -T 2>/dev/null | roots_of \
        | grep -E '(^|/)(ktc-gui|ktc-filament/gui|StealthChanger-GUI/gui)/?$' | head -1 || true)
    if [ -n "$r" ]; then printf '%s\tnginx -T\n' "$r"; return; fi
  fi
  # install.sh's default place
  if [ -e "$GUI_LINK_DEFAULT" ] || [ -L "$GUI_LINK_DEFAULT" ]; then
    printf '%s\tthe default GUI folder (could not read the nginx config)\n' "$GUI_LINK_DEFAULT"
  fi
}
found=$(gui_root)
root=${found%%$'\t'*}; how=${found#*$'\t'}
root=${root%/}
if [ -z "$root" ]; then
  info "No GUI web page found (no readable ktc-gui nginx site and no $GUI_LINK_DEFAULT)."
  info "To serve the GUI on port 8090, run:"
  info "  bash $CLONE/gui/install.sh --link"
else
  info "GUI web root: $root (from $how)"
  if [ "$(readlink -f "$root" 2>/dev/null)" = "$(readlink -f "$CLONE/gui")" ]; then
    if [ -L "$root" ]; then info "already links to $CLONE/gui, nothing to do"
    else info "already serves the clone, nothing to do"; fi
  elif [[ "$root" == "$HOME/"* ]] && [ "$root" != "$CLONE" ] && [[ "$root" != "$CLONE/"* ]] \
       && [[ "$root" != "$DATA/"* ]]; then
    if [ -L "$root" ]; then
      info "was a link to $(readlink "$root"); relinking"
      ln -sfn "$CLONE/gui" "$root"
    else
      if [ -e "$root" ]; then
        mkdir -p "$BACKUP" && mv "$root" "$BACKUP/$(basename "$root")"
        info "moved the old GUI copy $root to $BACKUP/$(basename "$root")"
      fi
      ln -s "$CLONE/gui" "$root"
    fi
    info "$root now links to $CLONE/gui, so Update Manager updates the page too."
    info "Reload an open GUI page once (Ctrl+F5) if it still shows the old version; from v0.2.2 on it reloads itself after updates."
  else
    info "nginx serves $root, which setup.sh does not move. To serve the clone, run:"
    info "  bash $CLONE/gui/install.sh --link"
  fi
fi

# ---------------------------------------------------------------- 4. moonraker.conf
say "4. moonraker.conf"
if grep -qE "^\[update_manager[[:space:]]+$UM_NAME\]" "$MOONRAKER_CONF"; then
  info "[update_manager $UM_NAME] already there"
else
  backup "$MOONRAKER_CONF"
  nl=""; [ -n "$(tail -c1 "$MOONRAKER_CONF")" ] && nl=$'\n'
  {
    printf '%s' "$nl"
    cat <<UM

[update_manager $UM_NAME]
# StealthChanger GUI + ktc-filament macros. Added by setup.sh.
type: git_repo
channel: stable
path: ~/$(realpath --relative-to="$HOME" "$CLONE" 2>/dev/null || basename "$CLONE")
origin: $ORIGIN
primary_branch: $BRANCH
is_system_service: False
managed_services: klipper
UM
  } >> "$MOONRAKER_CONF"
  info "added [update_manager $UM_NAME]"
  MR_CHANGED=1
fi

# ---------------------------------------------------------------- 5. printer.cfg
say "5. printer.cfg includes"
if [ ! -f "$PRINTER_CFG" ]; then
  info "$PRINTER_CFG not found. Add these after your KTC-Easy include:"
  info "  [include ktc-filament/usage/usage_addon.cfg]"
  info "  [include $OVERRIDES_NAME]"
else
  PCFG_COPY=$(mktemp)
  cp -a "$PRINTER_CFG" "$PCFG_COPY"
  set +e
  out=$(python3 - "$PRINTER_CFG" "$OVERRIDES_NAME" <<'PY'
import re, sys
path, ovr = sys.argv[1], sys.argv[2]
lines = open(path, encoding="utf-8").read().split("\n")
inc = re.compile(r"^\[include\s+(\S+)\s*\]")
def find(name):
    return [i for i, l in enumerate(lines) if (inc.match(l) or [None, None])[1] == name]
mods = [i for i, l in enumerate(lines) if inc.match(l) and inc.match(l).group(1).startswith("ktc-filament/")]
if not mods:
    print("NOINC"); sys.exit(3)
added = []
if not find("ktc-filament/usage/usage_addon.cfg"):
    last = max(i for i in mods if "/filament/" in lines[i] or "/orbiter/" in lines[i])
    lines.insert(last + 1, "[include ktc-filament/usage/usage_addon.cfg]")
    added.append("ktc-filament/usage/usage_addon.cfg")
    mods = [i for i, l in enumerate(lines) if inc.match(l) and inc.match(l).group(1).startswith("ktc-filament/")]
o = find(ovr)
if not o:
    lines.insert(max(mods) + 1, "[include %s]" % ovr)
    added.append(ovr)
elif min(o) < max(mods):
    print("ORDER")
if added:
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print("ADDED " + " ".join(added))
PY
)
  rc=$?
  set -e
  if [ "$rc" -eq 3 ]; then
    info "printer.cfg has no [include ktc-filament/...] lines, so nothing was changed. Add:"
    info "  [include ktc-filament/filament/filament_macros.cfg]"
    info "  [include ktc-filament/orbiter/orbiter_addon.cfg]"
    info "  [include ktc-filament/usage/usage_addon.cfg]"
    info "  [include $OVERRIDES_NAME]"
  elif [ "$rc" -ne 0 ]; then
    die "could not update printer.cfg (unchanged)"
  else
    case "$out" in
      *ADDED*) mkdir -p "$BACKUP" && cp -a "$PCFG_COPY" "$BACKUP/printer.cfg"
               info "added: ${out#*ADDED } (old printer.cfg in $BACKUP)"; CFG_CHANGED=1 ;;
      *) info "includes already there" ;;
    esac
    case "$out" in *ORDER*) info "NOTE: [include $OVERRIDES_NAME] must come AFTER the ktc-filament includes. Move it down." ;; esac
  fi
  rm -f "$PCFG_COPY"
fi

# ---------------------------------------------------------------- 6. restart
say "6. Restart"
api() {  # api GET|POST PATH -> body on stdout, non-zero on failure
  python3 - "$1" "$MOONRAKER_URL$2" <<'PY'
import sys, urllib.request
m, url = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, method=m, data=b"" if m == "POST" else None)
try:
    print(urllib.request.urlopen(req, timeout=10).read().decode())
except Exception as e:
    print(e, file=sys.stderr); sys.exit(1)
PY
}
state=$(api GET "/printer/objects/query?print_stats" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["status"]["print_stats"]["state"])' 2>/dev/null || echo unknown)
info "printer state: $state"
if [ "$RESTART" = 0 ]; then
  info "--no-restart: nothing restarted."
elif [ "$CFG_CHANGED$MR_CHANGED" = 00 ]; then
  info "nothing changed, no restart needed"
elif [ "$state" = printing ] || [ "$state" = paused ]; then
  info "The printer is $state, so nothing was restarted. When it is done, run"
  info "FIRMWARE_RESTART in the console and restart Moonraker (Mainsail > power menu)."
elif [ "$state" = unknown ]; then
  info "Moonraker did not answer, so nothing was restarted. When the printer is idle:"
  info "  sudo systemctl restart klipper moonraker"
else
  if [ "$CFG_CHANGED" = 1 ]; then
    if api POST /printer/restart >/dev/null; then info "Klipper restarting"
    else info "Klipper restart failed; run RESTART in the console"; fi
    for _ in $(seq 1 30); do
      sleep 1
      ks=$(api GET /printer/info 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' 2>/dev/null || true)
      if [ "$ks" = ready ] || [ "$ks" = error ]; then break; fi
    done
    case "${ks:-}" in
      ready) info "Klipper is ready" ;;
      error) info "Klipper reports a config error. Open Mainsail to read it; your old files are in $BACKUP" ;;
      *)     info "Klipper is still starting (state: ${ks:-?}); check Mainsail" ;;
    esac
  fi
  if [ "$MR_CHANGED" = 1 ]; then
    if api POST /server/restart >/dev/null; then info "Moonraker restarting (Mainsail reconnects in a few seconds)"
    else info "Moonraker restart failed; run: sudo systemctl restart moonraker"; fi
  fi
fi

say "Done"
info "Version: $(git -C "$CLONE" describe --tags --always)"
info "Updates: Mainsail > Machine > Update Manager > $UM_NAME (or Orca's Device tab)."
info "Your settings: $OVERRIDES (never touched by updates)."
[ -d "$BACKUP" ] && info "Backups from this run: $BACKUP"
exit 0
