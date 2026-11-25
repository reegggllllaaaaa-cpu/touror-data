"""全台號誌路口（LANES_REGION=kp 時只做高雄＋屏東）：Mapillary 照片多模態判讀車道 → 自家資料集（CC BY-SA 4.0）。可重跑、可續跑。
Token 只經 mly.py 從檔案讀取，不印、不寫檔。

階段：
  python kp.py extract          # OSM（指定車道相關道路）＋號誌 → work/kp_osm.pkl
  python kp.py select [N]       # 路口優先序＋選照片（只對 pending／新照片／未判讀方向）→ work/kp_manifest.json
  python kp.py sheets           # 下載縮圖、裁路面、每 4 個方向拼一張 → 照片/kp/S###.jpg（只做尚未判讀的）
  python kp.py decide           # 讀 work/kp_judgments.json，套保守收錄規則 → CSV、pending、衝突、統計
  python kp.py elevcheck        # 已備妥／已判讀的方向，若照片拍在高架上就作廢、改列 pending（select 內建同一規則）
  python kp.py segextract       # 主幹道缺口路段（work/gaps_major.csv，車道推論輸出）→ 取幾何 → work/kp_seg.pkl
  python kp.py segcands [--nofetch]  # 只算路段候選數（全量查 Mapillary 圖磚，快取 30 天；--nofetch 只用快取）→ work/kp_seg_cands.json
路段中點候選（2026-11-26 起，見 kp_seg.py）：select N 的 1/3 方向與 1/3 查詢留給路段；LANES_SEG_ONLY=1 時只做路段（全部配額）。
判讀：人或多模態模型看 照片/kp/S###.jpg，逐張照片記 lanes、turn:lanes、可見度，寫入 work/kp_judgments.json。

路徑（皆可用環境變數覆寫）：
  LANES_WORKDIR   工作區（work/、照片/），預設 <repo>/../資料/車道/mapillary/視覺判讀
  LANES_PBF       taiwan-latest.osm.pbf，預設 <repo>/../資料/搜尋/路網來源/osm/taiwan-latest.osm.pbf
  LANES_SIGNALS   taiwan-signals.bin，預設 <repo>/../資料/紅綠燈/taiwan-signals.bin
  LANES_RIDES     騎行軌跡回報包資料夾（找 */**/ride.gpx），預設 ~/資料中心/工作日誌/回報包
  LANES_FORKS     岔路稽核表 forks_taiwan.csv（scripts/audit/fork_sides.py 產生），預設 <repo>/../資料/搜尋/靠左靠右稽核/forks_taiwan.csv；
                  表內所有岔路節點的進入方向也列為判讀候選（2026-11-25 起）；表比 work/kp_osm.pkl 新時 select 會自動重跑 extract
  LANES_REGION    taiwan（預設，全台）或 kp（舊範圍：高雄＋屏東）；改了要重跑 extract
  LANES_RECHECK_DAYS  pending／衝突方向多久重查一次 Mapillary（預設 7 天）
每週上限：select N 的 N＝本週最多備妥幾個待判讀方向；Mapillary 查詢上限 900 次，其中 1/3 留給重查、其餘給從沒查過的路口。
已判過的方向只有出現更新的照片才會重判（見 needs_judge）。
輸出一律寫到 <repo>/data/lanes/。Mapillary token 只由 mly.py 從檔案讀取。
"""
import sys, os, json, csv, math, glob, re, pickle, datetime, hashlib, collections
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
ROOT = os.path.dirname(REPO)
sys.path.insert(0, HERE)
VD = os.environ.get("LANES_WORKDIR") or os.path.join(ROOT, "資料", "車道", "mapillary", "視覺判讀")
WORK = os.path.join(VD, "work"); CACHE = os.path.join(WORK, "cache"); os.makedirs(CACHE, exist_ok=True)
IMG = os.path.join(VD, "照片"); SHEET = os.path.join(IMG, "kp"); os.makedirs(SHEET, exist_ok=True)
PBF = os.environ.get("LANES_PBF") or os.path.join(ROOT, "資料", "搜尋", "路網來源", "osm", "taiwan-latest.osm.pbf")
SIG = os.environ.get("LANES_SIGNALS") or os.path.join(ROOT, "資料", "紅綠燈", "taiwan-signals.bin")
FORKS = os.environ.get("LANES_FORKS") or os.path.join(ROOT, "資料", "搜尋", "靠左靠右稽核", "forks_taiwan.csv")
REPORTS = os.environ.get("LANES_RIDES") or os.path.join(os.path.expanduser("~"), "資料中心", "工作日誌", "回報包")
OUTD = os.path.join(REPO, "data", "lanes")
MAX_WAY_M = 400      # AI 判讀只套用在長度 ≤ 400 m 的 way
ELEV_RAD = 15        # 照片 15 m 內最近道路若為高架（bridge／layer>0）且目標 way 不是高架 → 排除
GOV = os.path.join(OUTD, "gov_lane_attrs.csv")
PKL = os.path.join(WORK, "kp_osm.pkl"); MAN = os.path.join(WORK, "kp_manifest.json"); JUD = os.path.join(WORK, "kp_judgments.json")
R = 6371000.0
HW_KEEP = {"trunk", "primary", "secondary", "tertiary", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
           "unclassified", "residential", "motorway", "motorway_link"}  # motorway 只用來判斷照片是否在高架上


def is_elev(t):
    if t.get("bridge") not in (None, "", "no"): return True
    try: return int(str(t.get("layer", "0")).split(";")[0]) > 0
    except ValueError: return False


def seg_dist(p, a, b):
    """點 p 到線段 ab 的距離（m）。"""
    bx, by = xy(b[0], b[1], a[0], a[1]); px, py = xy(p[0], p[1], a[0], a[1]); L = bx * bx + by * by
    t = 0 if L == 0 else max(0, min(1, (px * bx + py * by) / L))
    return math.hypot(px - t * bx, py - t * by)


def way_len(w): return sum(dist(w["nodes"][i][1:], w["nodes"][i + 1][1:]) for i in range(len(w["nodes"]) - 1))


class Elev:
    """照片高架判斷：15 m 內最近道路是 bridge／layer>0 且目標 way 不是高架 → 該照片作廢。"""
    def __init__(s, ways):
        s.g = collections.defaultdict(list); s.byid = {w["id"]: w for w in ways}
        for w in ways:
            ns = w["nodes"]; e = is_elev(w["tags"])
            for i in range(len(ns) - 1):
                a, b = ns[i][1:], ns[i + 1][1:]
                for k in {(round(a[0] / 0.001), round(a[1] / 0.001)), (round(b[0] / 0.001), round(b[1] / 0.001))}:
                    s.g[k].append((a, b, e, w["id"]))
    def bad(s, p, way_id):
        tw = s.byid.get(way_id)
        if tw and is_elev(tw["tags"]): return False
        k = (round(p[0] / 0.001), round(p[1] / 0.001)); best = (ELEV_RAD + 1, False)
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for sa, sb, e, wid in s.g[(k[0] + a, k[1] + b)]:
                    d = seg_dist(p, sa, sb)
                    if d < best[0]: best = (d, e)
        return best[0] <= ELEV_RAD and best[1]
HW_JUDGE = {"trunk", "primary", "secondary", "tertiary"}
RANK = {"trunk": 0, "primary": 0, "secondary": 1, "tertiary": 2, "trunk_link": 1, "primary_link": 1, "secondary_link": 2,
        "tertiary_link": 2, "unclassified": 3, "residential": 3, "motorway": 0, "motorway_link": 1}
# 岔路候選（forks 表）的進入道路可判讀等級：號誌路口的 HW_JUDGE 之外，再加匝道與一般道路（不含 motorway：國道車道 OSM 多已有）
FORK_HW = HW_JUDGE | {"trunk_link", "primary_link", "secondary_link", "tertiary_link", "unclassified", "residential"}


REGION = os.environ.get("LANES_REGION", "taiwan")
RECHECK_DAYS = int(os.environ.get("LANES_RECHECK_DAYS", "7"))


def in_region(la, lo):
    """預設全台（含離島）；LANES_REGION=kp 時為高雄市＋屏東縣（近似框；排除台南與台東）。"""
    if REGION != "kp": return 21.8 <= la <= 26.5 and 118.0 <= lo <= 122.2
    if not (21.88 <= la <= 23.15 and 120.15 <= lo <= 120.92): return False
    if la > 22.90 and lo < 120.47: return False          # 台南（湖內／茄萣以北）
    if la > 23.0 and lo < 120.55: return False           # 台南南化/玉井一帶
    if lo > 120.86 and 22.25 <= la <= 22.62: return False  # 台東大武/達仁
    return True


def xy(la, lo, la0, lo0): return ((lo - lo0) * math.cos(math.radians(la0)) * math.pi / 180 * R, (la - la0) * math.pi / 180 * R)
def dist(a, b): x, y = xy(b[0], b[1], a[0], a[1]); return math.hypot(x, y)
def bearing(a, b): x, y = xy(b[0], b[1], a[0], a[1]); return math.degrees(math.atan2(x, y)) % 360
def adiff(a, b): return abs((a - b + 180) % 360 - 180)
def ymd(ms): return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d") if ms else ""
def today(): return datetime.date.today().isoformat()


def days_since(d):
    try: return (datetime.date.today() - datetime.date.fromisoformat(d)).days
    except Exception: return 99999


def needs_judge(k, a, J):
    """ready 且（沒判過，或判讀紀錄的標籤不是目前標籤＝有新照片重選過、要重判）。"""
    return a.get("status") == "ready" and (k not in J or J[k].get("label") != a.get("label"))


def jload(p, d):
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else d


def jsave(p, o): json.dump(o, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ---------------- extract ----------------
def extract():
    import struct, osmium
    data = open(SIG, "rb").read()
    _, _, _, n, rs, _, _, ts, _ = struct.unpack("<4sHHIHHII8s", data[:32])
    sigs = []
    for i in range(n):
        la, lo, fl = struct.unpack_from("<iiB", data, 32 + i * rs)
        la /= 1e6; lo /= 1e6
        if in_region(la, lo): sigs.append((la, lo, fl))
    near = set()
    for la, lo in [(a, b) for a, b, _ in sigs] + [(f["lat"], f["lon"]) for f in load_forks()]:
        k = (round(la / 0.003), round(lo / 0.003))
        for a in (-1, 0, 1):
            for b in (-1, 0, 1): near.add((k[0] + a, k[1] + b))

    class H(osmium.SimpleHandler):
        def __init__(s): super().__init__(); s.ways = []
        def way(s, w):
            if w.tags.get("highway") not in HW_KEEP: return
            try: nodes = [(nd.ref, nd.location.lat, nd.location.lon) for nd in w.nodes]
            except osmium.InvalidLocationError: return
            if any((round(la / 0.003), round(lo / 0.003)) in near for _, la, lo in nodes):
                s.ways.append({"id": w.id, "tags": dict(w.tags), "nodes": nodes})
    h = H(); h.apply_file(PBF, locations=True, idx="flex_mem")
    pickle.dump({"ways": h.ways, "signals": sigs, "sig_ts": ts}, open(PKL, "wb"))
    print("signals", len(sigs), "ways", len(h.ways))


# ---------------- helpers ----------------
def load_forks():
    """forks 表 → 每個（節點, 進入 way, 方向）一筆；kind＝vert（橋／隧道／高架與平面並存）或 link（匝道分流）。"""
    if not os.path.exists(FORKS): return []
    out = {}
    for r in csv.DictReader(open(FORKS, encoding="utf-8-sig")):
        if not r.get("進入方向"): continue   # 舊版表（2026-11-25 前）沒有進入方向欄 → 重跑 fork_sides.py
        k = (int(r["node"]), int(r["進入way"]), r["進入方向"])
        vert = r.get("立體種類") in ("bridge", "tunnel", "layer") or "link對立體" in r.get("收錄規則", "")
        f = out.setdefault(k, {"node": k[0], "way": k[1], "dir": k[2], "lat": float(r["lat"]), "lon": float(r["lon"]), "kind": "link"})
        if vert: f["kind"] = "vert"
    return list(out.values())


def fork_inters(ways, ride_hits, seen):
    """岔路候選：進入方向（停止線＝岔路節點）。tier：騎過 0、橋／平面並存 1、匝道分流 2；與號誌路口同一方向的不重複。"""
    byid = {w["id"]: w for w in ways}; res = []
    for f in load_forks():
        w = byid.get(f["way"]); c = (f["lat"], f["lon"])
        if not w: continue
        aps = [a for a in approaches(c, [w], rad=3) if a["dir"] == f["dir"] and a["hw"] in FORK_HW]
        aps = [a for a in aps if f'{a["way"]}:{a["dir"]}' not in seen]
        if not aps: continue
        for a in aps: seen.add(f'{a["way"]}:{a["dir"]}')
        rh = ride_hits(c)
        res.append({"c": c, "tier": 0 if rh else 1 if f["kind"] == "vert" else 2, "ride": rh, "aps": aps,
                    "rank": min(RANK[a["hw"]] for a in aps), "fork": f["node"], "kind": f["kind"]})
    return res



def cluster(sigs, rad=35):
    out = []; grid = collections.defaultdict(list)
    for la, lo, fl in sigs:
        if fl & 2 and not fl & 1: continue
        key = (round(la / 0.0005), round(lo / 0.0005)); hit = None
        for dk in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            for c in grid[(key[0] + dk[0], key[1] + dk[1])]:
                if dist((c[0], c[1]), (la, lo)) < rad: hit = c; break
            if hit: break
        if hit: hit[2].append((la, lo))
        else:
            c = [la, lo, [(la, lo)]]; out.append(c); grid[key].append(c)
    return [(sum(p[0] for p in c[2]) / len(c[2]), sum(p[1] for p in c[2]) / len(c[2])) for c in out]


def approaches(center, ways, rad=30):
    res = []
    for w in ways:
        t = w["tags"]; ns = w["nodes"]
        best = min(range(len(ns)), key=lambda i: dist(center, ns[i][1:]))
        if dist(center, ns[best][1:]) > rad: continue
        ow = t.get("oneway", "no")
        ow = "-1" if ow == "-1" else ("yes" if ow in ("yes", "1", "true") or t.get("junction") == "roundabout" else "no")
        for dirn, step in (("forward", -1), ("backward", 1)):
            if (ow == "yes" and dirn == "backward") or (ow == "-1" and dirn == "forward"): continue
            j = best; acc = 0; up = ns[best][1:]
            while 0 <= j + step < len(ns) and acc < 50:
                acc += dist(ns[j][1:], ns[j + step][1:]); j += step; up = ns[j][1:]
            if acc < 20: continue
            ends = (best == len(ns) - 1) if dirn == "forward" else (best == 0)
            if ow != "no":
                tl = t.get("turn:lanes") or ""; ln = t.get("lanes", "")
            else:
                tl = t.get(f"turn:lanes:{dirn}", ""); ln = t.get(f"lanes:{dirn}", "")
            res.append({"way": w["id"], "dir": dirn, "oneway": ow, "hw": t.get("highway"), "name": t.get("name", ""),
                        "stop": list(ns[best][1:]), "up": list(up), "brg": round(bearing(up, ns[best][1:]), 1), "ends_here": ends,
                        "osm_tl": tl, "osm_lanes": ln})
    return res


def ride_points():
    pts = []
    for g in glob.glob(os.path.join(REPORTS, "*", "**", "ride.gpx"), recursive=True):
        txt = open(g, encoding="utf-8", errors="replace").read()
        pts += [(float(a), float(b)) for a, b in re.findall(r'<trkpt[^>]*lat="([-\d.]+)"[^>]*lon="([-\d.]+)"', txt)]
    return pts


def cg(path, **p):
    from mly import graph
    fresh = p.pop("_fresh", False)  # 每週重查 pending：略過快取、覆寫同一快取檔
    k = hashlib.sha1((path + json.dumps(p, sort_keys=True)).encode()).hexdigest(); f = os.path.join(CACHE, k + ".json")
    if os.path.exists(f) and not fresh: return json.load(open(f, encoding="utf-8"))
    try: r = graph(path, **p)
    except RuntimeError as e: return {"error": str(e)[:200]}
    json.dump(r, open(f, "w", encoding="utf-8")); return r


def photo_along(im, ap):
    g = im.get("computed_geometry") or im.get("geometry"); hdg = im.get("computed_compass_angle", im.get("compass_angle"))
    if not g or hdg is None or im.get("is_pano"): return None
    p = (g["coordinates"][1], g["coordinates"][0]); s = ap["stop"]
    ux, uy = xy(s[0], s[1], ap["up"][0], ap["up"][1]); n = math.hypot(ux, uy) or 1; ux /= n; uy /= n
    dx, dy = xy(p[0], p[1], s[0], s[1]); along = -(dx * ux + dy * uy); lat = abs(dx * uy - dy * ux)
    if 20 <= along <= 45 and lat <= 9 and adiff(hdg, ap["brg"]) <= 30: return along
    return None


def daytime(ms):
    if not ms: return False
    h = (datetime.datetime.utcfromtimestamp(ms / 1000) + datetime.timedelta(hours=8)).hour
    return 7 <= h <= 16


def im_pt(im):
    g = im.get("computed_geometry") or im.get("geometry")
    return (g["coordinates"][1], g["coordinates"][0]) if g else None


def pick(ims, ap, elev=None):
    cand = [(photo_along(im, ap), im) for im in ims if daytime(im.get("captured_at"))]
    cand = [(a, im) for a, im in cand if a is not None and not (elev and elev.bad(im_pt(im), ap["way"]))]
    cand.sort(key=lambda t: -(t[1].get("captured_at") or 0))
    out = []
    for a, im in cand:  # 最新優先；第二張盡量不同序列、相距 ≥8 m
        if not out: out.append((a, im)); continue
        if im.get("sequence") != out[0][1].get("sequence") and abs(a - out[0][0]) >= 3: out.append((a, im)); break
    if out and len(out) < 2:
        for a, im in cand:
            if im["id"] not in [o[1]["id"] for o in out] and abs(a - out[0][0]) >= 8: out.append((a, im)); break
    return out, len(cand)


# ---------------- select ----------------
def select(limit=150, max_queries=900):
    if os.path.exists(FORKS) and (not os.path.exists(PKL) or os.path.getmtime(FORKS) > os.path.getmtime(PKL)):
        print("forks 表比 kp_osm.pkl 新 → 重跑 extract"); extract()
    D = pickle.load(open(PKL, "rb")); ways = D["ways"]; elev = Elev(ways)
    grid = collections.defaultdict(list)
    for w in ways:
        for _, la, lo in w["nodes"]: grid[(round(la / 0.003), round(lo / 0.003))].append(w)
    def near_ways(c):
        k = (round(c[0] / 0.003), round(c[1] / 0.003)); s = {}
        for a in (-1, 0, 1):
            for b in (-1, 0, 1):
                for w in grid[(k[0] + a, k[1] + b)]: s[w["id"]] = w
        return list(s.values())
    rp = ride_points(); rgrid = collections.defaultdict(list)
    for p in rp: rgrid[(round(p[0] / 0.001), round(p[1] / 0.001))].append(p)
    def ride_hits(c):
        k = (round(c[0] / 0.001), round(c[1] / 0.001))
        return sum(1 for a in (-1, 0, 1) for b in (-1, 0, 1) for p in rgrid[(k[0] + a, k[1] + b)] if dist(c, p) <= 30)
    inters = []
    for c in cluster(D["signals"]):
        aps = [a for a in approaches(c, near_ways(c)) if a["hw"] in HW_JUDGE]
        if not aps: continue
        major = {a["name"] or a["way"] for a in aps if a["hw"] in ("trunk", "primary")}
        sec = {a["name"] or a["way"] for a in aps if a["hw"] in ("trunk", "primary", "secondary")}
        rh = ride_hits(c)
        tier = 0 if rh else 1 if len(major) >= 2 else 2 if len(sec) >= 2 else 3
        if tier == 3: continue
        inters.append({"c": c, "tier": tier, "ride": rh, "aps": aps, "rank": min(RANK[a["hw"]] for a in aps)})
    seen = {f'{a["way"]}:{a["dir"]}' for it in inters for a in it["aps"]}
    fi = fork_inters(ways, ride_hits, seen); inters += fi
    print("fork candidates (entry directions):", sum(len(i["aps"]) for i in fi), collections.Counter(i["kind"] for i in fi))
    inters.sort(key=lambda i: (i["tier"], -i["ride"], i["rank"]))
    print("intersections tier0/1/2:", collections.Counter(i["tier"] for i in inters))
    man = jload(MAN, {"approaches": {}, "runs": []}); A = man["approaches"]; judged = jload(JUD, {})
    seg_only = os.environ.get("LANES_SEG_ONLY") == "1"
    from kp_seg import SEG_SHARE, select_segments
    seg_lim = limit if seg_only else limit // SEG_SHARE; seg_q = max_queries if seg_only else max_queries // SEG_SHARE
    limit -= seg_lim; max_queries -= seg_q
    if seg_only: inters = []
    q = 0; ready = sum(1 for k, a in A.items() if needs_judge(k, a, judged) and a.get("kind") != "segment")
    # 兩階段：先重查到期的 pending／衝突（最久沒查的優先，最多用 1/3 查詢額度），再查從沒查過的新路口。
    # 已在 RECHECK_DAYS 內查過的不再花查詢；accepted 與已備妥的方向不重查。
    rq, newq = [], []
    for it in inters:
        keys = [f'{a["way"]}:{a["dir"]}' for a in it["aps"]]
        todo = [(a, k) for a, k in zip(it["aps"], keys) if A.get(k, {}).get("status") not in ("ready", "accepted")]
        if not todo: continue
        if any(k not in A for _, k in todo): newq.append((it, todo))
        else:
            age = min(days_since(A[k].get("checked", "")) for _, k in todo)
            if age >= RECHECK_DAYS: rq.append((age, it, todo))
    rq.sort(key=lambda t: -t[0])
    plan = [(it, todo, max_queries // 3) for _, it, todo in rq] + [(it, todo, max_queries) for it, todo in newq]
    print("recheck due", len(rq), "new intersections", len(newq))
    for it, todo, qcap in plan:
        if ready >= limit or q >= max_queries: break
        if q >= qcap: continue
        c = it["c"]; d = 0.0007
        fresh = any(k in A and A[k].get("checked") != today() for _, k in todo)
        r = cg("images", bbox=f"{c[1]-d},{c[0]-d},{c[1]+d},{c[0]+d}", fields="id,captured_at,is_pano,compass_angle,computed_compass_angle,computed_geometry,geometry,sequence", limit=2000, **({"_fresh": True} if fresh else {}))
        q += 1; ims = r.get("data", [])
        for a, k in todo:
            ps, nc = pick(ims, a, elev); old = A.get(k, {})
            newest = max([im.get("captured_at") or 0 for _, im in ps], default=0)
            base = {"way": a["way"], "dir": a["dir"], "hw": a["hw"], "name": a["name"], "oneway": a["oneway"], "brg": a["brg"],
                    "lat": round(a["stop"][0], 6), "lon": round(a["stop"][1], 6), "ends_here": a["ends_here"], "osm_tl": a["osm_tl"],
                    "osm_lanes": a["osm_lanes"], "tier": it["tier"], "ride": it["ride"], "checked": today(), "n_cand": nc}
            if len(ps) >= 2 and (k not in judged or newest > old.get("newest", 0)):
                if k in judged: base["prev_label"] = judged[k].get("label")  # 有新照片 → 重判（新標籤）
                base.update(status="ready", imgs=[im["id"] for _, im in ps], caps=[f"{round(al)}m {ymd(im.get('captured_at'))}" for al, im in ps],
                            captured=[ymd(im.get("captured_at")) for _, im in ps], newest=newest)
                A[k] = base; ready += 1
                if ready >= limit: break
            elif k in judged and old.get("status") in ("judged", "conflict", "lanes_only"):
                old["checked"] = today()
            else:
                base.update(status="pending", newest=newest or old.get("newest", 0)); A[k] = {**old, **base}
    seg_ready0 = sum(1 for k, a in A.items() if needs_judge(k, a, judged) and a.get("kind") == "segment")
    sq, sr = select_segments(A, judged, max(0, seg_lim - seg_ready0), seg_q, ways, ride_hits)
    man["runs"].append({"date": today(), "region": REGION, "queries": q, "ready": ready, "seg_queries": sq, "seg_ready": sr}); jsave(MAN, man)
    print("queries", q, "ready", ready, "pending", sum(1 for a in A.values() if a["status"] == "pending"))


# ---------------- sheets ----------------
def thumb(iid):
    from mly import graph, fetch_bytes
    f = os.path.join(IMG, f"{iid}.jpg")
    if not os.path.exists(f):
        r = graph(f"{iid}", fields="thumb_2048_url,thumb_1024_url")
        url = r.get("thumb_2048_url") or r.get("thumb_1024_url")
        if not url:  # 照片已下架或無縮圖：略過這張，不讓整批失敗
            return None
        open(f, "wb").write(fetch_bytes(url))
    return f


def sheets(per=4):
    from PIL import Image, ImageDraw
    man = jload(MAN, {}); judged = jload(JUD, {})
    A = man["approaches"]
    # 修復舊版編號 bug（標籤用「已編號數＋1」，重選時標籤被清掉就會撞號）：待判讀方向的標籤若與其他方向或其他方向的判讀紀錄重複，
    # 清掉標籤與拼圖，下面重新編號、重拼。新標籤一律取「manifest 與判讀紀錄中最大編號＋1」。
    owners = collections.defaultdict(set)
    for k, a in A.items():
        if a.get("label"): owners[a["label"]].add(k)
    for k, j in judged.items():
        if j.get("label"): owners[j["label"]].add(k)
    fixed = 0
    for k, a in A.items():
        if needs_judge(k, a, judged) and a.get("label") and len(owners[a["label"]]) > 1:
            a.pop("label"); a.pop("sheet", None); fixed += 1
    if fixed: print("撞號重新編號", fixed)
    # 路口方向：標籤 A###、拼圖 S###；路段點（kind=segment）：標籤 B###、拼圖 G###，裁切保留較多路面以看分隔島與機慢車道
    for seg in (False, True):
        sheets_kind(A, judged, owners, per, seg)
    jsave(MAN, man)


def sheets_kind(A, judged, owners, per, seg):
    from PIL import Image, ImageDraw
    LP, SP, top = ("B", "G", .35) if seg else ("A", "S", .45)
    nums = [int(l[1:]) for l in owners if re.fullmatch(LP + r"\d+", l)]
    nxt = [max(nums, default=0) + 1]
    def new_label():
        l = f"{LP}{nxt[0]:03d}"; nxt[0] += 1; return l
    todo = [(k, a) for k, a in A.items() if needs_judge(k, a, judged) and not a.get("sheet") and (a.get("kind") == "segment") == seg]
    todo.sort(key=lambda t: (t[1]["tier"], -t[1]["ride"], RANK[t[1]["hw"]]))
    n0 = len(glob.glob(os.path.join(SHEET, SP + "*.jpg"))); W = 760
    for s in range(0, len(todo), per):
        rows = []
        for k, a in todo[s:s + per]:
            lab = a.get("label") or new_label(); a["label"] = lab
            tiles = []
            for iid, cap in zip(a["imgs"][:2], a["caps"][:2]):
                try:
                    tf = thumb(iid)
                    im = Image.open(tf).convert("RGB") if tf else None
                except Exception:  # 快取檔損壞或下載失敗：刪掉快取，略過這張
                    try:
                        os.remove(os.path.join(IMG, f"{iid}.jpg"))
                    except OSError:
                        pass
                    im = None
                if im is None:
                    continue
                w, h = im.size
                im = im.crop((int(w * .08), int(h * top), int(w * .92), int(h * .90)))
                im = im.resize((W, int(im.size[1] * W / im.size[0])), Image.LANCZOS); tiles.append((im, cap))
            if not tiles:  # 兩張照片都拿不到：留待下週重查
                a["status"] = "pending"; a.pop("label", None); continue
            hh = max(t[0].size[1] for t in tiles) + 20; row = Image.new("RGB", (W * 2 + 8, hh), (40, 40, 40)); d = ImageDraw.Draw(row)
            for i, (t, cap) in enumerate(tiles):
                row.paste(t, (i * (W + 8), 20)); d.text((i * (W + 8) + 4, 4), f"{lab}{'ab'[i]}  {a['hw']} {a['name']} brg={round(a['brg'])}  {cap}", fill=(255, 255, 0))
            rows.append(row)
        H = sum(r.size[1] for r in rows) + 6 * len(rows); sh = Image.new("RGB", (W * 2 + 8, H), (0, 0, 0)); y = 0
        for r in rows: sh.paste(r, (0, y)); y += r.size[1] + 6
        name = f"{SP}{n0 + s // per + 1:03d}.jpg"; sh.save(os.path.join(SHEET, name), quality=90)
        for k, a in todo[s:s + per]: a["sheet"] = name
        print(name, [a["label"] for _, a in todo[s:s + per]])


# ---------------- decide ----------------
def norm_tl(s):
    return "|".join("" if x.strip() in ("", "none") else ";".join(sorted(x.strip().split(";"), key=lambda v: ["reverse", "left", "slight_left", "through", "slight_right", "right"].index(v) if v in ["reverse", "left", "slight_left", "through", "slight_right", "right"] else 9)) for x in s.split("|")) if s else ""


def gov_dir_lanes():
    g = {}
    if not os.path.exists(GOV): return g
    for r in csv.DictReader(open(GOV, encoding="utf-8")):
        if r.get("confidence") == "low": continue
        g[r["osm_way_id"]] = r
    return g


def tl_full(tl, n):
    """turn:lanes 的每一條車道都有箭頭、且條數＝n。"""
    if not tl or not n: return False
    segs = norm_tl(tl).split("|")
    return len(segs) == n and all(segs)


def tl_compatible(tl, ref):
    """tl 不與 ref 矛盾：空、或條數相同且每個有值的車道都等於 ref。"""
    if not tl or not any(x for x in norm_tl(tl).split("|")): return True
    a, b = norm_tl(tl).split("|"), norm_tl(ref).split("|")
    return len(a) == len(b) and all(not x or x == y for x, y in zip(a, b))


def judge_one(pa, pb):
    """回傳 (lanes, turn_lanes, tl_conf)。規則：
    兩張都清楚且 turn:lanes 一致 → medium；一張清楚（所有車道箭頭可見）、另一張不矛盾（車道數相同或看不清、箭頭不衝突）→ low；
    否則只看兩張車道數是否一致（只收 lanes）。"""
    la, lb = pa.get("lanes"), pb.get("lanes")
    if la and la == lb and pa.get("vis") == "clear" and pb.get("vis") == "clear" and pa.get("tl") \
            and norm_tl(pa["tl"]) == norm_tl(pb.get("tl", "")) and len(pa["tl"].split("|")) == la and any(x for x in norm_tl(pa["tl"]).split("|")):
        return la, norm_tl(pa["tl"]), "medium"
    for c, o in ((pa, pb), (pb, pa)):
        n = c.get("lanes")
        if c.get("vis") == "clear" and tl_full(c.get("tl"), n) and o.get("lanes") in (None, n) and tl_compatible(o.get("tl", ""), c["tl"]):
            return n, norm_tl(c["tl"]), "low"
    return (la if la and la == lb else None), None, None


def moto_one(pa, pb, f):
    """路口機車左轉線索（two_stage／moto_ban_inner）兩張合併。舊判讀沒有這欄視為 unclear。
    兩張同為 yes／no → medium；一張 yes／no、另一張 unclear → low；yes 與 no 衝突或都 unclear → unclear。"""
    va, vb = (p.get(f) if p.get(f) in ("yes", "no") else "unclear" for p in (pa, pb))
    if va == vb: return (va, "medium") if va != "unclear" else ("unclear", "")
    if "unclear" in (va, vb): return (va if vb == "unclear" else vb), "low"
    return "unclear", ""


def decide():
    man = jload(MAN, {}); A = man["approaches"]; J = jload(JUD, {}); G = gov_dir_lanes()
    moto_rows = []   # 路口機車左轉線索 → mapillary_ai_moto_left.csv（給 build_moto_left_turn.py）
    WL = {w["id"]: way_len(w) for w in pickle.load(open(PKL, "rb"))["ways"]} if os.path.exists(PKL) else {}
    from kp_seg import seg_judge, seg_note, write_segments
    rows, conflicts, stats = [], [], collections.Counter(); seg_rows = []
    for k, j in J.items():
        a = A.get(k)
        if not a: continue
        if needs_judge(k, a, J): stats["awaiting_rejudge"] += 1; continue  # 新照片已備妥、舊判讀不套用到新照片
        note = j.get("note", "")
        if a.get("kind") == "segment":   # 路段點：只收車道數（不判箭頭），兩張一致才收，信心見 kp_seg.seg_judge
            lanes, conf = seg_judge(j["a"], j["b"])
            if not lanes:
                a["status"] = "pending"; a["reason"] = "兩張照片車道數不一致或看不清"; stats["seg_pending"] += 1; continue
            why = []
            if a["osm_lanes"] and a["oneway"] != "no" and str(a["osm_lanes"]).split(";")[0] != str(lanes): why.append(f"OSM lanes={a['osm_lanes']}")
            g = G.get(str(a["way"]))
            if g:
                gv = g["lanes"] if a["oneway"] != "no" else g.get(f"lanes_{a['dir']}", "")
                if gv and str(int(float(gv))) != str(lanes): why.append(f"gov {g['source']}={gv}")
            if why:
                a["status"] = "conflict"; conflicts.append({"key": k, "label": a.get("label"), "lanes": lanes, "turn_lanes": "", "why": "; ".join(why), "lat": a["lat"], "lon": a["lon"]})
                stats["seg_conflict"] += 1; continue
            a["status"] = "accepted"; a.pop("reason", None); stats[f"seg_accepted_{conf}"] += 1
            seg_rows.append({"osm_way_id": a["way"], "direction": a["dir"], "lanes": lanes, "turn_lanes": "", "confidence": conf,
                             "image_ids": " ".join(a["imgs"]), "captured_at": " ".join(a["captured"]), "note": seg_note(j)})
            continue
        ts, tsc = moto_one(j["a"], j["b"], "two_stage"); mb, mbc = moto_one(j["a"], j["b"], "moto_ban_inner")
        if ts != "unclear" or mb != "unclear":   # 與車道數是否採用無關，路口標誌另外輸出
            moto_rows.append({"osm_way_id": a["way"], "direction": a["dir"], "lat": a["lat"], "lon": a["lon"],
                              "two_stage": ts, "two_stage_conf": tsc, "moto_ban_inner": mb, "moto_ban_inner_conf": mbc,
                              "image_ids": " ".join(a["imgs"]), "captured_at": " ".join(a["captured"])})
            stats[f"moto_two_stage_{ts}_{tsc}"] += 1; stats[f"moto_ban_inner_{mb}_{mbc}"] += 1
        lanes, tl, tlc = judge_one(j["a"], j["b"])
        if not lanes:
            a["status"] = "pending"; a["reason"] = "兩張照片車道數不一致或看不清"; stats["pending"] += 1; continue
        wl = WL.get(a["way"])
        if wl and wl > MAX_WAY_M:
            a["status"] = "pending"; a["reason"] = f"way 過長需切分（{round(wl)} m）"; stats["pending_long_way"] += 1; continue
        why = []
        if a["osm_lanes"] and str(a["osm_lanes"]).split(";")[0] != str(lanes): why.append(f"OSM lanes={a['osm_lanes']}")
        if a["osm_tl"] and tl and norm_tl(a["osm_tl"]) != tl: why.append(f"OSM turn:lanes={a['osm_tl']}")
        g = G.get(str(a["way"]))
        if g:
            gv = g["lanes"] if a["oneway"] != "no" else g.get(f"lanes_{a['dir']}", "")
            if gv and str(int(float(gv))) != str(lanes): why.append(f"gov {g['source']}={gv}")
        if why:
            a["status"] = "conflict"; conflicts.append({"key": k, "label": a.get("label"), "lanes": lanes, "turn_lanes": tl or "", "why": "; ".join(why), "lat": a["lat"], "lon": a["lon"]})
            stats["conflict"] += 1; continue
        if tl and not a["ends_here"]:
            note = (note + "; " if note else "") + "way 未在此路口斷開，turn:lanes 不收"; tl = None
        a["status"] = "accepted"; a.pop("reason", None)
        rows.append({"osm_way_id": a["way"], "direction": a["dir"], "lanes": lanes, "turn_lanes": tl or "",
                     "confidence": tlc if tl else "low", "image_ids": " ".join(a["imgs"]), "captured_at": " ".join(a["captured"]), "note": note})
        stats[f"accepted_tl_{tlc}" if tl else "accepted_lanes"] += 1
    # 同一 way+direction 多路口：值不同就只留一致的部分
    by = collections.defaultdict(list)
    for r in rows: by[(r["osm_way_id"], r["direction"])].append(r)
    final = []
    for key, rs in by.items():
        if len({r["lanes"] for r in rs}) > 1: stats["dup_dropped"] += 1; continue
        r = dict(rs[0])
        if len({x["turn_lanes"] for x in rs}) > 1: r["turn_lanes"] = ""; r["confidence"] = "low"
        elif r["turn_lanes"]: r["confidence"] = min((x["confidence"] for x in rs), key=["low", "medium"].index)
        final.append(r)
    os.makedirs(OUTD, exist_ok=True)
    with open(os.path.join(OUTD, "mapillary_ai_lanes.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, ["osm_way_id", "direction", "lanes", "turn_lanes", "confidence", "image_ids", "captured_at", "note"]); w.writeheader(); w.writerows(sorted(final, key=lambda r: (int(r["osm_way_id"]), r["direction"])))
    with open(os.path.join(OUTD, "mapillary_ai_moto_left.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, ["osm_way_id", "direction", "lat", "lon", "two_stage", "two_stage_conf", "moto_ban_inner",
                               "moto_ban_inner_conf", "image_ids", "captured_at"])
        w.writeheader(); w.writerows(sorted(moto_rows, key=lambda r: (int(r["osm_way_id"]), r["direction"], r["lat"])))
    stats["moto_rows"] = len(moto_rows)
    write_segments(seg_rows, OUTD, stats)
    pend = [a for a in A.values() if a["status"] in ("pending", "ready")]
    with open(os.path.join(OUTD, "mapillary_pending.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["osm_way_id", "direction", "lat", "lon", "last_checked", "reason"])
        for a in sorted(pend, key=lambda a: (a["tier"], a["way"])):
            w.writerow([a["way"], a["dir"], a["lat"], a["lon"], a["checked"], a.get("reason") or ("照片已備妥待判讀" if a["status"] == "ready" else "缺少 2 張合格照片（20–45 m、朝路口、白天、非高架）")])
    jsave(os.path.join(WORK, "kp_conflicts.json"), conflicts); jsave(MAN, man)
    stats["rows"] = len(final); stats["rows_tl_medium"] = sum(1 for r in final if r["turn_lanes"] and r["confidence"] == "medium")
    stats["rows_tl_low"] = sum(1 for r in final if r["turn_lanes"] and r["confidence"] == "low"); stats["rows_lanes_only"] = sum(1 for r in final if not r["turn_lanes"])
    stats["pending_total"] = len(pend)
    print(dict(stats)); jsave(os.path.join(WORK, "kp_stats.json"), dict(stats))


def photo_index():
    """從 Mapillary 查詢快取建 照片 id → 座標。"""
    idx = {}
    for f in glob.glob(os.path.join(CACHE, "*.json")):
        try: d = json.load(open(f, encoding="utf-8"))
        except Exception: continue
        for im in d.get("data", []) if isinstance(d, dict) else []:
            p = im_pt(im)
            if p: idx[im["id"]] = p
    return idx


def elevcheck():
    """已備妥／已判讀的方向：兩張照片任一張拍在高架上（或判讀註記為高架誤配）→ 作廢判讀、改列 pending。"""
    man = jload(MAN, {}); A = man["approaches"]; J = jload(JUD, {}); inv = jload(os.path.join(WORK, "kp_invalid.json"), {})
    elev = Elev(pickle.load(open(PKL, "rb"))["ways"]); idx = photo_index(); n = collections.Counter()
    for k, a in A.items():
        if a["status"] not in ("ready", "pending", "judged", "conflict", "lanes_only", "accepted") or not a.get("imgs"): continue
        if a["status"] == "pending" and k not in J: continue
        if a.get("kind") == "segment": continue   # 路段點在 select 時已用（缺口 way＋路口 way）高架規則篩過；這裡的 pkl 不含缺口 way
        bad = [i for i in a["imgs"] if i in idx and elev.bad(idx[i], a["way"])]
        noted = "高架道路上" in J.get(k, {}).get("note", "")
        if not bad and not noted: continue
        if k in J: inv[k] = {**J.pop(k), "invalid": "照片拍在高架（" + ("規則" if bad else "") + ("註記" if noted else "") + "）"}; n["judged_invalid"] += 1
        else: n["ready_invalid"] += 1
        a["status"] = "pending"; a["reason"] = "照片拍在高架上，已作廢待重選"; a["elev_imgs"] = bad
        for f in ("imgs", "caps", "captured"): a.pop(f, None)
        a["newest"] = 0
    jsave(MAN, man); jsave(JUD, J); jsave(os.path.join(WORK, "kp_invalid.json"), inv); print(dict(n), "photo_index", len(idx))

def forkcands():
    """只統計岔路候選數（不查 Mapillary）。"""
    D = pickle.load(open(PKL, "rb")); fk = load_forks()
    fi = fork_inters(D["ways"], lambda c: 0, set())   # 與號誌方向的去重在 select 做；這裡列全部岔路候選
    n = sum(len(i["aps"]) for i in fi)
    print("forks 表進入方向", len(fk), "可判讀候選", n, collections.Counter(i["kind"] for i in fi), "其中 way 未在 pkl／非可判讀等級／上游不足 20 m", len(fk) - n)


if __name__ == "__main__":
    m = sys.argv[1]
    if m == "extract": extract()
    elif m == "select": select(int(sys.argv[2]) if len(sys.argv) > 2 else 150)
    elif m == "sheets": sheets()
    elif m == "decide": decide()
    elif m == "elevcheck": elevcheck()
    elif m == "forkcands": forkcands()
    elif m == "segextract": __import__("kp_seg").segextract()
    elif m == "segcands": __import__("kp_seg").segcands(fetch="--nofetch" not in sys.argv)
