"""底圖顯示用平滑：高架（bridge）、匝道（*_link）、國道／快速道路（motorway／trunk）的線條
重取樣＋輕度平滑，避免 OSM 稀疏節點在導航地圖上畫出折角。只改「底圖專用」的 pbf 複本，路網照用原檔。

規則：
- 固定點（座標與 node id 都不動）：way 端點、被兩條以上道路 way（或同一 way 兩次）引用的節點、帶標籤的節點
  （號誌、出口等）。所以與其他道路的接點完全不變，不會接不上。
- 兩個固定點之間的一段：只有兩點（直線）就不動；否則每 3 m 重取樣，做 [1/4,1/2,1/4] 平滑多次（段端點不動），
  每個點離原折線最多 1.5 m（超過就拉回到 1.5 m），最後以 0.25 m 容差 Douglas–Peucker 精簡，避免節點暴增。
- 新節點用 NEW_NODE_BASE 起的新 id，寫在所有原節點之後（id 遞增，planetiler 稀疏節點索引可用）。

用法：python -I smooth_display.py <in.pbf> <out.pbf> [--report out.json]
"""
import json
import math
import sys

STEP_M = 3.0
ITERATIONS = 6
MAX_OFFSET_M = 1.5
SIMPLIFY_M = 0.25
NEW_NODE_BASE = 30_000_000_000  # renumber_synthetic.py 的合成節點在 2e10 起，OSM 目前約 1.3e10
R_EARTH = 6378137.0


NON_VEHICLE = {"footway", "path", "pedestrian", "steps", "cycleway", "bridleway", "corridor", "elevator", "platform"}


def eligible(tags):
    """tags：dict。回傳這條 way 要不要做顯示平滑（只平滑車行道路的線；人行／自行車道與面狀 area=yes 不動）。"""
    hw = tags.get("highway")
    if not hw or hw in NON_VEHICLE or tags.get("area") == "yes":
        return False
    if hw.endswith("_link") or hw in ("motorway", "trunk"):
        return True
    br = tags.get("bridge")
    return br is not None and br != "no"


# ---- 幾何（局部等距平面，公尺） ----

def to_xy(lonlat, lat0):
    k = math.cos(math.radians(lat0))
    return [(math.radians(lon) * R_EARTH * k, math.radians(lat) * R_EARTH) for lon, lat in lonlat]


def to_lonlat(xy, lat0):
    k = math.cos(math.radians(lat0))
    return [(math.degrees(x / (R_EARTH * k)), math.degrees(y / R_EARTH)) for x, y in xy]


def _closest_on_seg(p, a, b):
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return (ax + t * dx, ay + t * dy)


def closest_on_polyline(p, line):
    best, bd = line[0], float("inf")
    for a, b in zip(line, line[1:]):
        q = _closest_on_seg(p, a, b)
        d = math.hypot(p[0] - q[0], p[1] - q[1])
        if d < bd:
            best, bd = q, d
    return best, bd


def resample(line, step=STEP_M):
    """沿弧長每 step 公尺取一點，兩端點保留原值。"""
    out = [line[0]]
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(line, line[1:]))
    if total <= step:
        return list(line)
    n = max(1, int(round(total / step)))
    seg = total / n
    targets = [seg * i for i in range(1, n)]
    acc, ti = 0.0, 0
    for a, b in zip(line, line[1:]):
        L = math.hypot(b[0] - a[0], b[1] - a[1])
        while ti < len(targets) and targets[ti] <= acc + L:
            t = 0.0 if L == 0 else (targets[ti] - acc) / L
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            ti += 1
        acc += L
    out.append(line[-1])
    return out


def simplify(line, tol=SIMPLIFY_M):
    if len(line) <= 2:
        return list(line)
    keep = [False] * len(line)
    keep[0] = keep[-1] = True
    stack = [(0, len(line) - 1)]
    while stack:
        i, j = stack.pop()
        far, fd = None, tol
        for k in range(i + 1, j):
            q = _closest_on_seg(line[k], line[i], line[j])
            d = math.hypot(line[k][0] - q[0], line[k][1] - q[1])
            if d > fd:
                far, fd = k, d
        if far is not None:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(line, keep) if k]


def smooth_piece(line, step=STEP_M, iterations=ITERATIONS, max_off=MAX_OFFSET_M, tol=SIMPLIFY_M):
    """兩個固定點之間的一段（公尺座標）。回傳新折線，首尾與輸入完全相同。"""
    if len(line) <= 2:
        return list(line)
    pts = resample(line, step)
    for _ in range(iterations):
        pts = [pts[0]] + [((pts[i - 1][0] + 2 * pts[i][0] + pts[i + 1][0]) / 4,
                           (pts[i - 1][1] + 2 * pts[i][1] + pts[i + 1][1]) / 4)
                          for i in range(1, len(pts) - 1)] + [pts[-1]]
    out = [line[0]]
    for p in pts[1:-1]:
        q, d = closest_on_polyline(p, line)
        if d > max_off:
            f = max_off / d
            p = (q[0] + (p[0] - q[0]) * f, q[1] + (p[1] - q[1]) * f)
        out.append(p)
    out.append(line[-1])
    return simplify(out, tol)


def smooth_way(lonlat, fixed):
    """lonlat：way 的座標；fixed：同長度布林（固定點）。回傳 [(lon,lat 或 None 表示沿用原點, 原 index 或 None)]。
    固定點以 (None, i) 表示（沿用原 node），新點以 ((lon,lat), None) 表示。"""
    lat0 = sum(p[1] for p in lonlat) / len(lonlat)
    xy = to_xy(lonlat, lat0)
    fixed = list(fixed)
    fixed[0] = fixed[-1] = True
    out = [(None, 0)]
    start = 0
    for i in range(1, len(xy)):
        if not fixed[i]:
            continue
        piece = smooth_piece(xy[start:i + 1])
        if len(piece) == i - start + 1 and piece == xy[start:i + 1]:
            out += [(None, k) for k in range(start + 1, i + 1)]  # 直線或太短：原點不動
        else:
            out += [(ll, None) for ll in to_lonlat(piece[1:-1], lat0)]
            out.append((None, i))
        start = i
    return out


def max_offset_m(orig_lonlat, new_lonlat):
    lat0 = sum(p[1] for p in orig_lonlat) / len(orig_lonlat)
    a, b = to_xy(orig_lonlat, lat0), to_xy(new_lonlat, lat0)
    return max(closest_on_polyline(p, a)[1] for p in b)


# ---- pbf ----

def main(argv):
    import osmium  # 只有實際轉檔才需要（單元測試不需要）

    src, dst = argv[0], argv[1]
    report_path = argv[argv.index("--report") + 1] if "--report" in argv else None

    class Pass1(osmium.SimpleHandler):
        """收集：平滑候選 way 的節點座標、所有道路 way 的節點引用次數（只計候選 way 用到的節點）、帶標籤節點。"""

        def __init__(self):
            super().__init__()
            self.cand = {}      # way id -> [(node id, lon, lat)]
            self.cand_nodes = set()
            self.tagged = set()

        def way(self, w):
            if eligible({t.k: t.v for t in w.tags}):
                try:
                    nodes = [(n.ref, n.location.lon, n.location.lat) for n in w.nodes]
                except osmium.InvalidLocationError:
                    return
                if len(nodes) >= 3:
                    self.cand[w.id] = nodes
                    self.cand_nodes.update(n[0] for n in nodes)

    p1 = Pass1()
    p1.apply_file(src, locations=True, idx="flex_mem")

    class Pass2(osmium.SimpleHandler):
        def __init__(self, cand_nodes):
            super().__init__()
            self.cand_nodes = cand_nodes
            self.uses = {}
            self.tagged = set()

        def node(self, n):
            if n.id in self.cand_nodes and len(n.tags):
                self.tagged.add(n.id)

        def way(self, w):
            if "highway" not in w.tags:
                return
            for r in w.nodes:
                if r.ref in self.cand_nodes:
                    self.uses[r.ref] = self.uses.get(r.ref, 0) + 1

    p2 = Pass2(p1.cand_nodes)
    p2.apply_file(src)

    new_nodes, new_ways, stats = [], {}, {"ways": 0, "changed_ways": 0, "new_nodes": 0, "max_offset_m": 0.0}
    nid = NEW_NODE_BASE
    for wid, nodes in p1.cand.items():
        stats["ways"] += 1
        fixed = [p2.uses.get(n[0], 0) > 1 or n[0] in p2.tagged for n in nodes]
        res = smooth_way([(n[1], n[2]) for n in nodes], fixed)
        if all(ll is None for ll, _ in res) and len(res) == len(nodes):
            continue
        refs, geo = [], []
        for ll, i in res:
            if ll is None:
                refs.append(nodes[i][0])
                geo.append((nodes[i][1], nodes[i][2]))
            else:
                new_nodes.append((nid, ll))
                refs.append(nid)
                geo.append(ll)
                nid += 1
        new_ways[wid] = refs
        stats["changed_ways"] += 1
        stats["max_offset_m"] = max(stats["max_offset_m"], max_offset_m([(n[1], n[2]) for n in nodes], geo))
    stats["new_nodes"] = len(new_nodes)

    class Writer(osmium.SimpleHandler):
        def __init__(self, w):
            super().__init__()
            self.w, self.flushed = w, False

        def _flush(self):
            if not self.flushed:
                for i, (lon, lat) in new_nodes:
                    self.w.add_node(osmium.osm.mutable.Node(id=i, location=osmium.osm.Location(lon, lat), version=1))
                self.flushed = True

        def node(self, n):
            self.w.add_node(n)

        def way(self, w):
            self._flush()
            refs = new_ways.get(w.id)
            self.w.add_way(w.replace(nodes=refs) if refs is not None else w)

        def relation(self, r):
            self._flush()
            self.w.add_relation(r)

    wr = osmium.SimpleWriter(dst, overwrite=True)
    Writer(wr).apply_file(src)
    wr.close()
    stats["max_offset_m"] = round(stats["max_offset_m"], 3)
    print(f"顯示平滑：候選 way {stats['ways']}、改形 {stats['changed_ways']}、新節點 {stats['new_nodes']}、"
          f"最大偏移 {stats['max_offset_m']} m")
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)
    return stats


if __name__ == "__main__":
    main(sys.argv[1:])
