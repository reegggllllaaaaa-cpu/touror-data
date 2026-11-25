#!/usr/bin/env python3
"""全台車道數推論引擎（每週由 weekly-roadnet 執行；本機可單獨跑）。

用法：infer_lanes.py <final.pbf> <輸出目錄> [--gov data/lanes/gov_lane_attrs.csv] [--mly data/lanes/mapillary_ai_lanes.csv]
        [--gate scripts/lanes_infer/gate.json]
產出（輸出目錄）：
  inferred_lanes.csv   每條沒有 OSM／政府／Mapillary 車道數的車用道路 way 的推論值（way_id、總車道數、來源、信心、分層）
                       雙向道路沒有方向證據：lanes_forward／lanes_backward 一律留空（不產生方向性車道導引）
  infer_report.json    現況覆蓋、兩種交叉驗證、各分層信心、推論後覆蓋、九如一路、缺口清單
  infer_report.md      同上，人看的版本
方法：
  a osm         OSM 原生 lanes／lanes:forward／backward（含 curated 覆蓋），不輸出（Lanes.java 直接用）
  b corridor    同一條路走廊最近已知值（1.5 km 內 corridor，1.5–5 km corridor_far）。只在「恰好兩段同名同級同方向性、
                沒有匝道、沒有同名路穿過」的節點相連（分岔／匯入／匝道處斷開）；兩個不同值的錨點距離相近（次近 ≤ 2 倍最近）就棄判。
  b junction_*  路口反推：同名（或同 ref）、同等級、同方向性的連續路段串成一條鏈；路口有匝道、分岔、等級變化就斷鏈。
                both＝兩端已知且相同（兩錨點間距與中間段數有上限，見 gate.json）；nearer＝兩端不同取較近端；one＝只有一端已知（2 km 內）。
  c width_*     OSM width 或政府路寬換算（換算係數由已知車道路段學出）
  d opposite    雙線化：同名同級、同層（layer／bridge／tunnel）、反向平行、中點 80 m 內且沿線重疊 ≥ 50% 的單行道；多個配對值不一致就棄判
  e prior       依等級、單雙向／雙線化、0.2 度網格的眾數；最低信心（Lanes.java 只放 lanes_approx）
信心（gate.json）：每個方法再依「道路等級群 × 單雙向 × 錨點距離帶」分層，各分層用 Wilson 95% 下限比對門檻、樣本不足即 off。
交叉驗證兩種：random＝OSM 已知車道的 way 依 id 隨機 5 折（零星缺標）；group＝依 chain／corridor／對向配對連通群組 5 折（整條路留出）。
分層信心取兩者較低者（group 該分層樣本不足時以 random 為準）。驗證母體只限原本有 lanes 的 OSM way，不能代表完全沒標的道路。
"""
import argparse, csv, heapq, json, math, os, sys, time
from collections import Counter, defaultdict
import numpy as np
import osmium

VEHICLE = {"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
           "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
           "service", "road", "track", "busway"}
ORDER = ["motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "living_street",
         "service", "track", "road", "busway", "motorway_link", "trunk_link", "primary_link", "secondary_link",
         "tertiary_link"]
FOLDS = 5
HWG_NAMES = ["fwy", "major", "minor", "local", "service", "link"]
HWG = {"motorway": 0, "trunk": 0, "primary": 1, "secondary": 1, "tertiary": 2, "unclassified": 2, "residential": 3,
       "living_street": 3, "road": 3, "busway": 3, "service": 4, "track": 4}
BAND_NAMES = ["-", "<=300m", "<=1km", "<=2km", ">2km"]
NS = 60  # 分層代碼 hwg*10 + oneway*5 + band
LEVELS = ["high", "medium", "low", "off"]
METHODS = ["junction_both", "junction_nearer", "junction_one", "corridor", "corridor_far", "opposite", "width_osm",
           "width_gov"]
DIST_METHODS = {"junction_both", "junction_nearer", "junction_one", "corridor", "corridor_far"}
SIDE_CONF = {"high": 2, "medium": 1}


def hwg_of(h):
    return 5 if h.endswith("_link") else HWG.get(h, 3)


def sname(code):
    return f"{HWG_NAMES[code // 10]}|{'oneway' if (code % 10) >= 5 else 'two'}|{BAND_NAMES[code % 5]}"


def pint(s):
    if s is None:
        return None
    try:
        f = float(str(s).split(";")[0].strip())
        if not math.isfinite(f):
            return None
        v = int(round(f))
    except (ValueError, OverflowError):
        return None
    return v if 0 < v <= 16 else None


def pwidth(s):
    if not s:
        return None
    try:
        v = float(str(s).split(";")[0].replace("m", "").strip())
    except (ValueError, OverflowError):
        return None
    return v if math.isfinite(v) and 2.0 <= v <= 80 else None


def hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742000 * math.asin(min(1, math.sqrt(a)))


def wilson(k, n, z=1.96):
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    return max(0.0, (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d)


class Ways(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.id, self.hw, self.key, self.name, self.ow, self.rev, self.struct = [], [], [], [], [], [], []
        self.first, self.last, self.length, self.lat, self.lon, self.bear = [], [], [], [], [], []
        self.p0, self.p1 = [], []
        self.tot, self.fwd, self.bwd, self.width = [], [], [], []
        self.ends = set()

    def way(self, w):
        t = w.tags
        hw = t.get("highway")
        if hw not in VEHICLE or t.get("area") == "yes" or len(w.nodes) < 2:
            return
        ow = t.get("oneway")
        rev = ow == "-1"
        one = rev or ow in ("yes", "1", "true") or (ow is None and (hw == "motorway" or t.get("junction") in ("roundabout", "circular")))
        pts, L = [], 0.0
        for n in w.nodes:
            if not n.location.valid():
                return
            pts.append((n.location.lat, n.location.lon))
        for i in range(1, len(pts)):
            L += hav(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1])
        a, b = w.nodes[0].ref, w.nodes[-1].ref
        if rev:  # 一律以行車方向表示（first→last）
            a, b, pts = b, a, pts[::-1]
        tot, f, bw = pint(t.get("lanes")), pint(t.get("lanes:forward")), pint(t.get("lanes:backward"))
        if rev:
            f, bw = bw, f
        if one and tot is None and f is not None:
            tot = f
        if not one and tot is None and f is not None and bw is not None:
            tot = f + bw
        m = pts[len(pts) // 2]
        self.id.append(w.id); self.hw.append(hw)
        name = t.get("name") or ""
        self.name.append(name)
        self.key.append(name or (t.get("ref") or ""))
        self.ow.append(1 if one else 0)
        self.rev.append(rev)
        self.struct.append(f"{t.get('layer') or '0'}|{'' if t.get('bridge') in (None, 'no') else 'b'}|{'' if t.get('tunnel') in (None, 'no') else 't'}")
        self.first.append(a); self.last.append(b); self.length.append(L)
        self.lat.append(m[0]); self.lon.append(m[1])
        self.p0.append(pts[0]); self.p1.append(pts[-1])
        self.bear.append(math.degrees(math.atan2((pts[-1][1] - pts[0][1]) * math.cos(math.radians(m[0])), pts[-1][0] - pts[0][0])) % 360)
        self.tot.append(tot); self.fwd.append(f); self.bwd.append(bw)
        self.width.append(pwidth(t.get("width")))
        self.ends.add(a); self.ends.add(b)


class Inc(osmium.SimpleHandler):
    def __init__(self, ends, idx):
        super().__init__()
        self.ends, self.idx, self.inc = ends, idx, defaultdict(list)

    def way(self, w):
        i = self.idx.get(w.id)
        if i is None:
            return
        for n in w.nodes:
            if n.ref in self.ends:
                self.inc[n.ref].append(i)


def _wid(r, idx):
    try:
        return idx.get(int(r["osm_way_id"]))
    except (ValueError, TypeError, KeyError):
        return None


def load_side(gov_path, mly_path, idx, W):
    """政府與 Mapillary 判讀值：{i: (來源, 總數, 信心)}（單行道為行車方向車道數）與政府路寬。
    方向欄位依 OSM 畫線方向記錄，oneway=-1 的行車方向是 backward；雙向缺任一側不合成總數；confidence=low 不當錨點。"""
    side, gw = {}, {}
    if gov_path and os.path.exists(gov_path):
        for r in csv.DictReader(open(gov_path, encoding="utf-8-sig")):
            i = _wid(r, idx)
            if i is None:
                continue
            w = pwidth(r.get("width"))
            if w:
                gw[i] = w
            conf = (r.get("confidence") or "").strip() or "medium"
            if conf not in SIDE_CONF:
                continue
            t, f, b = pint(r.get("lanes")), pint(r.get("lanes_forward")), pint(r.get("lanes_backward"))
            if W.rev[i]:
                f, b = b, f
            if t is None:
                if W.ow[i]:
                    t = f
                elif f is not None and b is not None:
                    t = f + b
            if t:
                side[i] = ("gov", t, conf)
    # 路口判讀（mapillary_ai_lanes.csv）之外，同資料夾的路段點判讀 mapillary_ai_segments.csv 也一起讀（2026-11-26 起）；
    # 同一 way 同方向兩檔都有時以路口判讀為準；同樣只收 medium 以上。
    mly_paths = [p for p in ([mly_path, os.path.join(os.path.dirname(mly_path), "mapillary_ai_segments.csv")] if mly_path else []) if os.path.exists(p)]
    if mly_paths:
        acc = defaultdict(dict)
        for r in (r for p in mly_paths for r in csv.DictReader(open(p, encoding="utf-8-sig"))):
            conf = (r.get("confidence") or "").strip()
            if conf not in SIDE_CONF:
                continue
            i = _wid(r, idx)
            n = pint(r.get("lanes"))
            if i is not None and n:
                acc[i].setdefault((r.get("direction") or "").strip(), (n, conf))
        for i, d in acc.items():
            if i in side:
                continue
            if W.ow[i]:
                x = d.get("backward" if W.rev[i] else "forward")
                if x:
                    side[i] = ("mapillary_ai", x[0], x[1])
            elif "forward" in d and "backward" in d:
                side[i] = ("mapillary_ai", d["forward"][0] + d["backward"][0],
                           min(d["forward"][1], d["backward"][1], key=SIDE_CONF.get))
    return side, gw


def build_chains(W, inc):
    n = len(W.id)
    link = np.array([h.endswith("_link") for h in W.hw])

    def cand(a, x):
        here = inc.get(x, ())
        if not W.key[a]:
            # 無路名：只在「只有這兩條路相接、同等級同方向性」的純斷點串接（中間沒有路口）
            if len(here) != 2:
                return None
            b = here[0] if here[1] == a else here[1]
            if b == a or W.key[b] or W.hw[b] != W.hw[a] or W.ow[b] != W.ow[a] or (x != W.first[b] and x != W.last[b]):
                return None
            if W.ow[a] and not ((x == W.last[a] and x == W.first[b]) or (x == W.first[a] and x == W.last[b])):
                return None
            return b
        out = []
        for b in here:
            if b == a:
                continue
            if link[b] and not link[a]:
                return None  # 匝道匯入匯出
            if W.key[b] != W.key[a]:
                continue
            if x != W.first[b] and x != W.last[b]:
                return None  # 同名路從中間穿過：分岔
            if W.hw[b] != W.hw[a] or W.ow[b] != W.ow[a]:
                continue  # 同名但等級或單雙向不同（如雙線化分合處的另一側）：不是延續，略過
            if W.ow[a] and not ((x == W.last[a] and x == W.first[b]) or (x == W.first[a] and x == W.last[b])):
                return None
            out.append(b)
        return out[0] if len(out) == 1 else None

    nb = [[None, None] for _ in range(n)]  # [經 first, 經 last]
    for a in range(n):
        for k, x in enumerate((W.first[a], W.last[a])):
            b = cand(a, x)
            if b is not None and cand(b, x) == a and W.first[a] != W.last[a]:
                nb[a][k] = b
    seen = np.zeros(n, bool)
    order, chain, pos = [], [], []
    cid = 0
    deg = [sum(x is not None for x in p) for p in nb]
    starts = [s for s in range(n) if deg[s] < 2] + list(range(n))  # 先從鏈頭走，剩下的是環
    for s in starts:
        if seen[s]:
            continue
        cur, prev, d = s, None, 0.0
        while cur is not None and not seen[cur]:
            seen[cur] = True
            order.append(cur); chain.append(cid); pos.append(d + W.length[cur] / 2)
            d += W.length[cur]
            nxt = [x for x in nb[cur] if x is not None and x != prev and not seen[x]]
            prev, cur = cur, (nxt[0] if nxt else None)
        cid += 1
    return np.array(order, dtype=np.int64), np.array(chain, dtype=np.int64), np.array(pos, dtype=float), nb


def junction_predict(order, chain, pos, known, maxd, maxn):
    """known：每個 way 的已知值（nan＝未知）。回傳 ({方法: 值}, {方法: 錨點距離})，依 way 索引。
    both 需兩錨點間距 ≤ maxd 且中間未知段數 ≤ maxn。"""
    n = len(known)
    keys = ("junction_both", "junction_nearer", "junction_one")
    out = {k: np.full(n, np.nan) for k in keys}
    dist = {k: np.full(n, np.inf) for k in keys}
    m = len(order)
    if m == 0:
        return out, dist
    kv = known[order]
    ar = np.arange(m)
    k = ~np.isnan(kv)
    left = np.maximum.accumulate(np.where(k, ar, -1))
    left = np.concatenate([[-1], left[:-1]])  # 嚴格在左
    right = np.minimum.accumulate(np.where(k, ar, m)[::-1])[::-1]
    right = np.concatenate([right[1:], [m]])
    lv = np.where((left >= 0) & (chain[np.clip(left, 0, m - 1)] == chain), left, -1)
    rv = np.where((right < m) & (chain[np.clip(right, 0, m - 1)] == chain), right, -1)
    L = np.where(lv >= 0, kv[np.clip(lv, 0, m - 1)], np.nan)
    R = np.where(rv >= 0, kv[np.clip(rv, 0, m - 1)], np.nan)
    dL = np.where(lv >= 0, pos - pos[np.clip(lv, 0, m - 1)], np.inf)
    dR = np.where(rv >= 0, pos[np.clip(rv, 0, m - 1)] - pos, np.inf)
    hasL, hasR = ~np.isnan(L), ~np.isnan(R)
    span_ok = hasL & hasR & ((dL + dR) <= maxd) & ((rv - lv - 1) <= maxn)
    both = np.where(span_ok & (L == R), L, np.nan)
    nearer = np.where(hasL & hasR & (L != R), np.where(dL <= dR, L, R), np.nan)
    oneL = ~hasR & hasL & (dL <= 2000)
    oneR = ~hasL & hasR & (dR <= 2000)
    one = np.where(oneL, L, np.where(oneR, R, np.nan))
    out["junction_both"][order] = both
    out["junction_nearer"][order] = nearer
    out["junction_one"][order] = one
    dist["junction_both"][order] = np.maximum(dL, dR)
    dist["junction_nearer"][order] = np.minimum(dL, dR)
    dist["junction_one"][order] = np.where(oneL, dL, dR)
    return out, dist


def corridor_graph(W, inc):
    """同一條路的走廊：某節點恰好有兩段同名（或同 ref）、同等級、同方向性的路段在端點相接才連；
    同組三段以上（分岔／匯入）、同名路從中間穿過、非匝道路段遇匝道、單行道首尾方向不接，都斷開。"""
    adj = defaultdict(set)
    for x, lst in inc.items():
        lst = set(lst)
        if len(lst) < 2:
            continue
        ramp = any(W.hw[i].endswith("_link") for i in lst)
        g, thru = defaultdict(list), set()
        for i in lst:
            if not W.key[i]:
                continue
            if x == W.first[i] or x == W.last[i]:
                g[(W.key[i], W.hw[i], W.ow[i])].append(i)
            else:
                thru.add(W.key[i])
        for kk, v in g.items():
            if len(v) != 2 or kk[0] in thru:
                continue
            if ramp and not kk[1].endswith("_link"):
                continue
            i, j = v
            if kk[2] and not ((x == W.last[i] and x == W.first[j]) or (x == W.first[i] and x == W.last[j])):
                continue
            adj[i].add(j); adj[j].add(i)
    return {k: list(v) for k, v in adj.items()}


def corridor_predict(W, adj, known, maxd):
    """多值最短路：每段記最近兩個「不同值」錨點的走廊距離（路段中心）；次近 ≤ 2 倍最近視為衝突棄判。回傳 (值, 距離)。"""
    n = len(known)
    Lw = W.length
    best = {}
    lab = {}
    h = []
    for i in np.nonzero(~np.isnan(known))[0]:
        i = int(i)
        for j in adj.get(i, ()):
            if not math.isnan(known[j]):
                continue
            d = (Lw[i] + Lw[j]) / 2
            v = float(known[i])
            if d <= maxd and d < best.get((j, v), math.inf):
                best[(j, v)] = d
                heapq.heappush(h, (d, j, v))
    while h:
        d, j, v = heapq.heappop(h)
        if d > best.get((j, v), math.inf):
            continue
        s = lab.setdefault(j, [])
        if len(s) >= 2 or any(x[1] == v for x in s):
            continue
        s.append((d, v))
        for k in adj.get(j, ()):
            if not math.isnan(known[k]):
                continue
            nd = d + (Lw[j] + Lw[k]) / 2
            if nd <= maxd and nd < best.get((k, v), math.inf):
                best[(k, v)] = nd
                heapq.heappush(h, (nd, k, v))
    val = np.full(n, np.nan)
    dist = np.full(n, np.inf)
    for j, s in lab.items():
        if len(s) == 2 and s[1][0] <= 2 * s[0][0]:
            continue
        val[j] = s[0][1]
        dist[j] = s[0][0]
    return val, dist


def opposite_pairs(W):
    """雙線化：同名同級、同層同結構、反向平行（方位差 >150 度）、中點距離 80 m 內、沿線投影重疊 ≥ 較短者 50% 的單行道配對。"""
    g = defaultdict(list)
    n = len(W.id)
    for i in range(n):
        if W.ow[i] and W.key[i]:
            g[(int(W.lat[i] / 0.001), int(W.lon[i] / 0.001))].append(i)
    pairs = [[] for _ in range(n)]

    def xy(p, lat0, lon0, c):
        return ((p[1] - lon0) * c * 111320.0, (p[0] - lat0) * 110540.0)

    for (a, b), lst in g.items():
        near = [j for da in (-1, 0, 1) for db in (-1, 0, 1) for j in g.get((a + da, b + db), ())]
        for i in lst:
            c = math.cos(math.radians(W.lat[i]))
            lat0, lon0 = W.p0[i]
            ex, ey = xy(W.p1[i], lat0, lon0, c)
            Li = math.hypot(ex, ey)
            if Li < 1:
                continue
            ux, uy = ex / Li, ey / Li
            for j in near:
                if j == i or W.key[j] != W.key[i] or W.hw[j] != W.hw[i] or W.struct[j] != W.struct[i]:
                    continue
                dbear = abs((W.bear[i] - W.bear[j] + 180) % 360 - 180)
                if dbear < 150:
                    continue
                d = hav(W.lat[i], W.lon[i], W.lat[j], W.lon[j])
                if d > 80:
                    continue
                ax, ay = xy(W.p0[j], lat0, lon0, c)
                bx, by = xy(W.p1[j], lat0, lon0, c)
                s1, s2 = sorted((ax * ux + ay * uy, bx * ux + by * uy))
                Lj = math.hypot(bx - ax, by - ay)
                ov = min(Li, s2) - max(0.0, s1)
                if ov >= 0.5 * min(Li, Lj):
                    pairs[i].append((d, j))
    return [[j for _, j in sorted(p)] for p in pairs]


def opp_predict(pairs, known):
    out = np.full(len(known), np.nan)
    for i, p in enumerate(pairs):
        vals = {known[j] for j in p if not math.isnan(known[j])}
        if len(vals) == 1:
            out[i] = vals.pop()
    return out


def cls_of(W, i, dual):
    return "dual" if dual[i] else ("oneway" if W.ow[i] else "two")


def fit_prior(W, known, dual, mask):
    nat, cell = defaultdict(Counter), defaultdict(Counter)
    for i in np.nonzero(mask & ~np.isnan(known))[0]:
        c = cls_of(W, i, dual)
        v = int(known[i])
        nat[(W.hw[i], c)][v] += 1
        cell[(W.hw[i], c, int(W.lat[i] / 0.2), int(W.lon[i] / 0.2))][v] += 1
    return nat, cell


def prior_predict(W, dual, nat, cell, idxs):
    out = {}
    for i in idxs:
        c = cls_of(W, i, dual)
        cc = cell.get((W.hw[i], c, int(W.lat[i] / 0.2), int(W.lon[i] / 0.2)))
        if cc and sum(cc.values()) >= 20:
            out[i] = cc.most_common(1)[0][0]
            continue
        nc = nat.get((W.hw[i], c))
        if nc:
            out[i] = nc.most_common(1)[0][0]
        else:
            out[i] = 1 if W.ow[i] or W.hw[i] in ("service", "track") else 2
    return out


def fit_width(W, known, widths, mask):
    r = defaultdict(list)
    for i, w in widths.items():
        if mask[i] and not math.isnan(known[i]) and known[i] > 0:
            r[W.ow[i]].append(w / known[i])
    return {k: float(np.median(v)) for k, v in r.items() if len(v) >= 10}


def width_predict(W, widths, div):
    out = np.full(len(W.id), np.nan)
    for i, w in widths.items():
        d = div.get(W.ow[i])
        if d:
            out[i] = max(1, min(8, round(w / d)))
    return out


def band_of(d):
    return np.where(d <= 300, 1, np.where(d <= 1000, 2, np.where(d <= 2000, 3, 4)))


def groups_of(n, nb, cadj, pairs):
    par = list(range(n))

    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x

    def uni(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            par[max(ra, rb)] = min(ra, rb)

    for a in range(n):
        for b in nb[a]:
            if b is not None:
                uni(a, b)
        for b in cadj.get(a, ()):
            uni(a, b)
        for b in pairs[a]:
            uni(a, b)
    return np.array([find(i) for i in range(n)], dtype=np.int64)


def region_of(lat, lon):
    if lat < 23.0 and lon < 120.4:
        return "離島"
    if lon > 121.35 and lat < 24.9 or (lon > 121.0 and lat < 23.3):
        return "東部"
    if lat >= 24.6:
        return "北部"
    if lat >= 23.5:
        return "中部"
    return "南部"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pbf")
    ap.add_argument("outdir")
    ap.add_argument("--gov")
    ap.add_argument("--mly")
    ap.add_argument("--gate", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate.json"))
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    gate = json.load(open(a.gate, encoding="utf-8"))
    P = gate["params"]
    MIN = gate["min_samples"]
    t0 = time.time()
    W = Ways()
    W.apply_file(a.pbf, locations=True)
    n = len(W.id)
    print(f"[{time.time()-t0:.0f}s] 車用道路 way {n}", flush=True)
    if n == 0:
        open(os.path.join(a.outdir, "inferred_lanes.csv"), "w", encoding="utf-8").write(
            "way_id,oneway,lanes,lanes_forward,lanes_backward,source,confidence,stratum\n")
        json.dump({"ways": 0, "error": "空輸入"}, open(os.path.join(a.outdir, "infer_report.json"), "w", encoding="utf-8"))
        print("空輸入：沒有車用道路", file=sys.stderr)
        sys.exit(1)
    idx = {w: i for i, w in enumerate(W.id)}
    I = Inc(W.ends, idx)
    I.apply_file(a.pbf)
    inc = I.inc
    del I
    W.ends = None
    print(f"[{time.time()-t0:.0f}s] 端點關聯 {len(inc)}", flush=True)
    length = np.array(W.length)
    osm_known = np.array([np.nan if v is None else float(v) for v in W.tot])
    side, gwidth = load_side(a.gov, a.mly, idx, W)
    side_known = np.full(n, np.nan)
    for i, (_, v, _) in side.items():
        if math.isnan(osm_known[i]):
            side_known[i] = v
    owidth = {i: w for i, w in enumerate(W.width) if w}
    order, chain, pos, nb = build_chains(W, inc)
    cadj = corridor_graph(W, inc)
    del inc
    print(f"[{time.time()-t0:.0f}s] 路段鏈 {int(chain.max()) + 1 if len(chain) else 0}；走廊節點 {len(cadj)}", flush=True)
    pairs = opposite_pairs(W)
    dual = np.array([len(p) > 0 for p in pairs])
    print(f"[{time.time()-t0:.0f}s] 雙線化配對 {int(dual.sum())}", flush=True)
    grp = groups_of(n, nb, cadj, pairs)
    ids = np.array(W.id, dtype=np.int64)
    hwg = np.array([hwg_of(h) for h in W.hw])
    owa = np.array(W.ow)
    sbase = hwg * 10 + owa * 5

    def predict_all(known, train):
        pr, ds = junction_predict(order, chain, pos, known, P["junction_both_max_m"], P["junction_both_max_ways"])
        pr["opposite"] = opp_predict(pairs, known)
        ds["opposite"] = np.zeros(n)
        v, d = corridor_predict(W, cadj, known, P["corridor_far_m"])
        nearm = d <= P["corridor_m"]
        pr["corridor"] = np.where(nearm, v, np.nan)
        pr["corridor_far"] = np.where(~nearm, v, np.nan)
        ds["corridor"] = ds["corridor_far"] = d
        pr["width_osm"] = width_predict(W, owidth, fit_width(W, osm_known, owidth, train))
        pr["width_gov"] = width_predict(W, gwidth, fit_width(W, osm_known, gwidth, train))
        ds["width_osm"] = ds["width_gov"] = np.zeros(n)
        code = {m: sbase + (band_of(ds[m]) if m in DIST_METHODS else 0) for m in METHODS}
        return pr, code

    # ---- 1. 現況 ----
    hw_arr = np.array(W.hw)
    before = {}
    for h in ORDER:
        mh = hw_arr == h
        tl = float(length[mh].sum())
        if tl == 0:
            continue
        before[h] = {"km": round(tl / 1000, 1), "osm_lanes_pct": round(100 * float(length[mh & ~np.isnan(osm_known)].sum()) / tl, 1),
                     "gov_mly_pct": round(100 * float(length[mh & ~np.isnan(side_known)].sum()) / tl, 1)}

    # ---- 3. 交叉驗證（random／group 兩種切折）----
    ko = ~np.isnan(osm_known)
    folds = {"random": (ids * 2654435761 % 2**32) % FOLDS, "group": (ids[grp] * 2654435761 % 2**32) % FOLDS}
    ST = {}
    for scheme, fold in folds.items():
        st = {m: np.zeros((3, NS), np.int64) for m in METHODS + ["prior"]}
        for f in range(FOLDS):
            hold = ko & (fold == f)
            train = ko & ~hold
            known = np.where(train, osm_known, side_known)
            pr, code = predict_all(known, train)
            hi = np.nonzero(hold)[0]
            nat, cell = fit_prior(W, osm_known, dual, train)
            pp = prior_predict(W, dual, nat, cell, hi)
            pr["prior"] = np.full(n, np.nan)
            pr["prior"][hi] = [pp[i] for i in hi]
            code["prior"] = sbase
            for m in METHODS + ["prior"]:
                p = pr[m][hi]
                ok = ~np.isnan(p)
                ii = hi[ok]
                c = code[m][ii]
                t = osm_known[ii]
                st[m][0] += np.bincount(c, minlength=NS)
                st[m][1] += np.bincount(c, weights=(p[ok] == t), minlength=NS).astype(np.int64)
                st[m][2] += np.bincount(c, weights=(np.abs(p[ok] - t) <= 1), minlength=NS).astype(np.int64)
            print(f"[{time.time()-t0:.0f}s] 交叉驗證 {scheme} 第 {f+1}/{FOLDS} 折", flush=True)
        ST[scheme] = st

    def level(nn, e, p1):
        for c in gate["levels"]:
            if nn >= MIN and wilson(e, nn) >= c["exact"] and wilson(p1, nn) >= c["pm1"]:
                return c["level"]
        return "off"

    conf = {}
    lvl_idx = {}
    wlo_e = {}
    strata = []
    for m in METHODS:
        conf[m] = ["off"] * NS
        lvl_idx[m] = np.full(NS, 9.0)
        wlo_e[m] = np.zeros(NS)
        for c in range(NS):
            A, B = ST["random"][m][:, c], ST["group"][m][:, c]
            la = level(*A)
            lb = level(*B) if B[0] >= MIN else la
            cf = LEVELS[max(LEVELS.index(la), LEVELS.index(lb))]
            conf[m][c] = cf
            wlo_e[m][c] = wilson(A[1], A[0])
            if cf != "off":
                lvl_idx[m][c] = LEVELS.index(cf)
            if A[0] or B[0]:
                strata.append({"method": m, "stratum": sname(c), "code": c, "conf": cf,
                               "random": {"n": int(A[0]), "exact": round(A[1] / A[0], 4) if A[0] else 0, "wlo_exact": round(wilson(A[1], A[0]), 4), "wlo_pm1": round(wilson(A[2], A[0]), 4)},
                               "group": {"n": int(B[0]), "exact": round(B[1] / B[0], 4) if B[0] else 0, "wlo_exact": round(wilson(B[1], B[0]), 4), "wlo_pm1": round(wilson(B[2], B[0]), 4)}})

    def overall(scheme, m, hmask=None):
        s = ST[scheme][m]
        cols = np.arange(NS) if hmask is None else hmask
        nn, e, p1 = (int(s[k][cols].sum()) for k in range(3))
        return {"n": nn, "exact": round(e / nn, 4) if nn else 0, "pm1": round(p1 / nn, 4) if nn else 0}

    cv = {s: {m: overall(s, m) for m in METHODS + ["prior"]} for s in ST}
    cv_hwg = {s: {m: {HWG_NAMES[g]: overall(s, m, np.arange(g * 10, g * 10 + 10)) for g in range(6)} for m in METHODS + ["prior"]} for s in ST}
    print(json.dumps(cv, ensure_ascii=False), flush=True)

    # ---- 2. 全量推論（全部已知值都當錨點；每段取信心最高、同級取 random Wilson 下限最高的方法）----
    known = np.where(ko, osm_known, side_known)
    pr, code = predict_all(known, ko)
    score = np.full((len(METHODS), n), np.inf)
    for k, m in enumerate(METHODS):
        c = code[m]
        sc = lvl_idx[m][c] + 0.5 * (1 - wlo_e[m][c])
        score[k] = np.where(~np.isnan(pr[m]) & (lvl_idx[m][c] < 9), sc, np.inf)
    pick = np.argmin(score, axis=0)
    pick_ok = np.isfinite(score[pick, np.arange(n)])
    nat, cell = fit_prior(W, osm_known, dual, ko)
    todo = np.nonzero(np.isnan(known))[0]
    pp = prior_predict(W, dual, nat, cell, todo)
    res_src = np.array(["osm"] * n, dtype=object)
    res_conf = np.array(["high"] * n, dtype=object)
    for i in np.nonzero(~np.isnan(side_known))[0]:
        res_src[i] = side[i][0]
        res_conf[i] = side[i][2]
    final = known.copy()
    rows, gaps = [], []
    used = defaultdict(int)
    for i in todo:
        if pick_ok[i]:
            m = METHODS[pick[i]]
            c = int(code[m][i])
            src, v, cf = m, int(pr[m][i]), conf[m][c]
            used[(m, c)] += 1
            st_name = sname(c)
        else:
            src, v, cf, st_name = "prior", int(pp[i]), "prior", sname(int(sbase[i]))
        final[i] = v
        res_src[i] = src
        res_conf[i] = cf
        # 單行道：行車方向車道數；雙向：無方向證據，方向欄位留空
        rows.append((W.id[i], "yes" if W.ow[i] else "no", v, v if W.ow[i] else "", "", src, cf, st_name))
        if src == "prior":
            if not W.key[i]:
                why = "無路名／編號，無法串接同一條路"
            elif i not in cadj:
                why = "同名同級同方向性的相鄰路段不存在，或節點為分岔／匯入／匝道（走廊斷開）"
            else:
                why = "同一條路 5 km（走廊）／2 km（連續鏈）內沒有可用且不衝突的已知車道，或該分層驗證不足"
            gaps.append((i, why))
    with open(os.path.join(a.outdir, "inferred_lanes.csv"), "w", newline="", encoding="utf-8") as fo:
        wr = csv.writer(fo)
        wr.writerow(["way_id", "oneway", "lanes", "lanes_forward", "lanes_backward", "source", "confidence", "stratum"])
        wr.writerows(rows)
    rows_by_conf = Counter(r[6] for r in rows)
    srow = {(s["method"], s["code"]): s for s in strata}
    used_rep = {f"{m}|{sname(c)}": {"conf": conf[m][c], "rows": k, "random": srow[(m, c)]["random"], "group": srow[(m, c)]["group"]}
                for (m, c), k in sorted(used.items(), key=lambda x: -x[1])}

    # ---- 4. 推論後覆蓋 ----
    MED = ("high", "medium")
    after = {}
    for h in ORDER:
        mh = hw_arr == h
        tl = float(length[mh].sum())
        if tl == 0:
            continue
        d = Counter()
        cm = 0.0
        lowk = 0.0
        for i in np.nonzero(mh)[0]:
            d[res_src[i]] += W.length[i]
            if res_conf[i] in MED:
                cm += W.length[i]
            elif res_conf[i] == "low":
                lowk += W.length[i]
        after[h] = {"km": round(tl / 1000, 1), "medium_up_pct": round(100 * cm / tl, 1), "low_pct": round(100 * lowk / tl, 1),
                    "prior_pct": round(100 * d["prior"] / tl, 1),
                    "by_source_pct": {k: round(100 * v / tl, 1) for k, v in d.most_common()}}
    reg = Counter(); reg_m = Counter()
    for i in range(n):
        r = region_of(W.lat[i], W.lon[i]) + ("（匝道）" if hwg[i] == 5 else "")
        reg[r] += W.length[i]
        if res_conf[i] in MED:
            reg_m[r] += W.length[i]
    by_region = {r: {"km": round(v / 1000, 1), "medium_up_pct": round(100 * reg_m[r] / v, 1)} for r, v in sorted(reg.items())}
    contrib = Counter()
    for i in todo:
        contrib[f"{res_src[i]}({res_conf[i]})"] += W.length[i]
    gap_reason = Counter()
    gap_major = []
    for i, why in gaps:
        gap_reason[why] += W.length[i]
        if W.hw[i] in ("motorway", "trunk", "primary", "secondary"):
            gap_major.append((W.hw[i], W.key[i], W.id[i], round(W.length[i]), why))
    gap_major.sort(key=lambda x: (ORDER.index(x[0]), -x[3]))
    with open(os.path.join(a.outdir, "gaps_major.csv"), "w", newline="", encoding="utf-8") as fo:
        wr = csv.writer(fo)
        wr.writerow(["highway", "name_or_ref", "way_id", "length_m", "reason"])
        wr.writerows(gap_major)
    focus = []
    for i in range(n):
        if W.name[i] == gate.get("focus_name", "九如一路"):
            focus.append({"way_id": W.id[i], "highway": W.hw[i], "oneway": W.ow[i], "m": round(W.length[i]),
                          "lanes": None if math.isnan(final[i]) else int(final[i]), "source": res_src[i], "confidence": res_conf[i]})
    rep = {"generated_s": round(time.time() - t0), "ways": n, "before": before, "cv": cv, "cv_by_hwg": cv_hwg, "strata": strata,
           "used": used_rep, "rows": len(rows), "rows_by_conf": dict(rows_by_conf), "after": after, "by_region": by_region,
           "infer_contrib_km": {k: round(v / 1000, 1) for k, v in contrib.most_common()},
           "gap_reason_km": {k: round(v / 1000, 1) for k, v in gap_reason.most_common()},
           "gap_major_n": len(gap_major), "focus": focus}
    json.dump(rep, open(os.path.join(a.outdir, "infer_report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    def pc(x):
        return f"{x:.1%}"

    L = ["# 全台車道數推論報告", "", f"車用道路 way：{n}；耗時 {rep['generated_s']} 秒", "",
         "> 驗證母體限制：交叉驗證只能在「原本 OSM 有 lanes」的 way 上做，這些路段偏向主要道路、都市區；"
         "完全沒標的 service／track／鄉間道路沒有真值，成績不能直接外推。random＝零星缺標（相鄰段仍在訓練側，偏樂觀）；"
         "group＝整條路（chain＋走廊＋對向配對連通群）一起留出（連續缺標，偏保守）。信心取兩者較低者。", "",
         "## 現況（推論前）", "", "| 等級 | 長度 km | OSM 有 lanes % | 政府＋Mapillary（中信心以上）% |", "|---|---|---|---|"]
    L += [f"| {h} | {v['km']} | {v['osm_lanes_pct']} | {v['gov_mly_pct']} |" for h, v in before.items()]
    L += ["", "## 交叉驗證（全體，兩種切折）", "", "| 方法 | random 樣本 | random 完全相同 | random ±1 | group 樣本 | group 完全相同 | group ±1 |", "|---|---|---|---|---|---|---|"]
    L += [f"| {m} | {cv['random'][m]['n']} | {pc(cv['random'][m]['exact'])} | {pc(cv['random'][m]['pm1'])} | {cv['group'][m]['n']} | {pc(cv['group'][m]['exact'])} | {pc(cv['group'][m]['pm1'])} |" for m in METHODS + ["prior"]]
    L += ["", f"## 分層信心（等級群｜單雙向｜錨點距離；樣本 ≥ {MIN} 且 Wilson 95% 下限達門檻；只列非 off）", "",
          "| 方法 | 分層 | 信心 | random n | random 完全相同 | group n | group 完全相同 |", "|---|---|---|---|---|---|---|"]
    L += [f"| {s['method']} | {s['stratum']} | {s['conf']} | {s['random']['n']} | {pc(s['random']['exact'])} | {s['group']['n']} | {pc(s['group']['exact'])} |" for s in strata if s["conf"] != "off"]
    L += ["", f"推論列數 {len(rows)}；依信心 {dict(rows_by_conf)}（prior／low 只進 lanes_approx，不進精確 lanes 欄位）", "",
          "## 推論後覆蓋（長度 %）", "", "| 等級 | 中信心以上 % | low % | prior % | 來源分布 % |", "|---|---|---|---|---|"]
    L += [f"| {h} | {v['medium_up_pct']} | {v['low_pct']} | {v['prior_pct']} | {', '.join(f'{k} {p}' for k, p in v['by_source_pct'].items())} |" for h, v in after.items()]
    L += ["", "## 區域（中信心以上 %，含 OSM／政府／Mapillary）", "", "| 區域 | km | 中信心以上 % |", "|---|---|---|"]
    L += [f"| {r} | {v['km']} | {v['medium_up_pct']} |" for r, v in by_region.items()]
    L += ["", "## 各方法貢獻（km，方法（信心））", ""] + [f"- {k}：{v}" for k, v in rep["infer_contrib_km"].items()]
    L += ["", "## 缺口原因（仍只有先驗，km）", ""] + [f"- {k}：{v}" for k, v in rep["gap_reason_km"].items()]
    L += ["", f"主要道路（motorway～secondary）缺口 {len(gap_major)} 段，清單見 gaps_major.csv", "", f"## {gate.get('focus_name','九如一路')}", "",
          "| way | 等級 | 單行 | 長度 m | 車道 | 來源 | 信心 |", "|---|---|---|---|---|---|---|"]
    L += [f"| {x['way_id']} | {x['highway']} | {x['oneway']} | {x['m']} | {x['lanes']} | {x['source']} | {x['confidence']} |" for x in focus]
    open(os.path.join(a.outdir, "infer_report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"[{time.time()-t0:.0f}s] 完成：推論 {len(rows)} 條 {dict(rows_by_conf)}", flush=True)


if __name__ == "__main__":
    main()
