---
layout: default
title: GUI
nav_order: 3
description: What each tab on the StealthChanger page does.
---

# GUI

Load, Unload, and Nozzle change are on each tool in the rack. The gear icon changes that tool's head drawing only (Anthead, Stealthburner, or Dragonburner, and Orbiter 2.5 or WW-BMG; Stealthburner stays Clockwork 2).

## Tool actions

Home, pick up a tool, and the other KTC-Easy actions. Same commands as Mainsail.

![Tool actions tab]({{ site.baseurl }}/assets/images/gui-tab-tool.png)

## Calibration

KTC-Easy offset calibration. **Re-check config** lists options `SAVE_CONFIG` will refuse because they are also set in an included file. Comment the listed option out of that file, then run `SAVE_CONFIG`.

![Calibration tab, including the SAVE_CONFIG conflict list]({{ site.baseurl }}/assets/images/gui-tab-calibration.png)

## Recovery

Resume, initialize, and the other KTC-Easy recovery commands.

![Recovery tab]({{ site.baseurl }}/assets/images/gui-tab-recovery.png)

## Firmware

Lists every `[mcu]` with a name (Main Board, T0, T1, or the MCU name). You can rename a board; the name is kept in `~/printer_data/config/stealthchanger-firmware/labels.json`. A board that is behind the host Klipper version is marked **Firmware update required**.

**Create** or **Edit** opens menuconfig in the page, from `~/klipper`'s Kconfig. **Create** starts from what the connected MCU reports (chip and CAN or USB) when that maps onto the menu. **Copy to…** copies a saved `.config` onto other boards and asks before replacing one. **Import** still takes a `.config` you already have. Those files live in `~/printer_data/config/stealthchanger-firmware/` and an update does not replace them. Build output is `~/printer_data/stealthchanger-firmware-build/<mcu>/`. The chip is whatever `CONFIG_MCU` says. A saved file that names a different chip than the board reports is marked on that row.

A `canbus_uuid` defaults to Katapult over CAN. A `/dev/serial/by-id` serial defaults to Katapult over USB when Katapult is installed, and to direct USB otherwise. **Flash all** updates every board that is behind and already has a `.config`: one confirmation, Klipper stopped once, stopped at the first failure. Flashing is refused while a print is printing or paused.

The page talks to a helper on `127.0.0.1` through `/firmware/` on this site. `setup.sh` installs it. See the changelog for v0.2.12.

## Filament

Spool name, colour, material, and loaded or empty per tool, plus mark loaded and mark empty. Local usage counts millimetres since the last spool change. Spoolman mode, when Moonraker has Spoolman, sets that tool's spool as the active spool on a tool change. With the Orbiter module, tangle mode is Pause, Warn, or Off.

Spool setup wizard opens the prompt in Mainsail.

![Filament tab, with usage set to Local and tangle mode Off]({{ site.baseurl }}/assets/images/gui-tab-filament.png)
