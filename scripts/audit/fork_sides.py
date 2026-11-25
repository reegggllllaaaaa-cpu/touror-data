"""靠左／靠右稽核：枚舉「一邊上橋／高架／匝道／隧道、另一邊留在平面」的岔路，比對 Valhalla 的左右判斷與幾何。

在 WSL 跑（需要 pyosmium 與 pyvalhalla，例：/root/v2venv/bin/python）：
  python fork_sides.py --pbf /root/rga/osm.pbf --tiles /root/rga/valhalla_tiles.tar --region kaohsiung --out <輸出資料夾>
  python fork_sides.py ... --region taiwan
  python fork_sides.py ... --find-name 松寮陸橋     # 只列出某名稱的 way（找案例用）

做法（全量，不抽樣）：
 1. pyosmium 讀出區域內所有汽機車道路（motorway…residential、*_link、living_street），保留 bridge/tunnel/layer/oneway 等標籤。
 2. 每個節點、每條「進入」方向：找出偏離進入方位 ≤135° 的所有「離開」分支（照 oneway）。兩兩配對，其中一支沿路 200 m 內
    走到 立體（bridge／tunnel／layer≠0／*_link），另一支 100 m 內都在平面、且兩支在 50 m 處的夾角 ≤45° → 算一個岔路。
    「平面」：原規則不含 *_link；2026-11-25 補：若立體那支真的走到 bridge／tunnel／layer，另一支是平面 *_link（迴轉道）也算。
    舊版漏收（九如一路東行 2222785405：右轉約 90° 後才分成上橋 219634146→212429277 與迴轉道 tertiary_link 212429268）：
    ①分支偏離 >60° 整個不看；②迴轉道是 *_link 被當成立體，兩支都「立體」不成對。輸出「收錄規則」欄標新規則收錄的。
 3. 幾何判斷：兩支各沿路走 50 m 取點，相對進入方位的有號角較小的是左邊（差 <3° 記「無法判斷」）。
 4. Valhalla：用 App 的機車 costing，從岔路前（進入路段往回最多 40 m）算到目標分支 120 m 處（帶 heading），
    找起點在岔路節點（≤20 m）的 maneuver，用 App 的 GuidanceClassifier.side(of:) 規則轉成左／右／直行／無提示。
 5. 兩個方向都測（往立體那支、往平面那支），各算一筆。

輸出：forks.csv（每筆一列）＋ summary.json。
"""
import argparse, csv, json, math, os, pickle, sys, tempfile, time
from collections import defaultdict

REGIONS = {
    "kaohsiung": (22.45, 120.15, 23.30, 121.05),   # 高雄市行政區外接框（含少量臺南／屏東邊界）
    "taiwan": (21.8, 119.2, 25.4, 122.1),
}
DRIVE = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
         "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
VERT = {"bridge", "tunnel", "layer"}
MAX_TURN = 135   # 分支相對進入方位的上限（2026-11-25 由 60° 放寬：右轉後才分岔、兩支並排的路口原本整個被排除）
COSTING = "motorcycle"
COSTING_OPTIONS = {"exclude_highways": True, "use_tracks": 0, "use_trails": 0}   # 同 curated/cases.json（App 機車 costing）

# Valhalla maneuver type → App GuidanceClassifier.side(of:)（App/GuidanceClassifier.swift:332-341）
RIGHT_T = {9, 10, 11, 12, 18, 20, 23, 37, 2, 5}      # slightRight right sharpRight uturnRight rampRight exitRight stayRight mergeRight startRight destinationRight
LEFT_T = {16, 15, 14, 13, 19, 21, 24, 38, 3, 6}      # slightLeft left sharpLeft uturnLeft rampLeft exitLeft stayLeft mergeLeft startLeft destinationLeft
STRAIGHT_T = {8, 17, 22, 25}                          # continue rampStraight stayStraight merge
TYPE_NAME = {8: "continue", 9: "slight_right", 10: "right", 11: "sharp_right", 15: "left", 16: "slight_left",
             17: "ramp_straight", 18: "ramp_right", 19: "ramp_left", 20: "exit_right", 21: "exit_left",
             22: "stay_straight", 23: "stay_right", 24: "stay_left", 25: "merge", 37: "merge_right", 38: "merge_left"}


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def bearing(a, b):
    la1, la2 = math.radians(a[0]), math.radians(b[0]); dl = math.radians(b[1] - a[1])
    y = math.sin(dl) * math.cos(la2); x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def rel(h, ref):
    return (h - ref + 540) % 360 - 180   # 負＝左、正＝右


def seg_dist(p, a, b):
    """點到線段距離（公尺，局部平面近似）"""
    k = math.cos(math.radians(p[0])) * 111320.0
    ax, ay = (a[1] - p[1]) * k, (a[0] - p[0]) * 110540.0
    bx, by = (b[1] - p[1]) * k, (b[0] - p[0]) * 110540.0
    dx, dy = bx - ax, by - ay; L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, -(ax * dx + ay * dy) / L))
    return math.hypot(ax + t * dx, ay + t * dy)


def interp(a, b, f):
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)


# ---------------------------------------------------------------- 1. 讀路網
def extract(pbf, bbox, cache):
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    import osmium
    s, w, n, e = bbox
    ways = []

    class H(osmium.SimpleHandler):
        def way(self, wy):
            hw = wy.tags.get("highway")
            if hw not in DRIVE:
                return
            if wy.tags.get("area") == "yes" or wy.tags.get("access") in ("no", "private") and wy.tags.get("motorcycle") not in ("yes", "designated"):
                return
            try:
                pts = [(nd.ref, nd.lat, nd.lon) for nd in wy.nodes]
            except osmium.InvalidLocationError:
                return
            if not any(s <= p[1] <= n and w <= p[2] <= e for p in pts):
                return
            t = {k: wy.tags.get(k) for k in ("highway", "name", "bridge", "tunnel", "layer", "oneway", "junction",
                                             "lanes", "turn:lanes", "turn:lanes:forward", "placement", "bridge:name") if wy.tags.get(k)}
            ways.append((wy.id, t, pts))

    H().apply_file(pbf, locations=True, idx="flex_mem")
    pickle.dump(ways, open(cache, "wb"))
    return ways


def wclass(t):
    """way 的立體等級：'bridge'/'tunnel'/'layer'/'link' 或 None（平面）"""
    if t.get("bridge") and t["bridge"] != "no":
        return "bridge"
    if t.get("tunnel") and t["tunnel"] != "no":
        return "tunnel"
    try:
        if int(float(t.get("layer", "0"))) != 0:
            return "layer"
    except ValueError:
        pass
    if t.get("highway", "").endswith("_link"):
        return "link"
    return None


def oneway_dir(t):
    o = t.get("oneway")
    if o in ("yes", "1", "true"):
        return 1
    if o == "-1":
        return -1
    if o == "no":
        return 0
    if t.get("junction") in ("roundabout", "circular") or t.get("highway") in ("motorway",):
        return 1
    return 0


# ---------------------------------------------------------------- 2. 圖
class Graph:
    def __init__(self, ways):
        self.ways = ways
        self.loc = {}
        self.out = defaultdict(list)   # node -> [(wi, pos, step)]  step=+1/-1
        for wi, (wid, t, pts) in enumerate(ways):
            ow = oneway_dir(t)
            for i, (nid, la, lo) in enumerate(pts):
                self.loc[nid] = (la, lo)
                if i + 1 < len(pts) and ow >= 0:
                    self.out[nid].append((wi, i, 1))
                if i > 0 and ow <= 0:
                    self.out[nid].append((wi, i, -1))

    def walk(self, wi, pos, step, dist, avoid_back=None):
        """從 ways[wi] 的 pos 沿 step 走 dist 公尺；到 way 端點時接轉向最小（<50°）的下一條。
        回傳 (點列, 每點累積距離, 經過的 (wi, 該段起點累積距離) 列)"""
        pts = [self.loc[self.ways[wi][2][pos][0]]]; acc = [0.0]; segs = [(wi, 0.0)]
        cur_w, cur_p, st = wi, pos, step; d = 0.0; guard = 0
        while d < dist and guard < 400:
            guard += 1
            wpts = self.ways[cur_w][2]
            nxt = cur_p + st
            if 0 <= nxt < len(wpts):
                p = (wpts[nxt][1], wpts[nxt][2]); seg = hav(pts[-1], p); d += seg
                pts.append(p); acc.append(d); cur_p = nxt
                continue
            # 端點：找下一條
            nid = wpts[cur_p][0]
            h_in = bearing(pts[-2], pts[-1]) if len(pts) > 1 else None
            best = None
            for (w2, p2, s2) in self.out.get(nid, ()):
                if w2 == cur_w:
                    continue
                q = self.ways[w2][2][p2 + s2]
                h = bearing(pts[-1], (q[1], q[2]))
                r = abs(rel(h, h_in)) if h_in is not None else 0
                if r < 50 and (best is None or r < best[0]):
                    best = (r, w2, p2, s2)
            if best is None:
                break
            _, cur_w, cur_p, st = best
            segs.append((cur_w, d))
        return pts, acc, segs

    def point_at(self, pts, acc, m):
        for i in range(1, len(pts)):
            if acc[i] >= m:
                seg = acc[i] - acc[i - 1]
                return interp(pts[i - 1], pts[i], (m - acc[i - 1]) / seg if seg > 0 else 0)
        return pts[-1]


def branch_info(g, wi, pos, step):
    pts, acc, segs = g.walk(wi, pos, step, 200)
    cls200 = [(wclass(g.ways[w][1]), d0) for (w, d0) in segs]
    elev_at = next((d0 for c, d0 in cls200 if c), None)
    vert_at = next((d0 for c, d0 in cls200 if c in VERT), None)   # 真的離開平面（bridge／tunnel／layer），不含 *_link
    return {"pts": pts, "acc": acc, "segs": segs, "elev_at": elev_at, "vert_at": vert_at,
            "elev_kind": next((c for c, d0 in cls200 if c), None), "len": acc[-1]}


def find_forks(g, bbox):
    s, w, n, e = bbox
    forks = []
    for nid, outs in list(g.out.items()):
        if len(outs) < 2:
            continue
        la, lo = g.loc[nid]
        if not (s <= la <= n and w <= lo <= e):
            continue
        # 進入方向：所有可走到 nid 的段
        for (iwi, ipos, istep) in g.inc.get(nid, []):
            ipts = g.ways[iwi][2]
            prev = ipts[ipos - istep]
            h_in = bearing((prev[1], prev[2]), (la, lo))
            if wclass(g.ways[iwi][1]):
                continue   # 已經在橋／匝道上：不算「平面分岔」
            cands = []
            for (wi, pos, step) in outs:
                if wi == iwi and step == -istep:
                    continue   # 掉頭
                q = g.ways[wi][2][pos + step]
                r = rel(bearing((la, lo), (q[1], q[2])), h_in)
                if abs(r) <= MAX_TURN:
                    cands.append((wi, pos, step))
            if len(cands) < 2:
                continue
            infos = [branch_info(g, *c) for c in cands]
            for ia in range(len(cands)):
                A = infos[ia]
                if A["elev_at"] is None:
                    continue
                for ib in range(len(cands)):
                    B = infos[ib]
                    if ib == ia or B["len"] < 30:
                        continue
                    old_rule = B["elev_at"] is None or B["elev_at"] >= 100
                    # 2026-11-25 補：A 真的上橋／下隧道、B 是平面 *_link（迴轉道、平面匝道）也算（九如一路 2222785405 漏收）
                    link_rule = A["vert_at"] is not None and (B["vert_at"] is None or B["vert_at"] >= 100)
                    if not (old_rule or link_rule):
                        continue
                    pa = g.point_at(A["pts"], A["acc"], 50); pb = g.point_at(B["pts"], B["acc"], 50)
                    ra = rel(bearing((la, lo), pa), h_in); rb = rel(bearing((la, lo), pb), h_in)
                    if abs(ra - rb) > 45:
                        continue
                    why = []
                    if max(abs(ra), abs(rb)) > 60: why.append("轉彎後並排")
                    if not old_rule: why.append("平面link對立體")
                    forks.append({"node": nid, "lat": la, "lon": lo, "h_in": h_in, "in": (iwi, ipos, istep),
                                  "A": cands[ia], "B": cands[ib], "Ainfo": A, "Binfo": B, "ra": ra, "rb": rb,
                                  "rule": "＋".join(why) or "原規則"})
    return forks


# ---------------------------------------------------------------- 4. Valhalla
def decode(enc, prec=6):
    coords, i, la, lo = [], 0, 0, 0
    while i < len(enc):
        for k in (0, 1):
            sh = res = 0
            while True:
                b = ord(enc[i]) - 63; i += 1; res |= (b & 0x1f) << sh; sh += 5
                if b < 0x20:
                    break
            v = ~(res >> 1) if res & 1 else res >> 1
            if k == 0:
                la += v
            else:
                lo += v
        coords.append((la / 10 ** prec, lo / 10 ** prec))
    return coords


def side_of(t):
    if t in RIGHT_T:
        return "right"
    if t in LEFT_T:
        return "left"
    if t in STRAIGHT_T:
        return "straight"
    return "other"


def valhalla_side(actor, g, fk, target):
    la, lo = fk["lat"], fk["lon"]
    iwi, ipos, istep = fk["in"]
    bpts, bacc, _ = g.walk(iwi, ipos, -istep, 40)
    o = bpts[-1]
    if hav(o, (la, lo)) < 8:
        return "no_origin", None, None
    info = fk["Ainfo"] if target == "A" else fk["Binfo"]
    d = g.point_at(info["pts"], info["acc"], min(120, info["len"] - 5))
    dh = bearing(g.point_at(info["pts"], info["acc"], min(110, info["len"] - 10)), d)
    req = {"locations": [{"lat": o[0], "lon": o[1], "heading": round(fk["h_in"]), "heading_tolerance": 35, "radius": 5},
                         {"lat": d[0], "lon": d[1], "heading": round(dh), "heading_tolerance": 35, "radius": 5}],
           "costing": COSTING, "costing_options": {COSTING: COSTING_OPTIONS}, "language": "zh-TW",
           "directions_options": {"units": "kilometers"}}
    try:
        r = json.loads(actor.route(json.dumps(req)))
    except Exception as ex:
        return "route_error", None, str(ex)[:80]
    L = r["trip"]["legs"][0]; sh = decode(L["shape"])
    near = min(range(len(sh)), key=lambda i: hav(sh[i], (la, lo)))
    if hav(sh[near], (la, lo)) > 15:
        return "route_miss", None, None
    p30 = g.point_at(info["pts"], info["acc"], min(40, info["len"] - 2))
    if min(seg_dist(p30, sh[i], sh[i + 1]) for i in range(len(sh) - 1)) > 10:
        return "route_other_branch", None, None
    best = None
    for m in L["maneuvers"]:
        bi = m["begin_shape_index"]
        if m["type"] in (1, 2, 3, 4, 5, 6):
            continue
        dd = hav(sh[bi], (la, lo))
        if dd <= 20 and (best is None or dd < best[0]):
            best = (dd, m)
    if best is None:
        return "none", None, None
    m = best[1]
    return side_of(m["type"]), m["type"], m.get("instruction", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", default="/root/rga/osm.pbf")
    ap.add_argument("--tiles", default="/root/rga/valhalla_tiles.tar")
    ap.add_argument("--region", default="kaohsiung", choices=list(REGIONS))
    ap.add_argument("--out", default=".")
    ap.add_argument("--find-name")
    ap.add_argument("--cache-dir", default="/root/rga/_audit_cache")
    a = ap.parse_args()
    os.makedirs(a.cache_dir, exist_ok=True); os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    bbox = REGIONS[a.region]
    cache = os.path.join(a.cache_dir, f"ways_{a.region}_{int(os.path.getmtime(a.pbf))}.pkl")
    ways = extract(a.pbf, bbox, cache)
    print(f"ways {len(ways)}  {time.time()-t0:.0f}s", flush=True)
    if a.find_name:
        for wid, t, pts in ways:
            if a.find_name in (t.get("name", "") + t.get("bridge:name", "")):
                print(wid, t, pts[0][1:], pts[-1][1:], len(pts))
        return
    g = Graph(ways)
    g.inc = defaultdict(list)   # node -> [(wi, pos, step)] 到達 node 的段（pos＝node 在 way 中的位置，step＝來的方向）
    for nid, outs in list(g.out.items()):
        for (wi, pos, step) in outs:
            nxt = g.ways[wi][2][pos + step][0]
            g.inc[nxt].append((wi, pos + step, step))
    forks = find_forks(g, bbox)
    print(f"forks {len(forks)}  {time.time()-t0:.0f}s", flush=True)
    import valhalla
    work = tempfile.mkdtemp(prefix="forkaudit_")
    actor = valhalla.Actor(valhalla.get_config(tile_extract=a.tiles, tile_dir=work, verbose=False))
    rows = []; stats = defaultdict(int)
    for k, fk in enumerate(forks):
        for target in ("A", "B"):
            rt, ro = (fk["ra"], fk["rb"]) if target == "A" else (fk["rb"], fk["ra"])
            geo = "unknown" if abs(rt - ro) < 3 else ("left" if rt < ro else "right")
            vs, vt, ins = valhalla_side(actor, g, fk, target)
            if vs in ("no_origin", "route_error", "route_miss", "route_other_branch") or geo == "unknown":
                verdict = "無法判斷"
            elif vs == geo:
                verdict = "一致"
            elif vs in ("left", "right"):
                verdict = "不一致（左右相反）"
            else:
                verdict = "無左右提示"
            stats[verdict] += 1
            tw = g.ways[(fk["A"] if target == "A" else fk["B"])[0]]
            ow = g.ways[(fk["B"] if target == "A" else fk["A"])[0]]
            iw = g.ways[fk["in"][0]]
            info = fk["Ainfo"] if target == "A" else fk["Binfo"]
            rows.append({"node": fk["node"], "lat": round(fk["lat"], 6), "lon": round(fk["lon"], 6),
                         "目標": "立體" if target == "A" else "平面",
                         "立體種類": fk["Ainfo"]["elev_kind"], "立體起點距岔路m": round(fk["Ainfo"]["elev_at"]),
                         "進入way": iw[0], "進入方向": "forward" if fk["in"][2] == 1 else "backward", "進入名稱": iw[1].get("name", ""), "目標way": tw[0], "目標名稱": tw[1].get("name", ""),
                         "目標highway": tw[1].get("highway"), "另一支way": ow[0], "另一支名稱": ow[1].get("name", ""),
                         "目標相對角": round(rt, 1), "另一支相對角": round(ro, 1), "幾何應": geo,
                         "valhalla側": vs, "valhalla_type": vt if vt is not None else "", "type名": TYPE_NAME.get(vt, vt if vt is not None else ""),
                         "valhalla指令": ins or "", "判定": verdict,
                         "進入turn:lanes": iw[1].get("turn:lanes", "") or iw[1].get("turn:lanes:forward", ""),
                         "收錄規則": fk["rule"], "osm": f"https://www.openstreetmap.org/node/{fk['node']}"})
        if k % 200 == 0:
            print(f"  {k}/{len(forks)} {time.time()-t0:.0f}s", flush=True)
    rows.sort(key=lambda r: (r["判定"] != "不一致（左右相反）", r["判定"], r["lat"]))
    fn = os.path.join(a.out, f"forks_{a.region}.csv")
    with open(fn, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["node"]); wr.writeheader(); wr.writerows(rows)
    summ = {"region": a.region, "bbox": bbox, "pbf": a.pbf, "tiles": a.tiles, "岔路節點數": len({r["node"] for r in rows}),
            "岔路方向數": len(forks), "判定筆數": len(rows), "統計": dict(stats), "秒": round(time.time() - t0),
            "costing": {COSTING: COSTING_OPTIONS}}
    json.dump(summ, open(os.path.join(a.out, f"summary_{a.region}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summ, ensure_ascii=False))


if __name__ == "__main__":
    main()
