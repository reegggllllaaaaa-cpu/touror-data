#!/usr/bin/env python3
"""車道推論驗證閘門：verify_infer.py <infer_report.json> <gate.json>
結束碼 0＝通過（推論值可進底圖）；1＝未通過（底圖不帶推論值，維持 OSM／政府／Mapillary＋舊預設）。"""
import json, sys

rep = json.load(open(sys.argv[1], encoding="utf-8"))
gate = json.load(open(sys.argv[2], encoding="utf-8"))
req = gate["require"]
fails = []
jb = rep["cv"].get("junction_both", {})
if jb.get("exact", 0) < req["junction_both_min_exact"]:
    fails.append(f"路口反推（兩端相同）交叉驗證完全相同率 {jb.get('exact')} < {req['junction_both_min_exact']}")
pr = rep["cv"].get("prior", {})
if pr.get("pm1", 0) < req["prior_min_pm1"]:
    fails.append(f"先驗 ±1 準確率 {pr.get('pm1')} < {req['prior_min_pm1']}")
rows = sum(1 for _ in open(sys.argv[1].replace("infer_report.json", "inferred_lanes.csv"), encoding="utf-8")) - 1
if rows < req["min_rows"]:
    fails.append(f"推論列數 {rows} < {req['min_rows']}")
if "focus_min_non_prior_share" in req:
    f = rep.get("focus", [])
    tot = sum(x["m"] for x in f)
    pri = sum(x["m"] for x in f if x["source"] == "prior")
    share = 1 - pri / tot if tot else 0
    print(f"{gate.get('focus_name')}：{len(f)} 段 {tot} m，非先驗 {share:.1%}（先驗段 {[x['way_id'] for x in f if x['source'] == 'prior']}）")
    if share < req["focus_min_non_prior_share"]:
        fails.append(f"{gate.get('focus_name')} 非先驗長度比 {share:.1%} < {req['focus_min_non_prior_share']:.0%}" if f else f"找不到 {gate.get('focus_name')}")
for m, v in rep["cv"].items():
    print(f"{m:16s} n={v['n']:7d} exact={v['exact']:.3f} pm1={v['pm1']:.3f} -> {v['confidence']}")
print(f"推論列數 {rows}")
if fails:
    print("未通過：\n" + "\n".join("- " + x for x in fails))
    sys.exit(1)
print("通過")
