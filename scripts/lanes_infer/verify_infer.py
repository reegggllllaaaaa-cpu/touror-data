#!/usr/bin/env python3
"""車道推論驗證閘門：verify_infer.py <infer_report.json> <gate.json>
結束碼 0＝通過（推論值可進底圖）；1＝未通過（底圖不帶推論值，維持 OSM／政府／Mapillary＋舊預設）。
檢查：①非空輸入 ②每個實際採用的「方法 × 分層」樣本數 ≥ min_samples、信心不是 off、Wilson 下限達該信心門檻
（random 一定要達；group 樣本足夠時也要達）③中信心以上推論列數 ④焦點道路中信心以上長度比。prior／low 不計入列數與品質。"""
import csv, json, os, sys

rep = json.load(open(sys.argv[1], encoding="utf-8"))
gate = json.load(open(sys.argv[2], encoding="utf-8"))
req = gate["require"]
MIN = gate["min_samples"]
lv = {x["level"]: x for x in gate["levels"]}
fails = []
if not rep.get("ways"):
    fails.append("空輸入：沒有車用道路")
csv_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[1])), "inferred_lanes.csv")
rows = {}
if os.path.exists(csv_path):
    for r in csv.DictReader(open(csv_path, encoding="utf-8")):
        rows[r["confidence"]] = rows.get(r["confidence"], 0) + 1
else:
    fails.append("找不到 inferred_lanes.csv")
for k, u in rep.get("used", {}).items():
    c = u["conf"]
    if c not in ("high", "medium", "low"):
        fails.append(f"{k}：信心 {c} 卻被採用")
        continue
    for sch in ("random", "group"):
        s = u[sch]
        if sch == "group" and s["n"] < MIN:
            continue
        if s["n"] < MIN or s["wlo_exact"] < lv[c]["exact"] or s["wlo_pm1"] < lv[c]["pm1"]:
            fails.append(f"{k}（{c}，{u['rows']} 列）{sch} n={s['n']} 下限 exact={s['wlo_exact']} pm1={s['wlo_pm1']} 未達")
med = rows.get("high", 0) + rows.get("medium", 0)
if med < req["min_medium_rows"]:
    fails.append(f"中信心以上推論列數 {med} < {req['min_medium_rows']}")
f = rep.get("focus", [])
tot = sum(x["m"] for x in f)
good = sum(x["m"] for x in f if x["confidence"] in ("high", "medium"))
share = good / tot if tot else 0
print(f"{gate.get('focus_name')}：{len(f)} 段 {tot} m，中信心以上 {share:.1%}（其餘 {[(x['way_id'], x['source'], x['confidence']) for x in f if x['confidence'] not in ('high', 'medium')]}）")
if share < req["focus_min_medium_share"]:
    fails.append(f"{gate.get('focus_name')} 中信心以上長度比 {share:.1%} < {req['focus_min_medium_share']:.0%}" if f else f"找不到 {gate.get('focus_name')}")
for sch, cv in rep.get("cv", {}).items():
    for m, v in cv.items():
        print(f"{sch:6s} {m:16s} n={v['n']:7d} exact={v['exact']:.3f} pm1={v['pm1']:.3f}")
print(f"推論列數 {sum(rows.values())}，依信心 {rows}；採用分層 {len(rep.get('used', {}))} 個")
if fails:
    print("未通過：\n" + "\n".join("- " + x for x in fails))
    sys.exit(1)
print("通過")
