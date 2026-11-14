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

## Voron Design logo (GUI header)

The mark at the top left of the GUI (`#brandLogo` in `gui/index.html`) is the
Voron Design hex logo: a red hexagon with the stylized V cut out. On the dark
header the cutout reads black, which is the red/black Voron mark.

The SVG in the page is Mainsail's built-in Voron logo, inlined so the GUI
does not request a file:

- Project: [mainsail-crew/mainsail](https://github.com/mainsail-crew/mainsail)
- File: `public/img/themes/sidebarLogo-voron.svg`
- Commit: [`ad9e71b4cd96e8ee6c10c799cf352042bb0da98b`](https://github.com/mainsail-crew/mainsail/blob/ad9e71b4cd96e8ee6c10c799cf352042bb0da98b/public/img/themes/sidebarLogo-voron.svg) (2024-07-06, "feat(theme): add voron build-in theme")

That file is the same hex-and-V mark as the official artwork published by
Voron Design:

- Project: [VoronDesign/Voron-Extras](https://github.com/VoronDesign/Voron-Extras)
- File: `Images/Logo/SVG/Hex_Logo.svg`
- Commit: [`eedb4ae82025a1e3626678a36cadd52c98e36b2e`](https://github.com/VoronDesign/Voron-Extras/blob/eedb4ae82025a1e3626678a36cadd52c98e36b2e/Images/Logo/SVG/Hex_Logo.svg) (2023-06-07)

Both repositories are GNU General Public License v3.0 only. The logo files
carry no extra non-commercial or no-derivatives term. GPL-3.0 is compatible
with the GPL-3.0 Voron artwork already credited in this repo
(`gui/dev/ref/cad/LICENSE-Stealthburner.txt`, and the GPL-3.0 text in
`gui/dev/ref/cad/LICENSE-AntHead.txt`). The header mark is included on those
same terms.

What changed when it was inlined: the XML prologue, doctype and the
`translate(-12.4117, -10)` wrapper are baked into the path; editor metadata is
not copied; the fill is `currentColor`, set to `#FF2300` (Mainsail's default
`--color-logo` for this file) so the red stays solid on the dark theme. The V
is a cutout, as in Mainsail's sidebar logo, rather than a separate black shape.

`#brandLogo` (`data-logo="voron"`) is the hook for a future "follow Mainsail's
selected logo" feature. That feature is not implemented; the header always
shows this Voron mark.

The Voron name and this mark are trademarks of their owners. This GUI is not
an official Voron Design product.

## What this file is not

No patent grant, no warranty, and no claim that a particular printer
configuration is safe.
