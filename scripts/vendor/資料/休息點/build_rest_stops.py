"""休息點資料 taiwan-reststops.bin（NAV-REST-1）：從 OSM PBF 抽出騎車途中可休息／補給的地點，寫成 App 內建的小型二進位檔。

用法：
  python -I build_rest_stops.py --pbf ..\\..\\驗證\\desk-1\\data\\taiwan-latest.osm.pbf
  python -I build_rest_stops.py --pbf ... --no-cafe          # 不收咖啡廳
需要：pip install osmium（pyosmium 4.x）

抽取（節點＋封閉 way 取頂點平均當代表點；relation 不收，量很少）：
  類型 1 服務區   highway=services
  類型 2 休息區   highway=rest_area
  類型 3 加油站   amenity=fuel
  類型 4 便利商店 shop=convenience
  類型 5 廁所     amenity=toilets
  類型 6 觀景點   tourism=viewpoint
  類型 7 咖啡廳   amenity=cafe（--no-cafe 可不收）
  排除 access=private／no、disused／abandoned、construction；停車場、機車行不收。
同類型 30 m 內的點（同一間店同時畫了節點與面）合併為一筆，留有名稱的。

旗標（u8）：bit0 有廁所（toilets=yes，或本身是廁所／服務區）、bit1 24 小時（opening_hours=24/7）、bit2 收費（fee=yes，廁所用）。

品牌代碼（u8，0＝無）：常見連鎖店正規化（7-ELEVEN、全家、萊爾富、OK超商、美廉社、台灣中油、台塑石化、全國加油站…），
其餘有 brand 標籤的出現 ≥ 15 次也進表，少於的當無品牌。
名稱：name:zh-Hant → name:zh → name；有 branch 標籤且名稱不含 branch 就接上（「7-ELEVEN 谷關門市」）。

檔案格式（little-endian；規格同 說明.md）：
  標頭 48 bytes：magic "TRST"、u16 版本 1、u16 標頭長度 48、u32 筆數、u16 每筆長度 16、u16 排序格網（微度，10000＝0.01°）、
    u32 紀錄區 CRC-32、u32 OSM 資料時間（Unix 秒）、u32 品牌表位移、u32 品牌表長度、u32 名稱表位移、u32 名稱表長度、
    u32 品牌表＋名稱表 CRC-32、4 bytes 保留 0。
  紀錄區（緊接標頭）每筆 16 bytes：i32 緯度×1e6、i32 經度×1e6、u8 類型、u8 品牌、u8 旗標、u8 保留 0、u32 名稱位移（0xFFFFFFFF＝無名稱）。
    依 (緯度格, 經度格, 緯度, 經度, 類型) 排序。
  品牌表：u16 品牌數 n，接 n 筆「u8 長度＋UTF-8」（品牌代碼 1…n）。
  名稱表：每個名稱「u8 長度＋UTF-8」（≤ 120 bytes，超過在字元邊界截斷），相同名稱只存一次；名稱位移是相對名稱表開頭。
"""
import argparse
import datetime
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import time
import zlib

MAGIC = b"TRST"
VERSION = 1
HEADER_SIZE = 48
RECORD_SIZE = 16
CELL_MICRODEG = 10000
NO_NAME = 0xFFFFFFFF
MAX_NAME_BYTES = 120

K_SERVICES, K_REST_AREA, K_FUEL, K_CONVENIENCE, K_TOILETS, K_VIEWPOINT, K_CAFE = 1, 2, 3, 4, 5, 6, 7
KIND_NAMES = {K_SERVICES: "服務區", K_REST_AREA: "休息區", K_FUEL: "加油站", K_CONVENIENCE: "便利商店",
              K_TOILETS: "廁所", K_VIEWPOINT: "觀景點", K_CAFE: "咖啡廳"}
F_TOILETS, F_24H, F_FEE = 1, 2, 4

# 常見連鎖品牌：正規化名稱 → 比對用關鍵字（小寫比對 brand／name／operator）
KNOWN_BRANDS = [
    ("7-ELEVEN", ["7-eleven", "7-11", "7–eleven", "seven-eleven", "統一超商", "7eleven", "7-elevn"]),
    ("全家", ["全家", "familymart", "family mart"]),
    ("萊爾富", ["萊爾富", "hi-life", "hilife", "hi life"]),
    ("OK超商", ["ok超商", "ok mart", "okmart", "ok·mart", "ok便利", "ok‧mart"]),
    ("美廉社", ["美廉社", "simple mart"]),
    ("台灣中油", ["台灣中油", "中油", "cpc", "臺灣中油"]),
    ("台塑石化", ["台塑", "formosa", "臺塑"]),
    ("全國加油站", ["全國加油", "npc"]),
    ("統一精工", ["統一精工", "速邁樂", "semeuro"]),
    ("台亞", ["台亞", "臺亞"]),
    ("福懋", ["福懋"]),
    ("星巴克", ["星巴克", "starbucks"]),
    ("路易莎", ["路易莎", "louisa"]),
    ("85度C", ["85度c", "85°c", "85c"]),
    ("cama", ["cama"]),
]
EXCLUDED_ACCESS = {"private", "no"}
# shop=convenience 裡不是真正便利商店的（無人取貨店，沒有廁所也不賣熱食飲料）
NOT_CONVENIENCE = ["蝦皮", "店到店", "shopee"]
# highway=services／rest_area 有不少室內誤標（「補摺機」「實驗室」「計程車服務站」）：有名稱時名稱要像休息站
REST_NAME_KEYS = ["休息", "服務區", "驛站", "遊憩", "觀景", "rest", "service"]


def norm_brand(tags, kind):
    fields = [tags.get("brand"), tags.get("brand:zh"), tags.get("name"), tags.get("name:zh"), tags.get("operator"),
              tags.get("name:en"), tags.get("brand:en")]
    text = " ".join(f for f in fields if f).lower()
    for canon, keys in KNOWN_BRANDS:
        # 「中油」「cpc」只在加油站算；其他類型的店名含「中油」多半不是中油
        if canon in ("台灣中油", "台塑石化", "全國加油站", "統一精工", "台亞", "福懋") and kind != K_FUEL:
            continue
        if canon in ("7-ELEVEN", "全家", "萊爾富", "OK超商", "美廉社") and kind not in (K_CONVENIENCE, K_FUEL, K_SERVICES):
            continue
        if canon in ("星巴克", "路易莎", "85度C", "cama") and kind != K_CAFE:
            continue
        if any(k in text for k in keys):
            return canon
    b = tags.get("brand:zh") or tags.get("brand")
    return b.strip() if b else None


def pick_name(tags):
    name = tags.get("name:zh-Hant") or tags.get("name:zh_TW") or tags.get("name:zh") or tags.get("name")
    if name:
        name = name.strip()
    branch = (tags.get("branch") or "").strip()
    if name and branch and branch not in name:
        name = f"{name} {branch}"
    elif not name and branch:
        name = branch
    return name or None


def classify(tags):
    hw = tags.get("highway")
    am = tags.get("amenity")
    if hw == "services":
        return K_SERVICES
    if hw == "rest_area":
        return K_REST_AREA
    if am == "fuel":
        return K_FUEL
    if tags.get("shop") == "convenience":
        return K_CONVENIENCE
    if am == "toilets":
        return K_TOILETS
    if tags.get("tourism") == "viewpoint":
        return K_VIEWPOINT
    if am == "cafe":
        return K_CAFE
    return None


def excluded(tags):
    if tags.get("access") in EXCLUDED_ACCESS:
        return True
    for k in ("disused", "abandoned", "construction"):
        if tags.get(k) == "yes":
            return True
    for k in tags:
        if k.k.startswith(("disused:", "abandoned:", "was:", "razed:")):
            return True
    return False


def flags_of(tags, kind):
    f = 0
    if kind in (K_TOILETS, K_SERVICES) or tags.get("toilets") == "yes":
        f |= F_TOILETS
    oh = (tags.get("opening_hours") or "").replace(" ", "").lower()
    if oh in ("24/7", "mo-su00:00-24:00", "00:00-24:00"):
        f |= F_24H
    if tags.get("fee") == "yes":
        f |= F_FEE
    return f


def scan(pbf, with_cafe):
    import osmium
    raw = []  # (lat, lon, kind, brand, flags, name, osm_type)
    stats = {"nodes": 0, "ways": 0, "excluded": 0, "by_kind_raw": {}}
    fp = (osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY)
          .with_locations()
          .with_filter(osmium.filter.KeyFilter("highway", "amenity", "shop", "tourism")))
    for o in fp:
        tags = o.tags
        kind = classify(tags)
        if kind is None or (kind == K_CAFE and not with_cafe):
            continue
        if excluded(tags):
            stats["excluded"] += 1
            continue
        if kind in (K_SERVICES, K_REST_AREA):
            nm = (tags.get("name") or "").lower()
            if (nm and (not any(k in nm for k in REST_NAME_KEYS) or "計程車" in nm)) or tags.get("landuse") == "garages":
                stats["excluded_bad_rest_area"] = stats.get("excluded_bad_rest_area", 0) + 1
                continue
        if kind == K_CONVENIENCE:
            text = " ".join(v for v in (tags.get("brand"), tags.get("name"), tags.get("operator")) if v).lower()
            if any(k in text for k in NOT_CONVENIENCE):
                stats["excluded_pickup_store"] = stats.get("excluded_pickup_store", 0) + 1
                continue
        if o.is_node():
            if not o.location.valid():
                continue
            lat, lon = o.location.lat, o.location.lon
            stats["nodes"] += 1
        else:
            pts = [(n.lat, n.lon) for n in o.nodes if n.location.valid()]
            if not pts:
                continue
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]
            lat = sum(p[0] for p in pts) / len(pts)
            lon = sum(p[1] for p in pts) / len(pts)
            stats["ways"] += 1
        raw.append((lat, lon, kind, norm_brand(tags, kind), flags_of(tags, kind), pick_name(tags)))
        stats["by_kind_raw"][KIND_NAMES[kind]] = stats["by_kind_raw"].get(KIND_NAMES[kind], 0) + 1
    try:
        hdr = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header()
        ts = hdr.get("osmosis_replication_timestamp") or hdr.get("timestamp")
    except Exception:  # noqa: BLE001
        ts = None
    if ts:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    else:
        dt = datetime.datetime.fromtimestamp(os.path.getmtime(pbf), datetime.timezone.utc)
    return raw, stats, dt


def dist_m(a_lat, a_lon, b_lat, b_lon):
    k = math.cos(math.radians((a_lat + b_lat) / 2))
    dx = math.radians(b_lon - a_lon) * 6371000 * k
    dy = math.radians(b_lat - a_lat) * 6371000
    return math.hypot(dx, dy)


def dedup(raw, radius=30.0):
    """同類型 radius 內合併（同一間店的節點與面）；留有名稱／品牌的、旗標取聯集"""
    cell = 0.001
    buckets = {}
    out = []
    # 有名稱的先放，合併時保留它
    for r in sorted(raw, key=lambda r: (r[5] is None, r[3] is None)):
        lat, lon, kind = r[0], r[1], r[2]
        cy, cx = int(math.floor(lat / cell)), int(math.floor(lon / cell))
        hit = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for i in buckets.get((kind, cy + dy, cx + dx), ()):
                    if dist_m(lat, lon, out[i][0], out[i][1]) <= radius:
                        hit = i
                        break
                if hit is not None:
                    break
            if hit is not None:
                break
        if hit is not None:
            o = out[hit]
            out[hit] = (o[0], o[1], o[2], o[3] or r[3], o[4] | r[4], o[5] or r[5])
        else:
            out.append(r)
            buckets.setdefault((kind, cy, cx), []).append(len(out) - 1)
    return out


def clip_utf8(s, limit=MAX_NAME_BYTES):
    b = s.encode("utf-8")
    if len(b) <= limit:
        return b
    b = b[:limit]
    while b and (b[-1] & 0xC0) == 0x80:
        b = b[:-1]
    if b and b[-1] >= 0xC0:
        b = b[:-1]
    return b


def cell(v):
    return v // CELL_MICRODEG


def encode(rows, brands, osm_time):
    """rows: (lat_e6, lon_e6, kind, brand_code, flags, name or None)"""
    rows = sorted(rows, key=lambda r: (cell(r[0]), cell(r[1]), r[0], r[1], r[2], r[3], r[4], r[5] or ""))
    names = bytearray()
    name_off = {}
    recs = bytearray()
    for lat, lon, kind, brand, flags, name in rows:
        off = NO_NAME
        if name:
            nb = clip_utf8(name)
            if nb:
                if nb not in name_off:
                    name_off[nb] = len(names)
                    names += bytes([len(nb)]) + nb
                off = name_off[nb]
        recs += struct.pack("<iiBBBBI", lat, lon, kind, brand, flags, 0, off)
    btab = bytearray(struct.pack("<H", len(brands)))
    for b in brands:
        bb = clip_utf8(b, 60)
        btab += bytes([len(bb)]) + bb
    brand_off = HEADER_SIZE + len(recs)
    name_tab_off = brand_off + len(btab)
    header = struct.pack("<4sHHIHHIIIIIII4s", MAGIC, VERSION, HEADER_SIZE, len(rows), RECORD_SIZE, CELL_MICRODEG,
                         zlib.crc32(recs) & 0xFFFFFFFF, int(osm_time.timestamp()),
                         brand_off, len(btab), name_tab_off, len(names),
                         zlib.crc32(bytes(btab) + bytes(names)) & 0xFFFFFFFF, b"\0" * 4)
    assert len(header) == HEADER_SIZE
    return bytes(header) + bytes(recs) + bytes(btab) + bytes(names)


def decode(data):
    (magic, ver, hsize, count, rsize, _cell, crc, _ts, boff, blen, noff, nlen, tcrc, _) = \
        struct.unpack("<4sHHIHHIIIIIII4s", data[:HEADER_SIZE])
    assert magic == MAGIC and ver == VERSION and rsize == RECORD_SIZE
    recs = data[hsize:hsize + count * rsize]
    assert len(recs) == count * rsize and (zlib.crc32(recs) & 0xFFFFFFFF) == crc
    btab = data[boff:boff + blen]
    ntab = data[noff:noff + nlen]
    assert (zlib.crc32(btab + ntab) & 0xFFFFFFFF) == tcrc
    n = struct.unpack_from("<H", btab, 0)[0]
    brands, p = [], 2
    for _ in range(n):
        ln = btab[p]
        brands.append(btab[p + 1:p + 1 + ln].decode("utf-8"))
        p += 1 + ln
    out = []
    for i in range(count):
        lat, lon, kind, brand, flags, _r, off = struct.unpack_from("<iiBBBBI", recs, i * rsize)
        name = None
        if off != NO_NAME:
            ln = ntab[off]
            name = ntab[off + 1:off + 1 + ln].decode("utf-8")
        out.append((lat, lon, kind, brand, flags, name))
    return out, brands


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", default=os.path.join(here, "taiwan-reststops.bin"))
    ap.add_argument("--copy-to", default=os.path.join(here, "..", "..", "ios", "App", "Resources", "taiwan-reststops.bin"))
    ap.add_argument("--stats", default=os.path.join(here, "build_stats.json"))
    ap.add_argument("--no-cafe", action="store_true")
    ap.add_argument("--brand-min", type=int, default=15)
    a = ap.parse_args()
    t0 = time.time()
    raw, stats, osm_time = scan(a.pbf, not a.no_cafe)
    merged = dedup(raw)
    # 品牌表：已知連鎖一律收；其他 brand 出現 ≥ brand_min 才收
    counts = {}
    for r in merged:
        if r[3]:
            counts[r[3]] = counts.get(r[3], 0) + 1
    known = [c for c, _ in KNOWN_BRANDS]
    brands = [b for b in known if counts.get(b)] + \
        sorted((b for b, c in counts.items() if b not in known and c >= a.brand_min), key=lambda b: (-counts[b], b))
    brands = brands[:255]
    code = {b: i + 1 for i, b in enumerate(brands)}
    rows = []
    for lat, lon, kind, brand, flags, name in merged:
        bc = code.get(brand, 0)
        # 名稱與品牌相同就不存名稱（顯示時用品牌）
        if name and bc and name.strip().lower() == brand.lower():
            name = None
        rows.append((int(round(lat * 1e6)), int(round(lon * 1e6)), kind, bc, flags, name))
    data = encode(rows, brands, osm_time)
    back, back_brands = decode(data)
    assert back_brands == brands
    assert sorted(back, key=lambda r: (r[0], r[1], r[2], r[3], r[4], r[5] or "")) == \
        sorted([(r[0], r[1], r[2], r[3], r[4], (clip_utf8(r[5]).decode("utf-8") or None) if r[5] else None) for r in rows],
               key=lambda r: (r[0], r[1], r[2], r[3], r[4], r[5] or "")), "往返解碼不一致"
    with open(a.out, "wb") as f:
        f.write(data)
    if a.copy_to:
        shutil.copyfile(a.out, a.copy_to)
    by_kind = {}
    named = {}
    for r in rows:
        by_kind[KIND_NAMES[r[2]]] = by_kind.get(KIND_NAMES[r[2]], 0) + 1
        if r[5] or r[3]:
            named[KIND_NAMES[r[2]]] = named.get(KIND_NAMES[r[2]], 0) + 1
    _, _, _, _, _, _, _, _, boff, blen, noff, nlen, _, _ = struct.unpack("<4sHHIHHIIIIIII4s", data[:HEADER_SIZE])
    stats.update({
        "pbf": os.path.basename(a.pbf),
        "osm_time": osm_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "with_cafe": not a.no_cafe,
        "records": len(rows),
        "by_kind": by_kind,
        "with_name_or_brand": named,
        "brands": {b: counts.get(b, 0) for b in brands},
        "brand_table_bytes": blen,
        "name_table_bytes": nlen,
        "record_bytes": len(rows) * RECORD_SIZE,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "seconds": round(time.time() - t0, 1),
        "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    with open(a.stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
