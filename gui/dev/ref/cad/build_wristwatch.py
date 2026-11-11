"""Front view of the WW-BMG Wristwatch, seated on the Anthead mount plane.

    python build_wristwatch.py "Wristwatch BMG v3.step" .

Source: bythorsthunder/Voron_Mods Wristwatch_Extruder_BMG/CAD/Wristwatch BMG v3.step
commit 359b95fb7263 (2024-05-23), GPL-3.0. The STEP stacks body and motor-plate
variants side by side. This uses the assembled set: the main body that the
tension arm and the in-place motor plate overlap, plus the BMG gears.

The two M3 holes on the bottom are 37 mm apart (Orbiter 2 spacing). The
filament bore between them is placed on the Anthead filament axis (X 147.72)
and the bottom face is placed so it lands on the same GUI contact as the
Orbiter art (y 74.2). Scale is 1.95 units/mm, the same as the other drawings.
"""
import sys, os, json
import numpy as np
import trimesh
from numba import njit
from skimage import measure
from shapely.geometry import Polygon

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

STEP, OUT = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(OUT, 'svg'), exist_ok=True)

# Anthead frame. The Orbiter trace bottoms at GUI y 74.2; the WW bottom face
# is put on that same line so one mount offset works for both extruders.
AX, TIPZ, S = 147.72, 48.59, 1.95
CONTACT_Y = 74.2
FILAMENT_X = -21.98   # bore between the two M3 holes, measured on the bottom face
BOTTOM_Z = 158.6


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


def solids(shp):
    ex = TopExp_Explorer(shp, TopAbs_SOLID)
    out = []
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
    if nm in ('Main_Bodies', 'Tension_Arm (1)', 'SOLID', 'Drive_Gear', '50_Tooth_Gear', 'Idler_Gear:1') and nm not in found:
        found[nm] = st.GetShape_s(label).Moved(loc)
    if st.IsAssembly_s(label):
        ch = TDF_LabelSequence()
        st.GetComponents_s(label, ch)
        for i in range(1, ch.Length() + 1):
            walk(ch.Value(i), loc)


roots = TDF_LabelSequence()
st.GetFreeShapes(roots)
for i in range(1, roots.Length() + 1):
    walk(roots.Value(i), TopLoc_Location())

# The assembled body is the solid the tension arm overlaps (the left one).
bodies = []
for sol in solids(found['Main_Bodies']):
    m = mesh_shape(sol, 0.12)
    if m is not None and m.centroid[0] < 0:
        bodies.append(m)
body = bodies[0]
print('body', np.round(body.bounds[0], 2), np.round(body.bounds[1] - body.bounds[0], 2), flush=True)

parts = [('Main_Body', 'k', body)]
arm = mesh_shape(found['Tension_Arm (1)'], 0.12)
parts.append(('Tension_Arm', 'k', arm))
plate = mesh_shape(found['SOLID'], 0.15)
# The in-place plate overlaps the body. Drop triangles far outside the body
# so a stacked variant cannot widen the drawing.
bx0, bx1 = body.bounds[0][0] - 2, body.bounds[1][0] + 2
keep = (plate.triangles_center[:, 0] > bx0) & (plate.triangles_center[:, 0] < bx1)
plate = plate.submesh([np.nonzero(keep)[0]], append=True)
parts.append(('Motor_Plate', 'k', plate))
for nm, layer in (('Drive_Gear', 'm'), ('50_Tooth_Gear', 'm'), ('Idler_Gear:1', 'm')):
    parts.append((nm.replace(':', '_'), layer, mesh_shape(found[nm], 0.08)))

# Seat: filament bore on the Anthead axis, bottom face on the Orbiter contact.
delta_x = AX - FILAMENT_X
delta_z = (TIPZ + (210 - CONTACT_Y) / S) - BOTTOM_Z
print('seat delta mm', round(delta_x, 3), round(delta_z, 3), flush=True)
for _, _, m in parts:
    m.apply_translation([delta_x, 0, delta_z])

R = 16
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
tri_all, part_of, n_all = [], [], []
for pi, (fn, layer, m) in enumerate(meshes):
    tri_all.append(m.triangles)
    part_of.append(np.full(len(m.triangles), pi))
    n_all.append(m.face_normals.copy())
tri = np.concatenate(tri_all)
part_of = np.concatenate(part_of)
N = np.concatenate(n_all)
P = np.stack([(tri[:, :, 0] - xmin) * R, (zmax - tri[:, :, 2]) * R], -1).astype(np.float64)
Z = tri[:, :, 1].astype(np.float64)
zb = np.full((H, W), 1e9)
idb = -np.ones((H, W), np.int64)
raster(P, Z, W, H, zb, idb)
hit = idb >= 0
part = np.where(hit, part_of[np.maximum(idb, 0)], -1)
for pi in range(len(meshes)):
    vis = hit & (part == pi)
    if vis.any() and np.median(N[idb[vis], 1]) > 0:
        N[part_of == pi] *= -1
        print('flip', meshes[pi][0], flush=True)
LAB = np.digitize(N @ LIGHT, [0.05, 0.25, 0.45, 0.68, 0.86]).astype(np.int64)
label = np.where(hit, LAB[np.maximum(idb, 0)], -1)


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


from scipy import ndimage
layers = {pi: meshes[pi][1] for pi in range(len(meshes))}
names = {pi: meshes[pi][0] for pi in range(len(meshes))}
key = part.astype(np.int64) * 100000 + np.where(label < 0, 99999, label)
key[~hit] = -1
regions = []
for k in np.unique(key):
    if k < 0:
        continue
    pi, lb = divmod(int(k), 100000)
    msk = key == k
    cc, ncc = ndimage.label(msk)
    for ci in range(1, ncc + 1):
        mm = cc == ci
        if mm.sum() < 14:
            continue
        g = mask_polys(mm)
        if g is None:
            continue
        nrm = N[idb[mm]].mean(0)
        nrm = nrm / (np.linalg.norm(nrm) + 1e-9)
        regions.append({
            'part': names[pi], 'layer': layers[pi], 'tone': tone_of(float(nrm @ LIGHT)),
            'ymin': round(float(g.bounds[1]), 2), 'ymax': round(float(g.bounds[3]), 2),
            'depth': float(np.median(zb[mm])), 'area': float(g.area), 'd': d_of(g),
        })
regions.sort(key=lambda r: -r['depth'])
aura = mask_polys(hit, 0.22, 2)
ys, xs = np.nonzero(hit)
top_y = float(px2gui(np.array([[ys.min(), xs[np.argmin(ys)]]]))[0, 1])
# The tallest pixel is the tension arm, off to one side. The bowden enters on
# the filament axis, so the tube ends at the top of the art in that column
# and tucks a little under the first faces.
gui_x = 80 + (xmin + (np.arange(W) + 0.5) / R - AX) * S
col = (gui_x >= 77.5) & (gui_x <= 82.5)
rows = np.nonzero((hit & col).any(1))[0]
inlet_top = float(px2gui(np.array([[rows.min(), int(np.argmax(col))]]))[0, 1])
inlet = inlet_top + 8.0
print('regions', len(regions), 'top', round(top_y, 2), 'inlet', round(inlet, 2), flush=True)
# contact check: lowest pixel of the drawing
bot = float(px2gui(np.array([[ys.max(), xs[np.argmax(ys)]]]))[0, 1])
print('bottom gui', round(bot, 2), flush=True)

out = {
    'scale_units_per_mm': S,
    'contact_y': CONTACT_Y,
    'tube': [78.4, -100, round(inlet - (-100), 2)],
    'aura': d_of(aura),
    'regions': [{k: r[k] for k in ('part', 'layer', 'tone', 'ymin', 'd')} for r in regions],
}
json.dump(out, open(os.path.join(OUT, 'ww_outlines.json'), 'w'))
# small preview
cols = {'k': '#3a3d42', 'm': '#a8adb3'}
svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -48 160 140">']
for r in regions:
    svg.append('<path d="%s" fill="%s"/>' % (r['d'], cols[r['layer']]))
svg.append('<path d="%s" fill="none" stroke="#22d3d3" stroke-width="0.6"/>' % d_of(aura))
svg.append('</svg>')
open(os.path.join(OUT, 'svg', 'ww_front.svg'), 'w').write(''.join(svg))
print('wrote ww_outlines.json', flush=True)
