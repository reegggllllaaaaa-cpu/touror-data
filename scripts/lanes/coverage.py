# 全台主要道路（trunk/primary/secondary/tertiary）車道數來源覆蓋率，依縣市（admin_level=4）。
# 用法：python -I coverage.py <final.pbf> <gov_lane_attrs.csv> <mapillary_ai_lanes.csv> <out.json>；報表：python coverage_report.py <out.json>
# 峰值記憶體約 2–3 GB（節點位置快取），使用者在用電腦時不要跑。
import sys, csv, json, math, osmium
from matplotlib.path import Path
pbf, govp, mlyp, out = sys.argv[1:5]
CLS = {"trunk","primary","secondary","tertiary"}
gov = set()
for r in csv.DictReader(open(govp, encoding="utf-8-sig")):
    if r.get("confidence") != "low" and (r.get("lanes") or r.get("lanes_forward")): gov.add(int(r["osm_way_id"]))
mly = set(int(r["osm_way_id"]) for r in csv.DictReader(open(mlyp, encoding="utf-8-sig")) if r.get("lanes"))
cty = []
for o in osmium.FileProcessor(pbf).with_areas(osmium.filter.TagFilter(("admin_level","4"))).with_filter(osmium.filter.EntityFilter(osmium.osm.AREA)):
    if o.tags.get("boundary") != "administrative" or o.from_way(): continue
    for ring in o.outer_rings():
        pts = [(n.lon, n.lat) for n in ring]
        xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
        cty.append((o.tags.get("name","?"), Path(pts), (min(xs),min(ys),max(xs),max(ys))))
print("counties", sorted(set(c[0] for c in cty)), file=sys.stderr)
def county(lon, lat):
    for n,p,b in cty:
        if b[0]<=lon<=b[2] and b[1]<=lat<=b[3] and p.contains_point((lon,lat)): return n
    return "（界外）"
def hav(a,b):
    la1,lo1,la2,lo2=map(math.radians,(a[1],a[0],b[1],b[0]))
    h=math.sin((la2-la1)/2)**2+math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 12742000*math.asin(math.sqrt(h))
st = {}; dump = []
C=(120.402,22.636)
for w in osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY).with_locations().with_filter(osmium.filter.KeyFilter("highway")):
    hw = w.tags.get("highway")
    try: pts=[(n.lon,n.lat) for n in w.nodes]
    except Exception: continue
    if not pts: continue
    near = abs(pts[0][0]-C[0])<0.012 and abs(pts[0][1]-C[1])<0.014
    if near and (hw in CLS or hw.endswith("_link")) and ("光明" in w.tags.get("name","") or w.tags.get("bridge")):
        dump.append({"id":w.id,"tags":dict(w.tags),"a":pts[0],"b":pts[-1],"n":len(pts)})
    if hw not in CLS: continue
    L=sum(hav(pts[i],pts[i+1]) for i in range(len(pts)-1))
    m=pts[len(pts)//2]; c=county(*m)
    src = "osm" if (w.tags.get("lanes") or w.tags.get("lanes:forward")) else "gov" if w.id in gov else "mly" if w.id in mly else "inferred"
    d=st.setdefault(c,{}).setdefault(hw,{}); x=d.setdefault(src,[0,0.0]); x[0]+=1; x[1]+=L
json.dump({"stats":st,"dump":dump}, open(out,"w",encoding="utf-8"), ensure_ascii=False)
print("ok", len(dump), file=sys.stderr)
