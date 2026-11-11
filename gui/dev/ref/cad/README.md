# Anthead + Orbiter front view from the real CAD

The Anthead art in `gui/js/art.js` (the `CAD` block) is traced from the
official CAD, not drawn by eye.

## Sources
| Part | Source | Licence |
|---|---|---|
| AntHead: Voron O2 main body (SF), standard left/right ducts, hex grill, Voron logo diffuser + light shield, 2510 fan, Revo mount, Revo | PrintersForAnts/AntHead, `CAD/AntHead_Assembly v105.zip` (`AntHead_Assembly_v105.step`), commit `41e50e15cd36b2c56408e6a0e57a75cfba180d01` (2026-10-05). https://github.com/PrintersForAnts/AntHead | GPL-3.0 (`LICENSE-AntHead.txt`) |
| LDO Orbiter v2.5 + Orbiter Smart Sensor v4.2 | Róbert Lőrincz / Orbiter Projects, RobertLorincz/Orbiter-Toolboards, `Orbiter O2S Toolboard/3D CAD/Orbiter v2.5_SmartSensor and O2S Toolboard.step`, commit `52a5a330e1c57ea71ecdc2192382872b0b254452` (2026-02-01). https://github.com/RobertLorincz/Orbiter-Toolboards (https://www.orbiterprojects.com) | CC0 1.0 (`LICENSE-Orbiter.txt`) |

The Orbitool O2S toolboard and its standoffs in that STEP are left out:
The reference heads don't have one. The red hex cap on the Orbiter hub is not in
the CAD; it is drawn from a reference photo.

## Files
- `src/ant/*.stl`, `src/orbp/*.stl`: the parts, exported from the two STEP
  files by `step_extract.py` in assembly coordinates (X right, Y to the back,
  front face at -Y, Z up). Not in the release zip (size); rebuild them from
  the STEP files.
- `build_outlines.py`: z-buffers the parts orthographically from the front,
  splits what is visible into regions (part + shading band of the face
  normal, light from the top left front), traces and simplifies them, and
  writes `outlines.json` and `svg/`.
- `export_art.py`: writes the `CAD` block of `gui/js/art.js` from `outlines.json`.
- `svg/front_regions.svg`: preview of all visible regions, default colours.
- `svg/front_silhouettes.svg`: head, body, ducts, grill, Orbiter, logo, Revo outlines.
- `svg/outline_<part>.svg`: the full projected outline of one part.

## Placement and scale
- The Orbiter sits on the O2 body's top face (Z 117.66) on its two M3 holes
  (37 mm apart, X 128.22 / 165.21) with the filament path over the body's
  filament hole (X 147.72): translate (146.335, -122.65, 127.96) mm, no
  rotation, gearbox face to the front, as on the reference heads.
- GUI units: 1.95 per mm. GUI x 80 = filament / nozzle axis (X 147.72 mm),
  GUI y 210 = nozzle tip (Z 48.59 mm). The head spans x 23.7-138.8,
  y -44.7 (sensor collet) to 210 (nozzle) in the `0 -48 160 270` viewBox.

## Rebuild
```
pip install trimesh numpy scipy shapely numba scikit-image cadquery-ocp   # cadquery-ocp only for step_extract.py
python step_extract.py "AntHead_Assembly_v105.step" src/ant '^(Main_Body_Voron_O2_v12|Left_Duct_Standard_v13|...)$'
python step_extract.py "Orbiter v2.5_SmartSensor and O2S Toolboard.step" src/orbp '^(Gearset|Latch \(1\)|...)$'
python build_outlines.py src .
python3 export_art.py
```
(The exact part names are the file names in `src/`, and the PARTS list in
`build_outlines.py`.)

## Stealthburner

`build_stealthburner.py` does the same front-view trace for the Voron
Stealthburner. Source is `CAD/Stealthburner_CW2_Assembly.step` in
VoronDesign/Voron-Stealthburner, commit `75a2ab07` (GPL-3.0, see
`LICENSE-Stealthburner.txt`). It writes `sb_outlines.json` and
`svg/sb_front_regions.svg`. `export_sb_art.py` replaces the `SB` block in
`gui/js/art.js`.

```
python build_stealthburner.py Stealthburner_CW2_Assembly.step src/sb .
python3 export_sb_art.py
```

Same scale as the Anthead: 1.95 units per mm, x 80 on the nozzle axis,
y 210 at the nozzle tip. Shading uses only faces that won the front z-buffer.
The cowl is a shell, so a median over every face points inward and would paint
the front as the deepest shadow; a part is flipped only when its visible faces
point away from the camera. The cowl is one printed part (body colour). The
Revo Voron hotend mount is a second printed part and uses the accent colour.
Clockwork 2 is the black extruder on top. The cowl's hotend opening is a
plain circle; the animated rotor fills it. The assembly's single 5015 is
mounted on the extruder rather than in a duct, so it is not drawn. Part
cooling is the cowl horns plus the `.air` streaks at the two outlets.

## Dragon Burner

`build_dragonburner.py` traces the standard v8 cowl. Source is
`V0/Dragon_Burner/CAD/Dragon_Burner_v0.2_v8.step` in chirpy2605/voron,
commit `a02fcea` (GPL-3.0, see `LICENSE-DragonBurner.txt`). The file places
`MainBody`, the left and right `Standard` ducts, `[c]_Voron_Diffuser` and
`RevoVoron_Mount`. The Revo metal is the Revo Voron from the Stealthburner
assembly, seated with the heatsink top on the underside of `RevoVoron_Mount`
and the heatsink axis on that mount's bore, so the nozzle tip is 5.1 mm
below the duct exits. The two part-cooling fans are 4010 blowers (40 mm
frame, 10 mm thick) in the cowl's side slots; only their front edges show,
and they are drawn black. `export_db_art.py` replaces the `DB` block.

```
python build_dragonburner.py Dragon_Burner_v0.2_v8.step Stealthburner_CW2_Assembly.step src/db .
python3 export_db_art.py
```

Same scale as the other heads: 1.95 units per mm, x 80 on the nozzle axis,
y 210 at the Revo nozzle tip. The cowl uses the body colour and the ducts
use the accent colour. Visible-face normals are corrected the same way as
the Stealthburner. Edge strokes are the cowl, the ducts, the fan opening
and the black blower edges.

`export_art.py` also writes `CAD.head` (the cowl without the Orbiter) and the
`ORB` block (the Orbiter alone, still in this frame). The assembled `CAD.r`
stays, so an Anthead with Orbiter 2.5 is the original trace and the Orbiter
is not drawn a second time. Dragon Burner uses `ORB` or `WW`, shifted by
`(0, -5.5)` so the mount contact (y 74.2) meets the cowl top (y 68.7).

## WW-BMG Wristwatch

`build_wristwatch.py` traces the assembled extruder. Source is
`Wristwatch_Extruder_BMG/CAD/Wristwatch BMG v3.step` in
bythorsthunder/Voron_Mods, commit `359b95fb7263` (2024-05-23, GPL-3.0, see
`LICENSE-Wristwatch.txt`). The STEP lays body and motor-plate variants out
beside each other. The trace uses the main body the tension arm overlaps,
that motor plate, the tension arm, and the BMG gears. The two M3 holes on
the bottom are 37 mm apart. The filament bore between them is put on x 80
and the bottom face on y 74.2, the Orbiter's contact, so both extruders
share one mount offset per head. `export_ww_art.py` replaces the `WW` block.

```
python build_wristwatch.py "Wristwatch BMG v3.step" .
python3 export_ww_art.py
```

## Reference print tweaks (in build_outlines.py, kept on rebuild)
- `FILL_HOLES`: the two round holes beside the Voron logo on Main_Body_Voron_O2_v12 are filled with the surrounding panel face.
- Two-colour grille: Hex_Grill_v11 shape is stock. The closed cells' floor (front faces 0.3-1.2 mm behind the web's front plane) becomes the virtual part `Hex_Grill_v11_floor` on the panel layer (`b`, gray); the hex web, cell chamfers and outer frame stay on the accent layer (`a`, red), matching his slicer face painting.
