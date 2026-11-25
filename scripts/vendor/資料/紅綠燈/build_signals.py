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
  標頭 32 bytes：magic "TSIG"、u16 版本 2（v1 無高位旗標，App 仍可讀）、u16 標頭長度 32、u32 筆數、u16 每筆長度 9、u16 排序格網（微度，10000＝0.01°）、
  u32 資料 CRC-32、u32 OSM 資料時間（Unix 秒）、8 bytes 保留 0。
  每筆 9 bytes：i32 緯度×1e6、i32 經度×1e6、u8 旗標。依 (緯度格, 經度格, 緯度, 經度) 排序。
"""
import argparse
import csv
import datetime
import hashlib
import json
import os
import shutil
import struct
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MAGIC = b"TSIG"
VERSION = 2  # v2：旗標高位元（bit4 elevated、bit5 offroad、bit6 atIntersection、bit7 merged），見 snap_signals.py；App 仍可讀 v1
HEADER_SIZE = 32
RECORD_SIZE = 9
CELL_MICRODEG = 10000
FLAG_SIGNAL = 1
FLAG_CROSSING = 2
FLAG_HIGHWAY_CROSSING = 4
EXCLUDED_KINDS = {"blinker", "blinkers", "blink_mode", "emergency"}

# 臺中市政府交通局號誌燈圖資（清理後的點位 CSV）
TAICHUNG_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "taichung_signals.csv")
# 政府／人工補點（臺南 137239、高雄 176561、新竹市 67783、使用者實地確認）：已轉 WGS84、吸附 touror_junctions 路口、30 m 去重
EXTRA_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gov_signals_extra.csv")
FLAG_GOV = 8


def load_extra_signals(csv_path):
    """讀 gov_signals_extra.csv（lat,lon,source,...），回傳 (records, 依 source 計數)。旗標 bit0｜bit3。"""
    records, by = [], {}
    if not os.path.exists(csv_path):
        return records, by
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            records.append((int(round(float(row["lat"]) * 1e6)), int(round(float(row["lon"]) * 1e6)), FLAG_SIGNAL | FLAG_GOV))
            by[row.get("source", "")] = by.get(row.get("source", ""), 0) + 1
    return records, by


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
    assert magic == MAGIC and ver in (1, 2) and rsize == RECORD_SIZE
    body = data[hsize:hsize + count * rsize]
    assert len(body) == count * rsize and (zlib.crc32(body) & 0xFFFFFFFF) == crc
    return [struct.unpack_from("<iiB", body, i * rsize) for i in range(count)]


def load_taichung_signals(csv_path):
    """讀取臺中市政府交通局號誌燈圖資清理 CSV，回傳 (records, stats)。

    CSV 欄位：lat, lon, layer, kind, comboroad, num, control, light
    篩選規則：
      - light == "三色號誌" 且 kind != "行人號誌桿" → flag=1（路口號誌）
      - light == "閃光號誌" 或 kind 為閃黃/閃紅 → 不輸出（腳本無適合旗標），只計數
      - kind == "行人號誌桿" 或 light 空白 → 不輸出，只計數
    """
    records = []
    stats = {
        "taichung_csv_rows": 0,
        "taichung_flag1": 0,
        "taichung_flasher": 0,
        "taichung_pedestrian_or_blank": 0,
        "taichung_skipped_other": 0,
    }
    if not os.path.exists(csv_path):
        return records, stats
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["taichung_csv_rows"] += 1
            light = (row.get("light") or "").strip()
            kind = (row.get("kind") or "").strip()
            if light == "三色號誌" and kind != "行人號誌桿":
                lat = int(round(float(row["lat"]) * 1e6))
                lon = int(round(float(row["lon"]) * 1e6))
                records.append((lat, lon, FLAG_SIGNAL))
                stats["taichung_flag1"] += 1
            elif light == "閃光號誌" or kind in ("閃黃燈號誌", "閃紅燈號誌"):
                stats["taichung_flasher"] += 1
            elif kind == "行人號誌桿" or light == "":
                stats["taichung_pedestrian_or_blank"] += 1
            else:
                stats["taichung_skipped_other"] += 1
    return records, stats


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--out", default=os.path.join(here, "taiwan-signals.bin"))
    ap.add_argument("--copy-to", default=os.path.join(here, "..", "..", "ios", "App", "Resources", "taiwan-signals.bin"))
    ap.add_argument("--stats", default=os.path.join(here, "build_stats.json"))
    ap.add_argument("--taichung-csv", default=TAICHUNG_CSV,
                    help="臺中市政府交通局號誌燈圖資 CSV；檔案不存在則跳過")
    ap.add_argument("--extra-csv", default=EXTRA_CSV, help="政府／人工補點 CSV；檔案不存在則跳過")
    ap.add_argument("--no-snap", action="store_true", help="不做吸附／旗標（輸出等同舊內容的 v2）")
    ap.add_argument("--snap-stats", default=os.path.join(here, "snap_stats.json"))
    a = ap.parse_args()
    t0 = time.time()
    records, stats, osm_time = scan(a.pbf)
    taichung_records, taichung_stats = load_taichung_signals(a.taichung_csv)
    records.extend(taichung_records)
    stats.update(taichung_stats)
    extra_records, extra_by = load_extra_signals(a.extra_csv)
    records.extend(extra_records)
    stats["extra_by_source"] = extra_by
    if not a.no_snap:
        import snap_signals
        records, snap_stats = snap_signals.process(records, a.pbf)
        stats["snap"] = snap_stats
        with open(a.snap_stats, "w", encoding="utf-8") as f:
            json.dump(snap_stats, f, ensure_ascii=False, indent=2)
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
