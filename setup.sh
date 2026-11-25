#!/usr/bin/env bash
# StealthChanger-GUI setup (run on the printer's Pi as your normal user).
#
# Moves ~/printer_data/config/ktc-filament onto a git clone of
# BlueGT350/StealthChanger-GUI, so Moonraker's update manager (Mainsail >
# Machine > Update Manager, or Orca's Device tab) keeps it up to date.
# Safe to run again: every step checks first and skips what is already done.
#
#   bash setup.sh                         ask about Orbiter, then install
#   bash setup.sh --orbiter               include the Orbiter button add-on
#   bash setup.sh --no-orbiter            core load/unload only, no buttons
#   bash setup.sh --no-restart            change files only, restart nothing
#   bash setup.sh --origin https://github.com/ORG/REPO.git
#
# The clone uses HTTPS. The default origin is
# https://github.com/BlueGT350/StealthChanger-GUI.git. There is no deploy
# key. --origin URL sets the git remote and the Update Manager origin.
# Without --origin, an existing origin remote is kept and used for
# moonraker.conf. A fresh install with no origin uses the public repo.
# An origin line that already matches that remote is left as it is.
#
# Orbiter button pins are written once to
# ~/printer_data/config/ktc-filament-orbiter.cfg and are not overwritten.
# See CHANGELOG.md.
#
# What it does:
#  1. clones the repo to ~/StealthChanger-GUI over HTTPS
#  2. moves Orbiter pins out of the clone (backup under stealthchanger-backups)
#  3. moves an old ktc-filament folder to ~/stealthchanger-backups/<time>/,
#     carries changed settings into ktc-filament-overrides.cfg, and links
#     ~/printer_data/config/ktc-filament -> ~/StealthChanger-GUI
#  4. points the GUI web page (nginx, port 8090) at the clone
#  5. installs the firmware helper (127.0.0.1 only) and, on a real printer,
#     refreshes the nginx site so /firmware/ is same-origin
#  6. adds [update_manager stealthchanger-gui] to moonraker.conf if missing
#  7. adds the module includes printer.cfg is missing
#  8. restarts Klipper and Moonraker, but never while printing or paused
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PUBLIC_ORIGIN=https://github.com/BlueGT350/StealthChanger-GUI.git
BRANCH=main
UM_NAME=stealthchanger-gui

# The SC_* variables exist only for testing setup.sh off the printer.
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
ORB_MODE=keep
ORIGIN_ARG=
ORIGIN_EXPLICIT=0
NONINTERACTIVE=${SC_NONINTERACTIVE:-0}

say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
backup() {  # backup FILE_OR_DIR  (copy, keeps the original in place)
  mkdir -p "$BACKUP"
  cp -a "$1" "$BACKUP/"
  info "backed up $1 -> $BACKUP/$(basename "$1")"
}
have_tty() { [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; }
while [ $# -gt 0 ]; do
  case "$1" in
    --no-restart) RESTART=0 ;;
    --noninteractive) NONINTERACTIVE=1 ;;
    --orbiter)
      [ "$ORB_MODE" = keep ] || die "use only one of --orbiter and --no-orbiter"
      ORB_MODE=on ;;
    --no-orbiter)
      [ "$ORB_MODE" = keep ] || die "use only one of --orbiter and --no-orbiter"
      ORB_MODE=off ;;
    --origin)
      shift
      [ -n "${1:-}" ] || die "--origin needs a git URL (https://...)"
      ORIGIN_ARG=$1
      ORIGIN_EXPLICIT=1 ;;
    --origin=*)
      ORIGIN_ARG=${1#--origin=}
      [ -n "$ORIGIN_ARG" ] || die "--origin needs a git URL"
      ORIGIN_EXPLICIT=1 ;;
    -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done
# SC_TEST_ORIGIN is the clone URL tests use when nothing is cloned yet.
# It is not --origin: an existing origin remote is kept.
if [ -n "${SC_TEST_ORIGIN:-}" ] && [ "$ORIGIN_EXPLICIT" = 0 ]; then
  ORIGIN_ARG=$SC_TEST_ORIGIN
  NONINTERACTIVE=1
fi
if [ "$ORIGIN_EXPLICIT" = 1 ]; then
  ORIGIN=$ORIGIN_ARG
elif [ -n "$ORIGIN_ARG" ]; then
  ORIGIN=$ORIGIN_ARG
else
  ORIGIN=$PUBLIC_ORIGIN
fi

CFG_CHANGED=0      # Klipper needs a restart
MR_CHANGED=0       # Moonraker needs a restart

[ "$(id -u)" -ne 0 ] || die "run this as your normal user (the one Klipper runs as), not root or sudo."
for c in git python3; do
  command -v "$c" >/dev/null || die "'$c' is missing."
done
[ -d "$CONFIG" ] || die "$CONFIG not found. Is this the printer's Pi?"
[ -f "$MOONRAKER_CONF" ] || die "$MOONRAKER_CONF not found."

pin_migrate() {  # save Orbiter pins before git fetch can reset them
  local src=${1:-} mig out
  mig=$SCRIPT_DIR/tools/migrate_orbiter_pins.py
  [ -f "$mig" ] || mig=$CLONE/tools/migrate_orbiter_pins.py
  [ -f "$mig" ] || { info "Orbiter pin migration script not found; skipping"; return 0; }
  local args=(python3 "$mig" --config "$CONFIG" --backup-dir "$BACKUP" --home "$HOME")
  [ -d "${CLONE:-}/.git" ] && args+=(--clone "$CLONE")
  [ -n "$src" ] && args+=(--source "$src")
  out=$("${args[@]}") || die "Orbiter pin migration failed (clone left as it was if git had not moved yet)"
  local line
  while IFS= read -r line; do
    [ -n "$line" ] && info "$line"
  done <<< "$out"
  case "$out" in *RESTART*) CFG_CHANGED=1 ;; esac
}

# ---------------------------------------------------------------- 1. pins, then clone
say "1. Clone $PUBLIC_ORIGIN"
# An old real folder (not yet the symlink) still has the edited pins.
if [ -d "$LINK/orbiter" ] && [ ! -L "$LINK" ]; then
  pin_migrate "$LINK/orbiter/orbiter_addon.cfg"
fi
if [ -d "$CLONE/.git" ]; then
  pin_migrate
fi
if [ -d "$CLONE/.git" ]; then
  # The URL stored in the repo, not a rewritten insteadOf URL. A rewrite can
  # embed credentials, and that must not be copied into moonraker.conf.
  cur=$(git -C "$CLONE" config --get remote.origin.url 2>/dev/null || true)
  if [ "$ORIGIN_EXPLICIT" = 1 ]; then
    if [ -z "$cur" ]; then
      git -C "$CLONE" remote add origin "$ORIGIN"
      info "set origin to $ORIGIN"
    elif [ "$cur" != "$ORIGIN" ]; then
      git -C "$CLONE" remote set-url origin "$ORIGIN"
      info "origin was '$cur', set to $ORIGIN"
    fi
  elif [ -n "$cur" ]; then
    if [ "$cur" != "$ORIGIN" ]; then
      info "keeping existing origin $cur"
    fi
    ORIGIN=$cur
  else
    git -C "$CLONE" remote add origin "$ORIGIN"
    info "no origin set; using $ORIGIN"
  fi
  info "git origin for this run: $ORIGIN"
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
  info "git origin for this run: $ORIGIN"
  if [ -e "$CLONE" ]; then
    mkdir -p "$BACKUP" && mv "$CLONE" "$BACKUP/" && info "moved an old $CLONE (not a git clone) to $BACKUP/"
  fi
  git clone --quiet --branch "$BRANCH" "$ORIGIN" "$CLONE"
  info "cloned, version $(git -C "$CLONE" describe --tags --always)"
  pin_migrate
fi
# nginx (www-data) serves the GUI straight from the clone
chmod o+x "$HOME" "$CLONE" 2>/dev/null || true
chmod -R o+rX "$CLONE/gui" 2>/dev/null || true

# ---------------------------------------------------------------- 3. config link
say "3. Link $LINK -> $CLONE"
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

# ---------------------------------------------------------------- 4. GUI web page
say "4. GUI web page (nginx)"
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

# ---------------------------------------------------------------- 4b. firmware helper
# User files (menuconfig) live outside the clone and are never overwritten.
# The unit is a user service. systemctl is skipped when KTC_NGINX_DIR is set,
# which the setup tests do, so a test run cannot enable a service on this machine.
install_firmware_helper() {
  local unit_dir unit py dest cfg tmp
  unit_dir="$HOME/.config/systemd/user"
  unit="$unit_dir/stealthchanger-firmware.service"
  py=$(command -v python3)
  dest="$CLONE/firmware/firmware_helper.py"
  cfg="$CONFIG/stealthchanger-firmware"
  mkdir -p "$unit_dir" "$cfg"
  if [ ! -f "$dest" ]; then
    info "firmware helper source not in this clone ($dest); skipping"
    return 0
  fi
  tmp=$(mktemp)
  cat > "$tmp" <<EOF
[Unit]
Description=StealthChanger firmware helper
After=network.target

[Service]
Type=simple
WorkingDirectory=$HOME
ExecStart=$py $dest
# A clean exit means the helper noticed its own code changed (Moonraker
# cannot restart a --user service). Restart=always brings the new code back.
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
  if [ -f "$unit" ] && cmp -s "$tmp" "$unit"; then
    info "firmware helper unit already installed"
    rm -f "$tmp"
  else
    mv "$tmp" "$unit"
    info "wrote $unit (listens on 127.0.0.1:7140)"
  fi
  info "MCU menuconfig files: $cfg (not in the git clone, not overwritten by updates)"
  if [ "${KTC_NGINX_DIR:-/etc/nginx}" != /etc/nginx ]; then
    info "firmware helper unit is in place (systemd enable skipped in this environment)"
    return 0
  fi
  if command -v nginx >/dev/null 2>&1 && sudo_ready; then
    if bash "$CLONE/gui/install.sh" --link; then
      info "nginx proxies /firmware/ to the helper"
    else
      info "could not refresh the nginx site. Re-run: bash $CLONE/gui/install.sh --link"
    fi
  else
    info "To proxy /firmware/ on the GUI port, run: bash $CLONE/gui/install.sh --link"
  fi
  if ! command -v systemctl >/dev/null 2>&1; then
    info "systemctl not found. Start the helper with: $py $dest"
    return 0
  fi
  systemctl --user daemon-reload || info "could not reload the user systemd (the unit file is in place)"
  # enable --now does not restart a helper that is already running, so an
  # update would leave the old process in place. Enable, then restart.
  if ! systemctl --user enable stealthchanger-firmware.service; then
    info "could not enable the firmware helper. Try: systemctl --user enable stealthchanger-firmware.service && systemctl --user restart stealthchanger-firmware.service"
    return 0
  fi
  systemctl --user restart stealthchanger-firmware.service \
    || info "could not restart the firmware helper. Try: systemctl --user restart stealthchanger-firmware.service"
}
say "4b. Firmware helper"
install_firmware_helper

# ---------------------------------------------------------------- 5. moonraker.conf
say "5. moonraker.conf"
if grep -qE "^\[update_manager[[:space:]]+$UM_NAME\]" "$MOONRAKER_CONF"; then
  # Point the section at the git remote (or --origin). Leave the file alone
  # when the origin line already matches, so a matching line is not rewritten.
  cp -a "$MOONRAKER_CONF" "$MOONRAKER_CONF.origin-bak"
  um_out=$(python3 - "$MOONRAKER_CONF" "$UM_NAME" "$ORIGIN" <<'PY'
import sys
path, name, origin = sys.argv[1:]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
header = "[update_manager %s]" % name
start = None
for i, line in enumerate(lines):
    if line.strip() == header:
        start = i
        break
if start is None:
    print("MISSING"); sys.exit(0)
end = len(lines)
for j in range(start + 1, len(lines)):
    if lines[j].startswith("[") and not lines[j][:1].isspace():
        end = j
        break
changed = False
for j in range(start + 1, end):
    if lines[j].lstrip().startswith("origin:"):
        new = "origin: %s\n" % origin
        if lines[j].rstrip("\r\n") != new.rstrip("\n"):
            lines[j] = new
            changed = True
        break
else:
    lines.insert(end, "origin: %s\n" % origin)
    changed = True
if changed:
    open(path, "w", encoding="utf-8").write("".join(lines))
    print("UPDATED")
else:
    print("SAME")
PY
)
  if [ "$um_out" = UPDATED ]; then
    info "set [update_manager $UM_NAME] origin to $ORIGIN"
    MR_CHANGED=1
  else
    rm -f "$MOONRAKER_CONF.origin-bak"
    info "[update_manager $UM_NAME] origin already $ORIGIN"
  fi
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
  info "added [update_manager $UM_NAME] origin $ORIGIN"
  MR_CHANGED=1
fi

# ---------------------------------------------------------------- 6. printer.cfg
say "6. printer.cfg includes"
cfg_has() { [ -f "$PRINTER_CFG" ] && grep -qE "^\[include[[:space:]]+$1[[:space:]]*\]" "$PRINTER_CFG"; }
if [ "$NONINTERACTIVE" = 0 ] && have_tty && [ "$ORB_MODE" = keep ]; then
  def=Y
  cfg_has 'ktc-filament/orbiter/orbiter_addon.cfg' || def=N
  if [ ! -f "$PRINTER_CFG" ] || ! cfg_has 'ktc-filament/filament/filament_macros.cfg'; then
    def=Y
  fi
  if [ "$def" = Y ]; then
    read -r -p "   Include the Orbiter button add-on? [Y/n] " ans </dev/tty || ans=
    case "$ans" in n|N|no|NO) ORB_MODE=off ;; *) ORB_MODE=on ;; esac
  else
    read -r -p "   Include the Orbiter button add-on? [y/N] " ans </dev/tty || ans=
    case "$ans" in y|Y|yes|YES) ORB_MODE=on ;; *) ORB_MODE=off ;; esac
  fi
fi
info "Orbiter include: $ORB_MODE"
if [ ! -f "$PRINTER_CFG" ]; then
  info "$PRINTER_CFG not found. Add these after your KTC-Easy include:"
  info "  [include ktc-filament/filament/filament_macros.cfg]"
  [ "$ORB_MODE" != off ] && info "  [include ktc-filament/orbiter/orbiter_addon.cfg]"
  [ "$ORB_MODE" != off ] && info "  [include ktc-filament-orbiter.cfg]"
  info "  [include ktc-filament/usage/usage_addon.cfg]"
  info "  [include $OVERRIDES_NAME]"
else
  has_ktc=0
  grep -qE '^\[include[[:space:]]+ktc-filament/' "$PRINTER_CFG" && has_ktc=1
  if [ "$has_ktc" = 0 ] && [ "$ORB_MODE" = keep ]; then
    info "printer.cfg has no [include ktc-filament/...] lines, so nothing was changed. Add:"
    info "  [include ktc-filament/filament/filament_macros.cfg]"
    info "  [include ktc-filament/orbiter/orbiter_addon.cfg]   # optional, --orbiter"
    info "  [include ktc-filament-orbiter.cfg]                # your button pins"
    info "  [include ktc-filament/usage/usage_addon.cfg]"
    info "  [include $OVERRIDES_NAME]"
  else
    PCFG_COPY=$(mktemp)
    cp -a "$PRINTER_CFG" "$PCFG_COPY"
    FIL_MODE=keep
    [ "$has_ktc" = 0 ] && FIL_MODE=on
    inc_py=$SCRIPT_DIR/tools/printer_includes.py
    [ -f "$inc_py" ] || inc_py=$CLONE/tools/printer_includes.py
    out=$(python3 "$inc_py" "$PRINTER_CFG" \
      --filament "$FIL_MODE" --orbiter "$ORB_MODE" \
      --usage auto --overrides "$OVERRIDES_NAME") || die "could not update printer.cfg (left unchanged if the write failed)"
    case "$out" in
      *ADDED*|*REMOVED*)
        mkdir -p "$BACKUP" && cp -a "$PCFG_COPY" "$BACKUP/printer.cfg"
        info "$out (old printer.cfg in $BACKUP)"; CFG_CHANGED=1 ;;
      *) info "includes already match ($out)" ;;
    esac
    rm -f "$PCFG_COPY"
  fi
fi

# ---------------------------------------------------------------- 7. restart
say "7. Restart"
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
