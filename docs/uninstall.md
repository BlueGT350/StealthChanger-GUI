---
layout: default
title: Uninstall
nav_order: 4
description: Remove StealthChanger GUI.
---

# Uninstall

```
bash ~/StealthChanger-GUI/uninstall.sh
```

Removes the `ktc-filament/` includes, the Orbiter pin include, the Update Manager entry, the nginx site, the config link, and the firmware helper service. It also removes `~/printer_data/stealthchanger-firmware-build/`. Menuconfig files in `~/printer_data/config/stealthchanger-firmware/` stay unless you pass `--purge`. `--dry-run` prints the actions and changes nothing. `--purge` also deletes `~/StealthChanger-GUI`. `--yes` restarts Klipper and Moonraker without asking; do not use it during a print.
