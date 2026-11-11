"""Front view of the Dragon Burner v8 from the official CAD plus the Revo.

    python build_dragonburner.py DRAGON.step STEALTHBURNER.step src/db .

Dragon source: chirpy2605/voron V0/Dragon_Burner/CAD/Dragon_Burner_v0.2_v8.step
commit a02fcea (GPL-3.0). It places MainBody, the Standard ducts, the Voron
diffuser and RevoVoron_Mount. It has no hotend metal and no fan solids.

The Revo metal is the same Revo Voron from
VoronDesign/Voron-Stealthburner CAD/Stealthburner_CW2_Assembly.step
(commit 75a2ab07). It is seated by putting the heatsink's top face on the
underside of RevoVoron_Mount and the heatsink axis on that mount's bore.
The nozzle tip, not the duct exit, is y 210.

The two part-cooling fans are 4010 blowers (40 mm frame, 10 mm thick), the
size named in the Dragon Burner manual. They slide into the cowl's side
slots from the rear: inner face on the measured side wall, front face just
behind the cowl skin, top at the underside of the ears. Only the front
edge shows. They are drawn black.

Same GUI frame as the other heads: 1.95 units/mm, x 80 on the nozzle axis.
"""
import sys, os, json
import numpy as np
import trimesh
from numba import njit
from skimage import measure
from shapely.geometry import Polygon
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
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
from OCP.BRep import BRep_Tool
from OCP.TopoDS import TopoDS

STEP, REVO_STEP, SRC, OUT = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
os.makedirs(SRC, exist_ok=True)
os.makedirs(os.path.join(OUT, 'svg'), exist_ok=True)

# exact STEP name -> (layer, mesh deflection mm)
WANT = {
    'MainBody': ('b', 0.10),
    'Standard (1)': ('a', 0.10),   # left duct, already placed
    'Standard': ('a', 0.10),       # right duct
    '[c]_Voron_Diffuser': ('logo', 0.04),
    '[o]_Carrier_Neopixel': ('k', 0.08),
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
        nodes = [(tri.Node(i).Transformed(tr).X(), tri.Node(i).Transformed(tr).Y(), tri.Node(i).Transformed(tr).Z())
                 for i in range(1, tri.NbNodes() + 1)]
        arr = np.asarray(nodes, float)
        if (arr.max(0) - arr.min(0)).max() > 200:
            continue
        rev = face.Orientation() == TopAbs_REVERSED
        tris = []
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

parts = []
for nm, (layer, lin) in WANT.items():
    m = mesh_shape(found[nm], lin)
    fn = nm.replace(' ', '_').replace('(', '').replace(')', '').replace('[', '').replace(']', '') + '.stl'
    m.export(os.path.join(SRC, fn))
    parts.append((fn, layer, m))
    print('%-28s %-4s tris %5d span %s' % (fn, layer, len(m.faces), np.round(m.bounds[1] - m.bounds[0], 1)), flush=True)

def load_named(path, wanted):
    doc = TDocStd_Document(TCollection_ExtendedString('doc'))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    assert reader.ReadFile(path) == IFSelect_RetDone
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    found_l = {}

    def walk(label, loc):
        nm = name_of(label)
        ref = TDF_Label()
        if tool.IsReference_s(label):
            tool.GetReferredShape_s(label, ref)
            walk(ref, loc.Multiplied(tool.GetLocation_s(label)))
            return
        if nm in wanted and nm not in found_l:
            found_l[nm] = tool.GetShape_s(label).Moved(loc)
            return
        if tool.IsAssembly_s(label):
            ch = TDF_LabelSequence()
            tool.GetComponents_s(label, ch)
            for i in range(1, ch.Length() + 1):
                walk(ch.Value(i), loc)

    roots_l = TDF_LabelSequence()
    tool.GetFreeShapes(roots_l)
    for i in range(1, roots_l.Length() + 1):
        walk(roots_l.Value(i), TopLoc_Location())
    return found_l


def solids_of(shp):
    out = []
    ex = TopExp_Explorer(shp, TopAbs_SOLID)
    while ex.More():
        out.append(TopoDS.Solid(ex.Current()))
        ex.Next()
    return out


def mount_axis(mount):
    """Bore centre (x, y) and the underside Z the heatsink sits on."""
    z0 = float(mount.bounds[0][2])
    sec = mount.section(plane_origin=[0, 0, z0 + 4.0], plane_normal=[0, 0, 1])
    best = None
    for path in sec.discrete:
        if len(path) < 20:
            continue
        xy = path[:, :2]
        c = xy.mean(0)
        r = np.hypot(xy[:, 0] - c[0], xy[:, 1] - c[1])
        if r.std() < 0.08 and 1.8 < r.mean() < 2.4:
            if best is None or r.mean() < best[0]:
                best = (r.mean(), c)
    if best is None:
        raise SystemExit('RevoVoron_Mount bore not found')
    return np.array([best[1][0], best[1][1], z0])


# Revo Voron metal, from the Stealthburner assembly, seated on this mount.
print('reading', REVO_STEP, flush=True)
revo_src = load_named(REVO_STEP, {'Heatsink', 'V7_wo_fanV7_Fitment_CAD_wo_fan_1.3'})
hs = mesh_shape(revo_src['Heatsink'], 0.12)
# The nozzle axis is the heatsink centre; its top face is the mating face.
axis_sb = np.array([float(hs.centroid[0]), float(hs.vertices[:, 1].mean()), float(hs.bounds[1][2])])
# Heatsink centroid x is 0 and the screw pattern is centred on y=0.2. Prefer the
# nozzle-axis y measured from the top flange screws if the centroid drifts.
top = hs.vertices[hs.vertices[:, 2] > hs.bounds[1][2] - 1.2]
axis_sb[0] = float(np.median(top[:, 0]))
axis_sb[1] = float(np.median(top[:, 1]))
mount = mesh_shape(load_named(STEP, {'RevoVoron_Mount'})['RevoVoron_Mount'], 0.15)
axis_db = mount_axis(mount)
delta = axis_db - axis_sb
print('revo seat delta mm', np.round(delta, 3), flush=True)

revo_parts = [('Heatsink.stl', 'm', hs)]
for i, sol in enumerate(solids_of(revo_src['V7_wo_fanV7_Fitment_CAD_wo_fan_1.3'])):
    sm = mesh_shape(sol, 0.08)
    if sm is None:
        continue
    span = sm.bounds[1] - sm.bounds[0]
    z0, z1 = float(sm.bounds[0][2]), float(sm.bounds[1][2])
    if span.max() > 40:
        continue
    if z0 < 1 and span[2] > 12:
        c = sm.triangles_center[:, 2]
        tip = sm.submesh([np.nonzero(c < 5.0)[0]], append=True)
        rest = sm.submesh([np.nonzero(c >= 5.0)[0]], append=True)
        revo_parts.append(('Revo_Nozzle.stl', 'y', tip))
        revo_parts.append(('Revo_Heatbreak.stl', 'm', rest))
    elif z1 < 8 and span[0] < 16:
        revo_parts.append(('Revo_Nut.stl', 'm', sm))
    elif span[0] > 14 and 8 < span[2] < 20 and abs(sm.centroid[0]) < 2:
        revo_parts.append(('Revo_Heater.stl', 'k', sm))
    elif abs(sm.centroid[0]) < 3:
        revo_parts.append(('Revo_Part_%d.stl' % i, 'm', sm))
for fn, layer, mesh in revo_parts:
    mesh.apply_translation(delta)
    parts.append((fn, layer, mesh))
    print('%-28s %-4s z %6.2f..%6.2f' % (fn, layer, mesh.bounds[0][2], mesh.bounds[1][2]), flush=True)
nozzle = [m for fn, layer, m in parts if fn == 'Revo_Nozzle.stl'][0]
AX = float(np.median(nozzle.vertices[nozzle.vertices[:, 2] < nozzle.bounds[0][2] + 0.4][:, 0]))
TIPZ = float(nozzle.bounds[0][2])
duct_z = min(float(m.bounds[0][2]) for fn, layer, m in parts if fn.startswith('Standard'))
print('AX', round(AX, 3), 'TIPZ', round(TIPZ, 3), 'ducts above tip mm', round(duct_z - TIPZ, 2), flush=True)

# 4010 blowers (40 mm frame, 10 mm thick) in the cowl's side slots. The inner
# face sits on the side wall; the front face is just behind the cowl skin so
# only the edge shows; the top meets the underside of the ears.
body = [m for fn, layer, m in parts if fn.startswith('MainBody')][0]
front_y = float(body.bounds[0][1])
# side walls: outward faces of the narrow waist
n = body.face_normals
c = body.triangles_center
left_wall = float(np.median(c[(n[:, 0] < -0.9) & (c[:, 2] < 100) & (c[:, 2] > 70) & (c[:, 0] < AX)][:, 0]))
right_wall = float(np.median(c[(n[:, 0] > 0.9) & (c[:, 2] < 100) & (c[:, 2] > 70) & (c[:, 0] > AX)][:, 0]))
# ears widen above this Z
ear_z = 101.0
FAN_H, FAN_T, FAN_D = 40.0, 10.0, 40.0
fan_cz = ear_z - FAN_H / 2
fan_cy = (front_y + 0.45) + FAN_D / 2
for name, wall, outward in (('Blower4010_L.stl', left_wall, -1), ('Blower4010_R.stl', right_wall, 1)):
    box = trimesh.creation.box(extents=[FAN_T, FAN_D, FAN_H])
    box.apply_translation([wall + outward * FAN_T / 2, fan_cy, fan_cz])
    parts.append((name, 'k', box))
    print('%-28s wall x %.2f z %.1f..%.1f' % (name, wall, fan_cz - FAN_H / 2, fan_cz + FAN_H / 2), flush=True)
S = 1.95
R = 18

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


meshes = list(parts)
allb = np.concatenate([m.bounds for _, _, m in meshes])
xmin, zmin = allb[:, 0].min() - 1.5, allb[:, 2].min() - 1.5
xmax, zmax = allb[:, 0].max() + 1.5, allb[:, 2].max() + 1.5
W, H = int((xmax - xmin) * R), int((zmax - zmin) * R)
print('raster', W, H, flush=True)
tri_all, part_of, n_all, lab_all = [], [], [], []
for pi, (fn, layer, m) in enumerate(meshes):
    tri = m.triangles
    n = m.face_normals.copy()
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
# Outward normals on the front face must point at the camera (-Y). A part whose
# visible faces point the other way is wound backwards; flip it so the light
# (from the front) lights the cowl instead of painting it all in shadow.
for pi in range(len(meshes)):
    vis = hit & (part_of[np.maximum(idb, 0)] == pi)
    if not vis.any():
        continue
    if np.median(N[idb[vis], 1]) > 0:
        N[part_of == pi] *= -1
        print('flip visible normals', meshes[pi][0], flush=True)
LAB = np.digitize(N @ LIGHT, [0.05, 0.25, 0.45, 0.68, 0.86]).astype(np.int64)
label = np.where(hit, LAB[np.maximum(idb, 0)], -1)


def px2gui(c):
    x = xmin + (c[:, 1] + 0.5) / R
    z = zmax - (c[:, 0] + 0.5) / R
    return np.stack([80 + (x - AX) * S, 210 - (z - TIPZ) * S], -1)


def mask_polys(mask, tol=0.25, min_area=0.3):
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
body_pi = [pi for pi in names if names[pi] == 'MainBody'][0]
logo_pis = [pi for pi in names if layers[pi] == 'logo']
cowl = part == body_pi
holes, nh = ndimage.label(ndimage.binary_fill_holes(cowl) & ~cowl)
win_mask = None
best_area = 0
for i in range(1, nh + 1):
    sel = holes == i
    area = int(sel.sum())
    if area > best_area:
        best_area = area
        win_mask = sel
if win_mask is None or best_area < 200:
    raise SystemExit('no fan opening in MainBody')
print('fan opening px', int(win_mask.sum()), flush=True)

key = part.astype(np.int64) * 100000 + np.where(label < 0, 99999, label)
key[~hit] = -1
key[win_mask] = -1
for pi in logo_pis:
    key[part == pi] = -1
regions = []
for k in np.unique(key):
    if k < 0:
        continue
    pi, lb = divmod(int(k), 100000)
    if layers.get(pi) in ('logo',):
        continue
    msk = key == k
    cc, ncc = ndimage.label(msk)
    for ci in range(1, ncc + 1):
        mm = cc == ci
        if mm.sum() < 18:
            continue
        g = mask_polys(mm)
        if g is None:
            continue
        nrm = N[idb[mm]].mean(0)
        nrm = nrm / (np.linalg.norm(nrm) + 1e-9)
        b = float(nrm @ LIGHT)
        c = g.centroid
        regions.append({
            'part': names[pi], 'layer': layers[pi], 'tone': tone_of(b),
            'cx': round(float(c.x), 2), 'cy': round(float(c.y), 2),
            'ymin': round(float(g.bounds[1]), 2),
            'depth': float(np.median(zb[mm])), 'area': float(g.area), 'd': d_of(g), 'geom': g,
        })

head = mask_polys(hit & ~win_mask, 0.22, 2)
# fill the outer silhouette so the aura is one outline, fan hole excluded from the stroke
head_filled = mask_polys(ndimage.binary_fill_holes(hit & ~win_mask) | (hit & ~win_mask), 0.22, 2)
logo = mask_polys(np.isin(part, logo_pis), 0.08, 0.3)
win = mask_polys(win_mask, 0.12, 0.2)
ys, xs = np.nonzero(win_mask)
fcx = 80 + (xmin + (xs.mean() + 0.5) / R - AX) * S
fcy = 210 - (zmax - (ys.mean() + 0.5) / R - TIPZ) * S
# inscribed radius of the opening, in GUI units
fr = np.sqrt(win_mask.sum() / np.pi) / R * S * 0.86

WX = xmin + (np.arange(W) + 0.5) / R
duct_pis = [pi for pi in names if layers[pi] == 'a']
outlets = []
for sign, sl in ((-1, WX < AX), (1, WX > AX)):
    sel = np.isin(part, duct_pis) & sl[None, :]
    if not sel.any():
        continue
    rr, cc = np.nonzero(sel)
    i = int(np.argmax(rr))  # lowest Z
    x = 80 + (xmin + (cc[i] + 0.5) / R - AX) * S
    y = 210 - (zmax - (rr[i] + 0.5) / R - TIPZ) * S
    outlets.append([round(x, 2), round(y, 2), round(x - sign * 10, 2), round(y + 7, 2)])

topz = float(body.bounds[1][2])
tube_y = 210 - (topz - TIPZ) * S
# The tube runs off the top of the view to the spool, and stops on the cowl's top face.
tube = [round(80 - 1.6, 2), -100, round(tube_y - (-100), 2)]

def outline_path(sel, tol=0.32, min_hole=0, min_part=0):
    if sel is None or not np.any(sel):
        return ''
    g = mask_polys(sel, tol, 2)
    if g is None:
        return ''

    def clean(p):
        holes = list(p.interiors)
        if min_hole:
            holes = [i for i in holes if abs(Polygon(i).area) >= min_hole]
        return Polygon(p.exterior, holes)

    polys = [g] if isinstance(g, Polygon) else [p for p in getattr(g, 'geoms', []) if isinstance(p, Polygon)]
    polys = [clean(p) for p in polys]
    if min_part:
        polys = [p for p in polys if p.area >= min_part]
    if not polys:
        return ''
    from shapely.geometry import MultiPolygon
    g = polys[0] if len(polys) == 1 else MultiPolygon(polys)
    return d_of(g)

# Cowl stroke is the outer shell. Logo slits stay out of it (the diffuser is
# stroked on its own). Screw holes are large enough to keep.
fan_pis = [pi for pi in names if names[pi].startswith('Blower4010')]
revo_glow = [pi for pi in names if names[pi].startswith(('Revo_Nozzle', 'Revo_Heater', 'Revo_Nut'))]
glow = mask_polys(np.isin(part, revo_glow), 0.2, 1)
edge = {
    'cowl': outline_path((part == body_pi) | win_mask, min_hole=40),
    'ducts': outline_path(np.isin(part, duct_pis), min_part=20),
    'fans': outline_path(np.isin(part, fan_pis), min_part=8),
}
body_regs = [r for r in regions if r['part'] == 'MainBody']
body_regs.sort(key=lambda r: -r['area'])
if body_regs:
    print('largest cowl', body_regs[0]['tone'], 'area', round(body_regs[0]['area'], 1), flush=True)

regions.sort(key=lambda r: -r['depth'])
out = {
    'scale_units_per_mm': S, 'axis_x_mm': round(AX, 3), 'tip_z_mm': round(TIPZ, 3),
    'light': np.round(LIGHT, 3).tolist(),
    'source': 'chirpy2605/voron V0/Dragon_Burner/CAD/Dragon_Burner_v0.2_v8.step @ a02fceadea974f728f80dacc2349508c0521a337',
    'fan': [round(float(fcx), 2), round(float(fcy), 2), round(float(fr), 2)],
    'tube': tube,
    'air': outlets,
    'glow': d_of(glow),
    'fan_window': d_of(win),
    'logo': d_of(logo),
    'edge': edge,
    'aura': d_of(head.buffer(2.2, join_style=1).simplify(0.4)) if head is not None else '',
    'silhouette': d_of(head) if head is not None else '',
    'head_bounds_gui': [round(float(v), 2) for v in head.bounds] if head is not None else [],
    'regions': [],
}
for r in regions:
    r.pop('geom')
    out['regions'].append({k: r[k] for k in ('part', 'layer', 'tone', 'cx', 'cy', 'ymin', 'd')})
json.dump(out, open(os.path.join(OUT, 'db_outlines.json'), 'w'))
print('regions', len(regions), 'fan', out['fan'], 'tube', tube, 'air', outlets, 'bounds', out['head_bounds_gui'])
print('logo Ms', out['logo'].count('M'))

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
    s.append('<path d="%s" fill="%s" stroke="%s" stroke-width="0.3" stroke-linejoin="round"/>' % (r['d'], c, c))
s.append('<path d="%s" fill="#33ff55"/>' % out['logo'])
s.append('</svg>')
open(os.path.join(OUT, 'svg', 'db_front_regions.svg'), 'w').write('\n'.join(s))
print('wrote', os.path.join(OUT, 'svg', 'db_front_regions.svg'))
