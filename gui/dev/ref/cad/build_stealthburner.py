"""Front-view outlines of the Voron Stealthburner + Clockwork 2 from the official CAD.

    python build_stealthburner.py /path/to/Stealthburner_CW2_Assembly.step src/sb .

The STEP is VoronDesign/Voron-Stealthburner CAD/Stealthburner_CW2_Assembly.step
(GPL-3.0). It is not stored in this repo. Assembly frame, same as the Anthead
pipeline: X right, Y to the back (front face at -Y), Z up.

Stealthburner_Body and the Revo front printhead contain a few unbounded
construction faces. Their analytic bounding boxes are metres wide; the mesh
nodes are not. Faces whose meshed nodes span more than 200 mm are dropped.

The single 5015 in this assembly sits on the extruder, not in a part-cooling
duct, so it is left out. Part cooling is the cowl's lower horns plus the .air
streaks at the two outlets. The BMG gears stay inside the Clockwork shell and
are not drawn. The cowl's fan opening is an open circle (no printed grille);
the animated rotor fills that circle.
"""
import sys, os, json
import numpy as np
import trimesh
from numba import njit
from skimage import measure
from shapely.geometry import MultiPolygon, Polygon
from scipy import ndimage

from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDF import TDF_Label
from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone
from OCP.TopLoc import TopLoc_Location
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID, TopAbs_REVERSED
from OCP.BRep import BRep_Tool
from OCP.TopoDS import TopoDS

STEP, SRC, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(SRC, exist_ok=True)
os.makedirs(os.path.join(OUT, 'svg'), exist_ok=True)

# name -> (layer, linear deflection mm). logo and the nozzle are meshed finer.
# layer: b body colour, k black, m metal, y brass, logo, fan (animated, not drawn)
WANT = {
    'Stealthburner_Body': ('b', 0.12),
    'LED_Diffuser': ('logo', 0.04),
    'LED_Diffuser_Mask': ('k', 0.06),
    'LED_Carrier': ('k', 0.10),
    '4010 Axial Fan': ('fan', 0.15),
    'Main_Body': ('k', 0.12),
    'Latch': ('k', 0.10),
    'Guilder_A': ('k', 0.10),
    'Guilder_B': ('k', 0.10),
    'Shuttle': ('k', 0.10),
    'M3 Thumb Screw (1) (1)': ('m', 0.08),
    # Separate printed hotend mount. It is wider than the cowl and shows at the
    # lower corners, so it takes the accent colour (the second print colour).
    'Toolhead Revo Voron Front': ('a', 0.12),
    'Heatsink': ('m', 0.10),
    'V7_wo_fanV7_Fitment_CAD_wo_fan_1.3': ('revo', 0.08),
}


def name_of(label):
    a = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), a):
        return a.Get().ToExtString()
    return '?'


def mesh_shape(shp, lin, ang=0.35):
    BRepMesh_IncrementalMesh(shp, lin, False, ang, True)
    verts, faces, base = [], [], 0
    ex = TopExp_Explorer(shp, TopAbs_FACE)
    while ex.More():
        face = TopoDS.Face(ex.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        ex.Next()
        if tri is None or tri.NbNodes() < 1 or tri.NbTriangles() < 1:
            continue
        tr = loc.Transformation()
        nodes = []
        for i in range(1, tri.NbNodes() + 1):
            p = tri.Node(i).Transformed(tr)
            nodes.append((p.X(), p.Y(), p.Z()))
        arr = np.asarray(nodes, float)
        if (arr.max(0) - arr.min(0)).max() > 200:
            continue
        tris = []
        rev = face.Orientation() == TopAbs_REVERSED
        for i in range(1, tri.NbTriangles() + 1):
            n1, n2, n3 = tri.Triangle(i).Get()
            tris.append((n1 - 1, n3 - 1, n2 - 1) if rev else (n1 - 1, n2 - 1, n3 - 1))
        verts.append(arr)
        faces.append(np.asarray(tris, np.int64) + base)
        base += len(arr)
    if not verts:
        return None
    m = trimesh.Trimesh(np.concatenate(verts), np.concatenate(faces), process=False)
    m.remove_unreferenced_vertices()
    return m


def solids_of(shp):
    out = []
    ex = TopExp_Explorer(shp, TopAbs_SOLID)
    while ex.More():
        out.append(TopoDS.Solid(ex.Current()))
        ex.Next()
    return out


print('reading', STEP, flush=True)
doc = TDocStd_Document(TCollection_ExtendedString('doc'))
reader = STEPCAFControl_Reader()
reader.SetNameMode(True)
assert reader.ReadFile(STEP) == IFSelect_RetDone
reader.Transfer(doc)
st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
found = {}


def walk(label, loc):
    nm = name_of(label)
    ref = TDF_Label()
    if st.IsReference_s(label):
        st.GetReferredShape_s(label, ref)
        walk(ref, loc.Multiplied(st.GetLocation_s(label)))
        return
    if nm in WANT and nm not in found:
        found[nm] = st.GetShape_s(label).Moved(loc)
        return
    if st.IsAssembly_s(label):
        ch = TDF_LabelSequence()
        st.GetComponents_s(label, ch)
        for i in range(1, ch.Length() + 1):
            walk(ch.Value(i), loc)


roots = TDF_LabelSequence()
st.GetFreeShapes(roots)
for i in range(1, roots.Length() + 1):
    walk(roots.Value(i), TopLoc_Location())
missing = [k for k in WANT if k not in found]
if missing:
    raise SystemExit('missing parts: ' + ', '.join(missing))

parts = []  # (file, layer, mesh)


def add(fn, layer, mesh):
    if mesh is None or len(mesh.faces) == 0:
        print(' skip empty', fn)
        return
    mesh.export(os.path.join(SRC, fn))
    parts.append((fn, layer, mesh))
    b0, b1 = mesh.bounds
    print('%-28s %-6s tris %6d span %s' % (fn, layer, len(mesh.faces), np.round(b1 - b0, 1)), flush=True)


for nm, (layer, lin) in WANT.items():
    if nm.startswith('V7_'):
        # The Revo model is one compound: nozzle, heater, heat break, and four
        # long open sheets that are not the printed hotend. Keep the closed parts
        # and split the nozzle cone (brass) off the heat break (steel).
        for i, sol in enumerate(solids_of(found[nm])):
            m = mesh_shape(sol, lin)
            if m is None:
                continue
            span = m.bounds[1] - m.bounds[0]
            z0, z1 = m.bounds[0][2], m.bounds[1][2]
            if span.max() > 40:
                print(' drop revo solid %d span %s (construction sheet)' % (i, np.round(span, 1)))
                continue
            if z0 < 1 and span[2] > 12:
                c = m.triangles_center[:, 2]
                tip = m.submesh([np.nonzero(c < 5.0)[0]], append=True)
                rest = m.submesh([np.nonzero(c >= 5.0)[0]], append=True)
                add('Revo_Nozzle.stl', 'y', tip)
                add('Revo_Heatbreak.stl', 'm', rest)
            elif z1 < 8 and span[0] < 16:
                add('Revo_Nut_%d.stl' % i, 'm' if z0 > 1 else 'y', m)
            elif span[0] > 14 and span[2] > 8:
                add('Revo_Heater.stl', 'k', m)
            else:
                add('Revo_Part_%d.stl' % i, 'm', m)
        continue
    add(nm.replace(' ', '_').replace('(', '').replace(')', '') + '.stl', layer, mesh_shape(found[nm], lin))

# nozzle tip and filament axis. The cowl is symmetric about X = 0; the Revo tip is the lowest Z.
tip = [p for p in parts if p[0] == 'Revo_Nozzle.stl'][0][2]
TIPZ = float(tip.bounds[0][2])
AX = 0.0
S = 1.95
R = 18
print('TIPZ', round(TIPZ, 3), 'parts', len(parts), flush=True)

LIGHT = np.array([-0.4, -0.55, 0.7]); LIGHT /= np.linalg.norm(LIGHT)
TONES = ['hi', 'lt', 'md', 'dk', 'dd', 'xd']


def tone_of(b):
    for t, th in zip(TONES, [0.86, 0.68, 0.45, 0.25, 0.05]):
        if b > th:
            return t
    return 'xd'


@njit(cache=True)
def raster(P, Z, W, H, zb, idb):
    for t in range(P.shape[0]):
        x0, y0 = P[t, 0, 0], P[t, 0, 1]
        x1, y1 = P[t, 1, 0], P[t, 1, 1]
        x2, y2 = P[t, 2, 0], P[t, 2, 1]
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:
            continue
        mnx = max(int(np.floor(min(x0, x1, x2))), 0)
        mxx = min(int(np.ceil(max(x0, x1, x2))), W - 1)
        mny = max(int(np.floor(min(y0, y1, y2))), 0)
        mxy = min(int(np.ceil(max(y0, y1, y2))), H - 1)
        for py in range(mny, mxy + 1):
            for px in range(mnx, mxx + 1):
                cx = px + 0.5
                cy = py + 0.5
                a = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / den
                b = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / den
                c = 1 - a - b
                if a < -1e-6 or b < -1e-6 or c < -1e-6:
                    continue
                z = a * Z[t, 0] + b * Z[t, 1] + c * Z[t, 2]
                if z < zb[py, px]:
                    zb[py, px] = z
                    idb[py, px] = t


meshes = [(fn, layer, m) for fn, layer, m in parts]
allb = np.concatenate([m.bounds for _, _, m in meshes])
xmin, zmin = allb[:, 0].min() - 1.5, allb[:, 2].min() - 1.5
xmax, zmax = allb[:, 0].max() + 1.5, allb[:, 2].max() + 1.5
W, H = int((xmax - xmin) * R), int((zmax - zmin) * R)
print('raster', W, H, flush=True)
tri_all, part_of, n_all, lab_all = [], [], [], []
for pi, (fn, layer, m) in enumerate(meshes):
    tri = m.triangles
    n = m.face_normals.copy()
    # Normals are corrected after the z-buffer, from the faces that are
    # actually visible. A median over every face flips the cowl: it is a
    # shell, so most of its area faces inward, and the front then shades as
    # the deepest shadow instead of the chosen colour.
    br = n @ LIGHT
    lab = np.digitize(br, [0.05, 0.25, 0.45, 0.68, 0.86]).astype(np.int64)
    tri_all.append(tri)
    part_of.append(np.full(len(tri), pi))
    n_all.append(n)
    lab_all.append(lab)
tri = np.concatenate(tri_all)
part_of = np.concatenate(part_of)
N = np.concatenate(n_all)
LAB = np.concatenate(lab_all)
P = np.stack([(tri[:, :, 0] - xmin) * R, (zmax - tri[:, :, 2]) * R], -1).astype(np.float64)
Z = tri[:, :, 1].astype(np.float64)
zb = np.full((H, W), 1e9)
idb = -np.ones((H, W), np.int64)
raster(P, Z, W, H, zb, idb)
hit = idb >= 0
part = np.where(hit, part_of[np.maximum(idb, 0)], -1)
# Visible faces must point toward the camera (-Y), same as the Anthead.
# Flip a part only when the faces that won the z-buffer point away.
for pi in range(len(meshes)):
    vis = hit & (part == pi)
    if not np.any(vis):
        continue
    if np.median(N[idb[vis], 1]) > 0:
        sel = np.flatnonzero(part_of == pi)
        N[sel] *= -1
        LAB[sel] = np.digitize(N[sel] @ LIGHT, [0.05, 0.25, 0.45, 0.68, 0.86]).astype(np.int64)
        print('flip visible normals', meshes[pi][0], flush=True)
label = np.where(hit, LAB[np.maximum(idb, 0)], -1)
print('hit fraction', round(hit.mean(), 3), flush=True)


def px2gui(c):
    x = xmin + (c[:, 1] + 0.5) / R
    z = zmax - (c[:, 0] + 0.5) / R
    return np.stack([80 + (x - AX) * S, 210 - (z - TIPZ) * S], -1)


def mask_polys(mask, tol=0.28, min_area=0.35):
    mk = np.pad(mask, 1).astype(float)
    rings = []
    for c in measure.find_contours(mk, 0.5):
        if len(c) < 4:
            continue
        p = Polygon(px2gui(c - 1))
        if not p.is_valid:
            p = p.buffer(0)
        if p.is_empty or p.area < min_area:
            continue
        rings.append(p)
    if not rings:
        return None
    rings.sort(key=lambda p: -p.area)
    res = None
    for p in rings:
        res = p if res is None else res.symmetric_difference(p)
    res = res.buffer(0).simplify(tol, preserve_topology=True)
    return res if not res.is_empty else None


def fmt(v):
    s = '%.1f' % v
    return s[:-2] if s.endswith('.0') else s


def d_of(g):
    if g is None:
        return ''

    def ring(cs):
        cs = list(cs)[:-1]
        return 'M' + 'L'.join('%s,%s' % (fmt(x), fmt(y)) for x, y in cs) + 'Z'

    polys = [g] if isinstance(g, Polygon) else list(getattr(g, 'geoms', []))
    out = ''
    for p in polys:
        if not isinstance(p, Polygon) or p.is_empty:
            continue
        out += ring(p.exterior.coords) + ''.join(ring(i.coords) for i in p.interiors)
    return out


layers = {pi: meshes[pi][1] for pi in range(len(meshes))}
names = {pi: meshes[pi][0][:-4] for pi in range(len(meshes))}
fan_pis = [pi for pi in names if layers[pi] == 'fan']
logo_pis = [pi for pi in names if layers[pi] == 'logo']
body_pi = [pi for pi in names if names[pi] == 'Stealthburner_Body'][0]
fan_pi = fan_pis[0]
WX = xmin + (np.arange(W) + 0.5) / R
fan_m = meshes[fan_pi][2]
fan_zc = float(fan_m.bounds.mean(0)[2])
fan_hit = hit & np.isin(part_of[np.maximum(idb, 0)], [fan_pi])
# The cowl's round hotend opening (not the Clockwork window, not the screw holes).
cowl = part == body_pi
holes, nh = ndimage.label(ndimage.binary_fill_holes(cowl) & ~cowl)
win_mask = np.zeros_like(cowl)
best = None
for i in range(1, nh + 1):
    sel = holes == i
    area = int(sel.sum())
    if area < 400:
        continue
    ys, xs = np.nonzero(sel)
    z = float(zmax - (ys.mean() + 0.5) / R)
    score = abs(z - fan_zc)
    if best is None or score < best[0]:
        best = (score, area, sel)
if best is None:
    raise SystemExit('no fan opening in the cowl')
win_mask = best[2]
print('fan opening px', int(win_mask.sum()), 'z', round(fan_zc, 2), flush=True)

# regions. Fan and logo are not shaded faces; the rotor and the LED draw them.
lab_img = label.copy()
key = part.astype(np.int64) * 100000 + np.where(lab_img < 0, 99999, lab_img)
key[~hit] = -1
key[fan_hit] = -1
key[win_mask] = -1
for pi in logo_pis:
    key[part == pi] = -1
regions = []
for k in np.unique(key):
    if k < 0:
        continue
    pi, lb = divmod(int(k), 100000)
    if layers.get(pi) in ('fan', 'logo'):
        continue
    msk = key == k
    cc, n = ndimage.label(msk)
    for ci in range(1, n + 1):
        mm = cc == ci
        if mm.sum() < 40:
            continue
        g = mask_polys(mm)
        if g is None:
            continue
        ids = idb[mm]
        nrm = N[ids].mean(0)
        nrm = nrm / (np.linalg.norm(nrm) + 1e-9)
        b = float(nrm @ LIGHT)
        c = g.centroid
        bb = g.bounds
        regions.append({
            'part': names[pi], 'layer': layers[pi], 'tone': tone_of(b), 'b': round(b, 3),
            'cx': round(c.x, 2), 'cy': round(c.y, 2), 'ymin': round(bb[1], 2), 'ymax': round(bb[3], 2),
            'depth': float(np.median(zb[mm])), 'area': g.area, 'd': d_of(g), 'geom': g,
        })


def sil(sel, holes=False):
    if holes:
        sel = ndimage.binary_fill_holes(sel)
    return mask_polys(sel, 0.22, 2)


head_sel = hit & ~fan_hit
head = sil(head_sel, True)
logo_sel = np.isin(part, logo_pis)
logo = mask_polys(logo_sel, 0.08, 0.4)
win = mask_polys(win_mask, 0.12, 0.2)
# glow: nozzle, heater, heat break
revo_names = ('Revo_Nozzle', 'Revo_Heater', 'Revo_Heatbreak', 'Revo_Nut')
revo_pis = [pi for pi in names if any(names[pi].startswith(p) for p in revo_names)]
glow = sil(np.isin(part, revo_pis), False)

# fan rotor placement: centre and radius of the window
ys, xs = np.nonzero(win_mask)
if len(xs) == 0:
    raise SystemExit('no fan window')
fcx = 80 + (xmin + (xs.mean() + 0.5) / R - AX) * S
fcy = 210 - (zmax - (ys.mean() + 0.5) / R - TIPZ) * S
fr = max((xs.max() - xs.min()) / 2, (ys.max() - ys.min()) / 2) / R * S * 0.92

# duct outlets: lowest cowl pixel on each side, outside the nozzle
cowl = part == body_pi
outlets = []
for sign, sl in ((-1, WX < -8), (1, WX > 8)):
    sel = cowl & sl[None, :]
    if not sel.any():
        continue
    rr, cc = np.nonzero(sel)
    # lowest Z = largest row index (row 0 is zmax)
    i = np.argmax(rr)
    x = 80 + (xmin + (cc[i] + 0.5) / R - AX) * S
    y = 210 - (zmax - (rr[i] + 0.5) / R - TIPZ) * S
    outlets.append([round(x, 2), round(y, 2), round(x - sign * 14, 2), round(y + 8, 2)])

# filament tube ends at the top of the Clockwork main body, on the axis
cw = [m for fn, layer, m in parts if fn.startswith('Main_Body')][0]
topz = float(cw.bounds[1][2])
tube_y = 210 - (topz - TIPZ) * S
tube = [round(80 - 1.6, 2), -100, round(tube_y - (-100), 2)]

regions.sort(key=lambda r: -r['depth'])
body_regs = [r for r in regions if r['part'] == 'Stealthburner_Body']
body_regs.sort(key=lambda r: -r['area'])
if body_regs:
    top = body_regs[0]
    print('largest cowl', top['tone'], 'b', top['b'], 'area', round(top['area'], 1), flush=True)
# Outlines of the visible parts. The fan opening is filled so the cowl stroke
# is the outer shape, the Clockwork seam and the logo pocket; the fan ring is
# the window path, stroked on its own. Revo metal stays out of the Clockwork outline.
cw_skip = ('Revo_',)
cw_pis = [pi for pi in names if layers[pi] == 'k' and not names[pi].startswith(cw_skip)]
mount_pis = [pi for pi in names if layers[pi] == 'a']


def outline_path(sel, tol=0.32, min_hole=0, min_part=0):
    if sel is None or not np.any(sel):
        return ''
    g = mask_polys(sel, tol, 3)
    if g is None:
        return ''
    # Tiny holes are the logo bars and raster crumbs. The logo is stroked from
    # its own path; a heavy cowl stroke around those bars would fill them in.
    def clean(p):
        holes = list(p.interiors)
        if min_hole:
            holes = [i for i in holes if abs(Polygon(i).area) >= min_hole]
        return Polygon(p.exterior, holes)

    polys = [g] if isinstance(g, Polygon) else [p for p in getattr(g, 'geoms', []) if isinstance(p, Polygon)]
    polys = [clean(p) for p in polys]
    # The mount's visible pixels include slivers around the fan opening.
    # Only the two lower corners are the separate accent part the user sees.
    if min_part:
        polys = [p for p in polys if p.area >= min_part]
    if not polys:
        return ''
    g = polys[0] if len(polys) == 1 else MultiPolygon(polys)
    return d_of(g)


edge = {
    'cowl': outline_path((part == body_pi) | win_mask, min_hole=150),
    'cw': outline_path(np.isin(part, cw_pis)),
    'mount': outline_path(np.isin(part, mount_pis), min_part=400),
}
out = {
    'scale_units_per_mm': S, 'axis_x_mm': AX, 'tip_z_mm': round(TIPZ, 3),
    'light': np.round(LIGHT, 3).tolist(),
    'source': 'VoronDesign/Voron-Stealthburner CAD/Stealthburner_CW2_Assembly.step @ 75a2ab079c5aa3a0b6fbb0957de764ce13879da3',
    'fan': [round(fcx, 2), round(fcy, 2), round(fr, 2)],
    'tube': tube,
    'air': outlets,
    'fan_window': d_of(win),
    'logo': d_of(logo),
    'glow': d_of(glow),
    'aura': d_of(head.buffer(2.4, join_style=1).simplify(0.45)) if head is not None else '',
    'silhouette': d_of(head) if head is not None else '',
    'edge': edge,
    'head_bounds_gui': [round(v, 2) for v in head.bounds] if head is not None else [],
    'regions': [],
}
for r in regions:
    r.pop('geom')
    out['regions'].append({k: r[k] for k in ('part', 'layer', 'tone', 'cx', 'cy', 'ymin', 'd')})
json.dump(out, open(os.path.join(OUT, 'sb_outlines.json'), 'w'))
print('regions', len(regions), 'fan', out['fan'], 'tube', tube, 'air', outlets, 'bounds', out['head_bounds_gui'])

pal = {
    'b': {'hi': '#d9dcdf', 'lt': '#bfc3c7', 'md': '#a3a7ab', 'dk': '#878b90', 'dd': '#6c7075', 'xd': '#55585c'},
    'a': {'hi': '#ff6b6e', 'lt': '#ef3a3d', 'md': '#d41216', 'dk': '#a90e12', 'dd': '#860b0e', 'xd': '#66090b'},
    'k': {'hi': '#5d5f63', 'lt': '#47494d', 'md': '#2a2b2e', 'dk': '#1f2022', 'dd': '#161718', 'xd': '#0e0f10'},
    'm': {'hi': '#e8eaed', 'lt': '#c9cdd1', 'md': '#a8adb3', 'dk': '#868b91', 'dd': '#686c71', 'xd': '#4c4f53'},
    'y': {'hi': '#e8c95a', 'lt': '#d4b43a', 'md': '#c9a227', 'dk': '#a07f1c', 'dd': '#7a6114', 'xd': '#54420d'},
}
vb = '0 -60 160 290'
s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" width="640" height="1160"><rect width="160" height="290" x="0" y="-60" fill="#20242a"/>' % vb]
s.append('<path d="%s" fill="#050506"/>' % out['fan_window'])
for r in out['regions']:
    c = pal[r['layer']][r['tone']]
    s.append('<path d="%s" fill="%s" stroke="%s" stroke-width="0.25" stroke-linejoin="round"/>' % (r['d'], c, c))
s.append('<path d="%s" fill="#33ff55" fill-opacity="0.85"/>' % out['logo'])
s.append('</svg>')
open(os.path.join(OUT, 'svg', 'sb_front_regions.svg'), 'w').write('\n'.join(s))
print('wrote', os.path.join(OUT, 'svg', 'sb_front_regions.svg'))
