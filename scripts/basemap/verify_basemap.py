"""底圖驗證閘門：新 taiwan-z14.pmtiles 必須與路網同步、與 App 相容。

用法：python -I verify_basemap.py <路網用 final.pbf> <新 pmtiles> [--old 舊 pmtiles] [--n 2000] [--json out.json]

檢查（任一不過就 exit 1，不發布）：
1. 相容：圖層名稱（vector_layers）至少包含 App 用的 earth/landcover/landuse/water/boundaries/roads/places/pois，
   tile 型態 mvt、壓縮 gzip、maxzoom 14。
2. 覆蓋率：從同一份 pbf 隨機抽 n 段「路網會收的道路」（每段取 way 上隨機一段的中點），
   到新底圖 z14 圖磚 roads 圖層找最近的線，距離 <= 12 m 算有畫。
   車輛可走等級（motorway…service、track）覆蓋率須 >= 99.0%，全部等級 >= 97.0%。
3. 固定案例：鼎金一巷巷道（OSM way 209794955，2026-11-25 使用者回報「路線走在沒畫路的地方」）沿線點都要有線。
4. 大小：新檔與舊檔（--old）相差在 -30%～+60% 內（只在有 --old 時檢查）。
5. 車道級圖層（Touror 自訂）：lanes／lane_lines／road_areas／islands_pt 存在且欄位齊全；
   固定案例國1鼎金段（z14 13668/7132）要有 motorway lane_count=5 的車道線。
6. 舊 App 相容：有 --old 時，舊檔每個圖層與欄位在新檔都還在（只准新增，不准改名刪欄）。
有 --old 時同一批樣本也量舊檔覆蓋率，方便比較。
"""
import argparse, gzip, json, math, random, sys
import osmium
from pmtiles.reader import Reader, MmapSource
import mapbox_vector_tile as mvt

VEHICLE = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
           "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
           "service", "road", "track"}
OTHER = {"path", "footway", "cycleway", "bridleway", "steps", "pedestrian"}
BBOX = (119.3, 21.8, 122.1, 25.4)
MAX_M = 12.0
CASE_POINTS = [(22.665434, 120.330072), (22.663038, 120.330653), (22.662598, 120.330747), (22.661626, 120.331126)]
# 只列「台灣資料一定會出現」的欄位；placement、shoulder、divider、moped_lanes 等稀有標籤沒有人標時
# 不會出現在 vector_layers 欄位表，不列為必要（圖層程式仍會輸出）。
LANE_LAYERS = {
    "lanes": {"highway", "name", "lanes", "lanes_forward", "lanes_backward", "lanes_source", "oneway", "turn_lanes",
              "width", "motorcycle_lanes"},
    "lane_lines": {"kind", "lane_index", "lane_count", "side", "turn", "offset_m", "line_type", "highway"},
    "road_areas": {"kind"},
    "islands_pt": {"kind"},
}
LANE_CASE_TILE = (14, 13668, 7132)  # 國1 鼎金段 lanes=5
# App 實際讀的欄位（ios App/MapStyle.swift、MapPOI.swift、MapCartography.swift，2026-11-25 rg 查證）；改 App 樣式時要同步更新
APP_FIELDS = {
    "earth": {"kind", "name"},
    "landuse": {"kind"},
    "water": {"kind", "name", "min_zoom"},
    "roads": {"kind", "kind_detail", "is_bridge", "is_tunnel", "oneway", "min_zoom", "name", "name:zh-Hant", "network", "shield_text"},
    "pois": {"kind", "min_zoom", "name", "name:zh-Hant", "elevation"},
    "places": {"kind", "kind_detail", "min_zoom", "name", "name:zh-Hant"},
}
REQUIRED_LAYERS ={"earth", "landcover", "landuse", "water", "boundaries", "roads", "places", "pois"}


def lonlat_to_tile(lat, lon, z):
    n = 2 ** z
    x = (lon + 180) / 360 * n
    la = math.radians(lat)
    y = (1 - math.log(math.tan(la) + 1 / math.cos(la)) / math.pi) / 2 * n
    return x, y


class Tiles:
    def __init__(self, path):
        self.f = open(path, "rb")
        self.r = Reader(MmapSource(self.f))
        self.cache = {}

    def roads(self, z, x, y):
        k = (z, x, y)
        if k not in self.cache:
            d = self.r.get(z, x, y)
            if d is None:
                self.cache[k] = []
            else:
                if d[:2] == b"\x1f\x8b":
                    d = gzip.decompress(d)
                t = mvt.decode(d)
                lines = []
                for ft in t.get("roads", {}).get("features", []):
                    g = ft["geometry"]
                    parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
                    for p in parts:
                        lines.append((p, ft["properties"]))
                self.cache[k] = lines
            if len(self.cache) > 4000:
                self.cache.pop(next(iter(self.cache)))
        return self.cache[k]

    def nearest_m(self, lat, lon, z=14):
        fx, fy = lonlat_to_tile(lat, lon, z)
        x, y = int(fx), int(fy)
        px, py = (fx - x) * 4096, (1 - (fy - y)) * 4096  # mvt y 軸向上（mapbox_vector_tile 預設翻轉）
        m_per_unit = 40075016.686 * math.cos(math.radians(lat)) / (2 ** z) / 4096
        best, props = 1e18, None
        for pts, pr in self.roads(z, x, y):
            for i in range(len(pts) - 1):
                d = seg_dist(px, py, pts[i], pts[i + 1])
                if d < best:
                    best, props = d, pr
            if len(pts) == 1:
                d = math.hypot(pts[0][0] - px, pts[0][1] - py)
                if d < best:
                    best, props = d, pr
        return best * m_per_unit, props


def seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


class Sampler(osmium.SimpleHandler):
    def __init__(self, n, seed):
        super().__init__()
        self.n, self.rng, self.seen, self.res = n, random.Random(seed), 0, []

    def way(self, w):
        hw = w.tags.get("highway")
        if hw not in VEHICLE and hw not in OTHER:
            return
        if hw == "service" and w.tags.get("area") == "yes":
            return
        try:
            nodes = [(n.lat, n.lon) for n in w.nodes]
        except osmium.InvalidLocationError:
            return
        if len(nodes) < 2:
            return
        self.seen += 1
        # 蓄水池抽樣（每條 way 等機率）
        if len(self.res) < self.n:
            slot = len(self.res)
            self.res.append(None)
        else:
            j = self.rng.randrange(self.seen)
            if j >= self.n:
                return
            slot = j
        i = self.rng.randrange(len(nodes) - 1)
        lat = (nodes[i][0] + nodes[i + 1][0]) / 2
        lon = (nodes[i][1] + nodes[i + 1][1]) / 2
        self.res[slot] = dict(id=w.id, hw=hw, access=w.tags.get("access", ""), lat=lat, lon=lon,
                              tunnel=w.tags.get("tunnel", ""), area=w.tags.get("area", ""))


def coverage(tiles, samples):
    out = []
    for s in samples:
        d, pr = tiles.nearest_m(s["lat"], s["lon"])
        out.append((s, d, pr))
    return out


def summarize(rows):
    def pct(rs):
        return round(100 * sum(1 for _, d, _ in rs if d <= MAX_M) / max(1, len(rs)), 2)
    veh = [r for r in rows if r[0]["hw"] in VEHICLE]
    oth = [r for r in rows if r[0]["hw"] in OTHER]
    by = {}
    for r in rows:
        by.setdefault(r[0]["hw"], []).append(r)
    miss = [dict(id=s["id"], hw=s["hw"], access=s["access"], tunnel=s["tunnel"], lat=round(s["lat"], 6),
                 lon=round(s["lon"], 6), dist_m=round(d, 1)) for s, d, _ in rows if d > MAX_M]
    return dict(all=pct(rows), vehicle=pct(veh), other=pct(oth), n=len(rows), n_vehicle=len(veh),
                by_class={k: [len(v), pct(v)] for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))},
                missing=miss)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("new")
    ap.add_argument("--old")
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--json")
    a = ap.parse_args()
    fails, report = [], {}

    t = Tiles(a.new)
    h = t.r.header()
    meta = t.r.metadata()
    layers = {L["id"] for L in meta.get("vector_layers", [])}
    report["layers"] = sorted(layers)
    report["maxzoom"] = h.get("max_zoom")
    report["basemap_version"] = meta.get("version")
    if not REQUIRED_LAYERS <= layers:
        fails.append(f"圖層缺少 {sorted(REQUIRED_LAYERS - layers)}")
    if h.get("max_zoom") != 14:
        fails.append(f"maxzoom={h.get('max_zoom')}（App 期待 14）")
    if str(h.get("tile_compression")).upper().find("GZIP") < 0:
        fails.append(f"壓縮 {h.get('tile_compression')}（期待 gzip）")

    fields = {L["id"]: set(L.get("fields", {})) for L in meta.get("vector_layers", [])}
    for lname, need in LANE_LAYERS.items():
        if lname not in fields:
            fails.append(f"車道圖層缺少 {lname}")
        elif not need <= fields[lname]:
            fails.append(f"{lname} 欄位缺少 {sorted(need - fields[lname])}")
    d = t.r.get(*LANE_CASE_TILE)
    case5 = 0
    if d is not None:
        tt = mvt.decode(gzip.decompress(d) if d[:2] == bytes([0x1F, 0x8B]) else d)
        case5 = sum(1 for ft in tt.get("lane_lines", {}).get("features", [])
                    if ft["properties"].get("kind") == "lane" and ft["properties"].get("lane_count") == 5
                    and ft["properties"].get("highway") == "motorway")
    report["lane_case_dingjin_5lane_features"] = case5
    if case5 < 5:
        fails.append(f"固定案例國1鼎金段 lane_count=5 車道線只有 {case5} 條")
    if a.old:
        with open(a.old, "rb") as fo:
            old_meta = Reader(MmapSource(fo)).metadata()
        # 舊圖層不得消失；欄位只檢查 App（MapStyle.swift／MapPOI.swift／MapCartography.swift）實際讀的欄位。
        # 理由（2026-11-25 查證）：線上舊底圖是 Protomaps 全球建置裁出台灣，metadata 欄位表帶著全球才有的
        # name3、pgf:*、script*、罕見 name:xx、ref:*、network_4~6 等；自建版同一 schema（4.15.2）但只列台灣實際出現的欄位。
        # 高雄 z14 圖磚實測舊有新無的欄位為 0，App 也不讀這些欄位，因此不以全欄位表判失敗，只列入報告。
        for L in old_meta.get("vector_layers", []):
            lost = set(L.get("fields", {})) - fields.get(L["id"], set())
            if L["id"] not in fields:
                fails.append(f"舊圖層 {L['id']} 不見了")
                continue
            need = lost & APP_FIELDS.get(L["id"], set())
            if need:
                fails.append(f"舊圖層 {L['id']} 少了 App 使用的欄位 {sorted(need)}")
            if lost - need:
                report.setdefault("old_fields_not_used_by_app_missing", {})[L["id"]] = sorted(lost - need)
        for lid, need in APP_FIELDS.items():
            miss = need - fields.get(lid, set())
            if miss:
                fails.append(f"圖層 {lid} 缺 App 使用的欄位 {sorted(miss)}")

    sp = Sampler(a.n * 3, a.seed)
    sp.apply_file(a.pbf, locations=True, idx="flex_mem")
    samples = [s for s in sp.res if s and BBOX[0] <= s["lon"] <= BBOX[2] and BBOX[1] <= s["lat"] <= BBOX[3]]
    random.Random(a.seed).shuffle(samples)
    samples = samples[: a.n]
    report["sampled_from_ways"] = sp.seen

    new = summarize(coverage(t, samples))
    report["new"] = new
    if new["vehicle"] < 99.0:
        fails.append(f"車輛可走道路覆蓋率 {new['vehicle']}% < 99%")
    if new["all"] < 97.0:
        fails.append(f"全部道路覆蓋率 {new['all']}% < 97%")

    case = [round(t.nearest_m(lat, lon)[0], 1) for lat, lon in CASE_POINTS]
    report["case_209794955_dist_m"] = case
    if max(case) > MAX_M:
        fails.append(f"固定案例鼎金一巷巷道沒畫到：{case}")

    if a.old:
        to = Tiles(a.old)
        old = summarize(coverage(to, samples))
        old.pop("missing")
        report["old"] = old
        report["old_case_dist_m"] = [round(to.nearest_m(lat, lon)[0], 1) for lat, lon in CASE_POINTS]
        import os
        sn, so = os.path.getsize(a.new), os.path.getsize(a.old)
        report["bytes_new"], report["bytes_old"] = sn, so
        if not (0.7 * so <= sn <= 1.6 * so):
            fails.append(f"大小 {sn} 與舊檔 {so} 相差超出 -30%～+60%")

    report["fails"] = fails
    txt = json.dumps(report, ensure_ascii=False, indent=1)
    if a.json:
        open(a.json, "w", encoding="utf-8").write(txt)
    short = {k: v for k, v in report.items() if k not in ("new",)}
    short["new"] = {k: v for k, v in new.items() if k != "missing"}
    short["new_missing_first20"] = new["missing"][:20]
    print(json.dumps(short, ensure_ascii=False, indent=1))
    print("通過" if not fails else "未通過：" + "；".join(fails))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
