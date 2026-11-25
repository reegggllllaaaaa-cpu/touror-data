"""主幹道缺口路段中點候選（2026-11-26 起）：車道推論 v2 的 gaps_major.csv（motorway～secondary 只有先驗、整條路沒標）
每段取代表點（長段每 1 km 一點），用 Mapillary 覆蓋圖磚（z14 image 圖層，一次拿到整格照片座標／方位／時間）找
點 30 m 內、拍攝方位與該行進方向相差 ≤30°、白天、非全景、非高架、且拍在目標道路上（離路線 ≤12 m）的照片；≥2 張才進候選。
由 kp.py 呼叫：segextract／segcands／select（路段配額）／sheets（G###.jpg、標籤 B###）／decide（→ mapillary_ai_segments.csv）。
manifest 鍵：seg:<way>:<forward|backward>:<點序>，kind=segment。Token 只經 mly.py 讀取。
"""
import os, sys, json, csv, math, gzip, pickle, collections
from kp import (WORK, PBF, RECHECK_DAYS, dist, bearing, adiff, seg_dist, daytime, ymd, today, days_since, jload, Elev,
                is_elev, ELEV_RAD)
import datetime

GAPS = os.environ.get("LANES_GAPS") or os.path.join(WORK, "gaps_major.csv")
SEG_PKL = os.path.join(WORK, "kp_seg.pkl")
TILES = os.path.join(WORK, "mlytiles"); os.makedirs(TILES, exist_ok=True)
TILE_URL = "https://tiles.mapillary.com/maps/vtp/mly1_public/2/{z}/{x}/{y}"
SEG_HW = ["motorway", "trunk", "primary", "secondary"]
SEG_RAD = 30         # 照片離代表點 ≤30 m
SEG_LAT = 10         # 照片離目標路線 ≤10 m（排除平行側車道／對向分離車道）
SEG_ANG = 25         # 相機方位與路段行進方向相差 ≤25°
SUN_MIN = 10         # 太陽高度角 ≥10°（另限台灣時間 06:30–17:30），排除清晨／黃昏／夜間
SEG_STEP = 1000      # 長段每 1 km 一點
SEG_SHARE = 3        # 每週方向上限與查詢預算的 1/3 留給路段候選
PENDING_REASON = "缺少 2 張合格照片（路段點 30 m 內、離路線 ≤10 m、同向 ±25°、太陽高度 ≥10°、非全景、高架層一致）"


def ow_of(t):
    ow = t.get("oneway", "no")
    if ow == "-1": return "-1"
    return "yes" if ow in ("yes", "1", "true") or t.get("junction") == "roundabout" or t.get("highway") == "motorway" and ow != "no" else "no"


def segextract():
    """gaps_major.csv 的 way id → 從 PBF 取幾何 → work/kp_seg.pkl。"""
    import osmium
    gaps = {}
    for r in csv.DictReader(open(GAPS, encoding="utf-8-sig")):
        if r["highway"] in SEG_HW:
            gaps[int(r["way_id"])] = {"hw": r["highway"], "name": r["name_or_ref"], "len": float(r["length_m"] or 0)}

    class H(osmium.SimpleHandler):
        def __init__(s): super().__init__(); s.ways = []
        def way(s, w):
            if w.id not in gaps: return
            try: nodes = [(nd.ref, nd.location.lat, nd.location.lon) for nd in w.nodes]
            except osmium.InvalidLocationError: return
            s.ways.append({"id": w.id, "tags": dict(w.tags), "nodes": nodes, "gap": gaps[w.id]})
    h = H(); h.apply_file(PBF, locations=True, idx="flex_mem")
    pickle.dump({"ways": h.ways, "gaps_mtime": os.path.getmtime(GAPS)}, open(SEG_PKL, "wb"))
    print("gaps", len(gaps), "ways found in PBF", len(h.ways), "missing", len(gaps) - len(h.ways))


def seg_points(w):
    """路段代表點：n＝ceil(長度/1 km)，取各等分中點；方位取點前後各 20 m 的連線。"""
    ns = [p[1:] for p in w["nodes"]]; cum = [0.0]
    for i in range(len(ns) - 1): cum.append(cum[-1] + dist(ns[i], ns[i + 1]))
    L = cum[-1]
    if L < 20: return []
    def at(s):
        s = max(0.0, min(L, s)); i = 0
        while i < len(cum) - 2 and cum[i + 1] < s: i += 1
        t = (s - cum[i]) / ((cum[i + 1] - cum[i]) or 1)
        return (ns[i][0] + (ns[i + 1][0] - ns[i][0]) * t, ns[i][1] + (ns[i + 1][1] - ns[i][1]) * t)
    n = max(1, math.ceil(L / SEG_STEP)); out = []
    for k in range(n):
        s = (k + 0.5) * L / n
        out.append((k, at(s), round(bearing(at(s - 20), at(s + 20)), 1)))
    return out


def tiles_for(p):
    d = 0.0004; out = set()
    for la in (p[0] - d, p[0] + d):
        for lo in (p[1] - d, p[1] + d):
            n = 2 ** 14; out.add((int((lo + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(la))) / math.pi) / 2 * n)))
    return sorted(out)


def tile_imgs(x, y, maxage=RECHECK_DAYS, fetch=True):
    """z14 image 圖層 → [[id, lat, lon, captured_at, compass, is_pano, seq], ...]；回傳 (照片, 是否打了 API)。快取在 work/mlytiles。"""
    f = os.path.join(TILES, f"14_{x}_{y}.json")
    if os.path.exists(f):
        d = json.load(open(f, encoding="utf-8"))
        if days_since(d.get("date", "")) < maxage or not fetch: return d["imgs"], False
    elif not fetch: return None, False
    import mapbox_vector_tile
    from mly import tile
    try: b = tile(TILE_URL, 14, x, y)
    except RuntimeError: return None, True
    if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
    out = []
    if b:
        lay = mapbox_vector_tile.decode(b).get("image", {}); ext = lay.get("extent", 4096)
        for ft in lay.get("features", []):
            g = ft["geometry"]
            if g["type"] != "Point": continue
            px, py = g["coordinates"]; pr = ft["properties"]
            lo = (x + px / ext) / 2 ** 14 * 360 - 180; yy = (y + (ext - py) / ext) / 2 ** 14
            la = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy))))
            out.append([str(pr.get("id")), round(la, 7), round(lo, 7), pr.get("captured_at") or 0, pr.get("compass_angle"),
                        bool(pr.get("is_pano")), str(pr.get("sequence_id", ""))])
    json.dump({"date": today(), "imgs": out}, open(f, "w", encoding="utf-8")); return out, True


def sun_ok(ms, lat, lon):
    """台灣時間 06:30–17:30 且太陽高度角 ≥ SUN_MIN（NOAA 簡式，誤差 <1°）。"""
    if not ms: return False
    u = datetime.datetime.utcfromtimestamp(ms / 1000); tw = u + datetime.timedelta(hours=8)
    if not (6 * 60 + 30 <= tw.hour * 60 + tw.minute <= 17 * 60 + 30): return False
    n = u.timetuple().tm_yday; g = 2 * math.pi / 365 * (n - 1 + (u.hour - 12) / 24)
    decl = 0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)         - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = u.hour * 60 + u.minute + u.second / 60 + eqt + 4 * lon
    ha = math.radians(tst / 4 - 180); la = math.radians(lat)
    el = math.degrees(math.asin(math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)))
    return el >= SUN_MIN


def layer_ok(elev, q, ang, w):
    """照片附近若同時有高架與平面道路：只看與相機方位一致（±SEG_ANG，雙向）的道路；其中有任何一條高架與否和目標 way 不同 → 無法確定拍在哪一層，排除。"""
    if not elev: return True
    te = is_elev(w["tags"]); k = (round(q[0] / 0.001), round(q[1] / 0.001))
    for a in (-1, 0, 1):
        for b in (-1, 0, 1):
            for sa, sb, e, wid in elev.g[(k[0] + a, k[1] + b)]:
                if wid == w["id"] or e == te: continue
                if seg_dist(q, sa, sb) > ELEV_RAD: continue
                h = bearing(sa, sb)
                if min(adiff(ang, h), adiff(ang, (h + 180) % 360)) <= SEG_ANG: return False
    return True


def seg_pick(imgs, p, brg, w, elev):
    ns = [n[1:] for n in w["nodes"]]; c = []
    for iid, la, lo, cap, ang, pano, seq in imgs:
        if pano or ang is None or not sun_ok(cap, la, lo): continue
        q = (la, lo); d = dist(p, q)
        if d > SEG_RAD or adiff(ang, brg) > SEG_ANG: continue
        if min(seg_dist(q, ns[i], ns[i + 1]) for i in range(len(ns) - 1)) > SEG_LAT: continue
        if elev and elev.bad(q, w["id"]): continue
        if not layer_ok(elev, q, ang, w): continue
        c.append((d, cap, iid, seq, q))
    c.sort(key=lambda t: -t[1]); out = []
    for t in c:  # 最新優先；第二張盡量不同序列，否則相距 ≥8 m
        if not out: out.append(t); continue
        if t[3] != out[0][3] and dist(t[4], out[0][4]) >= 3: out.append(t); break
    if out and len(out) < 2:
        for t in c:
            if t[2] != out[0][2] and dist(t[4], out[0][4]) >= 8: out.append(t); break
    return out, len(c)


def seg_cands(ride_hits):
    """候選點清單，依 騎過 → 道路等級（motorway→secondary）→ 缺口長度（長的先）排序。"""
    D = pickle.load(open(SEG_PKL, "rb")); res = []
    for w in D["ways"]:
        g = w["gap"]; ow = ow_of(w["tags"]); pts = seg_points(w)
        if not pts: continue
        ride = sum(ride_hits(n[1:]) for n in w["nodes"]) + sum(ride_hits(p) for _, p, _ in pts)
        for k, p, b in pts:
            dirs = [(d, b if d == "forward" else (b + 180) % 360) for d in ("forward", "backward")
                    if not ((ow == "yes" and d == "backward") or (ow == "-1" and d == "forward"))]
            res.append({"w": w, "way": w["id"], "k": k, "p": p, "dirs": dirs, "hw": g["hw"], "name": w["tags"].get("name") or g["name"],
                        "len": g["len"], "ride": ride, "oneway": ow, "osm_lanes": w["tags"].get("lanes", "")})
    res.sort(key=lambda c: (0 if c["ride"] else 1, SEG_HW.index(c["hw"]), -c["len"], c["way"], c["k"]))
    return D["ways"], res


def tier_of(c): return 0 if c["ride"] else 1 + SEG_HW.index(c["hw"])


def select_segments(A, judged, limit, max_queries, base_ways, ride_hits):
    """路段候選選照片：每個點查涵蓋它的圖磚（快取超過 RECHECK_DAYS 才重抓，抓一格算一次查詢）。"""
    if not os.path.exists(SEG_PKL) or (os.path.exists(GAPS) and os.path.getmtime(GAPS) > os.path.getmtime(SEG_PKL)):
        print("gaps_major.csv 比 kp_seg.pkl 新 → 重跑 segextract"); segextract()
    ways, cands = seg_cands(ride_hits); elev = Elev(base_ways + ways)
    q = ready = 0; st = collections.Counter()
    for c in cands:
        if ready >= limit or q >= max_queries: break
        keys = [(d, b, f"seg:{c['way']}:{d}:{c['k']}") for d, b in c["dirs"]]
        todo = [(d, b, k) for d, b, k in keys if A.get(k, {}).get("status") not in ("ready", "accepted")
                and (k not in A or days_since(A[k].get("checked", "")) >= RECHECK_DAYS)]
        if not todo: continue
        imgs = []
        for t in tiles_for(c["p"]):
            r, hit = tile_imgs(*t); q += hit; imgs += r or []
        for d, b, k in todo:
            ps, nc = seg_pick(imgs, c["p"], b, c["w"], elev); old = A.get(k, {})
            newest = max([t[1] for t in ps], default=0)
            base = {"kind": "segment", "way": c["way"], "dir": d, "pt": c["k"], "hw": c["hw"], "name": c["name"], "oneway": c["oneway"], "brg": b,
                    "lat": round(c["p"][0], 6), "lon": round(c["p"][1], 6), "ends_here": False, "osm_tl": "", "osm_lanes": c["osm_lanes"],
                    "tier": tier_of(c), "ride": c["ride"], "gap_len": round(c["len"]), "checked": today(), "n_cand": nc}
            if len(ps) >= 2 and (k not in judged or newest > old.get("newest", 0)):
                if k in judged: base["prev_label"] = judged[k].get("label")
                base.update(status="ready", imgs=[t[2] for t in ps], caps=[f"{round(t[0])}m {ymd(t[1])}" for t in ps],
                            captured=[ymd(t[1]) for t in ps], newest=newest)
                A[k] = base; ready += 1; st["ready"] += 1
                if ready >= limit: break
            elif k in judged and old.get("status") in ("judged", "conflict", "lanes_only"):
                old["checked"] = today()
            else:
                base.update(status="pending", reason=PENDING_REASON, newest=newest or old.get("newest", 0)); A[k] = {**old, **base}; st["pending"] += 1
    print("segments: tile queries", q, dict(st))
    return q, ready


def segcands(fetch=True):
    """全量統計路段候選（每個點都查圖磚；快取 30 天內不重抓）。--nofetch 只用快取。結果寫 work/kp_seg_cands.json。"""
    from kp import ride_points, PKL
    if not os.path.exists(SEG_PKL): segextract()
    rp = ride_points(); rg = collections.defaultdict(list)
    for p in rp: rg[(round(p[0] / 0.001), round(p[1] / 0.001))].append(p)
    def ride_hits(c):
        k = (round(c[0] / 0.001), round(c[1] / 0.001))
        return sum(1 for a in (-1, 0, 1) for b in (-1, 0, 1) for p in rg[(k[0] + a, k[1] + b)] if dist(c, p) <= 30)
    base = pickle.load(open(PKL, "rb"))["ways"] if os.path.exists(PKL) else []
    ways, cands = seg_cands(ride_hits); elev = Elev(base + ways)
    S = collections.defaultdict(collections.Counter); okway = collections.defaultdict(set); q = 0
    for i, c in enumerate(cands):
        imgs = []; miss = False
        for t in tiles_for(c["p"]):
            r, hit = tile_imgs(*t, maxage=30, fetch=fetch); q += hit
            if r is None: miss = True
            imgs += r or []
        s = S[c["hw"]]; s["points"] += 1; s["ride_points"] += bool(c["ride"])
        if miss: s["points_no_tile"] += 1
        for d, b in c["dirs"]:
            ps, nc = seg_pick(imgs, c["p"], b, c["w"], elev); s["dirs"] += 1
            if nc >= 1: s["dirs_ge1"] += 1
            if len(ps) >= 2: s["dirs_ready"] += 1; okway[c["hw"]].add(c["way"])
        if i % 2000 == 0: print("…", i, "/", len(cands), "tile fetches", q, flush=True)
    out = {"date": today(), "ways": len(ways), "points": len(cands), "tile_fetches": q, "by_hw": {}}
    for hw in SEG_HW:
        out["by_hw"][hw] = {**S[hw], "ways_total": sum(1 for w in ways if w["gap"]["hw"] == hw), "ways_with_photos": len(okway[hw])}
    json.dump(out, open(os.path.join(WORK, "kp_seg_cands.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))


def seg_judge(pa, pb):
    """路段判讀：兩張 lanes 相同 → 兩張都 vis=clear 為 medium，否則 low；一張 clear、另一張看不清 → low；其餘不收。"""
    la, lb = pa.get("lanes"), pb.get("lanes")
    if la and la == lb: return la, "medium" if pa.get("vis") == "clear" and pb.get("vis") == "clear" else "low"
    for c, o in ((pa, pb), (pb, pa)):
        if c.get("lanes") and c.get("vis") == "clear" and o.get("lanes") is None: return c["lanes"], "low"
    return None, None


def seg_note(j):
    zh = {"yes": "有", "no": "無"}
    def both(f):
        a, b = j["a"].get(f, "unknown"), j["b"].get(f, "unknown")
        return zh.get(a, "不明") if a == b else ("不一致" if "unknown" not in (a, b) else zh.get(a if b == "unknown" else b, "不明"))
    n = f"路段點；機慢車道:{both('moto')}；分隔島:{both('median')}"
    return n + ("; " + j["note"] if j.get("note") else "")


SEG_CSV_FIELDS = ["osm_way_id", "direction", "lanes", "turn_lanes", "confidence", "image_ids", "captured_at", "note"]


def write_segments(rows, outd, stats):
    by = collections.defaultdict(list)
    for r in rows: by[(r["osm_way_id"], r["direction"])].append(r)
    final = []
    for key, rs in by.items():
        if len({r["lanes"] for r in rs}) > 1: stats["seg_dup_dropped"] += 1; continue   # 同一 way 同方向各點車道數不同 → 不收
        r = dict(rs[0]); r["confidence"] = min((x["confidence"] for x in rs), key=["low", "medium"].index)
        r["image_ids"] = " ".join(x["image_ids"] for x in rs); r["captured_at"] = " ".join(x["captured_at"] for x in rs)
        if len(rs) > 1: r["note"] += f"; {len(rs)} 點一致"
        final.append(r)
    with open(os.path.join(outd, "mapillary_ai_segments.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, SEG_CSV_FIELDS); w.writeheader(); w.writerows(sorted(final, key=lambda r: (int(r["osm_way_id"]), r["direction"])))
    stats["seg_rows"] = len(final); stats["seg_rows_medium"] = sum(1 for r in final if r["confidence"] == "medium")
