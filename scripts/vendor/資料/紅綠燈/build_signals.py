"""紅綠燈資料 taiwan-signals.bin（NAV-SIGNAL-1）：從 OSM PBF 抽出號誌節點，寫成 App 內建的小型二進位檔。

用法（在任何目錄都可）：
  python -I build_signals.py --pbf ..\\..\\驗證\\desk-1\\data\\taiwan-latest.osm.pbf
  python -I build_signals.py --pbf ... --out taiwan-signals.bin --copy-to ..\\..\\ios\\App\\Resources\\taiwan-signals.bin
需要：pip install osmium（pyosmium 4.x）

抽取規則（只看節點）：
  - highway=traffic_signals                     → 旗標 bit0（路口號誌）
  - crossing=traffic_signals 或 crossing:signals=yes → 旗標 bit1（行人穿越號誌；行人按鈕燈對騎士影響較小，App 分開計）
  - highway=crossing                             → 旗標 bit2（只在同時有 bit1 時才會收進來，供 App 區分）
  - traffic_signals=blinker／blinkers／blink_mode／emergency（閃光號誌、緊急車輛號誌）不會讓人停車，排除（統計另列）。
  同一路口多個節點不在這裡合併；App 讀取時以 20 m 去重（SignalIndex）。

檔案格式（little-endian；規格同 說明.md）：
  標頭 32 bytes：magic "TSIG"、u16 版本 1、u16 標頭長度 32、u32 筆數、u16 每筆長度 9、u16 排序格網（微度，10000＝0.01°）、
  u32 資料 CRC-32、u32 OSM 資料時間（Unix 秒）、8 bytes 保留 0。
  每筆 9 bytes：i32 緯度×1e6、i32 經度×1e6、u8 旗標。依 (緯度格, 經度格, 緯度, 經度) 排序。
"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import struct
import sys
import time
import zlib

MAGIC = b"TSIG"
VERSION = 1
HEADER_SIZE = 32
RECORD_SIZE = 9
CELL_MICRODEG = 10000
FLAG_SIGNAL = 1
FLAG_CROSSING = 2
FLAG_HIGHWAY_CROSSING = 4
EXCLUDED_KINDS = {"blinker", "blinkers", "blink_mode", "emergency"}


def scan(pbf):
    import osmium
    records = {}
    stats = {"highway_traffic_signals": 0, "crossing_signals": 0, "both": 0,
             "excluded_blinker_emergency": 0, "traffic_signals_values": {}}
    fp = (osmium.FileProcessor(pbf, osmium.osm.NODE)
          .with_filter(osmium.filter.KeyFilter("highway", "crossing", "crossing:signals")))
    for n in fp:
        tags = n.tags
        hw = tags.get("highway")
        cr = tags.get("crossing")
        is_signal = hw == "traffic_signals"
        is_crossing = cr == "traffic_signals" or tags.get("crossing:signals") == "yes"
        if not (is_signal or is_crossing):
            continue
        kind = tags.get("traffic_signals")
        if kind:
            stats["traffic_signals_values"][kind] = stats["traffic_signals_values"].get(kind, 0) + 1
        if kind in EXCLUDED_KINDS:
            stats["excluded_blinker_emergency"] += 1
            continue
        if not n.location.valid():
            continue
        flags = (FLAG_SIGNAL if is_signal else 0) | (FLAG_CROSSING if is_crossing else 0) \
            | (FLAG_HIGHWAY_CROSSING if hw == "crossing" else 0)
        stats["highway_traffic_signals"] += 1 if is_signal else 0
        stats["crossing_signals"] += 1 if is_crossing else 0
        stats["both"] += 1 if (is_signal and is_crossing) else 0
        lat = int(round(n.location.lat * 1e6))
        lon = int(round(n.location.lon * 1e6))
        records[n.id] = (lat, lon, flags)
    try:
        hdr = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header()
        ts = hdr.get("osmosis_replication_timestamp") or hdr.get("timestamp")
    except Exception:  # noqa: BLE001
        ts = None
    if ts:
        dt = datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    else:
        dt = datetime.datetime.fromtimestamp(os.path.getmtime(pbf), datetime.timezone.utc)
    return list(records.values()), stats, dt


def cell(v):
    return v // CELL_MICRODEG  # Python 的 // 對負數也是 floor，與 Swift 讀取端一致


def encode(records, osm_time):
    records = sorted(records, key=lambda r: (cell(r[0]), cell(r[1]), r[0], r[1], r[2]))
    body = b"".join(struct.pack("<iiB", lat, lon, flags) for lat, lon, flags in records)
    header = struct.pack("<4sHHIHHII8s", MAGIC, VERSION, HEADER_SIZE, len(records), RECORD_SIZE, CELL_MICRODEG,
                         zlib.crc32(body) & 0xFFFFFFFF, int(osm_time.timestamp()), b"\0" * 8)
    assert len(header) == HEADER_SIZE
    return header + body


def decode(data):
    magic, ver, hsize, count, rsize, cellsize, crc, ts, _ = struct.unpack("<4sHHIHHII8s", data[:HEADER_SIZE])
    assert magic == MAGIC and ver == VERSION and rsize == RECORD_SIZE
    body = data[hsize:hsize + count * rsize]
    assert len(body) == count * rsize and (zlib.crc32(body) & 0xFFFFFFFF) == crc
    return [struct.unpack_from("<iiB", body, i * rsize) for i in range(count)]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", default=os.path.join(here, "taiwan-signals.bin"))
    ap.add_argument("--copy-to", default=os.path.join(here, "..", "..", "ios", "App", "Resources", "taiwan-signals.bin"))
    ap.add_argument("--stats", default=os.path.join(here, "build_stats.json"))
    a = ap.parse_args()
    t0 = time.time()
    records, stats, osm_time = scan(a.pbf)
    data = encode(records, osm_time)
    back = decode(data)
    assert sorted(back) == sorted(records), "往返解碼不一致"
    with open(a.out, "wb") as f:
        f.write(data)
    if a.copy_to:
        shutil.copyfile(a.out, a.copy_to)
    lats = [r[0] for r in records]
    lons = [r[1] for r in records]
    stats.update({
        "pbf": os.path.basename(a.pbf),
        "osm_time": osm_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "records": len(records),
        "signal_only": sum(1 for r in records if r[2] & FLAG_SIGNAL and not r[2] & FLAG_CROSSING),
        "crossing_only": sum(1 for r in records if r[2] & FLAG_CROSSING and not r[2] & FLAG_SIGNAL),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bbox_e6": [min(lats), min(lons), max(lats), max(lons)] if records else None,
        "seconds": round(time.time() - t0, 1),
        "built_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    with open(a.stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
