#!/usr/bin/env python
"""C1 陷阱分岔表（docs/複雜路口引導設計.md §4.3）：離線算出「人在平面、一支通往國道／快速道路」等陷阱分岔，輸出 trap_forks.json。

在 WSL 跑（需要 pyosmium；--route 另需 pyvalhalla），例：
  /root/v2venv/bin/python trap_forks.py --pbf /root/rga/osm.pbf --out data/lanes/trap_forks.json \
      --forks-csv <forks_taiwan.csv> --report <報表.json>

做法（全量，不抽樣；使用者裁決 2026-11-26：只做正向引導，不加算路懲罰，所以輸出只有「該走哪一支」的中性事實，沒有指認／否定文案）：
 1 分岔點 D：每個節點、每條進入方向，偏離進入方位 ≤135° 的可駛出分支 ≥2（照 oneway，不含原路折返）。
 2 每支「通往」：第一條 way 是 *_link 就沿匝道追到第一條非匝道道路（上限 3 km、8 次換 way）→ motorway／trunk／surface；
   國道（motorway）對所有機車禁行（同 App PlateRouting：exclude_highways）、快速道路（trunk）白牌禁行；motorcycle=no 全禁。
 3 陷阱分岔：出邊「通往等級不同」或「禁行牌別不同」，且兩支 30 m 處方位差 <30° 或橫向間距 <15 m（kind=parallel）。
   「誤上點」：進入邊在平面（非 link、非 motorway／trunk），一支通往 motorway／trunk、一支留在平面（context=surface）；
   夾角大的一般轉入匝道（如 Z6 文川路）也收，kind=turn。
 4 左右：30 m 處相對進入方位的有號角排序（不用 Valhalla 的 stay left/right）。
 5 車道：車道數守恆（進入 lanes＝各支 lanes 加總，依左右分配）；匝道缺 lanes 視為 1 線並註記 lanes_assumed。
   信心：curated/車道覆蓋.csv 有進入 way 且守恆＝high；守恆成立＝medium；其餘＝low（只講邊）；兩支角差 <3°＝low。
 6 不可回頭點：以「必然進入國道（或國道＋快速道路）」的有向邊集合（之後所有可行分支都通往該等級，固定點迭代）往上游追，
   邊界上的節點＝最後決策點（還有一支不會進國道）、進入必然集合的第一個節點＝不可回頭點。
 7 聚類成區：400 m 內的陷阱分岔併成一區（直線距離近似路網距離），複雜度＝陷阱數×0.4＋(層數−1)×0.2＋(分岔數−1)×0.15，上限 1。
輸出：trap_forks.json（forks、zones、params）；--forks-csv 時把 forks_taiwan.csv 加三欄 通往等級／禁行牌別／區編號（原地改寫）。
"""
import argparse, csv, json, math, os, pickle, sys, time
from array import array
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fork_sides import hav, bearing, rel, oneway_dir, interp, REGIONS, DRIVE  # noqa: E402

TAGS = ("highway", "name", "ref", "bridge", "tunnel", "layer", "oneway", "junction", "lanes", "lanes:forward", "lanes:backward",
        "turn:lanes", "destination", "destination:ref", "motorcycle", "motor_vehicle", "access", "motorroad")
ALL = ["white", "yellow", "red"]
POI_KEYS = ("Honda", "HONDA", "本田", "honda")


def extract(pbf, bbox, cache):
    if os.path.exists(cache):
        return pickle.load(open(cache, "rb"))
    import osmium
    s, w, n, e = bbox
    ways, pois = [], []

    class H(osmium.SimpleHandler):
        def node(self, nd):
            nm = nd.tags.get("name", "") + nd.tags.get("brand", "")
            if any(k in nm for k in POI_KEYS) and 22.5 < nd.location.lat < 22.8 and 120.2 < nd.location.lon < 120.45:
                pois.append(("n", nd.id, dict(nd.tags), nd.location.lat, nd.location.lon))

        def way(self, wy):
            hw = wy.tags.get("highway")
            nm = wy.tags.get("name", "") + wy.tags.get("brand", "")
            if hw not in DRIVE:
                if any(k in nm for k in POI_KEYS):
                    try:
                        p = [(nd.lat, nd.lon) for nd in wy.nodes]
                        la, lo = sum(x[0] for x in p) / len(p), sum(x[1] for x in p) / len(p)
                        if 22.5 < la < 22.8 and 120.2 < lo < 120.45:
                            pois.append(("w", wy.id, dict(wy.tags), la, lo))
                    except Exception:
                        pass
                return
            if wy.tags.get("area") == "yes" or wy.tags.get("access") in ("no", "private") and wy.tags.get("motorcycle") not in ("yes", "designated"):
                return
            try:
                pts = [(nd.ref, nd.lat, nd.lon) for nd in wy.nodes]
            except osmium.InvalidLocationError:
                return
            if not any(s <= p[1] <= n and w <= p[2] <= e for p in pts):
                return
            ways.append((wy.id, {k: wy.tags.get(k) for k in TAGS if wy.tags.get(k)}, pts))

    H().apply_file(pbf, locations=True, idx="flex_mem")
    pickle.dump((ways, pois), open(cache, "wb"), protocol=4)
    return ways, pois


def hclass(t):
    hw = t.get("highway", "")
    if hw == "motorway":
        return "motorway"
    if hw == "trunk":
        return "trunk"
    if hw.endswith("_link"):
        return "link"
    return "surface"


def layer_of(t):
    try:
        return int(float(t.get("layer", "0")))
    except ValueError:
        return 0


def dir_lanes(t, step):
    """way 在 step 方向的車道數（None＝未知）"""
    def num(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None
    if oneway_dir(t) != 0:
        return num(t.get("lanes"))
    f = num(t.get("lanes:forward" if step > 0 else "lanes:backward"))
    if f:
        return f
    L = num(t.get("lanes"))
    return L // 2 if L and L % 2 == 0 else None


class G:
    """有向邊圖：邊 i = ways[ew[i]] 上 pos[i] → pos[i]+st[i]"""
    def __init__(self, ways):
        self.ways = ways
        self.loc = {}
        self.ew, self.ep, self.es, self.eu, self.ev = array("i"), array("i"), array("b"), array("q"), array("q")
        self.out = defaultdict(list); self.inn = defaultdict(list)
        for wi, (wid, t, pts) in enumerate(ways):
            ow = oneway_dir(t)
            for i, (nid, la, lo) in enumerate(pts):
                self.loc[nid] = (la, lo)
            for i in range(len(pts) - 1):
                a, b = pts[i][0], pts[i + 1][0]
                if ow >= 0:
                    self._add(wi, i, 1, a, b)
                if ow <= 0:
                    self._add(wi, i + 1, -1, b, a)

    def _add(self, wi, pos, st, u, v):
        k = len(self.ew)
        self.ew.append(wi); self.ep.append(pos); self.es.append(st); self.eu.append(u); self.ev.append(v)
        self.out[u].append(k); self.inn[v].append(k)

    def succ(self, e):
        u = self.eu[e]
        return [f for f in self.out[self.ev[e]] if self.ev[f] != u]

    def tags(self, e):
        return self.ways[self.ew[e]][1]

    def ebear(self, e):
        return bearing(self.loc[self.eu[e]], self.loc[self.ev[e]])

    def nxt(self, e):
        """沿路續行：同一條 way 優先，否則轉向最小（<50°）的下一條"""
        best = None; h = self.ebear(e)
        for f in self.succ(e):
            if self.ew[f] == self.ew[e]:
                return f
            r = abs(rel(self.ebear(f), h))
            if r < 50 and (best is None or r < best[0]):
                best = (r, f)
        return best[1] if best else None

    def walk(self, e, dist):
        """從邊 e 起走 dist 公尺：回傳 (點列, 累積距離, 邊列)"""
        pts = [self.loc[self.eu[e]]]; acc = [0.0]; edges = []; d = 0.0; guard = 0
        while e is not None and d < dist and guard < 300:
            guard += 1; edges.append(e)
            p = self.loc[self.ev[e]]; d += hav(pts[-1], p); pts.append(p); acc.append(d)
            if d >= dist:
                break
            e = self.nxt(e)
        return pts, acc, edges

    def back_bearing(self, e, dist=20.0):
        """進入方位：沿進入 way 往回最多 dist 公尺的點 → 節點"""
        v = self.loc[self.ev[e]]; wi, pos, st = self.ew[e], self.ep[e], self.es[e]
        pts = self.ways[wi][2]; p = pos; d = 0.0; q = self.loc[self.eu[e]]
        while d < dist and 0 <= p - st < len(pts) and 0 <= p < len(pts):
            q = (pts[p][1], pts[p][2]); d = hav(q, v)
            p -= st
        return bearing(q, v)


def point_at(pts, acc, m):
    for i in range(1, len(pts)):
        if acc[i] >= m:
            seg = acc[i] - acc[i - 1]
            return interp(pts[i - 1], pts[i], (m - acc[i - 1]) / seg if seg > 0 else 0)
    return pts[-1]


def chase(g, e0, maxd=3000, maxchg=8):
    """沿 *_link 追到第一條非匝道道路 → {等級: (最短距離, ref, way)}、經過 motorcycle=no"""
    t0 = g.tags(e0)
    c0 = hclass(t0)
    mno = t0.get("motorcycle") == "no"
    if c0 != "link":
        return {c0: (0.0, t0.get("ref", ""), g.ways[g.ew[e0]][0])}, mno
    res = {}; seen = {e0: 0.0}; q = deque([(e0, 0.0, 0)])
    while q:
        e, d, chg = q.popleft()
        d2 = d + hav(g.loc[g.eu[e]], g.loc[g.ev[e]])
        succ = g.succ(e)
        # 匝道在這個節點接上一般道路＝追到了；不再經由同一節點的其他匝道續追
        # （例：九如一路迴轉道 212429268 接回平面的路口，旁邊另有中山高入口匝道，舊寫法會誤判通往國道）
        landed = [f for f in succ if hclass(g.tags(f)) != "link"]
        for f in (landed or succ):
            tf = g.tags(f); cf = hclass(tf)
            if cf != "link":
                if cf not in res or d2 < res[cf][0]:
                    res[cf] = (round(d2, 1), tf.get("ref", ""), g.ways[g.ew[f]][0])
                continue
            c2 = chg + (g.ew[f] != g.ew[e])
            if d2 > maxd or c2 > maxchg or (f in seen and seen[f] <= d2):
                continue
            mno = mno or tf.get("motorcycle") == "no"
            seen[f] = d2; q.append((f, d2, c2))
    return res, mno


def plates_for(classes, mno):
    if mno or "motorway" in classes:
        return ALL[:]
    if "trunk" in classes:
        return ["white"]
    return []


def lead_label(classes):
    for c in ("motorway", "trunk", "surface"):
        if c in classes:
            if len(classes) == 1:
                return c
    if not classes:
        return "unknown"
    return "mixed:" + "+".join(sorted(classes))


def top_class(classes):
    for c in ("motorway", "trunk", "surface"):
        if c in classes:
            return c
    return "unknown"


def doomed_set(g, target):
    """必然進入 target 等級的有向邊（所有可行續行都通往 target；死路不算）"""
    n = len(g.ew)
    tgt = set(i for i, (wid, t, pts) in enumerate(g.ways) if hclass(t) in target)
    doom = bytearray(n); rem = array("i", [0]) * n
    q = deque()
    for e in range(n):
        rem[e] = len(g.succ(e))
        if g.ew[e] in tgt:
            doom[e] = 1; q.append(e)
    while q:
        f = q.popleft(); v, w = g.eu[f], g.ev[f]
        for e in g.inn[v]:
            if doom[e] or g.eu[e] == w:
                continue
            rem[e] -= 1
            if rem[e] <= 0:
                doom[e] = 1; q.append(e)
    return doom


def branch_desc(g, f, h_in):
    pts, acc, edges = g.walk(f, 150)
    p30 = point_at(pts, acc, 30)
    ra = rel(bearing(pts[0], p30), h_in)
    classes, mno = chase(g, f)
    t = g.tags(f)
    lays = sorted({layer_of(g.tags(x)) for x in edges})
    vert = next((("bridge" if g.tags(x).get("bridge", "no") != "no" else "tunnel" if g.tags(x).get("tunnel", "no") != "no" else "layer")
                 for x in edges if g.tags(x).get("bridge", "no") != "no" or g.tags(x).get("tunnel", "no") != "no" or layer_of(g.tags(x)) != 0), None)
    tc = top_class(classes)
    ref = classes.get(tc, (0, "", 0))[1] if tc in classes else ""
    land = {"motorway": "國道", "trunk": "快速道路"}.get(tc) or {"bridge": "上橋", "layer": "高架", "tunnel": "地下道"}.get(vert, "平面")
    ln = dir_lanes(t, g.es[f]); assumed = False
    if ln is None and hclass(t) == "link" and oneway_dir(t) != 0:
        ln, assumed = 1, True
    p40 = point_at(pts, acc, 40)
    return {"edge": f, "way": g.ways[g.ew[f]][0], "name": t.get("name", ""), "highway": t.get("highway"), "rel": round(ra, 1),
            "x30": round(30 * math.sin(math.radians(ra)), 1), "leads_to": lead_label(classes), "leads_class": tc,
            "leads_ref": ref, "leads_way": classes.get(tc, (0, "", None))[2] if tc in classes else None,
            "leads_m": classes.get(tc, (None,))[0] if tc in classes else None,
            "layers": lays, "vertical": vert, "landing": land, "lanes": ln, "lanes_assumed": assumed,
            "prohibited": plates_for(classes, mno), "motorcycle_no": mno,
            "destination": t.get("destination", ""), "destination_ref": t.get("destination:ref", ""),
            "p40": [round(p40[0], 6), round(p40[1], 6)]}


def side_names(n):
    return {2: ["left", "right"], 3: ["left", "middle", "right"]}.get(n, ["b%d" % (i + 1) for i in range(n)])


def find_forks(g, bbox, curated_lanes):
    s, w, n, e = bbox
    out = []
    for nid, outs in list(g.out.items()):
        if len(outs) < 2:
            continue
        la, lo = g.loc[nid]
        if not (s <= la <= n and w <= lo <= e):
            continue
        # 便宜的預篩：出邊的第一條 way 等級／motorcycle 不全一樣才細算
        kinds = {(hclass(g.tags(f)), g.tags(f).get("motorcycle") == "no") for f in outs}
        if len(kinds) < 2 and not any(k[0] == "link" for k in kinds):
            continue
        for ein in g.inn[nid]:
            tin = g.tags(ein)
            h_in = g.back_bearing(ein)
            brs = []
            for f in g.succ(ein):
                if abs(rel(g.ebear(f), h_in)) > 135:
                    continue
                brs.append(f)
            if len(brs) < 2:
                continue
            ds = [branch_desc(g, f, h_in) for f in brs]
            ds.sort(key=lambda d: d["rel"])
            classes = {d["leads_class"] for d in ds}
            plates = {tuple(d["prohibited"]) for d in ds}
            if len(classes) < 2 and len(plates) < 2:
                continue
            # 陷阱對：等級或牌別不同，且角差 <30° 或 30 m 處橫距 <15 m
            par = False; tight = 999.0
            for i in range(len(ds)):
                for j in range(i + 1, len(ds)):
                    a, b = ds[i], ds[j]
                    if a["leads_class"] == b["leads_class"] and a["prohibited"] == b["prohibited"]:
                        continue
                    dang = abs(a["rel"] - b["rel"]); dx = abs(a["x30"] - b["x30"])
                    tight = min(tight, dang)
                    if dang < 30 or dx < 15:
                        par = True
            ec = hclass(tin)
            hw_br = [d for d in ds if d["leads_class"] in ("motorway", "trunk")]
            sf_br = [d for d in ds if d["leads_class"] == "surface"]
            ctx = "surface" if ec == "surface" else "ramp" if ec == "link" else "highway"
            mis = ctx == "surface" and hw_br and sf_br
            if not par and not mis:
                continue
            names = side_names(len(ds))
            for i, d in enumerate(ds):
                d["side"] = names[i]; d["order"] = i + 1
            # 車道守恆
            ein_l = dir_lanes(tin, g.es[ein]); src = "osm"
            wid_in = g.ways[g.ew[ein]][0]
            if wid_in in curated_lanes:
                ein_l = curated_lanes[wid_in]; src = "curated"
            bl = [d["lanes"] for d in ds]
            split = None
            if ein_l and all(bl) and sum(bl) == ein_l:
                k = 1
                for d in ds:
                    d["lane_range"] = [k, k + d["lanes"] - 1]; k += d["lanes"]
                split = "%d=%s" % (ein_l, "+".join(str(x) for x in bl))
            gaps = [ds[i + 1]["rel"] - ds[i]["rel"] for i in range(len(ds) - 1)]
            if min(gaps) < 3:
                conf = "low"
            elif split and src == "curated":
                conf = "high"
            elif split:
                conf = "medium"
            else:
                conf = "low"
            out.append({"node": nid, "lat": round(la, 6), "lon": round(lo, 6), "entry_way": wid_in, "entry_name": tin.get("name", ""),
                        "entry_highway": tin.get("highway"), "entry_edge": ein, "entry_lanes": ein_l, "entry_lanes_src": src if ein_l else None,
                        "context": ctx, "kind": "parallel" if par else "turn", "misentry": bool(mis), "min_angle": round(tight, 1),
                        "lane_split": split, "confidence": conf, "branches": ds})
    return out


def no_return(g, fk, doom_mw, doom_hw):
    """對誤上點的國道／快速道路那支：往上游追不可回頭點與最後決策點"""
    res = []
    for d in fk["branches"]:
        if d["leads_class"] not in ("motorway", "trunk"):
            continue
        doom = doom_mw if d["leads_class"] == "motorway" else doom_hw
        f = d["edge"]; ein = fk["entry_edge"]
        if not doom[f]:
            res.append({"branch_way": d["way"], "status": "not_forced"})
            continue
        # 從 f 往上游走必然邊，找邊界：前一條邊不是必然 → 該節點＝最後決策點
        bounds = []; seen = {f}; q = deque([(f, 0.0)])
        if not doom[ein]:   # 進入邊本身還有安全出路：最後決策點＝這個分岔點（依此進入方向）
            bounds.append((ein, f, 0.0)); q.clear()
        while q and len(bounds) < 6:
            x, dist = q.popleft()
            u = g.eu[x]
            for p in g.inn[u]:
                if g.eu[p] == g.ev[x] or p in seen:
                    continue
                seen.add(p)
                dp = dist + hav(g.loc[g.eu[p]], g.loc[u])
                if doom[p]:
                    if dp < 3000:
                        q.append((p, dp))
                else:
                    bounds.append((p, x, dist))
        items = []
        for p, x, dist in bounds:
            u = g.eu[x]; h_in = g.back_bearing(p)
            safe = [s for s in g.succ(p) if not doom[s] and abs(rel(g.ebear(s), h_in)) <= 135]
            if not safe:
                continue
            rx = rel(g.ebear(x), h_in)
            rs = [rel(g.ebear(s), h_in) for s in safe]
            side = "right" if all(r > rx for r in rs) else "left" if all(r < rx for r in rs) else "both"
            items.append({"last_decision": {"node": u, "lat": round(g.loc[u][0], 6), "lon": round(g.loc[u][1], 6),
                                            "entry_way": g.ways[g.ew[p]][0], "take_side": side,
                                            "take_ways": sorted({g.ways[g.ew[s]][0] for s in safe}),
                                            "dist_to_fork_m": round(dist, 1)},
                          "no_return": {"node": g.ev[x], "lat": round(g.loc[g.ev[x]][0], 6), "lon": round(g.loc[g.ev[x]][1], 6),
                                        "way": g.ways[g.ew[x]][0], "leads_to": d["leads_class"]}})
        res.append({"branch_way": d["way"], "status": "forced", "points": items})
    return res


def zones(forks, g):
    # 直線 400 m 聚類（網格加速）
    idx = defaultdict(list)
    for i, f in enumerate(forks):
        idx[(int(f["lat"] / 0.004), int(f["lon"] / 0.004))].append(i)
    par = list(range(len(forks)))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]; a = par[a]
        return a
    for i, f in enumerate(forks):
        ci, cj = int(f["lat"] / 0.004), int(f["lon"] / 0.004)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for j in idx.get((ci + di, cj + dj), ()):
                    if j > i and hav((f["lat"], f["lon"]), (forks[j]["lat"], forks[j]["lon"])) <= 400:
                        par[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(len(forks)):
        groups[find(i)].append(i)
    zs = []
    for members in groups.values():
        fs = [forks[i] for i in members]
        zid = "tz%d" % min(f["node"] for f in fs)
        lays = set()
        for f in fs:
            for d in f["branches"]:
                lays |= set(d["layers"])
            lays.add(layer_of(g.tags(f["entry_edge"])))
        nodes = {f["node"] for f in fs}
        traps = sum(1 for f in fs if f["kind"] == "parallel")
        score = min(1.0, traps * 0.4 + (len(lays) - 1) * 0.2 + (len(nodes) - 1) * 0.15)
        for f in fs:
            f["zone"] = zid
        zs.append({"id": zid, "forks": len(fs), "nodes": len(nodes), "traps": traps, "layers": sorted(lays), "score": round(score, 2),
                   "lat": round(sum(f["lat"] for f in fs) / len(fs), 6), "lon": round(sum(f["lon"] for f in fs) / len(fs), 6)})
    return zs


def chain_order(g, forks):
    """區內順序與到下一個分岔的距離：沿留在平面那支往下游走 1500 m，碰到的下一個分岔"""
    key = {(f["node"], f["entry_way"]): f for f in forks}
    bynode = defaultdict(list)
    for f in forks:
        bynode[f["node"]].append(f)
    nxt = {}
    for f in forks:
        stay = next((d for d in f["branches"] if d["leads_class"] == "surface"), f["branches"][-1])
        pts, acc, edges = g.walk(stay["edge"], 1500)
        dsum = 0.0
        for k, e in enumerate(edges):
            dsum = acc[k + 1]
            v = g.ev[e]
            for f2 in bynode.get(v, ()):
                if f2 is not f and f2["entry_edge"] == e:
                    nxt[id(f)] = f2; f["next_fork"] = {"node": f2["node"], "dist_m": round(dsum, 1)}
                    break
            if id(f) in nxt:
                break
    has_pred = {id(x) for x in nxt.values()}
    for f in forks:
        if id(f) in has_pred:
            continue
        k, cur, guard = 1, f, 0
        while cur is not None and guard < 50:
            if cur.get("zone_order") is None or cur["zone_order"] < k:
                cur["zone_order"] = k
            cur = nxt.get(id(cur)); k += 1; guard += 1
    for f in forks:
        f.setdefault("zone_order", 1)


def clean(f):
    f = dict(f); f.pop("entry_edge", None)
    f["branches"] = [{k: v for k, v in d.items() if k != "edge"} for d in f["branches"]]
    return f


def augment_csv(path, g, forks):
    """forks_taiwan.csv 加三欄：通往等級、禁行牌別、區編號（原地改寫，保留 BOM）"""
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    cols = list(rows[0].keys())
    for c in ("通往等級", "禁行牌別", "區編號"):
        if c not in cols:
            cols.append(c)
    zpts = [(f["lat"], f["lon"], f["zone"]) for f in forks]
    zidx = defaultdict(list)
    for p in zpts:
        zidx[(int(p[0] / 0.004), int(p[1] / 0.004))].append(p)
    for r in rows:
        nid, tw = int(r["node"]), int(r["目標way"])
        cands = [f for f in g.out.get(nid, ()) if g.ways[g.ew[f]][0] == tw]
        lt, pl = "", ""
        if cands:
            try:
                ra = float(r["目標相對角"])
            except ValueError:
                ra = 0.0
            ins = [e for e in g.inn.get(nid, ()) if g.ways[g.ew[e]][0] == int(r["進入way"])]
            if ins and len(cands) > 1:
                h = g.back_bearing(ins[0])
                cands.sort(key=lambda f: abs(rel(g.ebear(f), h) - ra))
            classes, mno = chase(g, cands[0])
            lt = lead_label(classes); pl = "/".join(plates_for(classes, mno))
        la, lo = float(r["lat"]), float(r["lon"]); z = ""
        ci, cj = int(la / 0.004), int(lo / 0.004)
        best = 201.0
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for p in zidx.get((ci + di, cj + dj), ()):
                    dd = hav((la, lo), (p[0], p[1]))
                    if dd < best:
                        best, z = dd, p[2]
        r["通往等級"], r["禁行牌別"], r["區編號"] = lt, pl, z
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols); wr.writeheader(); wr.writerows(rows)
    return len(rows), sum(1 for r in rows if r["區編號"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--region", default="taiwan")
    ap.add_argument("--bbox", help="只輸出此框內的分岔 s,w,n,e（路網仍讀全區）")
    ap.add_argument("--cache", default="/root/rga/_audit_cache")
    ap.add_argument("--curated-lanes", default=os.path.join(os.path.dirname(__file__), "..", "..", "curated", "車道覆蓋.csv"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--forks-csv")
    ap.add_argument("--report")
    ap.add_argument("--pois", help="把 Honda 等 POI 寫到此 json")
    a = ap.parse_args()
    t0 = time.time()
    bbox = REGIONS[a.region]
    os.makedirs(a.cache, exist_ok=True)
    ways, pois = extract(a.pbf, bbox, os.path.join(a.cache, "trapways_%s_%d.pkl" % (a.region, int(os.path.getmtime(a.pbf)))))
    print("ways", len(ways), "pois", len(pois), round(time.time() - t0), "s", flush=True)
    if a.pois:
        json.dump(pois, open(a.pois, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    cur = {}
    if os.path.exists(a.curated_lanes):
        for r in csv.DictReader(open(a.curated_lanes, encoding="utf-8-sig")):
            if r.get("採用", "是") == "是" and r.get("lanes"):
                cur[int(r["way_id"])] = int(r["lanes"])
    g = G(ways)
    print("edges", len(g.ew), round(time.time() - t0), "s", flush=True)
    fb = tuple(map(float, a.bbox.split(","))) if a.bbox else bbox
    forks = find_forks(g, fb, cur)
    print("forks", len(forks), round(time.time() - t0), "s", flush=True)
    doom_mw = doomed_set(g, {"motorway"}); doom_hw = doomed_set(g, {"motorway", "trunk"})
    print("doomed", sum(doom_mw), sum(doom_hw), round(time.time() - t0), "s", flush=True)
    for f in forks:
        if f["misentry"]:
            f["no_return"] = no_return(g, f, doom_mw, doom_hw)
    zs = zones(forks, g)
    chain_order(g, forks)
    forks.sort(key=lambda f: (f["zone"], f["zone_order"], f["node"]))
    doc = {"version": time.strftime("%Y%m%d"), "source": os.path.basename(a.pbf), "license": "ODbL 1.0 © OpenStreetMap contributors",
           "params": {"max_turn": 135, "trap_angle": 30, "trap_lateral_m": 15, "chase_m": 3000, "chase_changes": 8, "zone_m": 400,
                      "plates": {"motorway": ALL, "trunk": ["white"]}},
           "zones": zs, "forks": [clean(f) for f in forks]}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(doc, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    rep = {"forks": len(forks), "zones": len(zs),
           "by_context": dict(sorted(defaultdict(int, {}).items())), "seconds": round(time.time() - t0)}
    cnt = defaultdict(int)
    for f in forks:
        cnt["context=" + f["context"]] += 1; cnt["kind=" + f["kind"]] += 1; cnt["confidence=" + f["confidence"]] += 1
        if f["misentry"]:
            cnt["misentry"] += 1; cnt["misentry_conf=" + f["confidence"]] += 1; cnt["misentry_kind=" + f["kind"]] += 1
            cnt["misentry_to=" + "+".join(sorted({d["leads_class"] for d in f["branches"] if d["leads_class"] in ("motorway", "trunk")}))] += 1
            for nr in f.get("no_return", []):
                cnt["no_return=" + nr["status"]] += 1
        if f["lane_split"]:
            cnt["lane_split"] += 1
    rep["counts"] = dict(sorted(cnt.items()))
    if a.forks_csv:
        n, z = augment_csv(a.forks_csv, g, forks)
        rep["forks_csv"] = {"rows": n, "with_zone": z}
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if a.report:
        json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
