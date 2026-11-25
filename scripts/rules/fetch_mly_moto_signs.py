"""下載全台號誌路口附近的 Mapillary 機車相關交通標誌（給 build_moto_left_turn.py 用）。
  python scripts/rules/fetch_mly_moto_signs.py [--signals data/rules/moto_left_turn.csv] [--out data/rules/mapillary_moto_signs.csv]

來源：Mapillary Graph API map_features（object_values 篩選），以含號誌節點的 z14 圖磚範圍分塊查詢。
類別（2026-11-26 抽樣臺北忠孝東路、仁愛路實照驗證）：
  regulatory--two-stage-right-turn-for-mopeds--g1 → 台灣「遵20 機慢車兩段左轉」（Mapillary 用日本類別名，圖形相同）
  regulatory--no-motorcycles--g1 / --g2           → 「禁行機車」（常附「禁內側車道」副牌，但也可能是全路段禁行）
Mapillary 沒有「機車待轉區」地面標線類別（只有通用 marking--discrete--other-marking），不抓。
金鑰：%USERPROFILE%\\.claude\\secrets\\mapillary-token.txt 或環境變數 MAPILLARY_TOKEN（不印出）。
"""
import argparse, csv, json, math, os, sys, time, urllib.parse, urllib.request, concurrent.futures as cf

R = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KIND = {"regulatory--two-stage-right-turn-for-mopeds--g1": "zun20",
        "regulatory--no-motorcycles--g1": "no_moto", "regulatory--no-motorcycles--g2": "no_moto"}
Z = 14


def token():
    t = os.environ.get("MAPILLARY_TOKEN")
    if t: return t.strip()
    return open(os.path.join(os.path.expanduser("~"), ".claude", "secrets", "mapillary-token.txt")).read().strip()


def tile_of(lat, lon):
    n = 2 ** Z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def tile_to_ll(x, y, px, py, ext):
    n = 2 ** Z
    fx, fy = x + px / ext, y + 1 - py / ext   # MVT y 軸向上
    lon = fx / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n))))
    return lat, lon


def fetch(xy, tok):
    """以圖磚範圍為 bbox 呼叫 Graph API map_features（object_values 篩選）。
    向量圖磚每塊上限 8192 個要素、市區會被截斷，所以不用圖磚內容，只借圖磚當分塊。"""
    x, y = xy
    la1, lo1 = tile_to_ll(x, y, 0, 0, 1); la2, lo2 = tile_to_ll(x, y, 1, 1, 1)
    q = dict(access_token=tok, fields="id,object_value,geometry,first_seen_at,last_seen_at,aligned_direction",
             bbox=f"{min(lo1, lo2)},{min(la1, la2)},{max(lo1, lo2)},{max(la1, la2)}", object_values=",".join(KIND), limit=2000)
    url = "https://graph.mapillary.com/map_features?" + urllib.parse.urlencode(q)
    for _ in range(3):
        try:
            d = json.load(urllib.request.urlopen(url, timeout=120))
            break
        except Exception:
            time.sleep(3)
    else:
        return xy, None
    out = []
    for f in d.get("data", []):
        k = KIND.get(f.get("object_value")); g = f.get("geometry") or {}
        if not k or g.get("type") != "Point": continue
        lon, lat = g["coordinates"]
        out.append([f["id"], k, f["object_value"], f"{lat:.7f}", f"{lon:.7f}", f.get("first_seen_at", "")[:10],
                    f.get("last_seen_at", "")[:10], f.get("aligned_direction", "")])
    return xy, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default=os.path.join(R, "data", "rules", "moto_left_turn.csv"))
    ap.add_argument("--out", default=os.path.join(R, "data", "rules", "mapillary_moto_signs.csv"))
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    tiles = set()
    for r in csv.DictReader(open(a.signals, encoding="utf-8")):
        tiles.add(tile_of(float(r["lat"]), float(r["lon"])))
    tok = token(); rows = {}; fail = 0
    print(f"{len(tiles)} 個 z{Z} 圖磚", flush=True)
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for i, (xy, out) in enumerate(ex.map(lambda t: fetch(t, tok), sorted(tiles))):
            if out is None: fail += 1; continue
            for r in out: rows[r[0]] = r   # 跨圖磚重複的同一要素以 id 去重
            if i % 500 == 0: print(i, len(rows), flush=True)
    with open(a.out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f); w.writerow(["feature_id", "kind", "object_value", "lat", "lon", "first_seen", "last_seen", "aligned_direction"])
        w.writerows(sorted(rows.values(), key=lambda r: str(r[0])))
    from collections import Counter
    print("完成", len(rows), dict(Counter(r[1] for r in rows.values())), "失敗圖磚", fail)


if __name__ == "__main__":
    main()
