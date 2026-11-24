#!/usr/bin/env python
"""套用 curated/way覆蓋.csv（採用=是）到 OSM PBF，輸出新 PBF；不改原檔。
用法：python -I apply_curated.py <in.pbf> <out.pbf> [way覆蓋.csv]
動作：解除private＝移除 access 並標 touror:override=rode；捷西路升級＝highway=tertiary、maxspeed=50、touror:override=jiexi。"""
import csv, sys, time, os, osmium

RESTRICT = {"private", "no", "permit", "customers", "destination", "delivery"}

def load(csvp):
    acts = {}
    with open(csvp, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["採用"].strip() == "是":
                acts[int(r["way_id"])] = r["動作"]
    return acts

def main():
    src, dst = sys.argv[1], sys.argv[2]
    csvp = sys.argv[3] if len(sys.argv) > 3 else __file__.rsplit("apply_curated.py", 1)[0] + "curated/way覆蓋.csv"
    acts = load(csvp)
    t0 = time.time(); done = {}
    if os.path.exists(dst): os.remove(dst)
    # 預掃：解除private 的 way 上的節點（閘門/車擋 barrier 的 access=private 會讓 Valhalla 加 private_access_penalty，一併解除）
    pn = set()
    for o in osmium.FileProcessor(src, osmium.osm.WAY):
        if acts.get(o.id) == "解除private":
            pn.update(n.ref for n in o.nodes)
    nodefix = []
    w = osmium.SimpleWriter(dst)
    for o in osmium.FileProcessor(src):
        if o.is_node():
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
                w.add_way(o); continue
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
    for i, (b, t) in done.items():
        print(i, {k: b.get(k) for k in ("highway", "access", "maxspeed", "oneway")}, "->", {k: t.get(k) for k in ("highway", "access", "maxspeed", "oneway")})

main()
