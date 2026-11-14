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
