#!/usr/bin/env bash
# setup.sh installs the firmware helper without touching menuconfig files.
# uninstall.sh removes the unit and, only with --purge, the menuconfig folder.
#   bash tests/test_firmware_setup.sh
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -ne 0 ] || { echo "run as a normal user"; exit 1; }
T=$(mktemp -d)
trap 'chmod -R u+rwX "$T" 2>/dev/null; rm -rf "$T"' EXIT
PASS=0
FAIL=0
ok() {
  if eval "$2"; then PASS=$((PASS + 1)); echo "  ok    $1"
  else FAIL=$((FAIL + 1)); echo "  FAIL  $1"; printf '%s\n' "${OUT:-}" | tail -n 40; fi
}

cp -a "$REPO" "$T/src"
rm -rf "$T/src/.git"
git -C "$T/src" init -q -b main
git -C "$T/src" add -A
git -C "$T/src" -c user.name=t -c user.email=t@t commit -qm test
git clone -q --bare "$T/src" "$T/bare.git"

mkdir -p "$T/bin"
: > "$T/systemctl.log"
cat > "$T/bin/sudo" <<'SUDO'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -n|-v) shift ;; -u) shift 2 ;; *) break ;; esac; done
case "${1:-}" in
  true|test) exit 0 ;;
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
cat > "$T/bin/systemctl" <<'SC'
#!/bin/sh
printf '%s\n' "$*" >> "$SC_SYSTEMCTL_LOG"
exit 0
SC
chmod +x "$T/bin/sudo" "$T/bin/nginx" "$T/bin/systemctl"

H=$T/home
mkdir -p "$H/printer_data/config" "$H/nginx/sites-available" "$H/nginx/sites-enabled"
printf '[server]\nhost: 0.0.0.0\n' > "$H/printer_data/config/moonraker.conf"
printf '[include ktc-filament/filament/filament_macros.cfg]\n' > "$H/printer_data/config/printer.cfg"
printf 'server { listen 8090; root %s/ktc-gui; }\n' "$H" > "$H/nginx/sites-available/ktc-gui"
ln -sf "$H/nginx/sites-available/ktc-gui" "$H/nginx/sites-enabled/ktc-gui"
chmod 644 "$H/nginx/sites-available/ktc-gui"

run_setup() {
  OUT=$(env HOME="$H" PATH="$T/bin:$PATH" SC_TEST_ORIGIN="$T/bare.git" KTC_NGINX_DIR="$H/nginx" \
    SC_MOONRAKER_URL=http://127.0.0.1:9 SC_NO_SUDO_PROMPT=1 SC_SYSTEMCTL_LOG="$T/systemctl.log" \
    bash "$REPO/setup.sh" --no-restart --no-orbiter </dev/null 2>&1)
  RC=$?
}

echo "1. setup installs the helper and does not call systemctl"
run_setup
UNIT="$H/.config/systemd/user/stealthchanger-firmware.service"
CFG="$H/printer_data/config/stealthchanger-firmware"
ok "setup exits 0" '[ $RC -eq 0 ]'
ok "unit file points at the clone helper" 'grep -q "ExecStart=.*$H/StealthChanger-GUI/firmware/firmware_helper.py" "$UNIT"'
ok "menuconfig directory exists and is outside the clone" '[ -d "$CFG" ] && [[ "$CFG" != "$H/StealthChanger-GUI/"* ]]'
ok "systemd was not enabled from the test environment" 'grep -q "systemd enable skipped" <<<"$OUT"'
ok "setup did not call systemctl" '[ ! -s "$T/systemctl.log" ]'
ok "setup enables the helper and then restarts it" \
  'grep -q "systemctl --user enable stealthchanger-firmware.service" "$REPO/setup.sh" && grep -q "systemctl --user restart stealthchanger-firmware.service" "$REPO/setup.sh" && ! grep -q "enable --now stealthchanger-firmware.service" "$REPO/setup.sh"'
ok "unit restarts on a clean exit" 'grep -q "^Restart=always$" "$UNIT" && grep -q "^RestartSec=2$" "$UNIT"'

printf 'CONFIG_MCU="stm32f446xx"\n' > "$CFG/toolhead.config"
cp -a "$CFG/toolhead.config" "$T/toolhead.config"
echo "2. a later setup does not overwrite the menuconfig"
run_setup
ok "second setup exits 0" '[ $RC -eq 0 ]'
ok "toolhead.config unchanged" 'cmp -s "$T/toolhead.config" "$CFG/toolhead.config"'

echo "3. install.sh proxies /firmware/ at loopback only"
ok "nginx site proxies /firmware/" 'grep -q "location /firmware/" "$REPO/gui/install.sh"'
ok "upstream default is loopback" 'grep -q "127.0.0.1:7140" "$REPO/gui/install.sh"'
RC=0
OUT=$(SC_FIRMWARE_UPSTREAM='10.1.2.3:9' bash "$REPO/gui/install.sh" --files-only </dev/null 2>&1) || RC=$?
ok "non-loopback upstream is rejected" '[ $RC -ne 0 ] && grep -q "127.0.0.1:port" <<<"$OUT"'

echo "4. uninstall removes the unit and the build directory, and keeps the menuconfig"
mkdir -p "$H/printer_data/stealthchanger-firmware-build/toolhead"
printf 'stale\n' > "$H/printer_data/stealthchanger-firmware-build/toolhead/klipper.bin"
: > "$T/systemctl.log"
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_SYSTEMCTL_LOG="$T/systemctl.log" \
  bash "$REPO/uninstall.sh" --yes </dev/null 2>&1)
RC=$?
ok "uninstall exits 0" '[ $RC -eq 0 ]'
ok "unit removed" '[ ! -e "$UNIT" ]'
ok "menuconfig kept" 'cmp -s "$T/toolhead.config" "$CFG/toolhead.config"'
ok "build directory removed" '[ ! -e "$H/printer_data/stealthchanger-firmware-build" ]'
ok "uninstall only disables the helper unit" '[ "$(cat "$T/systemctl.log")" = "--user disable --now stealthchanger-firmware.service" ]'

echo "5. purge removes the menuconfig folder"
OUT=$(env HOME="$H" PATH="$T/bin:$PATH" KTC_NGINX_DIR="$H/nginx" \
  SC_MOONRAKER_URL=http://127.0.0.1:9 SC_SYSTEMCTL_LOG="$T/systemctl.log" \
  bash "$REPO/uninstall.sh" --yes --purge </dev/null 2>&1)
RC=$?
ok "purge exits 0" '[ $RC -eq 0 ]'
ok "menuconfig folder removed" '[ ! -e "$CFG" ]'

echo
echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
