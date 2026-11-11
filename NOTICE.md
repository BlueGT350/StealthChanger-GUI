# Third-party work shipped in this tree

The macros, installer and web GUI in this repository are distributed under
the GNU General Public License v3.0. The license text is `LICENSE`.
That choice covers the traced toolhead outlines, which are derived from
GPL-3.0 CAD. It is not a statement about any other person's printer,
photos or config.

## Toolhead drawings

The outlines in `gui/js/art.js` are traced from CAD, not drawn from scratch.
How to rebuild them, and the exact commits, are in
`gui/dev/ref/cad/README.md`.

| Drawing | Upstream | Commit named in this tree | Their licence |
|---|---|---|---|
| AntHead cowl | PrintersForAnts/AntHead | `41e50e15cd36b2c56408e6a0e57a75cfba180d01` | GPL-3.0 (`gui/dev/ref/cad/LICENSE-AntHead.txt`) |
| Stealthburner + Clockwork 2 | VoronDesign/Voron-Stealthburner | `75a2ab079c5aa3a0b6fbb0957de764ce13879da3` | GPL-3.0 (`gui/dev/ref/cad/LICENSE-Stealthburner.txt`) |
| Dragon Burner v8 | chirpy2605/voron `V0/Dragon_Burner` | `a02fceadea974f728f80dacc2349508c0521a337` | GPL-3.0 (`gui/dev/ref/cad/LICENSE-DragonBurner.txt`) |
| WW-BMG Wristwatch | bythorsthunder/Voron_Mods | `359b95fb7263` | GPL-3.0 (`gui/dev/ref/cad/LICENSE-Wristwatch.txt`) |
| LDO Orbiter v2.5 + Smart Sensor | RobertLorincz/Orbiter-Toolboards | `52a5a330e1c57ea71ecdc2192382872b0b254452` | CC0 1.0 (`gui/dev/ref/cad/LICENSE-Orbiter.txt`) |

The raw STEP files are not in this repository (size). The rebuild scripts
under `gui/dev/ref/cad/` are. The Orbiter text in `LICENSE-Orbiter.txt` is
the CC0 1.0 legalcode already stored with that attribution.

## What this file is not

No patent grant, no warranty, and no claim that a particular printer
configuration is safe.
