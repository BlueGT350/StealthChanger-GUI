# Changelog

## v0.2.14

- Flashing a board that is already on the host Klipper version succeeds. The log says it was reflashed with that same version. A reconnect that stays on a different version is still an error. A host version with `-dirty` on the end matches the same commit without that suffix.
- The Firmware tab leaves a gap between the Host Klipper and print_stats pills and the Refresh / Flash All buttons.

## v0.2.13

- The Firmware tab no longer shows the paragraph about where menuconfig files and board names are stored.
- A board whose firmware is newer than the host Klipper version shows **Firmware Ahead**. **Firmware update required** and **Up to date** are unchanged.
- **Flash All** still updates only boards that are behind, and now says so in its tooltip. Boards that share a menuconfig (comments and blank lines ignored) are built once; that binary is reused. A different menuconfig is built on its own. A progress bar shows the board and the step (Building, Reusing build, Uploading, Verifying, Done, or Failed) while a flash is running.
- `setup.sh` keeps an existing git origin unless you pass `--origin`. That remote is what `[update_manager stealthchanger-gui]` uses. A new install with no origin still uses the public repo. An origin line that already matches is left as it is.

## v0.2.12

- Each board on the Firmware tab is in its own card, with that board's name, status, and buttons inside it.
- The firmware helper restarts itself after an update when no flash is running. Re-run `bash ~/StealthChanger-GUI/setup.sh` once after updating to v0.2.12; after that it's automatic. The Firmware tab warns when the helper version does not match the GUI.

## v0.2.11

Firmware build, after v0.2.10 on a printer:

- The helper's `make` `OUT` directory now ends with `/`. Klipper joins `$(OUT)` directly onto `src/...`, so a missing slash built `out/mcusrc/sched.o` and failed with `autoconf.h: No such file`.
- Build files go in `~/printer_data/stealthchanger-firmware-build/<mcu>/`, outside the Mainsail config folder. That board's directory is emptied before each build. `uninstall.sh` removes the build directory. Menuconfig files stay unless you pass `--purge`.
- `setup.sh` enables the firmware helper and then restarts it, so an update replaces a helper that is already running.
- Typing in a menuconfig text field no longer rebuilds that field on each keystroke. The caret and the text stay put. Dependencies refresh when you leave the field, or after a short pause.
- **Copy to…** copies one board's saved `.config` onto the boards you tick. Replacing a `.config` that is already there asks first.
- **Create** preselects the architecture, processor, and communication interface from the MCU Klipper is already talking to (`mcu_constants` `MCU` and `CLOCK_FREQ`, and CAN versus USB from the config) when those map onto Klipper's Kconfig. A saved `.config` whose `CONFIG_MCU` does not match the reported MCU is marked on that board's row.

## v0.2.10

Firmware tab, after trying v0.2.9 on a printer:

- A board with `canbus_uuid` comes up with **Katapult CAN** already selected. A `/dev/serial/by-id` serial comes up with **Katapult USB** when Katapult's flashtool is installed, and **Direct USB** otherwise. A saved choice still wins.
- Each board has a name. `[mcu]` is **Main Board**. A tool whose extruder or heater pins use that MCU (`EBB0:PA1` under `[tool T0]`) is **T0**, **T1**, and so on. Anything else keeps the MCU name. The name is editable and is stored in `~/printer_data/config/stealthchanger-firmware/labels.json`, outside the git clone.
- Config includes are read the way Klipper reads them: relative to the file that contains the include, with globs and symlinks. `mainsail.cfg`, the `ktc-filament` includes, and `../tools/T*.cfg` are normal and are not warnings. Parsing only stops for a file cycle.
- **Flash all** updates every board that is behind the host Klipper version and already has a `.config`. One confirmation. Boards run in order and stop at the first failure. The print state is checked before each board. Klipper is stopped once and started again at the end.
- A board that is behind shows **Firmware update required**. Otherwise, when its version is known, it shows **Up to date**.
- **Create** and **Edit** open Klipper's menuconfig in the page (the Kconfig and `lib/kconfiglib` from `~/klipper`). Menus, checkboxes, and text fields follow the dependencies. Saving writes that board's `.config` and does not flash. **Import** is still there.

## v0.2.9

The GUI has a Firmware tab. It lists each `[mcu]` from the Klipper config
(USB `serial` or CAN `canbus_uuid`), shows that board's firmware version
next to the host Klipper version, and marks a mismatch.

Flashing is refused while `print_stats.state` is `printing` or `paused`.
The check runs again immediately before Klipper is stopped. A confirm
dialog is required. The log streams into the tab. If a flash fails,
Klipper is started again and the tab says how to recover.

Three methods, picked from how the board is connected:

- Katapult over CAN (`flashtool.py -i can0 -u <uuid>`, after a bootloader request).
- Katapult over USB (`flashtool.py -d /dev/serial/by-id/...`, including a bootloader request).
- Direct USB, no Katapult: `make flash FLASH_DEVICE=...`. STM32 uses Klipper's DFU path and RP2040 uses the boot path. Which one it is comes from `CONFIG_MCU` in the menuconfig file you imported. If that line is missing, or it is neither of those, the updater stops and does not guess.

The menuconfig file for each MCU is stored in
`~/printer_data/config/stealthchanger-firmware/<mcu>.config`, outside the
git clone, so an update does not replace it. Import an existing `.config`.
Nothing in the repo writes those files.

Flashing cannot run inside the static page. `setup.sh` installs a small
helper that listens on `127.0.0.1:7140` only. The GUI nginx site proxies
`/firmware/` to it, so the browser stays on the same origin. The helper
accepts four actions (status, save config, choose method, flash) and does
not run a shell command from the request. `gui/install.sh` allows this
user to `systemctl stop/start/is-active klipper` and nothing else.
`uninstall.sh` removes the helper. The menuconfig files stay unless you
pass `--purge`.

## v0.2.8

If you pick a tool while X, Y or Z is not homed, the GUI asks "Home now?"
Yes homes the printer and then does the tool change. No leaves the current
tool as it is.

## v0.2.7

Orbiter pins now live in `~/printer_data/config/ktc-filament-orbiter.cfg`.
After updating, re-run setup.sh once to move any edited pins there (a backup
is made at `~/stealthchanger-backups/<time>/orbiter_addon.cfg`). The file is
outside the git clone and is not overwritten on later runs. Do not put the
`[gcode_button]` blocks back into `orbiter/orbiter_addon.cfg`.

`setup.sh` clones over HTTPS. The default origin is
`https://github.com/BlueGT350/StealthChanger-GUI.git`. It does not create
a deploy key. `--origin <URL>` points the clone at another repo, and is
the only time an existing `[update_manager stealthchanger-gui]` origin
line is rewritten. `--orbiter` / `--no-orbiter` turn the button add-on on
or off; with neither flag, a terminal asks.

`uninstall.sh` backs up `ktc-filament-orbiter.cfg`, removes its include,
and deletes the file only with `--purge`.

The GUI header uses the Voron Design hex mark (see `NOTICE.md`).
