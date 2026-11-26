"""底圖 roads 立體分層與顯示平滑的局部檢查（WSL：需 osmium、pmtiles、mapbox_vector_tile）。
用法：python -I check_road_layers.py <原 pbf> <平滑後 pbf> <pmtiles> --bbox lon1,lat1,lon2,lat2 [--json out.json]
1. 欄位：bbox 內 z14 roads 圖層出現的欄位（應含 layer、lanes、is_bridge、is_link、oneway）。
2. 不跨層合併：bbox 內每條 bridge way 的中點，在 z14 roads 圖層 3.5 m 內要找得到 layer 相同的線
   （容差＝平滑 1.5 m＋z14 合併後 0.1px 簡化約 1 m＋圖磚量化；area=yes 面狀橋不是線，略過）。
3. 接點：原檔被 ≥2 條道路 way 共用的節點，平滑後每條原本經過它的 way 都還經過它，且座標差 < 0.1 m。
"""
import argparse, json, math, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osmium
from verify_basemap import Tiles, lonlat_to_tile, seg_dist  # noqa: E402
from smooth_display import eligible, NON_VEHICLE  # noqa: E402


def load(pbf, bbox):
    x1, y1, x2, y2 = bbox

    class H(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways = {}

        def way(self, w):
            if "highway" not in w.tags:
                return
            try:
                pts = [(n.ref, n.location.lon, n.location.lat) for n in w.nodes]
            except osmium.InvalidLocationError:
                return
            if any(x1 <= p[1] <= x2 and y1 <= p[2] <= y2 for p in pts):
                self.ways[w.id] = (pts, {t.k: t.v for t in w.tags})

    h = H()
    h.apply_file(pbf, locations=True, idx="flex_mem")
    return h.ways


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("orig"), ap.add_argument("smoothed"), ap.add_argument("tiles")
    ap.add_argument("--bbox", required=True)
    ap.add_argument("--json")
    a = ap.parse_args()
    bbox = tuple(float(v) for v in a.bbox.split(","))
    tiles = Tiles(a.tiles)
    rep = {}

    # 1. 欄位
    tx1, ty1 = (int(v) for v in lonlat_to_tile(bbox[3], bbox[0], 14))
    tx2, ty2 = (int(v) for v in lonlat_to_tile(bbox[1], bbox[2], 14))
    fields, nfeat, nlayer = set(), 0, 0
    for x in range(tx1, tx2 + 1):
        for y in range(ty1, ty2 + 1):
            for _, pr in tiles.roads(14, x, y):
                fields |= set(pr)
                nfeat += 1
                nlayer += "layer" in pr
    rep["fields"] = sorted(fields)
    rep["features_z14"], rep["features_with_layer"] = nfeat, nlayer

    orig, smo = load(a.orig, bbox), load(a.smoothed, bbox)

    # 2. 橋中點的 layer
    bad, n = [], 0
    for wid, (pts, tags) in orig.items():
        if tags.get("bridge") in (None, "no") or tags.get("area") == "yes"                 or tags.get("highway") in NON_VEHICLE:  # 短人行橋 Protomaps 在 z14 會略去，只驗車行道路
            continue
        try:
            want = int(tags.get("layer", "1").split(";")[0])
        except ValueError:
            want = 1
        mid = pts[len(pts) // 2] if len(pts) > 2 else (0, (pts[0][1] + pts[1][1]) / 2, (pts[0][2] + pts[1][2]) / 2)
        lon, lat = mid[1], mid[2]
        if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
            continue
        n += 1
        fx, fy = lonlat_to_tile(lat, lon, 14)
        x, y = int(fx), int(fy)
        px, py = (fx - x) * 4096, (1 - (fy - y)) * 4096
        mpu = 40075016.686 * math.cos(math.radians(lat)) / (2 ** 14) / 4096
        best = min((seg_dist(px, py, p[i], p[i + 1]) for p, pr in tiles.roads(14, x, y)
                    if pr.get("layer", 0) == want for i in range(len(p) - 1)), default=1e18) * mpu
        if best > 3.5:
            bad.append({"way": wid, "layer": want, "nearest_same_layer_m": round(best, 2)})
    rep["bridges_checked"], rep["bridges_bad"] = n, bad[:20]
    rep["bridges_bad_count"] = len(bad)

    # 3. 接點
    uses = {}
    for pts, _ in orig.values():
        for ref, _, _ in pts:
            uses[ref] = uses.get(ref, 0) + 1
    smoothed_ways = [w for w, (pts, tags) in orig.items()
                     if eligible(tags) and w in smo and [p[0] for p in smo[w][0]] != [p[0] for p in pts]]
    worst, missing, checked = 0.0, [], 0
    for wid, (pts, _) in orig.items():
        if wid not in smo:
            continue
        new = {p[0]: p for p in smo[wid][0]}
        for ref, lon, lat in pts:
            if uses.get(ref, 0) < 2:
                continue
            checked += 1
            q = new.get(ref)
            if q is None:
                missing.append({"way": wid, "node": ref})
                continue
            d = math.hypot((q[1] - lon) * 111320 * math.cos(math.radians(lat)), (q[2] - lat) * 110540)
            worst = max(worst, d)
    rep["smoothed_ways_in_bbox"] = len(smoothed_ways)
    rep["shared_nodes_checked"], rep["shared_nodes_missing"] = checked, missing[:20]
    rep["shared_nodes_max_shift_m"] = round(worst, 4)
    ok = not missing and worst < 0.1 and not bad and {"layer", "is_bridge", "oneway"} <= fields
    rep["ok"] = ok
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
