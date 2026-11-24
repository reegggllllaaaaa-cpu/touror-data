#!/usr/bin/env python
"""路網驗證閘門。用法：python -I verify.py <新tar> <舊tar> <cases.json> <舊bytes> <報告json> [apply_curated.log]
①大小與舊版相差 <= max_size_ratio；②新舊 tar 都能載入並算出每個案例（機車 costing，同 App 的 exclude_highways），
新版總時間 <= 舊版 x max_time_ratio；③curated 覆蓋的 way 在新 OSM 消失 -> 警示（不阻擋）。
結束碼：0 通過、1 不通過（原因寫在報告 failures）。"""
import json, os, sys
import valhalla

def route_all(tar, cfgj, tag):
    work = os.path.join(os.path.dirname(os.path.abspath(tar)), "_none_" + tag); os.makedirs(work, exist_ok=True)
    actor = valhalla.Actor(valhalla.get_config(tile_extract=tar, tile_dir=work, verbose=False))
    res = {}
    for case in cfgj["cases"]:
        tot, km, err = 0.0, 0.0, None
        for leg in case["legs"]:
            req = {"locations": [{"lat": leg["from"][0], "lon": leg["from"][1]}, {"lat": leg["to"][0], "lon": leg["to"][1]}],
                   "costing": cfgj["costing"], "costing_options": {cfgj["costing"]: cfgj["costing_options"]}, "units": "kilometers"}
            try:
                r = json.loads(actor.route(json.dumps(req)))
                s = r["trip"]["summary"]; tot += s["time"]; km += s["length"]
            except Exception as e:
                err = str(e)[:200]; break
        res[case["id"]] = {"name": case["name"], "sec": None if err else round(tot, 1), "km": None if err else round(km, 2), "err": err}
    return res

def main():
    a = sys.argv[1:]
    new, old, cj, oldb, rep = a[:5]
    log = a[5] if len(a) > 5 else None
    cfg = json.load(open(cj, encoding="utf-8"))
    out = {"failures": [], "warnings": []}
    nb, ob = os.path.getsize(new), int(oldb)
    out["size"] = {"new": nb, "old": ob, "ratio": round(nb / ob - 1, 4) if ob else None}
    if ob and abs(nb / ob - 1) > cfg["max_size_ratio"]:
        out["failures"].append(f"大小與上一版相差 {100*(nb/ob-1):+.1f}%（上限 ±{100*cfg['max_size_ratio']:.0f}%）：{ob} → {nb}")
    try: rn = route_all(new, cfg, "new")
    except Exception as e: rn = {}; out["failures"].append(f"新 tar 無法載入：{str(e)[:200]}")
    try: ro = route_all(old, cfg, "old") if old and os.path.exists(old) else {}
    except Exception as e: ro = {}; out["warnings"].append(f"舊 tar 無法載入，略過新舊比較：{str(e)[:200]}")
    if not ro: out["warnings"].append("沒有舊版可比較，只檢查新版能算路")
    out["cases"] = {}
    for cid, n in rn.items():
        o = ro.get(cid)
        row = {"name": n["name"], "new_sec": n["sec"], "new_km": n["km"], "old_sec": o["sec"] if o else None, "old_km": o["km"] if o else None}
        if n["err"]: out["failures"].append(f"案例 {n['name']} 新版算不出路線：{n['err']}")
        elif o and o["sec"]:
            row["ratio"] = round(n["sec"] / o["sec"] - 1, 4)
            if n["sec"] > o["sec"] * cfg["max_time_ratio"]:
                out["failures"].append(f"案例 {n['name']} 總時間變差 {100*row['ratio']:+.1f}%（{o['sec']:.0f}s → {n['sec']:.0f}s，上限 +{100*(cfg['max_time_ratio']-1):.0f}%）")
        elif o and o["err"]: out["warnings"].append(f"案例 {n['name']} 舊版算不出路線，無法比較")
        out["cases"][cid] = row
    if log and os.path.exists(log):
        for l in open(log, encoding="utf-8", errors="replace"):
            if "缺：" in l:
                miss = l.split("缺：", 1)[1].strip()
                if miss not in ("[]", ""): out["warnings"].append(f"curated 覆蓋的 way 在新 OSM 已不存在：{miss}")
            if l.startswith("套用 "): out["apply"] = l.strip()
    out["ok"] = not out["failures"]
    json.dump(out, open(rep, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    sys.exit(0 if out["ok"] else 1)

main()
