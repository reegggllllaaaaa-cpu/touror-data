# -*- coding: utf-8 -*-
"""各資料檔的讀取與格式檢查（Python 端；App 端用 SignalIndex／RestStopIndex／SpeedCameraDatabase… 同規格）。
也用來產生 CHANGELOG 的「每筆記錄的鍵」。"""
import json
import struct
import zlib

TSIG = b'TSIG'
# 紅綠燈檔版本：v1＝低 4 位旗標；v2＝再加高位旗標（bit4 elevated、bit5 offroad、bit6 atIntersection、bit7 merged，
# 由 vendor 的 snap_signals.py 計算）。App（SignalIndex.supportedVersions 1...2）讀 v1 時遮掉高位元。
SIGNALS_VERSION = 2
SIGNALS_SUPPORTED = (1, 2)
SIG_FLAG_ELEVATED, SIG_FLAG_OFFROAD, SIG_FLAG_AT_JUNCTION, SIG_FLAG_MERGED = 16, 32, 64, 128
TRST = b'TRST'
TSPL = b'TSPL'


class FormatError(Exception):
    pass


def read_signals_bin(data):
    """taiwan-signals.bin → (header dict, [(lat_e6, lon_e6, flags)])；格式錯丟 FormatError"""
    if len(data) < 32:
        raise FormatError('紅綠燈檔太短')
    if data[:4] != TSIG:
        raise FormatError('紅綠燈檔魔術字不符')
    ver, hsize, count, rsize, cell, crc, ts = struct.unpack_from('<HHIHHII', data, 4)
    if ver not in SIGNALS_SUPPORTED or hsize < 32 or rsize != 9:
        raise FormatError('紅綠燈檔標頭錯誤 ver=%d hsize=%d rsize=%d' % (ver, hsize, rsize))
    end = hsize + count * rsize
    if len(data) < end:
        raise FormatError('紅綠燈檔被截斷')
    body = data[hsize:end]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        raise FormatError('紅綠燈檔 CRC 不符')
    rows = []
    for i in range(count):
        lat, lon, fl = struct.unpack_from('<iiB', body, i * 9)
        if not (-90e6 <= lat <= 90e6 and -180e6 <= lon <= 180e6):
            raise FormatError('紅綠燈檔第 %d 筆座標超出範圍' % i)
        rows.append((lat, lon, fl if ver >= 2 else fl & 0x0F))
    return dict(version=ver, count=count, osm_ts=ts, cell=cell), rows


def write_signals_bin(rows, osm_ts, cell=10000, version=SIGNALS_VERSION):
    """rows：[(lat_e6, lon_e6, flags)]；依 (緯度格, 經度格, 緯度, 經度) 排序後寫出（與 build_signals.py 相同，預設 v2）"""
    if version not in SIGNALS_SUPPORTED:
        raise FormatError('紅綠燈檔版本 %r 不支援' % version)
    def c(v):
        return v // cell
    rows = sorted(rows, key=lambda r: (c(r[0]), c(r[1]), r[0], r[1], r[2]))
    body = b''.join(struct.pack('<iiB', a, b, f if version >= 2 else f & 0x0F) for a, b, f in rows)
    head = TSIG + struct.pack('<HHIHHII', version, 32, len(rows), 9, cell, zlib.crc32(body) & 0xFFFFFFFF, int(osm_ts))
    head += b'\0' * (32 - len(head))
    return head + body


def read_reststops_bin(data):
    """taiwan-reststops.bin → (header, [(lat_e6, lon_e6, kind, brand, flags, name)])"""
    if len(data) < 48 or data[:4] != TRST:
        raise FormatError('休息點檔魔術字或長度不符')
    ver, hsize, count, rsize, cell, crc, ts, boff, blen, noff, nlen, crc2 = struct.unpack_from('<HHIHHIIIIIII', data, 4)
    if ver != 1 or rsize != 16 or hsize < 48:
        raise FormatError('休息點檔標頭錯誤')
    end = hsize + count * rsize
    if len(data) < end or len(data) < boff + blen or len(data) < noff + nlen:
        raise FormatError('休息點檔被截斷')
    if zlib.crc32(data[hsize:end]) & 0xFFFFFFFF != crc:
        raise FormatError('休息點檔紀錄區 CRC 不符')
    if zlib.crc32(data[boff:boff + blen] + data[noff:noff + nlen]) & 0xFFFFFFFF != crc2:
        raise FormatError('休息點檔品牌／名稱表 CRC 不符')
    names = data[noff:noff + nlen]
    rows = []
    for i in range(count):
        lat, lon, kind, brand, flags, _, name_off = struct.unpack_from('<iiBBBBI', data, hsize + i * 16)
        name = ''
        if name_off != 0xFFFFFFFF:
            n = names[name_off]
            name = names[name_off + 1:name_off + 1 + n].decode('utf-8')
        rows.append((lat, lon, kind, brand, flags, name))
    return dict(version=ver, count=count, osm_ts=ts), rows


def read_speedlimits_bin(data):
    """taiwan-speedlimits.bin → (header dict, [(lat, lon, kmh, bearing, route, flags)])；NAV-SPEEDLIMIT-OFFICIAL-1"""
    if len(data) < 24 or data[:4] != TSPL:
        raise FormatError('速限檔魔術字或長度不符')
    magic, ver, hlen, n, rlen, _, crc, ts = struct.unpack_from('<4sHHIHHII', data, 0)
    if ver != 1 or rlen != 12:
        raise FormatError('速限檔標頭錯誤 ver=%d rlen=%d' % (ver, rlen))
    if zlib.crc32(data[hlen:]) & 0xFFFFFFFF != crc:
        raise FormatError('速限檔 CRC 不符')
    off = hlen + n * rlen
    if len(data) < off + 2:
        raise FormatError('速限檔被截斷')
    (nr,) = struct.unpack_from('<H', data, off)
    off += 2
    routes = []
    for _ in range(nr):
        if off >= len(data):
            raise FormatError('速限檔路線表被截斷')
        ln = data[off]
        if off + 1 + ln > len(data):
            raise FormatError('速限檔路線名被截斷')
        routes.append(data[off + 1:off + 1 + ln].decode('utf-8'))
        off += 1 + ln
    out = []
    for i in range(n):
        la, lo, kmh, b, rf = struct.unpack_from('<iiBBH', data, hlen + i * rlen)
        if not (-90e6 <= la <= 90e6 and -180e6 <= lo <= 180e6):
            raise FormatError('速限檔第 %d 筆座標超出範圍' % i)
        out.append((la / 1e6, lo / 1e6, kmh, b * 360.0 / 256, routes[rf & 0x0FFF], rf >> 12))
    return dict(version=ver, count=n, ts=ts, routes=len(routes)), out


# taiwan-signals-quality.bin（TSGQ v1，2026-11-26）：與 taiwan-signals.bin 逐筆對齊的信心旁檔，App 不讀也不影響 TSIG。
# 檔頭 32 bytes：'TSGQ', <H 版本=1, <H 檔頭長=32, <I 號誌筆數 n, <I 疑似漏燈點數 m, <I 對應 TSIG 檔頭的 crc32,
#   <I 本檔內容 crc32, 8 bytes 保留（0）。
# 內容：n bytes（第 i byte 對應 taiwan-signals.bin 第 i 筆）：
#   bit0-1 信心 1=low 2=medium 3=high；bit2 OSM；bit3 政府清單 40 m 內；bit4 Mapillary（偵測≥2 照片或照片判讀）；
#   bit5 使用者實地確認；bit6 與相鄰號誌（80–1000 m 最近 4 盞）之間有疑似未收錄號誌路口；bit7 保留。
# 接著 m 筆疑似未收錄號誌路口，各 9 bytes：<i lat_e6, <i lon_e6, <B 線索（1 OSM 號誌／號誌行人穿越節點、
#   2 Mapillary 燈頭偵測、4 主幹道（trunk/primary/secondary 不同路名）交會）。
TSGQ = b'TSGQ'


def write_signal_quality_bin(q, suspects, signals_crc=0):
    body = bytes(q) + b''.join(struct.pack('<iiB', a, b, c) for a, b, c in sorted(suspects))
    return TSGQ + struct.pack('<HHIIII8x', 1, 32, len(q), len(suspects), signals_crc or 0, zlib.crc32(body) & 0xFFFFFFFF) + body


def read_signal_quality_bin(data):
    if data[:4] != TSGQ or len(data) < 32:
        raise FormatError('號誌信心檔開頭不是 TSGQ')
    ver, hs, n, m, scrc, crc = struct.unpack_from('<HHIIII', data, 4)
    if ver != 1 or hs != 32 or len(data) != 32 + n + 9 * m:
        raise FormatError('號誌信心檔長度或版本不符')
    body = data[32:]
    if zlib.crc32(body) & 0xFFFFFFFF != crc:
        raise FormatError('號誌信心檔 crc 不符')
    q = body[:n]
    if any(b & 3 == 0 for b in q):
        raise FormatError('號誌信心檔有未定信心的紀錄')
    sus = [struct.unpack_from('<iiB', body, n + 9 * i) for i in range(m)]
    return dict(version=ver, count=n, suspects=m, signals_crc=scrc), q, sus


def signals_header_crc(data):
    return struct.unpack_from('<I', data, 16)[0]


def load_json(data):
    return json.loads(data.decode('utf-8-sig'))


def record_keys(name, data):
    """CHANGELOG 用：{鍵: 內容摘要}。鍵相同、內容不同＝修改。"""
    if name == 'taiwan-signals':
        _, rows = read_signals_bin(data)
        return {(a, b): f for a, b, f in rows}
    if name == 'taiwan-reststops':
        _, rows = read_reststops_bin(data)
        return {(r[0], r[1], r[2]): r[3:] for r in rows}
    if name == 'taiwan-signals-quality':
        _, q, sus = read_signal_quality_bin(data)
        return {('s', a, b): c for a, b, c in sus}
    doc = load_json(data)
    if name == 'speedcams':
        return {c['i']: json.dumps(c, sort_keys=True, ensure_ascii=False) for c in doc['cameras']}
    if name == 'mobile-hotspots':
        return {(s['c'], s.get('d'), s['r']): json.dumps(s, sort_keys=True, ensure_ascii=False) for s in doc['spots']}
    if name == 'signal-timing-taipei':
        return {j['id']: json.dumps(j, sort_keys=True, ensure_ascii=False) for j in doc['junctions']}
    if name == 'speed-limits-national':
        return {(s['road'], s.get('text', '')): s['limit'] for s in doc['segments']}
    return {}


def count_records(name, data):
    """筆數（二進位檔是紀錄數，同座標的重複節點也算；JSON 是陣列長度）"""
    if name == 'taiwan-signals':
        return len(read_signals_bin(data)[1])
    if name == 'taiwan-signals-quality':
        return read_signal_quality_bin(data)[0]['count']
    if name == 'taiwan-reststops':
        return len(read_reststops_bin(data)[1])
    if name == 'taiwan-speedlimits':
        return len(read_speedlimits_bin(data)[1])
    doc = load_json(data)
    for key in ('cameras', 'spots', 'junctions', 'segments'):
        if key in doc:
            return len(doc[key])
    return len(record_keys(name, data))


def diff(old, new):
    """(新增, 移除, 修改) 筆數"""
    added = sum(1 for k in new if k not in old)
    removed = sum(1 for k in old if k not in new)
    modified = sum(1 for k in new if k in old and old[k] != new[k])
    return added, removed, modified
