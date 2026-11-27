#!/usr/bin/env python
"""用 Valhalla 實算一條路線，列出經過 trap_forks.json 的哪些分岔點、走了哪一支（C1 §5.0 驗證用）。

  python trap_route_check.py --tiles /root/rga/valhalla_tiles.tar --traps trap_forks.json --plate white \
      --from 22.68333,120.31537 --to 22.61,120.41
白牌：同 App PlateRouting（exclude_highways＋use_highways 0，起訖不吸附到國道）。
"""
import argparse, json, math, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fork_sides import seg_dist  # noqa: E402  （verify.py 匯入時就會執行 main，不能 import）


def decode6(enc):
    pts, i, lat, lon = [], 0, 0, 0
    while i < len(enc):
        for k in (0, 1):
            sh, res = 0, 0
            while True:
                b = ord(enc[i]) - 63; i += 1; res |= (b & 0x1f) << sh; sh += 5
                if b < 0x20:
                    break
            d = ~(res >> 1) if res & 1 else res >> 1
            if k == 0:
                lat += d
            else:
                lon += d
        pts.append((lat / 1e6, lon / 1e6))
    return pts


def shape_dist(sh, p):
    return min((seg_dist(tuple(p), sh[i], sh[i + 1]) for i in range(len(sh) - 1)), default=1e9)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiles", required=True); ap.add_argument("--traps", required=True)
    ap.add_argument("--from", dest="frm", required=True); ap.add_argument("--to", required=True)
    ap.add_argument("--plate", default="white"); ap.add_argument("--tol", type=float, default=8.0)
    a = ap.parse_args()
    import valhalla
    work = os.path.join(os.path.dirname(os.path.abspath(a.tiles)), "_none_trap"); os.makedirs(work, exist_ok=True)
    actor = valhalla.Actor(valhalla.get_config(tile_extract=a.tiles, tile_dir=work, verbose=False))
    f = list(map(float, a.frm.split(","))); t = list(map(float, a.to.split(",")))
    co = {"exclude_highways": True, "use_tracks": 0, "use_trails": 0}
    if a.plate == "white":
        co["use_highways"] = 0
    filt = {"search_filter": {"max_road_class": "trunk"}}
    req = {"locations": [{"lat": f[0], "lon": f[1], **filt}, {"lat": t[0], "lon": t[1], **filt}],
           "costing": "motorcycle", "costing_options": {"motorcycle": co}, "units": "kilometers", "language": "zh-TW"}
    r = json.loads(actor.route(json.dumps(req)))
    leg = r["trip"]["legs"][0]; sh = decode6(leg["shape"])
    tr = json.loads(actor.trace_attributes(json.dumps({"encoded_polyline": leg["shape"], "shape_match": "edge_walk", "costing": "motorcycle",
                                                     "filters": {"attributes": ["edge.way_id", "edge.names", "edge.road_class"], "action": "include"}})))
    road = []
    for e in tr.get("edges", []):
        nm = "/".join(e.get("names", [])) or "(無名)"
        if not road or road[-1][0] != nm:
            road.append([nm, e.get("road_class")])
    doc = json.load(open(a.traps, encoding="utf-8"))
    passed = []
    for fk in doc["forks"]:
        if shape_dist(sh, [fk["lat"], fk["lon"]]) > a.tol:
            continue
        took = None
        for b in fk["branches"]:
            dd = shape_dist(sh, b["p40"])
            if dd <= a.tol and (took is None or dd < took[1]):
                took = (b, dd)
        # 進入方向也要對：進入 way 前 40 m 的點不好取，改用「路線在分岔前有沒有經過進入 way」近似（trace edges）
        ways = {e.get("way_id") for e in tr.get("edges", [])}
        if fk["entry_way"] not in ways or took is None:
            continue
        passed.append({"node": fk["node"], "lat": fk["lat"], "lon": fk["lon"], "entry": fk["entry_name"], "entry_way": fk["entry_way"],
                       "context": fk["context"], "kind": fk["kind"], "misentry": fk["misentry"], "zone": fk["zone"],
                       "took_way": took[0]["way"], "took_side": took[0]["side"], "took_leads": took[0]["leads_to"],
                       "branches": [(b["side"], b["way"], b["leads_to"], b["prohibited"]) for b in fk["branches"]]})
    near = []   # 路線沒經過、但 300 m 內的誤上點（走錯一個路口就會碰到）
    for fk in doc["forks"]:
        if not fk["misentry"] or any(p["node"] == fk["node"] for p in passed):
            continue
        if abs(fk["lat"] - sh[0][0]) > 0.5:
            continue
        dd = shape_dist(sh, [fk["lat"], fk["lon"]])
        if dd <= 300:
            near.append({"node": fk["node"], "entry": fk["entry_name"], "entry_way": fk["entry_way"], "dist_m": round(dd, 1),
                         "lat": fk["lat"], "lon": fk["lon"], "branches": [(b["side"], b["way"], b["leads_to"]) for b in fk["branches"]]})
    s = r["trip"]["summary"]
    out = {"km": round(s["length"], 2), "min": round(s["time"] / 60, 1), "plate": a.plate,
           "roads": road, "maneuvers": [m["instruction"] for m in leg["maneuvers"]][:40], "forks_passed": passed, "misentry_within_300m": near}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
