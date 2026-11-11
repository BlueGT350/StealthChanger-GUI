
"""Front-view outlines of the reference Anthead + LDO Orbiter 2.5 from the official CAD.

Run with a venv that has trimesh, numpy, shapely, numba, scikit-image:
    python build_outlines.py src .        (then: python3 export_art.py)
SRC_DIR (src/) holds the STLs exported (in assembly coordinates) from
  AntHead_Assembly_v105.step (PrintersForAnts/AntHead, CAD/AntHead_Assembly v105.zip)
  Orbiter v2.5_SmartSensor and O2S Toolboard.step (RobertLorincz/Orbiter-Toolboards)
by step_extract.py. Assembly frame: X right, Y to the back (front face at -Y), Z up.
The Orbiter is placed on the O2 main body by its mounting holes (see ORB_T).

Method: a z-buffer render looking along +Y (orthographic) gives, per pixel, the
nearest triangle. Pixels are grouped into regions by part + planar facet (curved
faces by shading band); each region is traced to a polygon, simplified, and
given a tone (hi/lt/md/dk/dd/xd) from its normal against a light from the top
left front. Output: outlines.json (GUI units) + SVG previews.
"""
import sys, json, os, numpy as np, trimesh
from numba import njit
from skimage import measure
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from scipy import ndimage

SRC, OUT = sys.argv[1], sys.argv[2]
os.makedirs(os.path.join(OUT, 'svg'), exist_ok=True)
AX = 147.72      # filament / nozzle axis X (mm) = GUI x 80
TIPZ = 48.59     # nozzle tip Z (mm)            = GUI y 210
S = 1.95         # GUI units per mm
R = 20           # raster px per mm
ORB_T = np.array([[1,0,0,146.335],[0,1,0,-122.65],[0,0,1,127.96],[0,0,0,1]], float)

# (file, layer, transform?)  layer: b body colour, a accent, k black, m metal,
# y brass, logo (LED diffuser), fan (hidden: replaced by the animated rotor)
PARTS = [
  ('ant/Main_Body_Voron_O2_v12.stl', 'b', None),
  ('ant/Left_Duct_Standard_v13.stl', 'a', None),
  ('ant/Right_Duct_Standard_v11.stl', 'a', None),
  ('ant/Hex_Grill_v11.stl', 'a', None),
  ('ant/Diffuser_voron_v14.stl', 'logo', None),
  ('ant/Light_Shield_voron_v20.stl', 'k', None),
  ('ant/2510_Axial_Fan.stl', 'fan', None),
  ('ant/Revo_v30.stl', 'a', None),
  ('ant/Revo_Voron.stl', 'revo', None),
  ('orbp/Orbiter_2_7_LDO_Molding_CaptiveTensioner_v10_v1.stl', 'k', ORB_T),
  ('orbp/HubHousing_V5_With_Logo.stl', 'k', ORB_T),
  ('orbp/Latch_1_.stl', 'k', ORB_T),
  ('orbp/Gearset.stl', 'k', ORB_T),
  ('orbp/Nuts_Bolts.stl', 'm', ORB_T),
  ('orbp/Orbiter_Spider_Shaft_v4.stl', 'm', ORB_T),
  ('orbp/Bearings.stl', 'm', ORB_T),
  ('orbp/LDO_36STH17_1004AHG_v6.stl', 'k', ORB_T),
  ('orbp/O2_SensorHousing_v4_2_MJF.stl', 'k', ORB_T),
  ('orbp/O2_Sensor_Coupler_v4_2_MJF.stl', 'k', ORB_T),
  ('orbp/TangleRing_v4_2_MJF.stl', 'k', ORB_T),
  ('orbp/Collet_Clip_.stl', 'k', ORB_T),
  ('orbp/LightRing_v4_2_CNC.stl', 'k', ORB_T),
  ('orbp/PCBTighter_v4_2_MJF.stl', 'k', ORB_T),
  ('orbp/MD.stl', 'm', ORB_T),
  ('orbp/Orbiter3_03Sensor_v1.stl', 'k', ORB_T),
]
LIGHT = np.array([-0.4, -0.55, 0.7]); LIGHT /= np.linalg.norm(LIGHT)
TONES = ['hi', 'lt', 'md', 'dk', 'dd', 'xd']
def tone_of(b):
    for t, th in zip(TONES, [0.86, 0.68, 0.45, 0.25, 0.05]):
        if b > th: return t
    return 'xd'

@njit(cache=True)
def raster(P, Z, W, H, zb, idb):
    for t in range(P.shape[0]):
        x0, y0 = P[t, 0, 0], P[t, 0, 1]; x1, y1 = P[t, 1, 0], P[t, 1, 1]; x2, y2 = P[t, 2, 0], P[t, 2, 1]
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12: continue
        mnx = max(int(np.floor(min(x0, x1, x2))), 0); mxx = min(int(np.ceil(max(x0, x1, x2))), W - 1)
        mny = max(int(np.floor(min(y0, y1, y2))), 0); mxy = min(int(np.ceil(max(y0, y1, y2))), H - 1)
        for py in range(mny, mxy + 1):
            for px in range(mnx, mxx + 1):
                cx = px + 0.5; cy = py + 0.5
                a = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / den
                b = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / den
                c = 1 - a - b
                if a < -1e-6 or b < -1e-6 or c < -1e-6: continue
                z = a * Z[t, 0] + b * Z[t, 1] + c * Z[t, 2]
                if z < zb[py, px]:
                    zb[py, px] = z; idb[py, px] = t

meshes = []
for f, layer, T in PARTS:
    m = trimesh.load(os.path.join(SRC, f), force='mesh')
    if T is not None: m.apply_transform(T)
    meshes.append((f, layer, m))
allv = np.concatenate([m.bounds for _, _, m in meshes])
xmin, zmin = allv[:, 0].min() - 2, allv[:, 2].min() - 2
xmax, zmax = allv[:, 0].max() + 2, allv[:, 2].max() + 2
W, H = int((xmax - xmin) * R), int((zmax - zmin) * R)
tri_all, part_of, n_all, lab_all = [], [], [], []
for pi, (f, layer, m) in enumerate(meshes):
    tri = m.triangles; n = m.face_normals
    # region label = shading band of the face (neighbouring faces in the same
    # band merge into one region; their shared edge would not show anyway)
    br = n @ LIGHT
    lab = np.digitize(br, [0.05, 0.25, 0.45, 0.68, 0.86]).astype(np.int64)
    tri_all.append(tri); part_of.append(np.full(len(tri), pi)); n_all.append(n); lab_all.append(lab)
tri = np.concatenate(tri_all); part_of = np.concatenate(part_of); N = np.concatenate(n_all); LAB = np.concatenate(lab_all)
P = np.stack([(tri[:, :, 0] - xmin) * R, (zmax - tri[:, :, 2]) * R], -1).astype(np.float64)
Z = tri[:, :, 1].astype(np.float64)
zb = np.full((H, W), 1e9); idb = -np.ones((H, W), np.int64)
raster(P, Z, W, H, zb, idb)
hit = idb >= 0
part = np.where(hit, part_of[np.maximum(idb, 0)], -1)
label = np.where(hit, LAB[np.maximum(idb, 0)], -1)

# --- reference print tweaks (applied to the raster so a rebuild keeps them) ---
# 1. FILL_HOLES: the two round through-holes either side of the Voron logo are
#    filled with the surrounding panel face (his panels are solid there).
#    (GUI cx, cy, radius); the hole edge is at r 4.0, the flat face from r 4.0 on.
FILL_HOLES = [(55.6, 102.3, 4.3), (104.35, 102.3, 4.3)]
body_i = [i for i, (f, _, _) in enumerate(meshes) if os.path.basename(f).startswith('Main_Body')][0]
_gx = 80 + (xmin + (np.arange(W) + 0.5) / R - AX) * S
_gy = 210 - (zmax - (np.arange(H) + 0.5) / R - TIPZ) * S
GX, GY = np.meshgrid(_gx, _gy)
for hx, hy, hr in FILL_HOLES:
    rr = np.hypot(GX - hx, GY - hy)
    ring = (rr >= hr) & (rr < hr + 1.5) & (part == body_i)
    lb = np.bincount(label[ring]).argmax()                  # the panel face around the hole
    src = ring & (label == lb)
    disk = rr < hr
    part[disk] = body_i; label[disk] = lb; hit[disk] = True
    idb[disk] = np.bincount(idb[src]).argmax(); zb[disk] = np.median(zb[src])
# 2. Two-colour grille: the reference print has painted the cell floors (the closed cells' back
#    plate) gray in the slicer; the hex web, cell chamfers and outer frame stay
#    red. Floor = front-facing grille pixels set back 0.3-1.2 mm from the web's
#    front plane. They become a virtual part with the body (panel) layer.
grill_i = [i for i, (f, _, _) in enumerate(meshes) if os.path.basename(f).startswith('Hex_Grill')][0]
_g = part == grill_i
_ff = _g & (np.abs(N[np.maximum(idb, 0)][..., 1] + 1) < 0.02)   # normal ~ -Y
_rr, _cc = np.nonzero(_ff); _z = zb[_ff]
_top = _z < np.percentile(_z, 5) + 0.15
_coef = np.linalg.lstsq(np.c_[_cc[_top], _rr[_top], np.ones(_top.sum())], _z[_top], rcond=None)[0]
_res = np.full(zb.shape, -9.0)
_res[_ff] = _z - (_coef[0] * _cc + _coef[1] * _rr + _coef[2])
GRILLE_SIL = ndimage.binary_fill_holes(_g)       # taken before the split (fan window)
floor = _ff & (_res > 0.3) & (_res < 1.2)
meshes.append(('ant/Hex_Grill_v11_floor.stl', 'b', meshes[grill_i][2]))
FLOOR_I = len(meshes) - 1
part[floor] = FLOOR_I

def px2gui(c):   # contour (row, col) -> GUI units
    x = xmin + (c[:, 1] + 0.5) / R; z = zmax - (c[:, 0] + 0.5) / R
    return np.stack([80 + (x - AX) * S, 210 - (z - TIPZ) * S], -1)

def mask_polys(mask, tol=0.3, min_area=0.4):
    mk = np.pad(mask, 1).astype(float)
    cs = measure.find_contours(mk, 0.5)
    rings = []
    for c in cs:
        if len(c) < 4: continue
        p = Polygon(px2gui(c - 1))
        if not p.is_valid: p = p.buffer(0)
        if p.area < min_area: continue
        rings.append(p)
    if not rings: return None
    # even-odd assemble: sort by area, a ring inside an odd count of rings is a hole
    rings.sort(key=lambda p: -p.area)
    res = None
    for p in rings:
        res = p if res is None else res.symmetric_difference(p)
    res = res.buffer(0).simplify(tol, preserve_topology=True)
    return res if not res.is_empty else None

def d_of(g):
    def ring(cs):
        cs = list(cs)[:-1]
        return 'M' + 'L'.join('%s,%s' % (fmt(x), fmt(y)) for x, y in cs) + 'Z'
    polys = [g] if isinstance(g, Polygon) else list(getattr(g, 'geoms', []))
    out = ''
    for p in polys:
        if not isinstance(p, Polygon) or p.is_empty: continue
        out += ring(p.exterior.coords) + ''.join(ring(i.coords) for i in p.interiors)
    return out
def fmt(v):
    s = '%.1f' % v
    return s[:-2] if s.endswith('.0') else s

layers = {pi: meshes[pi][1] for pi in range(len(meshes))}
names = {pi: os.path.basename(meshes[pi][0])[:-4] for pi in range(len(meshes))}
grille_pi = [pi for pi in names if names[pi].startswith('Hex_Grill')][0]
body_pi = [pi for pi in names if names[pi].startswith('Main_Body')][0]
fan_pi = [pi for pi in names if names[pi].startswith('2510')][0]
# the fan window: whatever shows through the grille openings (fan, hotend mount, revo)
gsil = GRILLE_SIL
behind = gsil & ~np.isin(part, [grille_pi, FLOOR_I, body_pi])
regions = []
win = mask_polys(behind | (part == fan_pi) & gsil, 0.15, 0.3)
# per-region polygons
lab_img = np.where(behind, -2, label)
key = part.astype(np.int64) * 100000 + np.where(lab_img < 0, 99999, lab_img)
key[~hit] = -1; key[behind] = -1
for k in np.unique(key):
    if k < 0: continue
    pi, lb = divmod(int(k), 100000)
    m = key == k
    cc, n = ndimage.label(m)
    for ci in range(1, n + 1):
        mm = cc == ci
        if mm.sum() < 60: continue
        g = mask_polys(mm)
        if g is None: continue
        ids = idb[mm]
        nrm = N[ids].mean(0); nrm /= np.linalg.norm(nrm) + 1e-9
        b = float(nrm @ LIGHT)
        c = g.centroid; bb = g.bounds
        regions.append({'part': names[pi], 'layer': layers[pi], 'tone': tone_of(b), 'b': round(b, 3),
                        'cx': round(c.x, 2), 'cy': round(c.y, 2), 'ymin': round(bb[1], 2), 'ymax': round(bb[3], 2),
                        'depth': float(np.median(zb[mm])), 'area': g.area, 'd': d_of(g), 'geom': g})
# silhouettes per layer group, and the whole head
def sil(sel):
    return mask_polys(sel, 0.2, 2)
out = {'scale_units_per_mm': S, 'axis_x_mm': AX, 'tip_z_mm': TIPZ, 'light': LIGHT.round(3).tolist(),
       'fan_window': d_of(win) if win is not None else '',
       'silhouette': {}, 'regions': []}
groups = {'head': hit, 'anthead': np.isin(part, [pi for pi in names if names[pi].startswith(('Main_Body', 'Left_Duct', 'Right_Duct', 'Hex_Grill'))]),
          'body': part == body_pi, 'grille': np.isin(part, [grille_pi, FLOOR_I]),
          'ducts': np.isin(part, [pi for pi in names if 'Duct' in names[pi]]),
          'orbiter': np.isin(part, [pi for pi in names if meshes[pi][2] is not None and meshes[pi][0].startswith('orbp/')]),
          'logo': part == [pi for pi in names if names[pi].startswith('Diffuser')][0],
          'revo': part == [pi for pi in names if names[pi] == 'Revo_Voron'][0]}
for gname, sel in groups.items():
    g = sil(ndimage.binary_fill_holes(sel) if gname in ('head', 'anthead', 'body', 'orbiter') else sel)
    out['silhouette'][gname] = d_of(g) if g is not None else ''
    if gname == 'head':
        out['aura'] = d_of(g.buffer(2.6, join_style=1).simplify(0.5))
        out['head_bounds_gui'] = [round(v, 2) for v in g.bounds]
    if gname == 'logo':
        out['logo'] = d_of(mask_polys(sel, 0.12, 1))
# full outline of every part (projected triangles, not just the visible bit)
from shapely.geometry import Polygon as SP
def full_outline(m):
    t = m.triangles
    pts = np.stack([80 + (t[:, :, 0] - AX) * S, 210 - (t[:, :, 2] - TIPZ) * S], -1)
    e1 = pts[:, 1] - pts[:, 0]; e2 = pts[:, 2] - pts[:, 0]
    ar = np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]) > 1e-6
    u = unary_union([SP(p) for p in pts[ar]])
    return u.buffer(0).simplify(0.15)
out['part_outline'] = {}
for pi, (f, layer, m) in enumerate(meshes):
    if pi != FLOOR_I and names[pi].startswith(('Main_Body', 'Left_Duct', 'Right_Duct', 'Hex_Grill', 'Diffuser', 'Orbiter_2_7', 'O2_SensorHousing', 'HubHousing')):
        out['part_outline'][names[pi]] = d_of(full_outline(m))
regions.sort(key=lambda r: -r['depth'])
for r in regions:
    r.pop('geom')
    out['regions'].append(r)
json.dump(out, open(os.path.join(OUT, 'outlines.json'), 'w'), indent=0)
print('regions', len(regions), 'bytes', sum(len(r['d']) for r in regions), 'raster', W, H)
# SVG previews
pal = {'b': {'hi': '#d9dcdf', 'lt': '#bfc3c7', 'md': '#a3a7ab', 'dk': '#878b90', 'dd': '#6c7075', 'xd': '#55585c'},
       'a': {'hi': '#ff6b6e', 'lt': '#ef3a3d', 'md': '#d41216', 'dk': '#a90e12', 'dd': '#860b0e', 'xd': '#66090b'},
       'k': {'hi': '#5d5f63', 'lt': '#47494d', 'md': '#2a2b2e', 'dk': '#1f2022', 'dd': '#161718', 'xd': '#0e0f10'},
       'm': {'hi': '#e8eaed', 'lt': '#c9cdd1', 'md': '#a8adb3', 'dk': '#868b91', 'dd': '#686c71', 'xd': '#4c4f53'}}
pal['hub'] = pal['a']; pal['revo'] = pal['m']; pal['logo'] = {t: '#33ff55' for t in TONES}; pal['fan'] = pal['k']
vb = '0 -60 160 290'
s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" width="640" height="1160"><rect x="0" y="-60" width="160" height="290" fill="#20242a"/>' % vb]
s.append('<path d="%s" fill="#050506"/>' % out['fan_window'])
for r in out['regions']:
    c = pal[r['layer']][r['tone']]
    s.append('<path d="%s" fill="%s" stroke="%s" stroke-width="0.3" stroke-linejoin="round"/>' % (r['d'], c, c))
s.append('</svg>')
open(os.path.join(OUT, 'svg', 'front_regions.svg'), 'w').write('\n'.join(s))
s = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" width="640" height="1160"><rect x="0" y="-60" width="160" height="290" fill="#fff"/>' % vb]
cols = {'head': '#000', 'body': '#777', 'ducts': '#d41216', 'grille': '#f60', 'orbiter': '#36c', 'logo': '#0a0', 'revo': '#a60'}
for g, d in out['silhouette'].items():
    if g in cols: s.append('<path d="%s" fill="none" stroke="%s" stroke-width="0.5" fill-rule="evenodd"/>' % (d, cols[g]))
s.append('</svg>')
open(os.path.join(OUT, 'svg', 'front_silhouettes.svg'), 'w').write('\n'.join(s))
for nm, d in out['part_outline'].items():
    open(os.path.join(OUT, 'svg', 'outline_%s.svg' % nm), 'w').write(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="%s" width="640" height="1160"><path d="%s" fill="#ccc" stroke="#000" stroke-width="0.4" fill-rule="evenodd"/></svg>' % (vb, d))
