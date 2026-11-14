# Changelog

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
