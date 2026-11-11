#!/usr/bin/env bash
# ktc-gui installer: copies the GUI to ~/ktc-gui and serves it with nginx on
# its own port (default 8090), proxying the Moonraker API on the same port.
# Mainsail is not touched.
#
#   ./install.sh                 install or update, port 8090
#   ./install.sh --port 8091     another port
#   ./install.sh --moonraker 127.0.0.1:7125
#   ./install.sh --files-only    only copy the files (update after a git pull)
#   ./install.sh --link          serve the files straight from this folder
#                                (git clone + Moonraker Update Manager; setup.sh
#                                in the repo root does this for you)
#   ./install.sh --uninstall     remove the nginx site and ~/ktc-gui
set -euo pipefail

PORT=8090
MOONRAKER=127.0.0.1:7125
DEST="$HOME/ktc-gui"
SITE=ktc-gui
FILES_ONLY=0
LINK=0
UNINSTALL=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --moonraker) MOONRAKER="$2"; shift 2 ;;
    --dir) DEST="$2"; shift 2 ;;
    --files-only) FILES_ONLY=1; shift ;;
    --link) LINK=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

NGINX_DIR=${KTC_NGINX_DIR:-/etc/nginx}   # override only for testing
AVAIL=$NGINX_DIR/sites-available/$SITE
ENABLED=$NGINX_DIR/sites-enabled/$SITE
# the site files can be root-only (mode 600), so test them through sudo
site_exists() { sudo test -f "$1" 2>/dev/null; }

if [ "$UNINSTALL" = 1 ]; then
  sudo rm -f "$ENABLED" "$AVAIL" "$AVAIL.bak"
  if command -v nginx >/dev/null; then sudo nginx -t && sudo systemctl reload nginx; fi
  if [ -L "$DEST" ]; then rm -f "$DEST"; else rm -rf "$DEST"; fi
  echo "ktc-gui removed (nginx site $SITE and $DEST)."
  exit 0
fi

case "$PORT" in ''|*[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac
[ -f "$SRC/index.html" ] || { echo "run this from the ktc-filament/gui folder" >&2; exit 1; }

# 1. files (dev/ tools and screenshots stay out)
if [ "$LINK" = 1 ]; then
  if [ -L "$DEST" ]; then
    ln -sfn "$SRC" "$DEST"
  else
    if [ -e "$DEST" ]; then mv "$DEST" "$DEST.old-$(date +%Y%m%d-%H%M%S)"; echo "Moved the old copy to $DEST.old-*"; fi
    ln -s "$SRC" "$DEST"
  fi
  echo "$DEST now links to $SRC (updates arrive with git / Update Manager)"
else
  if [ -L "$DEST" ]; then echo "$DEST is a link to $(readlink "$DEST"); use --link, or remove the link first." >&2; exit 1; fi
  mkdir -p "$DEST/js"
  cp "$SRC/index.html" "$SRC/style.css" "$DEST/"
  cp "$SRC"/js/*.js "$DEST/js/"
  echo "Copied the GUI to $DEST"
fi
# the Klipper side (filament/, orbiter/, usage/) is included from printer.cfg
# as ktc-filament/..., so the folder must sit in the Klipper config folder
if [ -e "$SRC/../.git" ]; then
  :   # git clone (setup.sh links it into ~/printer_data/config/ktc-filament)
else case "$(cd "$SRC/.." && pwd)" in
  */printer_data/config/*) ;;
  *) echo "Note: $(cd "$SRC/.." && pwd) is not inside ~/printer_data/config. printer.cfg's"
     echo "      [include ktc-filament/...] lines need the folder there (usage/ is new in rev 2)." ;;
esac; fi
[ "$FILES_ONLY" = 1 ] && { echo "Files updated. Reload the page in the browser."; exit 0; }

command -v nginx >/dev/null || { echo "nginx not found. MainsailOS and KIAUH installs have it; otherwise: sudo apt install nginx" >&2; exit 1; }
if ! site_exists "$AVAIL" && sudo ss -ltn "( sport = :$PORT )" 2>/dev/null | grep -q LISTEN; then
  echo "Port $PORT is already in use. Pick another with --port." >&2; exit 1
fi

# 2. nginx must be able to read $DEST. MainsailOS / KIAUH homes already allow
#    this (Mainsail lives in ~/mainsail the same way).
# shellcheck disable=SC2009  # pgrep cannot print the user
NGUSER=$(ps -o user= -C nginx 2>/dev/null | grep -v root | head -1 || true)
NGUSER=${NGUSER:-www-data}
if ! sudo -u "$NGUSER" test -r "$DEST/index.html" 2>/dev/null; then
  echo "nginx ($NGUSER) cannot read $DEST, allowing it to pass through $HOME (chmod o+x, same as KIAUH)."
  chmod o+x "$HOME"
  [ "$LINK" = 1 ] && chmod o+x "$(dirname "$SRC")"
  chmod -R o+rX "$DEST/"
fi

# 3. nginx site. Names are prefixed ktc_ so they cannot clash with Mainsail's
#    "upstream apiserver" / "map ... $connection_upgrade" in the same http block.
TMP=$(mktemp)
cat > "$TMP" <<NGINX
# ktc-gui, written by ktc-filament/gui/install.sh. Re-run the script to change it.
map \$http_upgrade \$ktc_connection_upgrade {
    default upgrade;
    ''      close;
}
upstream ktc_moonraker {
    server $MOONRAKER;
}
server {
    listen $PORT;
    root $DEST;
    index index.html;
    access_log off;

    location ^~ /dev/ { return 404; }   # test tools, not part of the GUI
    location / {
        try_files \$uri \$uri/ /index.html;
        add_header Cache-Control "no-cache";
    }
    location /websocket {
        proxy_pass http://ktc_moonraker/websocket;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$ktc_connection_upgrade;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme \$scheme;
        proxy_read_timeout 86400;
    }
    location ~ ^/(printer|api|access|machine|server)/ {
        proxy_pass http://ktc_moonraker\$request_uri;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme \$scheme;
    }
}
NGINX
if site_exists "$AVAIL"; then sudo cp -p "$AVAIL" "$AVAIL.bak"; fi
sudo install -m 644 "$TMP" "$AVAIL"   # mktemp files are 600; keep the site readable
rm -f "$TMP"
sudo ln -sf "$AVAIL" "$ENABLED"
if ! sudo nginx -t; then
  echo "nginx rejected the config, putting the old one back." >&2
  if site_exists "$AVAIL.bak"; then sudo mv "$AVAIL.bak" "$AVAIL"; else sudo rm -f "$ENABLED" "$AVAIL"; fi
  exit 1
fi
sudo systemctl reload nginx
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "ktc-gui is running: http://${IP:-<printer-ip>}:$PORT/"
echo "Mainsail is unchanged on its usual port."
