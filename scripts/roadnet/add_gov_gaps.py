#!/usr/bin/env python
"""把政府缺口（林業及自然保育署林道、自然步道）補入 PBF。
用法：python -I add_gov_gaps.py <in.pbf> <out.pbf> <缺口dir> <林道permit.json> <輸出清單csv> <統計json>
林道：林道_缺口_vehicle.geojson（含 any 的全部；OSM 無任何道路或只有步行類）→ highway=track
步道：步道_缺口_any.geojson（OSM 完全沒有路）→ highway=path
端點吸附 15 m 內 OSM 節點（林道只吸車行道類節點，步道吸全部 highway 節點）；吸不到＝孤立線並記錄。
林道有管制點（shp 管制點欄非空且不是「無…」）者整條加 access=permit。"""
import sys, json, csv, glob, time, os
import numpy as np, osmium
from pyproj import Transformer
from scipy.spatial import cKDTree

SNAP = 30.0  # 缺口端點定義上離 OSM 線 >15 m，故用 15 m 緩衝 + 取樣間距餘裕 = 30 m
NONVEH = {"footway","path","steps","cycleway","pedestrian","proposed","construction","corridor","bridleway","platform","elevator","ladder","bus_stop","raceway","services","rest_area","motorway","motorway_link"}
tf = Transformer.from_crs(4326, 3826, always_xy=True)

def hw_nodes(src):
    veh, allx = {}, {}
    for w in osmium.FileProcessor(src, osmium.osm.NODE | osmium.osm.WAY).with_locations():
        if not w.is_way(): continue
        h = w.tags.get("highway")
        if not h: continue
        isveh = h not in NONVEH
        for n in w.nodes:
            if not n.location.valid(): continue
            t = (n.lon, n.lat)
            allx[n.ref] = t
            if isveh: veh[n.ref] = t
    def tree(d):
        ids = np.fromiter(d.keys(), dtype=np.int64, count=len(d))
        xy = np.array(list(d.values()))
        X, Y = tf.transform(xy[:,0], xy[:,1])
        return ids, xy, cKDTree(np.c_[X, Y])
    return tree(veh), tree(allx)

def length_m(coords):
    X, Y = tf.transform([c[0] for c in coords], [c[1] for c in coords])
    return float(np.hypot(np.diff(X), np.diff(Y)).sum())

def main():
    src, dst, gapdir, shp, outcsv, outjson = sys.argv[1:7]
    t0 = time.time()
    permit = json.load(open(shp, encoding="utf-8"))   # 林道permit.json：名稱→是否有管制點（由林道分布圖 shp 預先轉出，邏輯同原版）
    (vi, vxy, vt), (ai, axy, at) = hw_nodes(src)
    print(f"節點索引 車行 {len(vi)} 全部 {len(ai)}，{time.time()-t0:.0f}s", flush=True)
    feats = []
    for kind, fn in (("林道", "林道_缺口_vehicle.geojson"), ("步道", "步道_缺口_any.geojson")):
        for f in json.load(open(os.path.join(gapdir, fn), encoding="utf-8"))["features"]:
            feats.append((kind, f["properties"], f["geometry"]["coordinates"]))
    new_nodes, new_ways, rows = [], [], []
    nid, wid = 9_000_000_000_000, 9_100_000_000_000
    st = {"林道": {"segs":0,"km":0.0,"both":0,"one":0,"iso":0,"permit_km":0.0}, "步道": {"segs":0,"km":0.0,"both":0,"one":0,"iso":0,"permit_km":0.0}}
    for kind, p, co in feats:
        if len(co) < 2: continue
        ids, xy, tree = (vi, vxy, vt) if kind == "林道" else (ai, axy, at)
        nodes = []
        snapped = []
        for k, c in enumerate(co):
            ref = None
            if k in (0, len(co)-1):
                x, y = tf.transform(c[0], c[1])
                d, j = tree.query([x, y], distance_upper_bound=SNAP)
                if np.isfinite(d):
                    ref = int(ids[j]); snapped.append(True)
                else: snapped.append(False)
            if ref is None:
                nid += 1; new_nodes.append((nid, c[0], c[1])); ref = nid
            nodes.append(ref)
        L = length_m(co)
        wid += 1
        tags = {"highway": "track" if kind == "林道" else "path", "name": p["name"],
                "touror:source": "forest-bureau" if kind == "林道" else "forest-bureau-trail"}
        pm = kind == "林道" and permit.get(p["name"], False)
        if kind == "林道":
            tags["touror:source:ref"] = "林道分布圖1141"
            if pm: tags["access"] = "permit"
        else:
            tags["touror:source:ref"] = "自然步道軌跡圖ds%s" % p.get("dataset_id", "")
        new_ways.append((wid, nodes, tags))
        s = st[kind]; s["segs"] += 1; s["km"] += L/1000
        n = sum(snapped)
        s["both" if n == 2 else "one" if n == 1 else "iso"] += 1
        if pm: s["permit_km"] += L/1000
        rows.append([wid, kind, p["name"], round(L,1), snapped[0], snapped[1], pm, co[0][1], co[0][0]])
    if os.path.exists(dst): os.remove(dst)
    w = osmium.SimpleWriter(dst)
    nflag = wflag = False
    def flush_nodes():
        for i, lo, la in new_nodes: w.add_node(osmium.osm.mutable.Node(id=i, location=(lo, la)))
    def flush_ways():
        for i, nl, tg in new_ways: w.add_way(osmium.osm.mutable.Way(id=i, nodes=nl, tags=tg))
    for o in osmium.FileProcessor(src):
        if o.is_node(): w.add_node(o)
        elif o.is_way():
            if not nflag: flush_nodes(); nflag = True
            w.add_way(o)
        else:
            if not nflag: flush_nodes(); nflag = True
            if not wflag: flush_ways(); wflag = True
            w.add_relation(o)
    if not nflag: flush_nodes()
    if not wflag: flush_ways()
    w.close()
    with open(outcsv, "w", newline="", encoding="utf-8-sig") as f:
        cw = csv.writer(f); cw.writerow(["new_way_id","類型","名稱","長度m","起點吸附","終點吸附","access=permit","起點lat","起點lon"]); cw.writerows(rows)
    json.dump(st, open(outjson, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(st, ensure_ascii=False), f"耗時 {time.time()-t0:.0f}s")

main()
