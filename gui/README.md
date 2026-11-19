# ktc-gui: StealthChanger web GUI (rev 2, v0.2.12)

Install and updates: see the repo [README](../README.md) (`setup.sh`, Update Manager). Fix list and status: [FIXLIST.md](FIXLIST.md).

A one-page control screen for a KTC-Easy toolchanger. Spools run across the top
and each toolhead sits under its spool. It talks straight to Moonraker. It runs
beside Mainsail and changes nothing in Mainsail.

Plain HTML/CSS/JS: no build step, no internet needed (no CDN).

## Install (on the printer)
The normal route is `setup.sh` in the repo root (see the main README): it
links the nginx root to the git clone, so Update Manager updates the page too.

By hand, over SSH:
```
cd ~/printer_data/config/ktc-filament/gui
./install.sh --link     # serve straight from this folder (git clone)
./install.sh            # or copy the files to ~/ktc-gui
```
Then open **http://&lt;printer-ip&gt;:8090**. Mainsail stays on its usual port.

What `install.sh` does:
- copies `index.html`, `style.css` and `js/` to `~/ktc-gui`, or with `--link`
  makes `~/ktc-gui` a link to this folder (an old copy is moved to
  `~/ktc-gui.old-<time>`);
- writes the nginx site `/etc/nginx/sites-available/ktc-gui`, links it into
  `sites-enabled`, and serves the GUI on port 8090. `/websocket` and Moonraker's
  HTTP paths are passed to Moonraker on `127.0.0.1:7125`;
- runs `nginx -t` before reloading and puts the old site back if the test fails.
  It never touches the Mainsail site or `moonraker.conf`.

Options:
| Command | Does |
|---|---|
| `./install.sh` | install, or update and rewrite the nginx site |
| `./install.sh --link` | serve this folder through a `~/ktc-gui` link (git installs) |
| `./install.sh --files-only` | update the GUI files only (copy installs) |
| `./install.sh --uninstall` | remove the nginx site and `~/ktc-gui` (a link only, never the clone) |
| `PORT=8091 ./install.sh` | use another port (also `MOONRAKER=host:port`, `DEST=dir`) |

The full uninstall (configs, symlink, this nginx site, optional clone) is
`uninstall.sh` in the repo root. See the main README.

No hard refresh is needed after an update (v0.2.2 and later). `index.html` loads
every file with `?v=<version>` and carries `<meta name="ktc-gui-version">`. The
page asks the web server which version it serves (on connect, Klipper ready,
Update Manager notices, when the tab comes back, and every 10 minutes) and, if
that is newer or older than itself, reloads on a fresh URL once idle: not while
printing, paused or changing tools, with a dialog open, or while typing. A loop
guard stops after two tries for the same version and asks for a manual reload.
This works with old nginx sites too; `install.sh` also sends
`Cache-Control: no-cache` so browsers check `index.html` every time.
If the header says the GUI folder is not the git clone, run `setup.sh` again.

### CORS (`cors_domains`)
Through the installed nginx site the GUI and Moonraker share one origin, so
**no Moonraker change is needed.** You only need `cors_domains` when the GUI is
served from a different machine or port than the Moonraker it talks to, such as
the dev mock or a copy on your PC. In that case add the GUI's origin to
`moonraker.conf`, then restart Moonraker:
```
[authorization]
cors_domains:
    *://my-pc:8090
```
If Moonraker asks for a login (`force_logins: True`), the GUI's machine also
needs to be in `trusted_clients`. Rev 1 has no login screen.

### Which printer it talks to
By default the GUI talks to the machine it was loaded from. To point it at
another printer, use `http://<gui-host>:8090/?host=<printer-ip>:7125`, or change
**Settings > Moonraker host** (saved in the browser).

## What's on the screen
- **Header:** Voron Design mark, GUI version and installed git commit (from Moonraker's Update
  Manager; an arrow means an update is waiting, hover for details), printer
  state, toolchanger state, the KTC-E and module versions, and a warning chip
  when a module is missing or too old.
- **Rack:** one column per tool, numbered from the toolchanger config.
  - Spool: colour, material, spool name, the temp profile in use, the
    sensor state (filament / empty / sensor off), and *Partly unloaded* after a
    nozzle change. Click it to open the spool editor. The spool sits left of
    its head, the filament line leaves the spool's outer edge and runs straight
    down into the Orbiter's top inlet, and the spool details sit to the right of
    the line.
  - Toolhead: each head sits on its dock on the shelf rail. The active head is
    lifted off its dock (and moves up/down during a tool change). Below it:
    temps (click to set), part fan and hotend fan, tool offsets and the ACTIVE
    marker. The hotend fan spins behind the grille, the part fans blow air, and
    the nozzle glows as it heats. Click a head to pick that tool up.
  - Head LEDs: the `//` logo and the light under the nozzle show the head's
    real LEDs, live (see *Head LEDs* below).
- **Spool editor:** material, colour, spool name, profile (HIGH / LOW /
  CUSTOM with load and unload temps), and the per-tool motion settings (load,
  unload and nozzle-change lengths and speeds). Every field has a tooltip.
  *Save* sends only what changed, with `SET_TOOL_FILAMENT`, `SET_FILAMENT_TEMP`
  and `SET_FILAMENT_MOTION`. The editor also has **Load**, **Unload** and
  **Nozzle change** buttons (`FILAMENT_LOAD`, `FILAMENT_UNLOAD`,
  `FILAMENT_UNLOAD_PARTIAL`).
- **Tabs:**
  - *Calibration:* calibrate all offsets; calibrate one tool's offset (runs T0
    first as the reference); nozzle probe offset; move over the calibration
    switch and query it; tool align start/test/done; SAVE_CONFIG (only when
    something is waiting to be saved). Buttons that move a tool send `G28`
    first when X, Y or Z isn't homed (the confirm says so). *Save* lists any
    offset that is also set in an included file, which would make SAVE_CONFIG
    fail, and says how to fix it (see the main README, *SAVE_CONFIG conflicts*).
    *Re-check config* reads the files again after you edit them.
  - *Tool actions:* home, initialize the toolchanger, detect the active tool,
    park (`UNSELECT_TOOL`, only if your config has it), test docking, pick up a
    tool, all heaters off, and the toolchanger status.
  - *Recovery:* RESUME (only when paused), `INITIALIZE_TOOLCHANGER`, and
    *Recover from error* (`INITIALIZE_TOOLCHANGER_ORIG RECOVER=1`, only when
    the toolchanger is in error), crash detection, docking mode, verify tool
    detected, reset tool parameters.
  - *Filament* (only when the filament module is installed): spool table with
    mark loaded / mark empty (`SET_FILAMENT_LOADED`), SHOW_TOOLS, the Mainsail
    spool setup wizard, and the Orbiter tangle mode (PAUSE / WARN / OFF; only
    with the Orbiter add-on). The tool that is printing always shows as loaded.
  - *Filament usage* (in the Filament tab, needs `usage/usage_addon.cfg`):
    Local or Spoolman. Local shows metres used per tool since the last spool
    change. Spoolman is only selectable when Moonraker reports Spoolman; then
    each tool gets a spool picker, *Edit spool* and *Add spool* (new filament
    and vendor included). It all goes through Moonraker's Spoolman proxy, so no
    browser tab opens. The used length also shows on each spool in the rack.
  - *Firmware:* MCU list from the Klipper config, with names (Main Board, T0,
    T1, or your own label). A board behind the host Klipper version is marked
    Firmware update required. Create or Edit opens menuconfig in the page;
    Import still takes an existing `.config`. Flash one board, or Flash all
    of the boards that are behind. A `canbus_uuid` defaults to Katapult over
    CAN. A `/dev/serial/by-id` serial defaults to Katapult over USB when
    Katapult is installed, otherwise direct `make flash` (STM32 DFU or RP2040
    boot, from `CONFIG_MCU`). Refuses while printing or paused. Menuconfig
    files and labels live in `~/printer_data/config/stealthchanger-firmware/`
    and are not part of this repo.
- **Console strip:** the last console lines and a G-code input.
- **Settings (gear icon):** Moonraker host; per tool: toolhead art, panel
  colour, accent colour (ducts and grille), and the LED mapping; *apply to all*
  for the colours. Saved in Moonraker's database under the
  `ktc_gui` namespace, so every browser sees the same setup.

### Head LEDs
The GUI subscribes to `color_data` of every `neopixel`, `dotstar`, `led`,
`pca9533` and `pca9632` object and draws each head's LEDs from it:
- **Which LED object:** *auto* picks the object whose name has `T<n>` as its
  own token, e.g. `neopixel headlight_T0` for T0 (`T1` does not match `T10`).
  In Settings you can pick another object or *none*.
- **Which LEDs in the chain:** the logo uses LED `1` and the nozzle light LEDs
  `2-3` by default (the Anthead wiring). Numbers are **1-based, like
  `SET_LED ... INDEX=`**, so `1` is the first LED (Klipper's `color_data[0]`).
  Use a number, a range or a list: `1`, `2-3`, `2,3`.
- **Colour:** the chosen LEDs are averaged; white (GRBW) is added to red,
  green and blue; the brightest channel sets the brightness of the glow.
- With no LED object (or *none*) the logo is drawn dim and unlit and there is
  no nozzle light.
- The mapping is saved with the other head prefs in the `ktc_gui` namespace.
- **Hover halo:** the spool glows in its own colour, lightened until it is at
  least 3:1 against the rack, so dark colours stay visible. Black and grey
  spools get a soft light-grey glow.
- **Tool change animation:** when the change ends, the head finishes its lift
  cycle and then eases to rest.
- **Phones:** the header is one row (430 px and 375 px tested); OK module
  chips are hidden and, under 400 px, the connection chip becomes a dot.

Defaults for the other per-tool parts, which match a standard KTC-E config:
`fan_generic T<n>_partfan` (or the tool's `fan:`), the `heater_fan` that
lists the tool's extruder (else `heater_fan T<n>_hotend_fan`) and
`filament_switch_sensor filament_sensor_T<n>`.

### Safety
- Anything that moves the printer, changes a tool or heats asks first.
- If you pick a tool while X, Y or Z is not homed, the GUI asks "Home now?"
  Yes homes the printer and then changes tools. No leaves the current tool
  as it is.
- While printing or paused, motion and tool-change buttons are greyed out, and
  hovering says why. Temps, fans, the spool editor's Save and tangle mode
  still work mid-print.
- Buttons for modules that aren't installed are removed, not just greyed out.

### Module detection
The GUI reads Klipper's objects on connect, and again after every Klipper restart:
| Shows | When it finds |
|---|---|
| Rack, tool actions, recovery | `toolchanger` (KTC-Easy) |
| Spool editor, Filament tab | `gcode_macro _FILAMENT_SETTINGS` |
| Sensor state, tangle mode | `gcode_macro _ORBITER_SETTINGS` |

## Dev and tests (on a PC)
```
python3 gui/dev/mock_moonraker.py --klipper ~/klipper     # http://127.0.0.1:8090/
python3 gui/dev/test_gui.py --klipper ~/klipper --shots   # needs: pip install playwright aiohttp; playwright install chromium
node gui/dev/test_commands.js                             # G-code builder unit test
node gui/dev/test_art.js                                  # Anthead / Stealthburner / Dragonburner art
node gui/dev/test_extruder.js                             # head/extruder composition, migration, Clockwork lock
```
The mock runs the real macro files through the same Klipper harness as
`tests/klipper_harness.py`, so the GUI is tested against the real macros, not a
copy. Mock options: `--modules base|filament|orbiter`, `--tools N`,
`--state printing|paused` (set `tc_status` to `error` with `POST /mock/set`), `--changing`, `--unselect`,
`--leds 0,1,2` (which tools get a `neopixel headlight_T<n>`, 3 LEDs, GRBW; default all),
`--spoolman connected|disconnected|off`, `--configs conflict|clean` (synthetic config
files with or without SAVE_CONFIG conflicts), `--no-usage`, `--homed xyz|xy|''`.
The mock animates the LEDs (active green, heating pulsing yellow, changing blue,
idle dim red; nozzle white) until you send `SET_LED` to one.
`test_gui.py` also covers 3- and 6-tool racks, every module combination, the
print lock, the nozzle change, the prefs, the LED mapping, tablet width and JS errors.
`--shots` saves screenshots to `gui/dev/screenshots/`.

## Credits
The header mark is the Voron Design hex logo, inlined from Mainsail's built-in
Voron logo (`sidebarLogo-voron.svg`). Same artwork as
VoronDesign/Voron-Extras `Images/Logo/SVG/Hex_Logo.svg`. Both are GPL-3.0.
Details and the commit ids: [NOTICE.md](../NOTICE.md).

Toolhead art traced from the real CAD (how: `gui/dev/ref/cad/README.md`):
- **AntHead** by PrintersForAnts, https://github.com/PrintersForAnts/AntHead, GPL-3.0.
- **LDO Orbiter v2.5 + Orbiter Smart Sensor** CAD by Róbert Lőrincz / Orbiter Projects,
  https://github.com/RobertLorincz/Orbiter-Toolboards (https://www.orbiterprojects.com), CC0 1.0.
- **Stealthburner + Clockwork 2** by Voron Design, https://github.com/VoronDesign/Voron-Stealthburner
  (`CAD/Stealthburner_CW2_Assembly.step`, commit `75a2ab07`), GPL-3.0.
- **Dragon Burner v8** by chirpy2605, https://github.com/chirpy2605/voron/tree/main/V0/Dragon_Burner
  (`CAD/Dragon_Burner_v0.2_v8.step`, commit `a02fcea`), GPL-3.0. The drawing is the
  standard cowl, ducts and Voron diffuser from that file. The Revo and the black
  side fans are seated on that cowl.
- **WW-BMG Wristwatch** by bythorsthunder, https://github.com/bythorsthunder/Voron_Mods
  (`Wristwatch_Extruder_BMG/CAD/Wristwatch BMG v3.step`, commit `359b95fb7263`), GPL-3.0.
  The drawing is the assembled main body, motor plate, tension arm and BMG gears.
  Its Orbiter-2 mount holes sit on the same contact as the Orbiter.

Settings stores a head and an extruder per tool (Moonraker namespace `ktc_gui`).
That choice only changes the drawing. Stealthburner's extruder selector shows
Clockwork 2 and stays disabled. A profile saved before v0.2.6, with no extruder
field, comes back as Orbiter 2.5.

## Later
- Follow Mainsail's selected logo. The header mark is `#brandLogo`
  (`data-logo="voron"`); that hook is not wired up.
- More heads and extruders on the same mount.
- Mainsail sidebar link and Orca device tab; MCU firmware update page.
