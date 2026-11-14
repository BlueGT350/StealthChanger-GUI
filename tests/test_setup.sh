#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2034,SC2088  # checks are eval strings, expanded later on purpose
# Dry run of setup.sh off the printer: temp HOME, fake printer_data, a local
# bare repo instead of GitHub, fake sudo/nginx, Moonraker unreachable (so it
# never restarts anything). Focus: finding the GUI's nginx root when the site
# file is root-only (mode 600), and idempotent relinking of ~/ktc-gui.
#   bash tests/test_setup.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] || { echo "run as a normal user (root can read every file)"; exit 1; }
T=$(mktemp -d); trap 'chmod -R u+rwX "$T" 2>/dev/null; rm -rf "$T"' EXIT
PASS=0; FAIL=0
ok()   { if eval "$2"; then PASS=$((PASS+1)); echo "  ok    $1"; else FAIL=$((FAIL+1)); echo "  FAIL  $1"; fi; }

# the repo as it is on disk (uncommitted edits included) -> bare repo
cp -a "$REPO" "$T/src" && rm -rf "$T/src/.git"
git -C "$T/src" init -q -b main && git -C "$T/src" add -A
git -C "$T/src" -c user.name=t -c user.email=t@t commit -qm test
git -C "$T/src" -c user.name=t -c user.email=t@t tag -a v9.9.9 -m test
git clone -q --bare "$T/src" "$T/bare.git"

# fake sudo: "root" reads files we made unreadable; FAKE_SUDO=none = no sudo rights
mkdir -p "$T/bin"
cat > "$T/bin/sudo" <<'SUDO'
#!/usr/bin/env bash
[ "${FAKE_SUDO:-nopasswd}" = none ] && { echo "sudo: a password is required" >&2; exit 1; }
while [ $# -gt 0 ]; do case "$1" in -n|-v) shift ;; -u) shift 2 ;; *) break ;; esac; done
[ $# -eq 0 ] && exit 0
asroot() { local f=$1; shift; local m; m=$(stat -c %a "$f"); chmod u+r "$f"; "$@" "$f"; local rc=$?; chmod "$m" "$f"; return $rc; }
case "$1" in
  true) exit 0 ;;
  cat)  [ -e "$2" ] || { echo "cat: $2: No such file" >&2; exit 1; }; asroot "$(readlink -f "$2")" cat ;;
  test) shift; [ "$1" = -f ] && [ -e "$2" ] ;;
  nginx) [ "$2" = -T ] || exit 1
         for f in "$KTC_NGINX_DIR"/sites-enabled/*; do echo "# configuration file $f:"; asroot "$(readlink -f "$f")" cat; done ;;
  *) echo "fake sudo: unexpected $*" >&2; exit 1 ;;
esac
SUDO
chmod +x "$T/bin/sudo"

mkhome() {  # mkhome NAME -> fresh fake home with an old rev 1 style install
  H=$T/$1; mkdir -p "$H/printer_data/config/ktc-filament" "$H/nginx/sites-available" "$H/nginx/sites-enabled" "$H/ktc-gui"
  cp -a "$REPO/filament" "$REPO/orbiter" "$H/printer_data/config/ktc-filament/"
  printf '[server]\nhost: 0.0.0.0\n' > "$H/printer_data/config/moonraker.conf"
  printf '[include ktc-filament/filament/filament_macros.cfg]\n[include ktc-filament/orbiter/orbiter_addon.cfg]\n' > "$H/printer_data/config/printer.cfg"
  echo '<html>rev 1 copy</html>' > "$H/ktc-gui/index.html"
}
site() {  # site NAME ROOT MODE -> sites-available/NAME + sites-enabled link
  printf 'map $http_upgrade $x { default upgrade; }\nserver {\n    listen 8090;\n    root %s;\n    index index.html;\n}\n' "$2" > "$H/nginx/sites-available/$1"
  ln -sf "$H/nginx/sites-available/$1" "$H/nginx/sites-enabled/$1"
  chmod "$3" "$H/nginx/sites-available/$1"
}
run() {  # run [ENV=...] -> output in $OUT
  OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
        SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 "$@" bash "$REPO/setup.sh" --no-restart </dev/null 2>&1)
  RC=$?
}
linked() { [ -L "$H/ktc-gui" ] && [ "$(readlink -f "$H/ktc-gui")" = "$(readlink -f "$H/StealthChanger-GUI/gui")" ]; }

echo "1. site file root-only (000 here, 600 on the Pi), sudo without password"
mkhome a; site ktc-gui "$H/ktc-gui" 000
ok "site really is unreadable" '! cat "$H/nginx/sites-enabled/ktc-gui" >/dev/null 2>&1'
run FAKE_SUDO=nopasswd
ok "setup.sh exits 0" '[ $RC -eq 0 ]'
ok "root found from the site file via sudo" 'grep -q "GUI web root: $H/ktc-gui (from the ktc-gui nginx site)" <<<"$OUT"'
ok "~/ktc-gui relinked to the clone" 'linked'
ok "old copy kept in the backup" 'grep -q "rev 1 copy" "$H"/stealthchanger-backups/*/ktc-gui/index.html'
ok "says what it did" 'grep -q "now links to $H/StealthChanger-GUI/gui" <<<"$OUT"'
echo "2. second run: nothing to do"
run FAKE_SUDO=nopasswd
ok "already linked" 'grep -q "already links to" <<<"$OUT" && linked'
ok "no new backup" '[ "$(ls "$H/stealthchanger-backups" | wc -l)" -eq 1 ]'

echo "3. root-only site, no sudo rights: falls back to ~/ktc-gui"
mkhome b; site ktc-gui "$H/ktc-gui" 000
run FAKE_SUDO=none
ok "fallback to the default folder" 'grep -q "GUI web root: $H/ktc-gui (from the default GUI folder" <<<"$OUT"'
ok "relinked" 'linked'
run FAKE_SUDO=none
ok "second run: already links" 'grep -q "already links to" <<<"$OUT"'

echo "4. hand-fixed Pi (~/ktc-gui already a link), root-only site"
mkhome c; rm -rf "$H/ktc-gui"; site ktc-gui "$H/ktc-gui" 000
git clone -q "$T/bare.git" "$H/StealthChanger-GUI"; ln -s "$H/StealthChanger-GUI/gui" "$H/ktc-gui"
run FAKE_SUDO=nopasswd
ok "nothing to do for the GUI" 'grep -q "already links to" <<<"$OUT" && ! grep -q "moved the old GUI copy" <<<"$OUT"'
run FAKE_SUDO=none
ok "same without sudo" 'grep -q "already links to" <<<"$OUT"'

echo "5. site under another name: found with nginx -T"
mkhome d; mv "$H/ktc-gui" "$H/my-gui"; site stealth "$H/my-gui" 000
run FAKE_SUDO=nopasswd
ok "no root in nginx -T matching the GUI names, no default: install hint" 'grep -q "install.sh --link" <<<"$OUT"'
mkhome e; site other "$H/ktc-gui" 000
run FAKE_SUDO=nopasswd
ok "root found via nginx -T" 'grep -q "GUI web root: $H/ktc-gui (from nginx -T)" <<<"$OUT" && linked'

echo "6. readable site serving a folder outside home"
mkhome f; site ktc-gui /var/www/ktc-gui 644
run FAKE_SUDO=nopasswd
ok "not moved, tells what to run" 'grep -q "which setup.sh does not move" <<<"$OUT" && [ -d "$H/ktc-gui" ] && ! [ -L "$H/ktc-gui" ]'

echo "7. no site, no ~/ktc-gui"
mkhome g; rm -rf "$H/ktc-gui"
run FAKE_SUDO=none
ok "install hint" 'grep -q "No GUI web page found" <<<"$OUT" && grep -q "install.sh --link" <<<"$OUT"'

echo "8. readable site, old link to another folder"
mkhome h; rm -rf "$H/ktc-gui"; mkdir "$H/elsewhere"; ln -s "$H/elsewhere" "$H/ktc-gui"; site ktc-gui "$H/ktc-gui/" 644
run FAKE_SUDO=none
ok "relinked from the old target" 'grep -q "was a link to $H/elsewhere; relinking" <<<"$OUT" && linked'

echo "9. HTTPS origin, no deploy key; --origin only rewrites an existing section"
mkhome i
run FAKE_SUDO=nopasswd
ok "new update_manager section uses the URL this run cloned" 'grep -q "origin: $T/bare.git" "$H/printer_data/config/moonraker.conf"'
ok "no deploy key" '! grep -q "deploy key" <<<"$OUT" && [ ! -e "$H/.ssh/stealthchanger_gui_deploy" ]'
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 FAKE_SUDO=nopasswd \
  bash "$REPO/setup.sh" --no-restart --origin https://example.com/private.git </dev/null 2>&1)
RC=$?
ok "--origin rewrites the existing section" '[ $RC -eq 0 ] && grep -q "^origin: https://example.com/private.git$" "$H/printer_data/config/moonraker.conf"'
run FAKE_SUDO=nopasswd
ok "a later run without --origin leaves that origin" 'grep -q "^origin: https://example.com/private.git$" "$H/printer_data/config/moonraker.conf"'
mkhome j
git clone -q "$T/bare.git" "$H/StealthChanger-GUI"
echo dirty >> "$H/StealthChanger-GUI/README.md"
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 FAKE_SUDO=nopasswd \
  bash "$REPO/setup.sh" --no-restart </dev/null 2>&1)
RC=$?
ok "default origin is the public HTTPS repo" '[ $RC -eq 0 ] && grep -q "^origin: https://github.com/BlueGT350/StealthChanger-GUI.git$" "$H/printer_data/config/moonraker.conf"'
ok "dirty clone was not fetched from GitHub" 'grep -q "local edits" <<<"$OUT"'

echo "10. --orbiter / --no-orbiter"
mkhome k
# drop the orbiter include the fixture starts with
python3 - "$H/printer_data/config/printer.cfg" <<'PY'
import sys
p=sys.argv[1]
t=open(p).read().replace("[include ktc-filament/orbiter/orbiter_addon.cfg]\n","")
open(p,"w").write(t)
PY
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 FAKE_SUDO=nopasswd \
  bash "$REPO/setup.sh" --no-restart --orbiter </dev/null 2>&1)
RC=$?
ok "--orbiter adds the add-on and the user pin include" '[ $RC -eq 0 ] && grep -q "^\[include ktc-filament/orbiter/orbiter_addon.cfg\]$" "$H/printer_data/config/printer.cfg" && grep -q "^\[include ktc-filament-orbiter.cfg\]$" "$H/printer_data/config/printer.cfg"'
ok "pin file created once from the template" 'cmp -s "$REPO/orbiter/orbiter_pins.cfg" "$H/printer_data/config/ktc-filament-orbiter.cfg"'
cp -a "$H/printer_data/config/ktc-filament-orbiter.cfg" "$T/pins-once"
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 FAKE_SUDO=nopasswd \
  bash "$REPO/setup.sh" --no-restart --orbiter </dev/null 2>&1)
ok "second run does not overwrite the pin file" 'cmp -s "$T/pins-once" "$H/printer_data/config/ktc-filament-orbiter.cfg"'
echo 'pin: ^nhkT0:gpio99' >> "$H/printer_data/config/ktc-filament-orbiter.cfg"
cp -a "$H/printer_data/config/ktc-filament-orbiter.cfg" "$T/pins-edited"
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 FAKE_SUDO=nopasswd \
  bash "$REPO/setup.sh" --no-restart --no-orbiter </dev/null 2>&1)
RC=$?
ok "--no-orbiter comments the includes and keeps the pin file" '[ $RC -eq 0 ] && grep -q "^# \[include ktc-filament/orbiter/orbiter_addon.cfg\]" "$H/printer_data/config/printer.cfg" && grep -q "^# \[include ktc-filament-orbiter.cfg\]" "$H/printer_data/config/printer.cfg" && cmp -s "$T/pins-edited" "$H/printer_data/config/ktc-filament-orbiter.cfg"'
cp -a "$H/printer_data/config/printer.cfg" "$T/before-conflict"
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 FAKE_SUDO=nopasswd \
  bash "$REPO/setup.sh" --orbiter --no-orbiter </dev/null 2>&1)
RC=$?
ok "both orbiter flags is an error and does not edit printer.cfg" '[ $RC -ne 0 ] && cmp -s "$T/before-conflict" "$H/printer_data/config/printer.cfg"'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
