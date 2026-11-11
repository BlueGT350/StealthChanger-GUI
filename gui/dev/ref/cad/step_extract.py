"""Export named parts of a STEP assembly as STL in assembly coordinates.

    python step_extract.py ASSEMBLY.step OUT_DIR '<regex on part names>' [list]

Needs cadquery-ocp (OCP 7.8+). Meshes at 0.05 mm linear / 0.3 rad angular deflection.
"""
import sys, re, time
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDF import TDF_Label
from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDataStd import TDataStd_Name
from OCP.IFSelect import IFSelect_RetDone
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer
from OCP.TopLoc import TopLoc_Location
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib

path, outdir, pattern = sys.argv[1], sys.argv[2], sys.argv[3]
listonly = len(sys.argv) > 4 and sys.argv[4] == 'list'
t=time.time()
doc = TDocStd_Document(TCollection_ExtendedString("doc"))
r = STEPCAFControl_Reader(); r.SetNameMode(True)
assert r.ReadFile(path) == IFSelect_RetDone
r.Transfer(doc)
print('read', time.time()-t, flush=True)
st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
def name(l):
    a = TDataStd_Name()
    if l.FindAttribute(TDataStd_Name.GetID_s(), a): return a.Get().ToExtString()
    return '?'
rx = re.compile(pattern)
seen = {}
def walk(label, loc, depth, pathn):
    nm = name(label)
    ref = TDF_Label()
    if st.IsReference_s(label):
        st.GetReferredShape_s(label, ref)
        loc = loc.Multiplied(st.GetLocation_s(label))
        nm2 = name(ref)
        walk(ref, loc, depth, pathn)
        return
    p = pathn + '/' + nm
    if listonly and depth < 5: print('  '*depth + nm)
    if rx.search(nm) and not listonly:
        shp = st.GetShape_s(label).Moved(loc)
        BRepMesh_IncrementalMesh(shp, 0.05, False, 0.3, True)
        k = seen.get(nm, 0); seen[nm] = k+1
        fn = f"{outdir}/{re.sub(r'[^A-Za-z0-9_]+','_',nm)}{'_'+str(k) if k else ''}.stl"
        w = StlAPI_Writer(); w.ASCIIMode = False; w.Write(shp, fn)
        print('wrote', fn, p, flush=True)
        return
    if st.IsAssembly_s(label):
        ch = TDF_LabelSequence(); st.GetComponents_s(label, ch)
        for i in range(1, ch.Length()+1): walk(ch.Value(i), loc, depth+1, p)
roots = TDF_LabelSequence(); st.GetFreeShapes(roots)
for i in range(1, roots.Length()+1): walk(roots.Value(i), TopLoc_Location(), 0, '')
print('done', time.time()-t)
