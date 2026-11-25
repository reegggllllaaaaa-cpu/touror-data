#!/usr/bin/env python3
"""全台號誌路口「各進入方向的機車左轉規則」產生器（可每週重建）。

用法：
  python -I scripts/rules/build_moto_left_turn.py [--pbf taiwan-latest.osm.pbf] [--out data/rules]
         [--lanes-dir ../資料/車道] [--inferred inferred_lanes.csv] [--no-download]

法規（道路交通安全規則 §99 第2項、§102 第1項第5款）：機車左轉預設換入內側車道直接左轉；
只有三種情況要兩段式：①內側車道禁行機車 ②三快車道以上單行道 ③設有遵20（兩段式左轉標誌）。

規則判定順序（先中先贏），沒有證據一律 unknown，不猜：
  0 approach_moto_banned      進入路段本身禁行機車（OSM motorcycle=no／政府禁行機車）→ unknown（機車不會從這裡來）
  1 taipei_exception_direct   臺北市「三車道以上例外開放機車直接左轉」路口＋方向 → direct  high
    taipei_exception_two_stage臺北市「二車道路段例外實施兩段式左轉」路口＋方向 → two_stage high
  2 osm_sign_zun20            OSM traffic_sign 含 遵20／兩段式（號誌節點或進入路段）→ two_stage high
  3 osm_inner_lane_moto_no    OSM motorcycle:lanes(:forward/:backward) 最內側（第一個）為 no → two_stage high
    taipei_lane3_inner_ban    臺北市「開放機車行駛第3車道」路段＝內側 1、2 車道禁行機車 → two_stage medium
  4 oneway_3plus_*            單行道且車道數 ≥3（OSM lanes／政府 medium↑／車道推論 v2 medium↑）→ two_stage medium
                              （trunk/primary/secondary 多為雙線化分隔道路，未必是法定單行道 → low）
  5 penghu_list               澎湖縣設有機車兩段式左轉路口（路口層級，以路名配對）→ two_stage medium
    kaohsiung_2016_enforce    高雄市105年取締未依規定兩段式左轉前十大路口（舊資料）→ two_stage low
  6 taipei_rule_3lane / taipei_rule_2lane  臺北市：該方向 ≥3 車道兩段式、≤2 車道直接左轉（北市管制原則，
                              見資料集 158073 名稱與說明）；車道數只用 OSM 明確值或政府 medium↑ → medium
  7 其餘 unknown
"""
import argparse, csv, io, json, math, os, re, subprocess, sys, time
from collections import defaultdict, Counter

R = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, R)

CAR_HW = {"trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
          "trunk_link", "primary_link", "secondary_link", "tertiary_link", "road"}
ZUN20 = re.compile(r"遵\s*20|兩段|two[_ ]?stage", re.I)


def low_priority():
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


def bearing(lat1, lon1, lat2, lon2):
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def dist_m(lat1, lon1, lat2, lon2):
    dx = (lon2 - lon1) * 111320 * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * 110540
    return math.hypot(dx, dy)


def to_int(v):
    try:
        return int(float(str(v).split(";")[0]))
    except Exception:
        return None


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ---------------- OSM ----------------
def load_osm(pbf):
    import osmium
    t0 = time.time()
    signals, sign_nodes, admin_rel = {}, set(), {}
    # pass 1：號誌節點、遵20 標誌節點、縣市界 relation
    fp = osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.RELATION).with_filter(
        osmium.filter.KeyFilter("highway", "traffic_sign", "boundary"))
    for o in fp:
        t = o.tags
        if o.is_node():
            if t.get("highway") == "traffic_signals":
                signals[o.id] = (o.location.lat, o.location.lon)
            if ZUN20.search(t.get("traffic_sign", "") or ""):
                sign_nodes.add(o.id)
        elif t.get("boundary") == "administrative" and t.get("admin_level") == "4":
            nm = t.get("name", "")
            if nm.endswith(("市", "縣")):
                admin_rel[nm] = [m.ref for m in o.members if m.type == "w"]
    print(f"[osm] pass1 號誌 {len(signals)}、遵20節點 {len(sign_nodes)}、縣市 {len(admin_rel)}（{time.time()-t0:.0f}s）", flush=True)
    bways = {w for ws in admin_rel.values() for w in ws}
    ways, bgeom = {}, {}
    fp = osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for o in fp:
        if not o.is_way():
            continue
        if o.id in bways:
            try:
                bgeom[o.id] = [(n.lon, n.lat) for n in o.nodes]
            except Exception:
                pass
        hw = o.tags.get("highway")
        if hw not in CAR_HW:
            continue
        refs = [n.ref for n in o.nodes]
        idx = [i for i, r in enumerate(refs) if r in signals]
        if not idx:
            continue
        keep = {k: v for k, v in o.tags if k in ("highway", "name", "ref", "oneway", "junction", "lanes", "lanes:forward",
                                                "lanes:backward", "motorcycle", "access", "motor_vehicle", "traffic_sign",
                                                "traffic_sign:forward", "traffic_sign:backward", "turn:lanes")
                or k.startswith("motorcycle:lanes")}
        pts = {}
        for i in idx:
            for j in (i - 1, i + 1):
                if 0 <= j < len(refs):
                    pts[j] = (o.nodes[j].lat, o.nodes[j].lon)
        ways[o.id] = (keep, refs, idx, pts)
    print(f"[osm] pass2 碰到號誌的車道 way {len(ways)}、縣界 way {len(bgeom)}（{time.time()-t0:.0f}s）", flush=True)
    return signals, sign_nodes, admin_rel, bgeom, ways


def build_counties(admin_rel, bgeom):
    from shapely.geometry import LineString
    from shapely.ops import polygonize, unary_union
    from shapely import STRtree
    polys, names = [], []
    for nm, ws in admin_rel.items():
        lines = [LineString(bgeom[w]) for w in ws if w in bgeom and len(bgeom[w]) >= 2]
        if not lines:
            continue
        p = unary_union(list(polygonize(unary_union(lines))))
        if not p.is_empty:
            polys.append(p); names.append(nm)
    tree = STRtree(polys)

    def lookup(lat, lon):
        from shapely.geometry import Point
        pt = Point(lon, lat)
        hits = list(tree.query(pt, predicate="intersects"))
        if not hits:
            return "未知"
        # 臺北市在新北市內、嘉義市在嘉義縣內：外圈 polygonize 會把內洞也填滿，取面積最小者
        return names[min(hits, key=lambda i: polys[i].area)]
    return lookup, names


# ---------------- 外部資料 ----------------
def load_gov_lanes(path):
    d = {}
    if os.path.exists(path):
        for r in read_csv(path):
            d[int(r["osm_way_id"])] = r
    return d


def load_inferred(path):
    """車道推論 v2：只留 confidence high/medium。path 空白時試著從 WSL 讀預設輸出。"""
    txt = None
    if path and os.path.exists(path):
        txt = open(path, encoding="utf-8").read()
    elif not path:
        try:
            txt = subprocess.run(["wsl", "-d", "Ubuntu-24.04", "-u", "root", "-e", "cat",
                                  "/root/lanesinfer/out/inferred_lanes.csv"], capture_output=True, timeout=300).stdout.decode("utf-8")
        except Exception as e:
            print("[inferred] 讀不到（略過）：", e)
    out = {}
    if txt:
        for r in csv.DictReader(io.StringIO(txt)):
            if r.get("confidence") in ("high", "medium"):
                out[int(r["way_id"])] = r
    print(f"[inferred] medium↑ {len(out)} 條")
    return out


def fetch_lists(raw_dir, download):
    os.makedirs(raw_dir, exist_ok=True)
    meta = {}
    if download:
        from scripts.common import datagov
        for d in ("157480", "47188"):
            try:
                meta[d] = datagov.fetch_dataset(d, os.path.join(raw_dir, d + ".csv"), fmt="CSV")
            except Exception as e:
                print(f"[datagov] {d} 下載失敗，沿用舊檔：{e}")
    return meta


ROAD_RE = re.compile(r"((?:[^\s與及、，,（(]{1,8}?)(?:大道|路|街|巷)(?:[一二三四五六七八九十]段)?)")


def parse_pair(text):
    text = re.sub(r"^.*?區", "", text)
    text = re.sub(r"（.*?）|\(.*?\)", "", text)
    parts = re.split(r"與|及", text)
    if len(parts) < 2:
        return None
    strip = lambda x: re.sub(r"(?:縣道|省道|台|臺)?\d+線", " ", x)
    a = ROAD_RE.findall(strip(parts[0])); b = ROAD_RE.findall(strip(parts[1]))
    ra = re.findall(r"(\d+)線", parts[0]); rb = re.findall(r"(\d+)線", parts[1])
    ka = a[-1] if a else (("ref", ra[-1]) if ra else None)
    kb = b[0] if b else (("ref", rb[0]) if rb else None)
    return (ka, kb) if ka and kb else None


# ---------------- 主流程 ----------------
SIDE = {"北": 0, "東": 90, "南": 180, "西": 270}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", default=os.path.join(R, "..", "資料", "搜尋", "路網來源", "osm", "taiwan-latest.osm.pbf"))
    ap.add_argument("--out", default=os.path.join(R, "data", "rules"))
    ap.add_argument("--lanes-dir", default=os.path.join(R, "..", "資料", "車道"))
    ap.add_argument("--inferred", default="")
    ap.add_argument("--no-download", action="store_true")
    a = ap.parse_args()
    low_priority()
    raw_dir = os.path.join(a.out, "raw")
    meta = fetch_lists(raw_dir, not a.no_download)

    signals, sign_nodes, admin_rel, bgeom, ways = load_osm(a.pbf)
    county_of, cnames = build_counties(admin_rel, bgeom)
    gov = load_gov_lanes(os.path.join(R, "data", "lanes", "gov_lane_attrs.csv"))
    inferred = load_inferred(a.inferred)

    banned = defaultdict(set)  # way -> {forward, backward}
    mr = os.path.join(a.lanes_dir, "moto_restrictions_for_routing.csv")
    if os.path.exists(mr):
        for r in read_csv(mr):
            if r["restriction"] == "motorcycle=no":
                banned[int(r["osm_way_id"])].add(r["direction"])

    # 號誌節點 → 進入方向
    node_ways = defaultdict(list)
    approaches = []  # dict
    for wid, (tags, refs, idx, pts) in ways.items():
        ow = tags.get("oneway", "")
        one_fwd = ow in ("yes", "1", "true") or tags.get("junction") in ("roundabout", "circular")
        one_bwd = ow == "-1"
        for i in idx:
            nid = refs[i]
            node_ways[nid].append(wid)
            lat, lon = signals[nid]
            if i > 0 and not one_bwd:
                approaches.append(dict(node=nid, way=wid, dir="forward", brg=bearing(lat, lon, *pts[i - 1])))
            if i < len(refs) - 1 and not one_fwd:
                approaches.append(dict(node=nid, way=wid, dir="backward", brg=bearing(lat, lon, *pts[i + 1])))
    print(f"[approach] {len(approaches)} 個進入方向，{len(node_ways)} 個號誌節點")

    # 臺北例外路口：最近號誌節點（40 m 內）＋側向
    tp_exc = {}  # (node, side_deg) -> rule
    exc_hit = Counter()
    sig_items = list(signals.items())
    grid = defaultdict(list)
    for nid, (la, lo) in sig_items:
        grid[(round(la * 200), round(lo * 200))].append(nid)

    def near_signals(la, lo, r=40):
        gi, gj = round(la * 200), round(lo * 200)
        c = [(dist_m(la, lo, *signals[n]), n) for di in (-1, 0, 1) for dj in (-1, 0, 1) for n in grid.get((gi + di, gj + dj), [])]
        return [n for d, n in sorted(c) if d <= r]

    exc_path = os.path.join(a.lanes_dir, "moto_intersection_exceptions.csv")
    if os.path.exists(exc_path):
        for r in read_csv(exc_path):
            rule = "direct" if "直接左轉" in r["kind"] else "two_stage"
            sides = set()
            for part in re.split(r"[、,，]", r["direction"]):
                part = part.strip()
                m = re.match(r"([東西南北])往", part)
                if m:
                    sides.add(m.group(1))
                elif "雙向" in part:
                    sides.update(ch for ch in part[:2] if ch in SIDE)
            if not (r.get("lat") and r.get("lon")):
                exc_hit["原始無座標"] += 1
                continue
            ns = near_signals(float(r["lat"]), float(r["lon"]))
            if not ns:
                exc_hit["未對到號誌"] += 1
                continue
            exc_hit["對到"] += 1
            for n in ns[:4]:
                for s in sides:
                    tp_exc[(n, SIDE[s])] = (rule, r["intersection"] + " " + r["direction"])

    # 澎湖、高雄清單：路名配對到號誌節點
    def names_of(w):
        t = ways[w][0]
        return t.get("name", ""), t.get("ref", "")

    def key_match(key, w):
        nm, ref = names_of(w)
        if isinstance(key, tuple):
            return key[1] in re.findall(r"\d+", ref)
        return bool(nm) and (nm == key or nm.startswith(key) or key.startswith(nm) or key.endswith(nm))

    list_nodes = {}  # node -> (basis, conf, text)
    list_hit = Counter()
    for fname, county, basis, conf in (("157480.csv", "澎湖縣", "penghu_list", "medium"),
                                       ("47188.csv", "高雄市", "kaohsiung_2016_enforce", "low")):
        p = os.path.join(raw_dir, fname)
        if not os.path.exists(p):
            continue
        for r in read_csv(p):
            text = r.get("路口名稱") or r.get("違規地點") or ""
            pair = parse_pair(text)
            hit = []
            if pair:
                for nid, ws in node_ways.items():
                    if any(key_match(pair[0], w) for w in ws) and any(key_match(pair[1], w) for w in ws):
                        hit.append(nid)
                hit = [n for n in hit if county_of(*signals[n]) == county]
            list_hit[(basis, "對到" if hit else "未對到")] += 1
            for n in hit:
                list_nodes[n] = (basis, conf, text.strip("。"))

    def lanes_dir(wid, d, tags):
        """回傳 (該方向車道數, 來源, 是否單行)；只用 OSM 明確值、政府 medium↑、推論 v2 medium↑。"""
        ow = tags.get("oneway", "") in ("yes", "1", "true", "-1")
        v = to_int(tags.get(f"lanes:{d}"))
        if v:
            return v, "osm", ow
        if ow and to_int(tags.get("lanes")):
            return to_int(tags.get("lanes")), "osm", ow
        g = gov.get(wid)
        if g and g.get("confidence") in ("high", "medium"):
            v = to_int(g.get(f"lanes_{d}"))
            gow = ow or g.get("oneway") in ("yes", "-1")
            if v:
                return v, "gov", gow
            if gow and to_int(g.get("lanes")):
                return to_int(g["lanes"]), "gov", gow
        if not ow and to_int(tags.get("lanes")) and to_int(tags.get("lanes")) <= 3:
            return 2 if to_int(tags.get("lanes")) >= 2 else 1, "osm_total_le3", ow  # 雙向總數 ≤3：每向 ≤2
        f = inferred.get(wid)
        if f and ow and to_int(f.get("lanes")):
            return to_int(f["lanes"]), "inferred_v2", ow
        return None, "", ow

    rows = []
    for ap_ in approaches:
        nid, wid, d = ap_["node"], ap_["way"], ap_["dir"]
        tags = ways[wid][0]
        lat, lon = signals[nid]
        county = county_of(lat, lon)
        nl, nsrc, ow = lanes_dir(wid, d, tags)
        rule, basis, conf = "unknown", "no_evidence", "none"
        mlanes = tags.get(f"motorcycle:lanes:{d}") or (tags.get("motorcycle:lanes") if ow else None)
        gmr = (gov.get(wid) or {}).get("motorcycle_restriction", "")
        exc = None
        for sd in (0, 90, 180, 270):
            if (nid, sd) in tp_exc and min(abs(ap_["brg"] - sd), 360 - abs(ap_["brg"] - sd)) <= 45:
                exc = tp_exc[(nid, sd)]
        if tags.get("motorcycle") == "no" or tags.get("access") == "no" or d in banned.get(wid, ()) or \
                (gmr.startswith("禁行機車") and (f"({d})" in gmr or "(both)" in gmr)):
            rule, basis, conf = "unknown", "approach_moto_banned", "none"
        elif exc:
            rule, basis, conf = exc[0], ("taipei_exception_direct" if exc[0] == "direct" else "taipei_exception_two_stage") + ":" + exc[1], "high"
        elif nid in sign_nodes or ZUN20.search(tags.get("traffic_sign", "") + tags.get(f"traffic_sign:{d}", "")):
            rule, basis, conf = "two_stage", "osm_sign_zun20", "high"
        elif mlanes and mlanes.split("|")[0].strip() == "no":
            rule, basis, conf = "two_stage", "osm_inner_lane_moto_no", "high"
        elif gmr.startswith("例外:開放機車行駛第3車道") and (f"({d})" in gmr or "(both)" in gmr):
            rule, basis, conf = "two_stage", "taipei_lane3_inner_ban", "medium"
        elif ow and nl and nl >= 3:
            # trunk/primary/secondary 的 oneway 多半是分隔島雙線化道路（法律上未必是「單行道」）→ low
            dual = tags.get("highway", "").split("_")[0] in ("trunk", "primary", "secondary")
            rule, basis, conf = "two_stage", f"oneway_3plus_{nsrc}", "low" if dual else "medium"
        elif nid in list_nodes:
            b, c, t = list_nodes[nid]
            rule, basis, conf = "two_stage", f"{b}:{t}", c
        elif county == "臺北市" and nl and nsrc != "inferred_v2":
            rule, basis, conf = ("two_stage", "taipei_rule_3lane", "medium") if nl >= 3 else ("direct", "taipei_rule_2lane", "medium")
        rows.append([nid, f"{lat:.7f}", f"{lon:.7f}", county, wid, d, round(ap_["brg"]), tags.get("name", ""),
                     tags.get("highway", ""), "yes" if ow else "no", nl or "", nsrc, rule, basis, conf])

    os.makedirs(a.out, exist_ok=True)
    rows.sort(key=lambda r: (r[0], r[4], r[5]))
    with open(os.path.join(a.out, "moto_left_turn.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["signal_node_id", "lat", "lon", "county", "osm_way_id", "way_dir", "approach_bearing", "road_name",
                    "highway", "oneway", "lanes_dir", "lanes_src", "rule", "basis", "confidence"])
        w.writerows(rows)
    stats = defaultdict(Counter)
    for r in rows:
        stats[r[3]][r[12]] += 1
        stats["全台"][r[12]] += 1
    bas = Counter(r[13].split(":")[0] for r in rows)
    out = {"generated": time.strftime("%Y-%m-%d %H:%M"), "pbf": os.path.basename(a.pbf),
           "signal_nodes": len(node_ways), "approaches": len(rows),
           "by_county": {k: dict(v) for k, v in sorted(stats.items())}, "by_basis": dict(bas.most_common()),
           "taipei_exceptions": dict(exc_hit), "list_match": {f"{k[0]}|{k[1]}": v for k, v in list_hit.items()},
           "datagov": meta}
    json.dump(out, open(os.path.join(a.out, "moto_left_turn_stats.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: out[k] for k in ("signal_nodes", "approaches", "by_basis", "taipei_exceptions", "list_match")}, ensure_ascii=False))
    for k, v in sorted(stats.items()):
        print(k, dict(v))


if __name__ == "__main__":
    main()
