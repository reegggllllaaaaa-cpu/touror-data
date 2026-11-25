#!/usr/bin/env python3
"""全台車道數推論引擎（每週由 weekly-roadnet 執行；本機可單獨跑）。

用法：infer_lanes.py <final.pbf> <輸出目錄> [--gov data/lanes/gov_lane_attrs.csv] [--mly data/lanes/mapillary_ai_lanes.csv]
        [--gate scripts/lanes_infer/gate.json]
產出（輸出目錄）：
  inferred_lanes.csv   每條沒有 OSM／政府／Mapillary 車道數的車用道路 way 的推論值（way_id、方向車道數、來源、信心）
  infer_report.json    現況覆蓋、交叉驗證準確率、推論後覆蓋、九如一路、缺口清單
  infer_report.md      同上，人看的版本
方法（依交叉驗證準確率決定信心與是否採用，見 gate.json）：
  a osm         OSM 原生 lanes／lanes:forward／backward（含 curated 覆蓋），不輸出（Lanes.java 直接用）
  b corridor    同一條路走廊（穿過一般路口，不穿匝道）最近已知值；1.5 km 內為 corridor，1.5–5 km 為 corridor_far
  b junction_*  路口反推：同名（或同 ref）、同等級、同方向性的連續路段串成一條鏈；路口有匝道匯入匯出、分岔、等級變化就斷鏈。
                both＝兩端已知且相同；nearer＝兩端不同取較近端；one＝只有一端已知（2 km 內）。政府與 Mapillary 判讀值也當錨點。
  c width_*     OSM width 或政府路寬換算（換算係數由已知車道路段學出）
  d opposite    雙線化道路：同名反向平行單行道已知車道，直接沿用
  e prior       依等級、單雙向／雙線化、所在 0.2 度網格（都市化程度代理）的眾數；最低信心，App 只淡化顯示
交叉驗證：OSM 已知車道的 way 依 id 分 5 折，每折藏起來讓 b–e 推，比對完全相同／±1。
"""
import argparse, csv, json, math, os, sys, time
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
SYN = 9_100_000_000_000


def pint(s):
    if s is None:
        return None
    try:
        v = int(round(float(str(s).split(";")[0].strip())))
    except ValueError:
        return None
    return v if 0 < v <= 16 else None


def pwidth(s):
    if not s:
        return None
    try:
        v = float(str(s).split(";")[0].replace("m", "").strip())
    except ValueError:
        return None
    return v if 2.0 <= v <= 80 else None


def hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742000 * math.asin(min(1, math.sqrt(a)))


class Ways(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.id, self.hw, self.key, self.name, self.ow = [], [], [], [], []
        self.first, self.last, self.length, self.lat, self.lon, self.bear = [], [], [], [], [], []
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
        if rev:
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
        self.first.append(a); self.last.append(b); self.length.append(L)
        self.lat.append(m[0]); self.lon.append(m[1])
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


def load_side(gov_path, mly_path, idx, W):
    """政府與 Mapillary 判讀值：{i: total}（單行道為該方向車道數）與政府路寬。"""
    side, gw = {}, {}
    if gov_path and os.path.exists(gov_path):
        for r in csv.DictReader(open(gov_path, encoding="utf-8-sig")):
            i = idx.get(int(r["osm_way_id"]))
            if i is None:
                continue
            w = pwidth(r.get("width"))
            if w:
                gw[i] = w
            if r.get("confidence") == "low":
                continue
            t, f, b = pint(r.get("lanes")), pint(r.get("lanes_forward")), pint(r.get("lanes_backward"))
            if t is None and f is not None:
                t = f if W.ow[i] else f + (b or 0)
            if t:
                side[i] = ("gov", t)
    if mly_path and os.path.exists(mly_path):
        acc = defaultdict(dict)
        for r in csv.DictReader(open(mly_path, encoding="utf-8-sig")):
            i = idx.get(int(r["osm_way_id"]))
            n = pint(r.get("lanes"))
            if i is not None and n:
                acc[i][r["direction"].strip()] = n
        for i, d in acc.items():
            if i in side:
                continue
            if W.ow[i]:
                v = d.get("forward")  # 單行道已正規化為行車方向；判讀檔以 OSM 畫線方向記錄
                if v is None:
                    v = d.get("backward")
                if v:
                    side[i] = ("mapillary_ai", v)
            elif "forward" in d and "backward" in d:
                side[i] = ("mapillary_ai", d["forward"] + d["backward"])
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
    return np.array(order), np.array(chain), np.array(pos), nb


def junction_predict(order, chain, pos, known):
    """known：每個 way 的已知值（nan＝未知）。回傳 (both, nearer, one, dist) 依 way 索引。"""
    n = len(known)
    kv = known[order]
    m = len(order)
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
    both = np.where(~np.isnan(L) & ~np.isnan(R) & (L == R), L, np.nan)
    nearer = np.where(~np.isnan(L) & ~np.isnan(R) & (L != R), np.where(dL <= dR, L, R), np.nan)
    oneL = np.isnan(R) & ~np.isnan(L) & (dL <= 2000)
    oneR = np.isnan(L) & ~np.isnan(R) & (dR <= 2000)
    one = np.where(oneL, L, np.where(oneR, R, np.nan))
    out = {k2: np.full(n, np.nan) for k2 in ("junction_both", "junction_nearer", "junction_one")}
    out["junction_both"][order] = both
    out["junction_nearer"][order] = nearer
    out["junction_one"][order] = one
    return out


def corridor_graph(W, inc):
    """同一條路的走廊：同名（或同 ref）、同等級、同方向性，在端點相接就連（穿過路口、分岔也連；匝道不連）。"""
    adj = defaultdict(set)
    for x, lst in inc.items():
        if len(lst) < 2:
            continue
        g = defaultdict(list)
        for i in lst:
            if W.key[i] and (x == W.first[i] or x == W.last[i]):
                g[(W.key[i], W.hw[i], W.ow[i])].append(i)
        for v in g.values():
            for i in v:
                for j in v:
                    if i != j:
                        adj[i].add(j)
    return {k: list(v) for k, v in adj.items()}


def corridor_predict(W, adj, known, maxd=1500.0):
    """多源最短路：每段取沿同一條路最近（路段中心距離 1.5 km 內）的已知值。"""
    import heapq
    n = len(known)
    best = np.full(n, np.inf)
    val = np.full(n, np.nan)
    h = []
    for i in np.nonzero(~np.isnan(known))[0]:
        if i in adj:
            for j in adj[i]:
                d = (W.length[i] + W.length[j]) / 2
                if d < best[j] and math.isnan(known[j]):
                    best[j] = d; val[j] = known[i]
                    heapq.heappush(h, (d, j))
    while h:
        d, i = heapq.heappop(h)
        if d > best[i] or d > maxd:
            continue
        for j in adj.get(i, ()):
            if not math.isnan(known[j]):
                continue
            nd = d + (W.length[i] + W.length[j]) / 2
            if nd < best[j] and nd <= maxd:
                best[j] = nd; val[j] = val[i]
                heapq.heappush(h, (nd, j))
    return val


def opposite_pairs(W):
    """雙線化：同名、同等級、反向平行（方位差 >150 度）、中點距離 80 m 內的單行道配對（依距離排序）。"""
    g = defaultdict(list)
    n = len(W.id)
    pts = []
    for i in range(n):
        if W.ow[i] and W.key[i]:
            g[(int(W.lat[i] / 0.001), int(W.lon[i] / 0.001))].append(i)
    pairs = [[] for _ in range(n)]
    for (a, b), lst in g.items():
        near = [j for da in (-1, 0, 1) for db in (-1, 0, 1) for j in g.get((a + da, b + db), ())]
        for i in lst:
            for j in near:
                if j == i or W.key[j] != W.key[i] or W.hw[j] != W.hw[i]:
                    continue
                dbear = abs((W.bear[i] - W.bear[j] + 180) % 360 - 180)
                if dbear < 150:
                    continue
                d = hav(W.lat[i], W.lon[i], W.lat[j], W.lon[j])
                if d <= 80:
                    pairs[i].append((d, j))
    return [[j for _, j in sorted(p)] for p in pairs]


def opp_predict(pairs, known):
    out = np.full(len(known), np.nan)
    for i, p in enumerate(pairs):
        for j in p:
            if not math.isnan(known[j]):
                out[i] = known[j]
                break
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
    t0 = time.time()
    W = Ways()
    W.apply_file(a.pbf, locations=True)
    n = len(W.id)
    print(f"[{time.time()-t0:.0f}s] 車用道路 way {n}", flush=True)
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
    for i, (_, v) in side.items():
        if math.isnan(osm_known[i]):
            side_known[i] = v
    owidth = {i: w for i, w in enumerate(W.width) if w}
    order, chain, pos, nb = build_chains(W, inc)
    cadj = corridor_graph(W, inc)
    del inc
    print(f"[{time.time()-t0:.0f}s] 路段鏈 {chain.max()+1 if len(chain) else 0}", flush=True)
    pairs = opposite_pairs(W)
    dual = np.array([len(p) > 0 for p in pairs])
    print(f"[{time.time()-t0:.0f}s] 雙線化配對 {int(dual.sum())}", flush=True)

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

    # ---- 3. 交叉驗證 ----
    ids = np.array(W.id)
    fold = (ids * 2654435761 % 2**32) % FOLDS
    ko = ~np.isnan(osm_known)
    methods = ["junction_both", "junction_nearer", "junction_one", "corridor", "corridor_far", "opposite", "width_osm", "width_gov", "prior"]
    stat = {m: {"n": 0, "exact": 0, "pm1": 0, "by_hw": defaultdict(lambda: [0, 0, 0])} for m in methods}
    for f in range(FOLDS):
        hold = ko & (fold == f)
        train = ko & ~hold
        known = np.where(train, osm_known, side_known)
        preds = junction_predict(order, chain, pos, known)
        preds["opposite"] = opp_predict(pairs, known)
        preds["corridor"] = corridor_predict(W, cadj, known)
        far = corridor_predict(W, cadj, known, 5000.0)
        preds["corridor_far"] = np.where(np.isnan(preds["corridor"]), far, np.nan)
        preds["width_osm"] = width_predict(W, owidth, fit_width(W, osm_known, owidth, train))
        preds["width_gov"] = width_predict(W, gwidth, fit_width(W, osm_known, gwidth, train))
        nat, cell = fit_prior(W, osm_known, dual, train)
        hi = np.nonzero(hold)[0]
        pp = prior_predict(W, dual, nat, cell, hi)
        preds["prior"] = np.full(n, np.nan)
        for i, v in pp.items():
            preds["prior"][i] = v
        for m in methods:
            p = preds[m][hi]
            ok = ~np.isnan(p)
            truth = osm_known[hi][ok]
            pv = p[ok]
            s = stat[m]
            s["n"] += int(ok.sum())
            s["exact"] += int((pv == truth).sum())
            s["pm1"] += int((np.abs(pv - truth) <= 1).sum())
            for i, v, t in zip(hi[ok], pv, truth):
                b = s["by_hw"][W.hw[i]]
                b[0] += 1; b[1] += int(v == t); b[2] += int(abs(v - t) <= 1)
        print(f"[{time.time()-t0:.0f}s] 交叉驗證第 {f+1}/{FOLDS} 折", flush=True)
    cv = {}
    for m in methods:
        s = stat[m]
        e = s["exact"] / s["n"] if s["n"] else 0
        p1 = s["pm1"] / s["n"] if s["n"] else 0
        conf = "prior" if m == "prior" else next((c["level"] for c in gate["levels"] if s["n"] >= gate["min_samples"] and e >= c["exact"] and p1 >= c["pm1"]), "off")
        cv[m] = {"n": s["n"], "exact": round(e, 4), "pm1": round(p1, 4), "confidence": conf,
                 "by_hw": {h: {"n": v[0], "exact": round(v[1] / v[0], 3), "pm1": round(v[2] / v[0], 3)} for h, v in sorted(s["by_hw"].items(), key=lambda x: -x[1][0])[:12]}}
    print(json.dumps({m: {k: cv[m][k] for k in ("n", "exact", "pm1", "confidence")} for m in methods}, ensure_ascii=False), flush=True)

    # ---- 2. 全量推論（全部已知值都當錨點）----
    known = np.where(ko, osm_known, side_known)
    preds = junction_predict(order, chain, pos, known)
    preds["opposite"] = opp_predict(pairs, known)
    preds["corridor"] = corridor_predict(W, cadj, known)
    far = corridor_predict(W, cadj, known, 5000.0)
    preds["corridor_far"] = np.where(np.isnan(preds["corridor"]), far, np.nan)
    preds["width_osm"] = width_predict(W, owidth, fit_width(W, osm_known, owidth, ko))
    preds["width_gov"] = width_predict(W, gwidth, fit_width(W, osm_known, gwidth, ko))
    nat, cell = fit_prior(W, osm_known, dual, ko)
    rank = sorted([m for m in methods if m != "prior" and cv[m]["confidence"] != "off"], key=lambda m: -cv[m]["exact"])
    todo = np.nonzero(np.isnan(known))[0]
    pp = prior_predict(W, dual, nat, cell, todo)
    res_src = np.array(["osm"] * n, dtype=object)
    res_src[~np.isnan(side_known)] = "side"
    for i in np.nonzero(~np.isnan(side_known))[0]:
        res_src[i] = side[i][0]
    chain_len = Counter(chain.tolist())
    chain_of = np.empty(n, int); chain_of[order] = chain
    rows, gaps = [], []
    final = known.copy()
    for i in todo:
        src, v = "prior", pp[i]
        for m in rank:
            pv = preds[m][i]
            if not math.isnan(pv):
                src, v = m, int(pv)
                break
        final[i] = v
        res_src[i] = src
        conf = cv[src]["confidence"] if src != "prior" else "prior"
        if W.ow[i]:
            lf, lb = v, 0
        else:
            lb = v // 2; lf = v - lb
        rows.append((W.id[i], "yes" if W.ow[i] else "no", v, lf, lb, src, conf))
        if src == "prior":
            if not W.key[i]:
                why = "無路名／編號，無法串接同一條路"
            elif i not in cadj:
                why = "同名同級同方向性的相鄰路段不存在（孤立路段或兩端都是等級／單雙向變化）"
            else:
                why = "同一條路 5 km（走廊）／2 km（連續鏈）內都沒有已知車道"
            gaps.append((i, why))
    with open(os.path.join(a.outdir, "inferred_lanes.csv"), "w", newline="", encoding="utf-8") as fo:
        wr = csv.writer(fo)
        wr.writerow(["way_id", "oneway", "lanes", "lanes_forward", "lanes_backward", "source", "confidence"])
        wr.writerows(rows)

    # ---- 4. 推論後覆蓋 ----
    after = {}
    for h in ORDER:
        mh = hw_arr == h
        tl = float(length[mh].sum())
        if tl == 0:
            continue
        d = Counter()
        for i in np.nonzero(mh)[0]:
            d[res_src[i]] += W.length[i]
        conf_ok = sum(v for k, v in d.items() if k in ("osm", "gov", "mapillary_ai") or (k in cv and cv[k]["confidence"] in ("high", "medium")))
        after[h] = {"km": round(tl / 1000, 1), "non_prior_pct": round(100 * (tl - d["prior"]) / tl, 1),
                    "medium_up_pct": round(100 * conf_ok / tl, 1),
                    "by_source_pct": {k: round(100 * v / tl, 1) for k, v in d.most_common()}}
    contrib = Counter()
    for i in todo:
        contrib[res_src[i]] += W.length[i]
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
                          "lanes": None if math.isnan(final[i]) else int(final[i]), "source": res_src[i],
                          "confidence": (cv[res_src[i]]["confidence"] if res_src[i] in cv else ("prior" if res_src[i] == "prior" else "high"))})
    rep = {"generated_s": round(time.time() - t0), "ways": n, "before": before, "cv": cv, "method_rank": rank,
           "after": after, "infer_contrib_km": {k: round(v / 1000, 1) for k, v in contrib.most_common()},
           "gap_reason_km": {k: round(v / 1000, 1) for k, v in gap_reason.most_common()},
           "gap_major_n": len(gap_major), "focus": focus}
    json.dump(rep, open(os.path.join(a.outdir, "infer_report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    L = ["# 全台車道數推論報告", "", f"車用道路 way：{n}；耗時 {rep['generated_s']} 秒", "",
         "## 現況（推論前）", "", "| 等級 | 長度 km | OSM 有 lanes % | 政府＋Mapillary % |", "|---|---|---|---|"]
    L += [f"| {h} | {v['km']} | {v['osm_lanes_pct']} | {v['gov_mly_pct']} |" for h, v in before.items()]
    L += ["", "## 交叉驗證（OSM 已知車道藏起來推，5 折）", "", "| 方法 | 樣本 | 完全相同 | ±1 | 採用信心 |", "|---|---|---|---|---|"]
    L += [f"| {m} | {cv[m]['n']} | {cv[m]['exact']:.1%} | {cv[m]['pm1']:.1%} | {cv[m]['confidence']} |" for m in methods]
    L += ["", f"套用順序（依準確率）：{' > '.join(rank)} > prior", "", "## 推論後覆蓋", "",
          "| 等級 | 非先驗 % | 中信心以上 % | 來源分布 % |", "|---|---|---|---|"]
    L += [f"| {h} | {v['non_prior_pct']} | {v['medium_up_pct']} | {', '.join(f'{k} {p}' for k, p in v['by_source_pct'].items())} |" for h, v in after.items()]
    L += ["", "## 各方法貢獻（km）", ""] + [f"- {k}：{v}" for k, v in rep["infer_contrib_km"].items()]
    L += ["", "## 缺口原因（仍只有先驗，km）", ""] + [f"- {k}：{v}" for k, v in rep["gap_reason_km"].items()]
    L += ["", f"主要道路（motorway～secondary）缺口 {len(gap_major)} 段，清單見 gaps_major.csv", "", f"## {gate.get('focus_name','九如一路')}", "",
          "| way | 等級 | 單行 | 長度 m | 車道 | 來源 | 信心 |", "|---|---|---|---|---|---|---|"]
    L += [f"| {x['way_id']} | {x['highway']} | {x['oneway']} | {x['m']} | {x['lanes']} | {x['source']} | {x['confidence']} |" for x in focus]
    open(os.path.join(a.outdir, "infer_report.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"[{time.time()-t0:.0f}s] 完成：推論 {len(rows)} 條", flush=True)


if __name__ == "__main__":
    main()
