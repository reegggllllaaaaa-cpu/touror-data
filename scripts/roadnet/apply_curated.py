#!/usr/bin/env python
"""套用 curated/way覆蓋.csv（採用=是）到 OSM PBF，輸出新 PBF；不改原檔。
用法：python -I apply_curated.py <in.pbf> <out.pbf> [way覆蓋.csv] [類型覆蓋.csv]
類型覆蓋.csv（預設取 way覆蓋.csv 同資料夾的 類型覆蓋.csv，沒有就略過）：依條件（ref＋原 highway）把整類 way 改 highway，
保留其他標籤並加 touror:override；例如國3甲（ref=3甲、highway=motorway）改 trunk，使黃紅牌的 exclude_highways 不再排除它。輸出被改 way 清單（id、長度）。
動作：解除private＝移除 access 並標 touror:override=rode；捷西路升級＝highway=tertiary、maxspeed=50、touror:override=jiexi。"""
import csv, sys, time, os, math, osmium

RESTRICT = {"private", "no", "permit", "customers", "destination", "delivery"}

def load(csvp):
    acts = {}
    with open(csvp, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["採用"].strip() == "是":
                acts[int(r["way_id"])] = r["動作"]
    return acts

def load_type_rules(csvp):
    rules = []
    if csvp and os.path.exists(csvp):
        with open(csvp, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["採用"].strip() == "是":
                    rules.append((r["條件_ref"].strip(), r["條件_highway"].strip(), r["改成_highway"].strip(), r["override標記"].strip(), r["id"].strip()))
    return rules

def match_type(rules, tags):
    for ref, hw, new, mark, rid in rules:
        if tags.get("highway") == hw and ref in [x.strip() for x in tags.get("ref", "").split(";")]:
            return new, mark, rid
    return None

def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 12742000*math.asin(math.sqrt(h))

def main():
    src, dst = sys.argv[1], sys.argv[2]
    csvp = sys.argv[3] if len(sys.argv) > 3 else __file__.rsplit("apply_curated.py", 1)[0] + "curated/way覆蓋.csv"
    acts = load(csvp)
    tcsv = sys.argv[4] if len(sys.argv) > 4 else os.path.join(os.path.dirname(os.path.abspath(csvp)), "類型覆蓋.csv")
    trules = load_type_rules(tcsv)
    t0 = time.time(); done = {}
    if os.path.exists(dst): os.remove(dst)
    # 預掃：解除private 的 way 上的節點（閘門/車擋 barrier 的 access=private 會讓 Valhalla 加 private_access_penalty，一併解除）
    pn = set()
    for o in osmium.FileProcessor(src, osmium.osm.WAY):
        if acts.get(o.id) == "解除private":
            pn.update(n.ref for n in o.nodes)
    tnodes = set()
    if trules:
        for o in osmium.FileProcessor(src, osmium.osm.WAY):
            if match_type(trules, o.tags): tnodes.update(n.ref for n in o.nodes)
    coords = {}; tdone = []
    nodefix = []
    w = osmium.SimpleWriter(dst)
    for o in osmium.FileProcessor(src):
        if o.is_node():
            if o.id in tnodes: coords[o.id] = (o.location.lat, o.location.lon)
            if o.id in pn and "barrier" in o.tags and any(o.tags.get(k) in RESTRICT for k in ("access", "motor_vehicle", "motorcycle")):
                t = {k: v for k, v in o.tags if k not in ("access", "motor_vehicle", "motorcycle", "barrier")}
                t["touror:override"] = "rode"; t["touror:barrier_removed"] = o.tags.get("barrier", "")
                nodefix.append((o.id, dict(o.tags)))
                w.add_node(o.replace(tags=t))
            else:
                w.add_node(o)
        elif o.is_way():
            a = acts.get(o.id)
            if a is None:
                m = match_type(trules, o.tags) if trules else None
                if m:
                    t = {k: v for k, v in o.tags}
                    t["touror:orig_highway"] = t["highway"]; t["highway"] = m[0]; t["touror:override"] = m[1]
                    pts = [coords[n.ref] for n in o.nodes if n.ref in coords]
                    tdone.append((o.id, m[2], sum(hav(pts[i], pts[i+1]) for i in range(len(pts)-1))))
                    w.add_way(o.replace(tags=t))
                else:
                    w.add_way(o)
                continue
            t = {k: v for k, v in o.tags}
            before = dict(t)
            if a == "解除private":
                t.pop("access", None); t["touror:override"] = "rode"
            elif a == "捷西路升級":
                t["highway"] = "tertiary"; t["maxspeed"] = "50"; t["touror:override"] = "jiexi"
            done[o.id] = (before, t)
            w.add_way(o.replace(tags=t))
        else:
            w.add_relation(o)
    w.close()
    miss = set(acts) - set(done)
    for i, t in nodefix: print("節點", i, t)
    print(f"套用 {len(done)}/{len(acts)} 條，耗時 {time.time()-t0:.0f}s；缺：{sorted(miss)}")
    for rid in sorted({x[1] for x in tdone}):
        L = [x for x in tdone if x[1] == rid]
        print(f"類型覆蓋 {rid}：改 {len(L)} 條 way，共 {sum(x[2] for x in L)/1000:.2f} km")
        for i, _, l in L: print("類型覆蓋way", rid, i, f"{l:.0f}m")
    if trules and not tdone: print("警示：類型覆蓋規則沒有命中任何 way")
    for i, (b, t) in done.items():
        print(i, {k: b.get(k) for k in ("highway", "access", "maxspeed", "oneway")}, "->", {k: t.get(k) for k in ("highway", "access", "maxspeed", "oneway")})

main()
