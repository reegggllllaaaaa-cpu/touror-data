"""紅綠燈位置修正（SIGNALS-FIX-1）：用 OSM PBF 的 highway way（含 bridge／layer）把號誌點吸附到平面路口，並標旗標。

由 build_signals.py 呼叫（process），也可單獨對既有 bin 重算：
  python -I snap_signals.py --pbf ..\\..\\驗證\\desk-1\\data\\taiwan-latest.osm.pbf --in-bin 舊.bin --out 新.bin
需要 numpy、pyosmium 4.x。全量處理、不抽樣。

規則（距離都用同一個等距投影，台灣範圍內誤差 <2%）：
  車行道＝highway 15 類（motorway trunk primary secondary tertiary unclassified residential service living_street road ＋五種 _link）。
  高架 way＝(bridge 有值且非 no，或 layer>0，或 motorway／motorway_link)，且 tunnel 不是 yes。其餘車行道＝平面。
  1. 在高架上：最近高架 way 距離 ≤8 m（motorway 類 ≤10 m），且比最近平面車行道近 0.5 m 以上。
     - 40 m 內有平面路口節點（平面車行道 way 共用 ≥2 次的節點）→ 吸附到最近的那個節點（snap_junction）
     - 否則 25 m 內有平面車行道 → 吸附到最近平面車行道上最近的點（snap_road）
     - 否則標 bit4 elevated（地圖與路線計數不畫、不計；資料保留）
  2. 不在高架上：離最近車行道（平面或高架）>40 m 標 bit5 offroad（不畫）；>20 m 吸附到最近車行道上最近的點（snap_offroad）。
  3. 吸附後，位置在平面路口節點 6 m 內者標 bit6（atIntersection，App 合併時優先選它當代表點）。
  4. 行人穿越燈（bit1 無 bit0）25 m 內有路口燈（bit0 無 bit1、未被隱藏）者標 bit7（merged，地圖併入路口燈、不另畫）。
旗標：bit0 路口號誌、bit1 行人穿越、bit2 highway=crossing、bit3 政府補點（發布版既有）、bit4 elevated、bit5 offroad、bit6 atIntersection、bit7 merged。
"""
import argparse
import math
import os
import sys

import numpy as np

CARRIAGEWAY = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "service",
               "living_street", "road", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
MOTORWAY = {"motorway", "motorway_link"}
FLAG_SIGNAL, FLAG_CROSSING = 1, 2
FLAG_ELEVATED, FLAG_OFFROAD, FLAG_AT_JUNCTION, FLAG_MERGED = 16, 32, 64, 128
FLAG_HIDDEN = FLAG_ELEVATED | FLAG_OFFROAD

ELEV_NEAR_M, ELEV_NEAR_MOTORWAY_M = 8.0, 10.0
ELEV_MARGIN_M = 0.5
SNAP_JUNCTION_M = 40.0
SNAP_ROAD_M = 25.0
OFFROAD_SNAP_M, OFFROAD_HIDE_M = 20.0, 40.0
AT_JUNCTION_M = 6.0
PED_MERGE_M = 25.0
PIECE_M = 40.0
CELL_M = 80.0
LAT0 = 23.6
KY = 111320.0
KX = 111320.0 * math.cos(math.radians(LAT0))


def to_xy(lat, lon):
    return np.asarray(lon, dtype=np.float64) * KX, np.asarray(lat, dtype=np.float64) * KY


def from_xy(x, y):
    return y / KY, x / KX


def load_ways(pbf, log=print):
    """讀 PBF 的車行道。回傳 (segs, junction_xy)。segs＝dict of arrays（已切成 ≤PIECE_M 的小段）。"""
    import osmium
    ax, ay, bx, by, elev, thr = [], [], [], [], [], []
    refs_all, x_all, y_all = [], [], []
    fp = (osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY).with_locations()
          .with_filter(osmium.filter.KeyFilter("highway")))
    n = 0
    for w in fp:
        if not hasattr(w, "nodes"):
            continue
        hw = w.tags.get("highway")
        if hw not in CARRIAGEWAY:
            continue
        tags = w.tags
        bridge = tags.get("bridge")
        layer = tags.get("layer")
        try:
            layer_v = int(float(layer)) if layer else 0
        except ValueError:
            layer_v = 0
        is_elev = hw in MOTORWAY or (bridge not in (None, "no")) or layer_v > 0
        if tags.get("tunnel") not in (None, "no"):
            is_elev = False
        refs, lat, lon = [], [], []
        for nd in w.nodes:
            if not nd.location.valid():
                continue
            refs.append(nd.ref)
            lat.append(nd.lat)
            lon.append(nd.lon)
        if len(refs) < 2:
            continue
        x, y = to_xy(lat, lon)
        n += 1
        if not is_elev:
            refs_all.append(np.asarray(refs, dtype=np.int64))
            x_all.append(x)
            y_all.append(y)
        ax.append(x[:-1]); ay.append(y[:-1]); bx.append(x[1:]); by.append(y[1:])
        k = len(x) - 1
        elev.append(np.full(k, is_elev, dtype=bool))
        thr.append(np.full(k, ELEV_NEAR_MOTORWAY_M if hw in MOTORWAY else ELEV_NEAR_M))
    log(f"  車行道 way {n:,}")
    ax, ay, bx, by = (np.concatenate(v) for v in (ax, ay, bx, by))
    elev = np.concatenate(elev)
    thr = np.concatenate(thr)
    # 長線段切成 ≤PIECE_M 的小段，格網才找得到
    ln = np.hypot(bx - ax, by - ay)
    parts = np.maximum(1, np.ceil(ln / PIECE_M)).astype(np.int64)
    if (parts > 1).any():
        idx = np.repeat(np.arange(len(ax)), parts)
        start = np.cumsum(parts) - parts
        j = np.arange(len(idx)) - np.repeat(start, parts)
        p = np.repeat(parts, parts).astype(np.float64)
        t0, t1 = j / p, (j + 1) / p
        dx, dy = (bx - ax)[idx], (by - ay)[idx]
        nax, nay = ax[idx] + dx * t0, ay[idx] + dy * t0
        nbx, nby = ax[idx] + dx * t1, ay[idx] + dy * t1
        ax, ay, bx, by, elev, thr = nax, nay, nbx, nby, elev[idx], thr[idx]
    refs = np.concatenate(refs_all)
    xs = np.concatenate(x_all)
    ys = np.concatenate(y_all)
    uniq, first, cnt = np.unique(refs, return_index=True, return_counts=True)
    sel = first[cnt >= 2]
    jx, jy = xs[sel], ys[sel]
    log(f"  切成小段 {len(ax):,}（高架 {int(elev.sum()):,}）；平面路口節點 {len(jx):,}")
    return (ax, ay, bx, by, elev, thr), (jx, jy)


class Grid:
    """以 CELL_M 方格索引一組點（線段用中點）。query 回傳 3×3 鄰格內的索引。"""

    def __init__(self, x, y):
        self.cx = np.floor(x / CELL_M).astype(np.int64)
        self.cy = np.floor(y / CELL_M).astype(np.int64)
        key = self.key(self.cx, self.cy)
        self.order = np.argsort(key, kind="stable")
        self.sorted_key = key[self.order]

    @staticmethod
    def key(cx, cy):
        return cx * 4_000_003 + cy

    def query(self, x, y):
        cx, cy = int(math.floor(x / CELL_M)), int(math.floor(y / CELL_M))
        out = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = cx + dx, cy + dy
                kk = k[0] * 4_000_003 + k[1]
                lo = np.searchsorted(self.sorted_key, kk, "left")
                hi = np.searchsorted(self.sorted_key, kk, "right")
                if hi > lo:
                    out.append(self.order[lo:hi])
        return np.concatenate(out) if out else np.empty(0, dtype=np.int64)


def seg_nearest(px, py, ax, ay, bx, by):
    """點到多條線段：回傳 (最小距離, 最近點 x, y)"""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = np.where(l2 > 0, ((px - ax) * dx + (py - ay) * dy) / np.where(l2 > 0, l2, 1), 0.0)
    t = np.clip(t, 0.0, 1.0)
    qx, qy = ax + t * dx, ay + t * dy
    d = np.hypot(px - qx, py - qy)
    i = int(np.argmin(d))
    return float(d[i]), float(qx[i]), float(qy[i])


def process(records, pbf, log=print):
    """records：[(lat_e6, lon_e6, flags)]。回傳 (新 records, 統計 dict)。"""
    segs, (jx, jy) = load_ways(pbf, log)
    ax, ay, bx, by, elev, thr = segs
    seg_grid = Grid((ax + bx) / 2, (ay + by) / 2)
    junc_grid = Grid(jx, jy)
    ws = {"total": len(records), "snap_junction": 0, "snap_road": 0, "elevated_flag": 0,
          "snap_offroad": 0, "offroad_flag": 0, "at_junction": 0, "merged": 0, "on_elevated": 0, "kept": 0}
    out = []
    ped_only_idx = []
    for (lat_e6, lon_e6, flags) in records:
        flags &= 0x0F  # 舊旗標（高位元重算）
        x, y = to_xy(lat_e6 / 1e6, lon_e6 / 1e6)
        x, y = float(x), float(y)
        cand = seg_grid.query(x, y)
        d_f = d_e = float("inf")
        pf = pe = None
        thr_e = ELEV_NEAR_M
        if len(cand):
            for want_elev in (False, True):
                m = cand[elev[cand] == want_elev]
                if not len(m):
                    continue
                d, qx, qy = seg_nearest(x, y, ax[m], ay[m], bx[m], by[m])
                if want_elev:
                    d_e, pe = d, (qx, qy)
                    # 該最近段的門檻
                    dd = np.hypot(x - np.clip(x, np.minimum(ax[m], bx[m]), np.maximum(ax[m], bx[m])),
                                  y - np.clip(y, np.minimum(ay[m], by[m]), np.maximum(ay[m], by[m])))
                    thr_e = float(thr[m][int(np.argmin(dd))])
                else:
                    d_f, pf = d, (qx, qy)
        moved = None
        on_elev = d_e <= thr_e and d_e + ELEV_MARGIN_M < d_f
        if on_elev:
            ws["on_elevated"] += 1
            jc = junc_grid.query(x, y)
            best = None
            if len(jc):
                dj = np.hypot(jx[jc] - x, jy[jc] - y)
                k = int(np.argmin(dj))
                if dj[k] <= SNAP_JUNCTION_M:
                    best = (float(jx[jc][k]), float(jy[jc][k]))
            if best is not None:
                moved = best
                ws["snap_junction"] += 1
            elif d_f <= SNAP_ROAD_M and pf is not None:
                moved = pf
                ws["snap_road"] += 1
            else:
                flags |= FLAG_ELEVATED
                ws["elevated_flag"] += 1
        else:
            d_min = min(d_f, d_e)
            if d_min > OFFROAD_HIDE_M:
                flags |= FLAG_OFFROAD
                ws["offroad_flag"] += 1
            elif d_min > OFFROAD_SNAP_M:
                moved = pf if d_f <= d_e else pe
                ws["snap_offroad"] += 1
        if moved is not None:
            x, y = moved
        if not flags & FLAG_HIDDEN:
            jc = junc_grid.query(x, y)
            if len(jc) and np.hypot(jx[jc] - x, jy[jc] - y).min() <= AT_JUNCTION_M:
                flags |= FLAG_AT_JUNCTION
                ws["at_junction"] += 1
        lat, lon = from_xy(x, y)
        if moved is not None:
            lat_e6, lon_e6 = int(round(float(lat) * 1e6)), int(round(float(lon) * 1e6))
        out.append([lat_e6, lon_e6, flags])
    # 行人燈併入同路口的路口燈
    veh = [(i, r) for i, r in enumerate(out) if r[2] & FLAG_SIGNAL and not r[2] & FLAG_CROSSING and not r[2] & FLAG_HIDDEN]
    vx, vy = to_xy([r[0] / 1e6 for _, r in veh], [r[1] / 1e6 for _, r in veh])
    vg = Grid(vx, vy)
    for r in out:
        if r[2] & FLAG_CROSSING and not r[2] & FLAG_SIGNAL and not r[2] & FLAG_HIDDEN:
            x, y = to_xy(r[0] / 1e6, r[1] / 1e6)
            c = vg.query(float(x), float(y))
            if len(c) and np.hypot(vx[c] - x, vy[c] - y).min() <= PED_MERGE_M:
                r[2] |= FLAG_MERGED
                ws["merged"] += 1
    ws["kept"] = sum(1 for r in out if not r[2] & FLAG_HIDDEN)
    ws["hidden"] = ws["total"] - ws["kept"]
    return [tuple(r) for r in out], ws


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    import build_signals as b
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--in-bin", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    data = open(a.in_bin, "rb").read()
    ts = int.from_bytes(data[20:24], "little")
    recs = b.decode(data)
    import datetime
    new, st = process(recs, a.pbf)
    open(a.out, "wb").write(b.encode(new, datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)))
    print(st)


if __name__ == "__main__":
    main()
