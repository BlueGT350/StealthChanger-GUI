#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2088
# uninstall.sh against a fake HOME: fake nginx/sudo, a local Moonraker that
# records every request, and printer.cfg with a SAVE_CONFIG block.
#   bash tests/test_uninstall.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] || { echo "run as a normal user"; exit 1; }
T=$(mktemp -d)
MPID=""
cleanup() { kill "$MPID" 2>/dev/null || true; chmod -R u+rwX "$T" 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
PASS=0; FAIL=0
ok() {
  if eval "$2"; then PASS=$((PASS+1)); echo "  ok    $1"
  else FAIL=$((FAIL+1)); echo "  FAIL  $1"; [ -n "${OUT:-}" ] && printf '%s\n' "$OUT" | tail -n 30; fi
}

mkdir -p "$T/bin"
cat > "$T/bin/sudo" <<'SUDO'
#!/usr/bin/env bash
[ -n "${SC_SUDO_LOG:-}" ] && printf '%s\n' "$*" >> "$SC_SUDO_LOG"
while [ $# -gt 0 ]; do case "$1" in -n|-v) shift ;; -u) shift 2 ;; *) break ;; esac; done
case "${1:-}" in
  rm)
    shift
    while [ $# -gt 0 ]; do case "$1" in -*) shift ;; *) break ;; esac; done
    for f in "$@"; do
      case "$f" in
        "$HOME"/nginx/*) rm -f "$f" ;;
        *) echo "fake sudo refusing rm $f" >&2; exit 1 ;;
      esac
    done
    ;;
  nginx|systemctl) exit 0 ;;
  *) echo "fake sudo unexpected: $*" >&2; exit 1 ;;
esac
SUDO
cat > "$T/bin/nginx" <<'NGINX'
#!/bin/sh
exit 0
NGINX
chmod +x "$T/bin/sudo" "$T/bin/nginx"

cat > "$T/moon.py" <<'PY'
#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
LOG = os.environ["SC_HTTP_LOG"]
class H(BaseHTTPRequestHandler):
    def _rec(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (self.command, self.path))
    def _send(self, code, body):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self._rec()
        if self.path.startswith("/printer/info"):
            self._send(200, '{"result":{"state":"ready"}}')
        else:
            self._send(500, "{}")
    def do_POST(self):
        self._rec()
        self._send(200, '{"result":"ok"}')
    def log_message(self, fmt, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), H)
print(server.server_address[1], flush=True)
server.serve_forever()
PY
SC_HTTP_LOG=$T/http.log python3 "$T/moon.py" > "$T/moon.port" &
MPID=$!
for _ in $(seq 1 50); do [ -s "$T/moon.port" ] && break; sleep 0.05; done
PORT=$(cat "$T/moon.port")
[ -n "$PORT" ] || { echo "moonraker stub did not start"; exit 1; }

mkhome() {  # mkhome NAME  -> $H with a setup.sh-shaped install
  H=$T/$1
  mkdir -p "$H/printer_data/config" "$H/nginx/sites-available" "$H/nginx/sites-enabled" \
    "$H/StealthChanger-GUI/gui"
  cat > "$H/printer_data/config/printer.cfg" <<'CFG'
# StealthChanger test printer.cfg
[include ktc-filament/filament/filament_macros.cfg]
[include ktc-filament/orbiter/orbiter_addon.cfg]
[include ktc-filament-orbiter.cfg]
[include ktc-filament/usage/usage_addon.cfg]
[include ktc-filament/usage/usage_addon.cfg] # not a full-line comment
# [include ktc-filament/filament/filament_macros.cfg]
[include ktc-filament-overrides.cfg]
[include other.cfg]

[save_variables]
filename: ~/printer_data/config/custom-vars.cfg

[printer]
kinematics: none

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [include ktc-filament/filament/filament_macros.cfg]
#*# filename: /etc/passwd
#*# [extruder]
#*# control = pid
#*# pid_kp = 22.123
CFG
  cat > "$H/printer_data/config/moonraker.conf" <<'MR'
[server]
host: 0.0.0.0
port: 7125

[update_manager stealthchanger-gui]
# StealthChanger GUI + ktc-filament macros. Added by setup.sh.
type: git_repo
channel: stable
path: ~/StealthChanger-GUI
origin: git@github-stealthchanger:BlueGT350/StealthChanger-GUI.git
primary_branch: main
is_system_service: False
managed_services: klipper

[update_manager mainsail]
type: web
channel: stable
repo: mainsail-crew/mainsail
# keep-me

[update_manager stealthchanger-gui-extra]
type: git_repo
path: ~/nope
MR
  printf 't0_loaded = True\n' > "$H/printer_data/config/variables.cfg"
  printf '[gcode_button T0_orbiter_button]\npin: ^nhkT0:gpio77\npress_gcode:\n  _ORBITER_BUTTON TOOL=0\n' > "$H/printer_data/config/ktc-filament-orbiter.cfg"
  printf 't0_color = FF0000\n' > "$H/printer_data/config/custom-vars.cfg"
  printf '[gcode_macro _FILAMENT_SETTINGS]\nvariable_high_load_temp: 255\n' > "$H/printer_data/config/ktc-filament-overrides.cfg"
  printf 'gui page\n' > "$H/StealthChanger-GUI/gui/index.html"
  printf 'clone marker\n' > "$H/StealthChanger-GUI/marker"
  ln -s "$H/StealthChanger-GUI" "$H/printer_data/config/ktc-filament"
  ln -s "$H/StealthChanger-GUI/gui" "$H/ktc-gui"
  printf 'server { root %s; }\n' "$H/ktc-gui" > "$H/nginx/sites-available/ktc-gui"
  ln -sf "$H/nginx/sites-available/ktc-gui" "$H/nginx/sites-enabled/ktc-gui"
  printf 'old site\n' > "$H/nginx/sites-available/ktc-gui.bak"
}

save_tail() {
  python3 - "$1" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
m = re.search(br"(?m)^[ \t]*#\*#.*SAVE_CONFIG", raw)
if not m:
    sys.exit(1)
sys.stdout.buffer.write(raw[m.start():])
PY
}

run() {  # run [args...]  -> $OUT $RC, HOME=$H
  : > "$T/sudo.log"
  OUT=$(env HOME="$H" PATH="$T/bin:$PATH" KTC_NGINX_DIR="$H/nginx" \
    SC_MOONRAKER_URL="http://127.0.0.1:$PORT" SC_SUDO_LOG="$T/sudo.log" \
    bash "$REPO/uninstall.sh" "$@" </dev/null 2>&1)
  RC=$?
}
posts() { grep -c '^POST ' "$T/http.log" 2>/dev/null || true; }

echo "0. rejected flags, help"
run_help=$(bash "$REPO/uninstall.sh" --help 2>&1) || true
ok "--help exits 0" 'bash "$REPO/uninstall.sh" --help >/dev/null'
ok "help names dry-run and purge" 'grep -q -- "--dry-run" <<<"$run_help" && grep -q -- "--purge" <<<"$run_help"'
ok "help does not offer --force" '! grep -q -- "--force" <<<"$run_help"'
force_rc=0
bash "$REPO/uninstall.sh" --force </dev/null >/dev/null 2>&1 || force_rc=$?
ok "--force is not a flag" '[ "$force_rc" -ne 0 ]'
ok "script does not query print_stats" '! grep -q print_stats "$REPO/uninstall.sh"'

echo "1. dry-run changes nothing and does not call Moonraker"
mkhome a
: > "$T/http.log"
cp -a "$H/printer_data/config/printer.cfg" "$T/orig-printer.cfg"
cp -a "$H/printer_data/config/moonraker.conf" "$T/orig-moonraker.conf"
cp -a "$H/printer_data/config/variables.cfg" "$T/orig-variables.cfg"
cp -a "$H/printer_data/config/custom-vars.cfg" "$T/orig-custom.cfg"
cp -a "$H/printer_data/config/ktc-filament-overrides.cfg" "$T/orig-overrides.cfg"
save_tail "$H/printer_data/config/printer.cfg" > "$T/orig-tail"
run --dry-run
ok "dry-run exits 0" '[ $RC -eq 0 ]'
ok "dry-run would remove each ktc-filament include" \
  'grep -q "would remove \[include ktc-filament/filament/filament_macros.cfg\]" <<<"$OUT" && grep -q "would remove \[include ktc-filament/orbiter/orbiter_addon.cfg\]" <<<"$OUT" && grep -q "would remove \[include ktc-filament/usage/usage_addon.cfg\]" <<<"$OUT" && grep -q "would remove \[include ktc-filament-orbiter.cfg\]" <<<"$OUT"'
ok "dry-run would remove the update_manager section" 'grep -q "would remove \[update_manager stealthchanger-gui\]" <<<"$OUT"'
ok "dry-run would remove the symlink and keep the clone" \
  'grep -q "would remove symlink $H/printer_data/config/ktc-filament" <<<"$OUT" && grep -q "would keep $H/StealthChanger-GUI" <<<"$OUT"'
ok "dry-run would uninstall nginx and ktc-gui" \
  'grep -q "would run: sudo rm -f" <<<"$OUT" && grep -q "would run: sudo nginx -t" <<<"$OUT" && grep -q "would remove symlink $H/ktc-gui" <<<"$OUT"'
ok "dry-run would back up variables files" \
  'grep -q "would back up $H/printer_data/config/variables.cfg" <<<"$OUT" && grep -q "would back up $H/printer_data/config/custom-vars.cfg" <<<"$OUT" && grep -q "would back up $H/printer_data/config/ktc-filament-overrides.cfg" <<<"$OUT"'
ok "dry-run names the firmware restart" 'grep -q "would restart Klipper (firmware restart" <<<"$OUT"'
ok "dry-run would keep the orbiter pin file" 'grep -q "would keep $H/printer_data/config/ktc-filament-orbiter.cfg" <<<"$OUT" && [ -f "$H/printer_data/config/ktc-filament-orbiter.cfg" ]'
ok "dry-run left every file in place" \
  'cmp -s "$T/orig-printer.cfg" "$H/printer_data/config/printer.cfg" && cmp -s "$T/orig-moonraker.conf" "$H/printer_data/config/moonraker.conf" && [ -L "$H/printer_data/config/ktc-filament" ] && [ -L "$H/ktc-gui" ] && [ -f "$H/StealthChanger-GUI/marker" ] && [ -f "$H/nginx/sites-available/ktc-gui" ] && [ ! -d "$H/stealthchanger-backups" ]'
ok "dry-run made no Moonraker request" '[ ! -s "$T/http.log" ]'

echo "2. --yes removes setup pieces, keeps SAVE_CONFIG, restarts"
: > "$T/http.log"
run --yes
ok "uninstall exits 0" '[ $RC -eq 0 ]'
ok "ktc-filament includes are gone" '! grep -q "usage_addon.cfg" "$H/printer_data/config/printer.cfg"'
ok "full-line comment include stays" 'grep -q "^# \[include ktc-filament/filament/filament_macros.cfg\]$" "$H/printer_data/config/printer.cfg"'
ok "overrides include and other include stay" \
  'grep -q "^\[include ktc-filament-overrides.cfg\]$" "$H/printer_data/config/printer.cfg" && grep -q "^\[include other.cfg\]$" "$H/printer_data/config/printer.cfg"'
ok "SAVE_CONFIG block is byte-for-byte the same" 'save_tail "$H/printer_data/config/printer.cfg" > "$T/new-tail" && cmp -s "$T/orig-tail" "$T/new-tail"'
ok "decoy lines inside SAVE_CONFIG stay" \
  'grep -q "^#\*# \[include ktc-filament/filament/filament_macros.cfg\]$" "$H/printer_data/config/printer.cfg" && grep -q "^#\*# filename: /etc/passwd$" "$H/printer_data/config/printer.cfg" && grep -q "^#\*# pid_kp = 22.123$" "$H/printer_data/config/printer.cfg"'
ok "update_manager stealthchanger-gui section is gone" \
  '! grep -q "^\[update_manager stealthchanger-gui\]$" "$H/printer_data/config/moonraker.conf" && ! grep -q "Added by setup.sh" "$H/printer_data/config/moonraker.conf" && ! grep -q "primary_branch" "$H/printer_data/config/moonraker.conf"'
ok "other update_manager sections stay" \
  'grep -q "^\[update_manager mainsail\]$" "$H/printer_data/config/moonraker.conf" && grep -q "mainsail-crew/mainsail" "$H/printer_data/config/moonraker.conf" && grep -q "# keep-me" "$H/printer_data/config/moonraker.conf" && grep -q "^\[update_manager stealthchanger-gui-extra\]$" "$H/printer_data/config/moonraker.conf" && grep -q "path: ~/nope" "$H/printer_data/config/moonraker.conf"'
ok "server section stays" 'grep -q "^\[server\]$" "$H/printer_data/config/moonraker.conf" && grep -q "port: 7125" "$H/printer_data/config/moonraker.conf"'
FIRST=$(ls -d "$H"/stealthchanger-backups/* | head -1)
ok "backup has the original printer.cfg includes" 'grep -q "^\[include ktc-filament/usage/usage_addon.cfg\]$" "$FIRST/printer.cfg"'
ok "backup SAVE_CONFIG matches the original" 'save_tail "$FIRST/printer.cfg" > "$T/bak-tail" && cmp -s "$T/orig-tail" "$T/bak-tail"'
ok "variables files backed up and not deleted" \
  'cmp -s "$T/orig-variables.cfg" "$H/printer_data/config/variables.cfg" && cmp -s "$T/orig-variables.cfg" "$FIRST/variables.cfg" && cmp -s "$T/orig-custom.cfg" "$H/printer_data/config/custom-vars.cfg" && cmp -s "$T/orig-custom.cfg" "$FIRST/custom-vars.cfg" && cmp -s "$T/orig-overrides.cfg" "$H/printer_data/config/ktc-filament-overrides.cfg" && cmp -s "$T/orig-overrides.cfg" "$FIRST/ktc-filament-overrides.cfg"'
ok "did not back up the SAVE_CONFIG decoy path" '[ ! -e "$FIRST/passwd" ] && [ -f /etc/passwd ]'
ok "symlink and nginx site and ktc-gui link are gone" \
  '[ ! -e "$H/printer_data/config/ktc-filament" ] && [ ! -L "$H/printer_data/config/ktc-filament" ] && [ ! -e "$H/ktc-gui" ] && [ ! -L "$H/ktc-gui" ] && [ ! -e "$H/nginx/sites-available/ktc-gui" ] && [ ! -e "$H/nginx/sites-enabled/ktc-gui" ] && [ ! -e "$H/nginx/sites-available/ktc-gui.bak" ]'
ok "clone was kept" '[ -f "$H/StealthChanger-GUI/marker" ] && [ -f "$H/StealthChanger-GUI/gui/index.html" ]'
ok "sudo reloaded nginx" 'grep -q "nginx -t" "$T/sudo.log" && grep -q "systemctl reload nginx" "$T/sudo.log"'
ok "firmware restart and moonraker restart, no print_stats" \
  'grep -q "^POST /printer/firmware_restart$" "$T/http.log" && grep -q "^POST /server/restart$" "$T/http.log" && ! grep -q print_stats "$T/http.log"'
ok "orbiter pin include removed, file kept, backup has the pin" \
  '! grep -q "^\[include ktc-filament-orbiter.cfg\]$" "$H/printer_data/config/printer.cfg" && grep -q "gpio77" "$H/printer_data/config/ktc-filament-orbiter.cfg" && grep -q "gpio77" "$FIRST/ktc-filament-orbiter.cfg"'
cp -a "$H/printer_data/config/printer.cfg" "$T/after-printer.cfg"
cp -a "$H/printer_data/config/moonraker.conf" "$T/after-moonraker.conf"
NPOST=$(posts)

echo "3. second run is safe"
run --yes
ok "second run exits 0" '[ $RC -eq 0 ]'
ok "configs unchanged by the second run" \
  'cmp -s "$T/after-printer.cfg" "$H/printer_data/config/printer.cfg" && cmp -s "$T/after-moonraker.conf" "$H/printer_data/config/moonraker.conf"'
ok "SAVE_CONFIG still the original bytes" 'save_tail "$H/printer_data/config/printer.cfg" > "$T/tail2" && cmp -s "$T/orig-tail" "$T/tail2"'
ok "second run does not restart" '[ "$(posts)" = "$NPOST" ] && grep -q "nothing changed, no restart needed" <<<"$OUT"'
ok "clone still kept" '[ -f "$H/StealthChanger-GUI/marker" ]'

echo "4. --purge removes only the clone, and a second purge is safe"
run --yes --purge
ok "purge exits 0" '[ $RC -eq 0 ]'
ok "clone is gone" '[ ! -e "$H/StealthChanger-GUI" ]'
ok "--purge removes the orbiter pin file" '[ ! -e "$H/printer_data/config/ktc-filament-orbiter.cfg" ]'
ok "repo uninstall.sh was not purged" '[ -f "$REPO/uninstall.sh" ]'
ok "printer.cfg still intact" 'cmp -s "$T/after-printer.cfg" "$H/printer_data/config/printer.cfg"'
run --yes --purge
ok "second purge exits 0" '[ $RC -eq 0 ]'
ok "second purge does not restart" 'grep -q "nothing changed, no restart needed" <<<"$OUT"'

echo "5. no --yes and no terminal: files change, no restart"
mkhome b
: > "$T/http.log"
run
ok "no --yes exits 0" '[ $RC -eq 0 ]'
ok "includes removed without a restart" \
  '! grep -q "usage_addon.cfg" "$H/printer_data/config/printer.cfg" && grep -q "No terminal" <<<"$OUT" && [ ! -s "$T/http.log" ]'

echo "6. a real ktc-gui directory is removed; a real ktc-filament folder is not"
mkhome c
rm -f "$H/ktc-gui"
mkdir -p "$H/ktc-gui"
printf 'copied gui\n' > "$H/ktc-gui/index.html"
rm "$H/printer_data/config/ktc-filament"
mkdir -p "$H/printer_data/config/ktc-filament"
printf 'real folder\n' > "$H/printer_data/config/ktc-filament/keep.cfg"
run --yes
ok "directory ~/ktc-gui removed" '[ ! -e "$H/ktc-gui" ]'
ok "real ktc-filament folder kept" '[ -f "$H/printer_data/config/ktc-filament/keep.cfg" ] && grep -q "real folder" <<<"$OUT"'

echo "7. --purge refuses a clone that resolves outside HOME"
mkhome d
save_tail "$H/printer_data/config/printer.cfg" > "$T/d-tail"
rm -rf "$H/StealthChanger-GUI"
mkdir -p "$T/outside-target"
printf 'safe\n' > "$T/outside-target/marker"
ln -s "$T/outside-target" "$H/StealthChanger-GUI"
run --purge --yes
ok "purge outside HOME fails" '[ $RC -ne 0 ]'
ok "outside target still there" '[ -f "$T/outside-target/marker" ]'
ok "refused before editing printer.cfg" 'save_tail "$H/printer_data/config/printer.cfg" > "$T/d-tail2" && cmp -s "$T/d-tail" "$T/d-tail2" && grep -q "usage_addon.cfg" "$H/printer_data/config/printer.cfg"'

echo "8. empty home is a no-op"
mkhome e
rm -rf "$H/printer_data" "$H/nginx" "$H/ktc-gui" "$H/StealthChanger-GUI"
mkdir -p "$H"
: > "$T/http.log"
run --yes
ok "empty home exits 0" '[ $RC -eq 0 ]'
ok "empty home does not restart" '[ ! -s "$T/http.log" ] && grep -q "nothing changed, no restart needed" <<<"$OUT"'

prompt_run() {  # prompt_run HOME ANSWER
  python3 - "$REPO/uninstall.sh" "$1" "$PORT" "$T" "$2" <<'PY'
import os, pty, select, sys, time
script, home, port, tmp, answer = sys.argv[1:]
pid, fd = pty.fork()
if pid == 0:
    os.environ["HOME"] = home
    os.environ["PATH"] = tmp + "/bin:" + os.environ.get("PATH", "")
    os.environ["KTC_NGINX_DIR"] = home + "/nginx"
    os.environ["SC_MOONRAKER_URL"] = "http://127.0.0.1:" + port
    os.environ["SC_SUDO_LOG"] = tmp + "/sudo.log"
    os.chdir(tmp)
    os.execv("/bin/bash", ["bash", script])
buf = b""
end = time.time() + 8
while time.time() < end and b"[y/N]" not in buf:
    r, _, _ = select.select([fd], [], [], 0.2)
    if not r:
        continue
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    buf += chunk
if b"[y/N]" not in buf:
    sys.stderr.buffer.write(buf)
    sys.exit(3)
os.write(fd, answer.encode() + b"\n")
end = time.time() + 8
while time.time() < end:
    r, _, _ = select.select([fd], [], [], 0.2)
    if not r:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid:
            os.close(fd)
            sys.stdout.buffer.write(buf)
            sys.exit(os.waitstatus_to_exitcode(status))
        continue
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    buf += chunk
wpid, status = os.waitpid(pid, 0)
os.close(fd)
sys.stdout.buffer.write(buf)
sys.exit(os.waitstatus_to_exitcode(status))
PY
}

echo "9. prompt: n does not restart, y does"
mkhome n
: > "$T/http.log"
set +e
OUT=$(prompt_run "$H" n 2>&1)
RC=$?
set -e
ok "answering n exits 0" '[ $RC -eq 0 ]'
ok "answering n removes includes and does not restart" \
  '! grep -q "usage_addon.cfg" "$H/printer_data/config/printer.cfg" && grep -q "Not restarting" <<<"$OUT" && [ ! -s "$T/http.log" ]'
mkhome y
: > "$T/http.log"
set +e
OUT=$(prompt_run "$H" y 2>&1)
RC=$?
set -e
ok "answering y exits 0" '[ $RC -eq 0 ]'
ok "answering y firmware-restarts" \
  'grep -q "^POST /printer/firmware_restart$" "$T/http.log" && grep -q "^POST /server/restart$" "$T/http.log" && ! grep -q print_stats "$T/http.log"'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
