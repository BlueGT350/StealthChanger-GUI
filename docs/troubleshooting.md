---
layout: default
title: Troubleshooting
nav_order: 5
description: Problems specific to StealthChanger GUI.
---

# Troubleshooting

**No Filament chip.** `printer.cfg` has no `ktc-filament` include. `setup.sh` adds the usage and overrides lines only when one is already there. Add these above the `#*#` block. The Orbiter lines are optional.

```
[include ktc-filament/filament/filament_macros.cfg]
[include ktc-filament/usage/usage_addon.cfg]
[include ktc-filament-overrides.cfg]
```

```
[include ktc-filament/orbiter/orbiter_addon.cfg]
[include ktc-filament-orbiter.cfg]
```

**Klipper rejects `nhkT0`.** Edit the pins in `~/printer_data/config/ktc-filament-orbiter.cfg`. The examples (`^nhkT0:gpio13` is the button on a Nitehawk-36 X-STOP, yellow FTU wire) show the idea; use your board name. Defining that button in both this file and the tool file is a duplicate pin.

**Update Manager will not update.** A changed file inside `~/StealthChanger-GUI` makes the clone dirty. Orbiter pins belong in `~/printer_data/config/ktc-filament-orbiter.cfg`, and other settings in `ktc-filament-overrides.cfg`. Those files are outside the clone, so updates do not replace them.

**`SAVE_CONFIG` fails.** After calibrating, `SAVE_CONFIG` writes new values (such as a tool's offsets) into the auto-generated `#*#` block at the bottom of `printer.cfg`. Klipper refuses if the same option is also set in an included file, such as your tool file, with an error like `SAVE_CONFIG section 'tool T1' option 'gcode_x_offset' conflicts with included value`. Calibration → **Re-check config** lists these clashes ahead of time. Comment out the listed option in the included file, then run `SAVE_CONFIG` again.

**The page is old or missing.** It is port 8090. If the version tooltip says nginx is not serving the clone:

```
bash ~/StealthChanger-GUI/gui/install.sh --link
```
