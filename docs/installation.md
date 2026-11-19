---
layout: default
title: Install
nav_order: 2
description: Clone the repository and run setup.sh on the printer.
---

# Install

On the printer:

```
git clone --branch main https://github.com/BlueGT350/StealthChanger-GUI.git ~/StealthChanger-GUI
bash ~/StealthChanger-GUI/setup.sh
```

`setup.sh` asks whether to include the Orbiter button add-on. Pass `--orbiter` or `--no-orbiter` to skip the question. It also adds the `stealthchanger-gui` Update Manager entry and the printer.cfg includes.

It installs the firmware helper (a user service on `127.0.0.1:7140`) and, when nginx and sudo are available, points the GUI site's `/firmware/` path at it. Menuconfig files go in `~/printer_data/config/stealthchanger-firmware/` and are not replaced on later runs.
