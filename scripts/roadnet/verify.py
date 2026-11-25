#!/usr/bin/env python
"""路網驗證閘門。用法：python -I verify.py <新tar> <舊tar> <cases.json> <舊bytes> <報告json> [apply_curated.log] [禁行機車走廊.json]
①大小與舊版相差 <= max_size_ratio；②新舊 tar 都能載入並算出每個案例（機車 costing，同 App 的 exclude_highways），
新版總時間 <= 舊版 x max_time_ratio；③curated 覆蓋的 way 在新 OSM 消失 -> 警示（不阻擋）。
④牌照案例（case 帶 plate＋expect）：照 App PlateRouting 的做法，把該牌照 prohibited 的走廊 way 以 exclude_locations 排除後算路，
  檢查路線是否經過（use）或繞開（avoid）指定規則的走廊；走廊檔由 apply_curated.py --moto-bans 每週產生。
⑤分岔案例（case 帶 expect_via）：路線形狀必須經過（want=true）或不經過（want=false）指定點 tol_m 公尺內，
  用來確認並排分岔（例：九如一路右轉後上橋 vs 平面迴轉道，相距約 8 m）算得出正確那一支。leg 可帶 from_opts／to_opts（heading 等）。
結束碼：0 通過、1 不通過（原因寫在報告 failures）。"""
import json, math, os, sys
import valhalla

def decode6(enc):
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(enc):
        for k in (0, 1):
            sh, res = 0, 0
            while True:
                b = ord(enc[i]) - 63; i += 1; res |= (b & 0x1f) << sh; sh += 5
                if b < 0x20: break
            d = ~(res >> 1) if res & 1 else res >> 1
            if k == 0: lat += d
            else: lon += d
        pts.append((lat / 1e6, lon / 1e6))
    return pts

def seg_dist_m(p, a, b):
    k = 111320.0; c = math.cos(math.radians(p[0]))
    ax, ay, bx, by = (a[1] - p[1]) * k * c, (a[0] - p[0]) * k, (b[1] - p[1]) * k * c, (b[0] - p[0]) * k
    dx, dy = bx - ax, by - ay; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
    return math.hypot(ax + t * dx, ay + t * dy)

def corridor_share(shape, rule, tol=10.0):
    """規則走廊各 way 中點有幾成落在路線 tol 公尺內。"""
    mids = []
    for ln in rule["corridor"]["lines"]:
        a, b = ln[len(ln) // 2 - 1] if len(ln) > 1 else ln[0], ln[len(ln) // 2]
        mids.append(((a[1] + b[1]) / 2, (a[0] + b[0]) / 2))
    hit = sum(1 for m in mids if any(seg_dist_m(m, shape[i], shape[i + 1]) <= tol for i in range(len(shape) - 1)))
    return hit / len(mids) if mids else 0.0

def plate_excludes(rules, plate, only=None):
    ex = []
    for r in rules:
        if r.get("plate", {}).get(plate) != "prohibited": continue
        if only is not None and r["id"] not in only: continue
        for ln in r["corridor"]["lines"]:
            a, b = (ln[len(ln) // 2 - 1], ln[len(ln) // 2]) if len(ln) > 1 else (ln[0], ln[0])
            ex.append({"lat": (a[1] + b[1]) / 2, "lon": (a[0] + b[0]) / 2})
    return ex

def pt_seg_m(p, a, b):
    k = math.cos(math.radians(p[0])) * 111320.0
    ax, ay, bx, by = (a[1] - p[1]) * k, (a[0] - p[0]) * 110540.0, (b[1] - p[1]) * k, (b[0] - p[0]) * 110540.0
    dx, dy = bx - ax, by - ay; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
    return math.hypot(ax + t * dx, ay + t * dy)

def shape_dist(sh, p):
    return min((pt_seg_m(p, sh[i], sh[i + 1]) for i in range(len(sh) - 1)), default=1e9)

def route_all(tar, cfgj, tag, rules=None):
    work = os.path.join(os.path.dirname(os.path.abspath(tar)), "_none_" + tag); os.makedirs(work, exist_ok=True)
    actor = valhalla.Actor(valhalla.get_config(tile_extract=tar, tile_dir=work, verbose=False))
    res = {}
    for case in cfgj["cases"]:
        tot, km, err, shapes = 0.0, 0.0, None, []
        for leg in case["legs"]:
            req = {"locations": [{"lat": leg["from"][0], "lon": leg["from"][1], **leg.get("from_opts", {})},
                                 {"lat": leg["to"][0], "lon": leg["to"][1], **leg.get("to_opts", {})}],
                   "costing": cfgj["costing"], "costing_options": {cfgj["costing"]: cfgj["costing_options"]}, "units": "kilometers"}
            plate, exp, share = case.get("plate"), case.get("expect"), None
            if plate and rules is None: err = "缺禁行機車走廊.json，無法驗證牌照案例"; break
            try:
                r = json.loads(actor.route(json.dumps(req)))
                # 同 App PlateRouting：算完檢查碰到哪些該牌照禁行的走廊，只排除那些（exclude 上限 50），最多 3 輪
                hit = set()
                for _ in range(3 if plate else 0):
                    sh = decode6(r["trip"]["legs"][0]["shape"])
                    new = {x["id"] for x in rules if x.get("plate", {}).get(plate) == "prohibited" and corridor_share(sh, x) > 0.2} - hit
                    if not new: break
                    hit |= new; req["exclude_locations"] = plate_excludes(rules, plate, hit)[:50]
                    r = json.loads(actor.route(json.dumps(req)))
                s = r["trip"]["summary"]; tot += s["time"]; km += s["length"]; shapes.append(decode6(r["trip"]["legs"][0]["shape"]))
                if plate and exp:
                    rule = next((x for x in rules if x["id"] == exp["rule"]), None)
                    if rule is None: err = "走廊檔沒有規則 " + exp["rule"]; break
                    share = round(corridor_share(decode6(r["trip"]["legs"][0]["shape"]), rule), 2)
            except Exception as e:
                err = str(e)[:200]; break
        res[case["id"]] = {"name": case["name"], "sec": None if err else round(tot, 1), "km": None if err else round(km, 2), "err": err,
                           "plate": case.get("plate"), "expect": case.get("expect"), "share": None if err else share,
                           "via": None if err else [(v, round(min(shape_dist(sh, v["at"]) for sh in shapes), 1)) for v in case.get("expect_via", [])]}
    return res

def main():
    a = sys.argv[1:]
    new, old, cj, oldb, rep = a[:5]
    log = a[5] if len(a) > 5 else None
    corr = a[6] if len(a) > 6 else None
    cfg = json.load(open(cj, encoding="utf-8"))
    rules = json.load(open(corr, encoding="utf-8")) if corr and os.path.exists(corr) else None
    out = {"failures": [], "warnings": []}
    nb, ob = os.path.getsize(new), int(oldb)
    out["size"] = {"new": nb, "old": ob, "ratio": round(nb / ob - 1, 4) if ob else None}
    if ob and abs(nb / ob - 1) > cfg["max_size_ratio"]:
        out["failures"].append(f"大小與上一版相差 {100*(nb/ob-1):+.1f}%（上限 ±{100*cfg['max_size_ratio']:.0f}%）：{ob} → {nb}")
    try: rn = route_all(new, cfg, "new", rules)
    except Exception as e: rn = {}; out["failures"].append(f"新 tar 無法載入：{str(e)[:200]}")
    try: ro = route_all(old, cfg, "old", rules) if old and os.path.exists(old) else {}
    except Exception as e: ro = {}; out["warnings"].append(f"舊 tar 無法載入，略過新舊比較：{str(e)[:200]}")
    if not ro: out["warnings"].append("沒有舊版可比較，只檢查新版能算路")
    out["cases"] = {}
    for cid, n in rn.items():
        o = ro.get(cid)
        row = {"name": n["name"], "new_sec": n["sec"], "new_km": n["km"], "old_sec": o["sec"] if o else None, "old_km": o["km"] if o else None}
        if n.get("expect") and not n["err"]:
            row["plate"], row["share"] = n["plate"], n["share"]
            want = n["expect"]["use"]
            if want and n["share"] < 0.3:  # 雙向走廊單趟只會覆蓋約一半
                out["failures"].append(f"牌照案例 {n['name']}（{n['plate']}）應經過 {n['expect']['rule']}，實際只覆蓋 {n['share']:.0%}")
            if not want and n["share"] > 0.15:
                out["failures"].append(f"牌照案例 {n['name']}（{n['plate']}）應繞開 {n['expect']['rule']}，實際仍經過 {n['share']:.0%}")
        for v, d in (n.get("via") or []):
            row.setdefault("via_m", []).append(d)
            if v.get("want", True) and d > v.get("tol_m", 3):
                out["failures"].append(f"分岔案例 {n['name']} 應經過 {v.get('label', v['at'])}，路線最近 {d} m")
            if not v.get("want", True) and d <= v.get("tol_m", 3):
                out["failures"].append(f"分岔案例 {n['name']} 不應經過 {v.get('label', v['at'])}，路線卻在 {d} m 內")
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
            if l.startswith("禁行機車："):
                out["moto_bans"] = l.strip()
                if "對不到 0；" not in l: out["warnings"].append("禁行機車有路段對不到 OSM（清單見 禁行機車對應清單.csv）：" + l.strip())
    out["ok"] = not out["failures"]
    json.dump(out, open(rep, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    sys.exit(0 if out["ok"] else 1)

main()
