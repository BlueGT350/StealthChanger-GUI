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

## Filament

Spool name, colour, material, and loaded or empty per tool, plus mark loaded and mark empty. Local usage counts millimetres since the last spool change. Spoolman mode, when Moonraker has Spoolman, sets that tool's spool as the active spool on a tool change. With the Orbiter module, tangle mode is Pause, Warn, or Off.

Spool setup wizard opens the prompt in Mainsail.

![Filament tab, with usage set to Local and tangle mode Off]({{ site.baseurl }}/assets/images/gui-tab-filament.png)
