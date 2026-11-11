# StealthChanger GUI

A web GUI for StealthChanger toolchangers running
[KTC-Easy](https://github.com/jwellman80/klipper-toolchanger-easy) on Klipper,
Moonraker and Mainsail.

It shows a rack view of your tools and spools, and ships filament load/unload
macros plus an optional Orbiter 2.5 button/sensor add-on and a filament-usage
add-on (local counts or Spoolman). The GUI auto-detects which modules are
installed.

Current release: **v0.2.6**.

## Install (on the printer's Pi)

Run this as the same user Klipper runs as (not root):

```bash
cd ~
git clone https://github.com/BlueGT350/StealthChanger-GUI.git
bash ~/StealthChanger-GUI/setup.sh
```

`setup.sh` is safe to run again. It:

1. Clones or updates `~/StealthChanger-GUI` over HTTPS (no deploy key).
2. Moves an existing `~/printer_data/config/ktc-filament` folder to
   `~/stealthchanger-backups/<time>/`, carries your changed settings into
   `ktc-filament-overrides.cfg`, and links
   `~/printer_data/config/ktc-filament` to the clone so existing
   `[include ktc-filament/...]` lines keep working.
3. Points the GUI nginx site (`ktc-gui`, port 8090) at the clone. If no GUI
   site exists yet, it tells you to run
   `bash ~/StealthChanger-GUI/gui/install.sh --link`.
4. Adds the Moonraker Update Manager entry below if it is missing.
5. Adds `[include ktc-filament/usage/usage_addon.cfg]` and
   `[include ktc-filament-overrides.cfg]` to `printer.cfg` if they are
   missing (and only if a `ktc-filament` include is already present).
6. Restarts Klipper and Moonraker only when something changed, and never
   while printing or paused.

`bash setup.sh --no-restart` changes files only.

Then open `http://<printer-ip>:8090`. Include the modules you want in
`printer.cfg` (after your KTC-Easy include):

```
[include ktc-filament/filament/filament_macros.cfg]
[include ktc-filament/orbiter/orbiter_addon.cfg]
[include ktc-filament/usage/usage_addon.cfg]
[include ktc-filament-overrides.cfg]
```

The Orbiter include is optional. If you use it, edit the example Nitehawk-36
button pins in `orbiter/orbiter_addon.cfg` to match your boards. Do not edit
files inside the clone for settings you want to keep: put overrides in
`ktc-filament-overrides.cfg`.

Manual setup without `setup.sh`: clone or copy the repo to
`~/printer_data/config/ktc-filament`, add the includes, and run
`gui/install.sh` (copy) or `gui/install.sh --link` (serve from the folder).

More GUI detail is in [gui/README.md](gui/README.md).

## Updates

Updates show up in Mainsail (*Machine > Update Manager*) or Orca's Device tab.
The entry `setup.sh` adds to `moonraker.conf`:

```
[update_manager stealthchanger-gui]
type: git_repo
channel: stable
path: ~/StealthChanger-GUI
origin: https://github.com/BlueGT350/StealthChanger-GUI.git
primary_branch: main
is_system_service: False
managed_services: klipper
```

`channel: stable` offers tagged releases (`vX.Y.Z`) only. Use `channel: dev`
to follow every commit on `main`. After an update Moonraker restarts Klipper
(it will not update while printing). From v0.2.2 on, an open GUI page reloads
itself onto the new version.

## Licence

The macros, installer and web GUI are GNU GPL v3.0 (`LICENSE`). Third-party CAD
attribution for the toolhead drawings is in `NOTICE.md`.
