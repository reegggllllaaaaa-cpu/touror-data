# -*- coding: utf-8 -*-
"""各資料檔的讀取與格式檢查（Python 端；App 端用 SignalIndex／RestStopIndex／SpeedCameraDatabase… 同規格）。
也用來產生 CHANGELOG 的「每筆記錄的鍵」。"""
import json
import struct
import zlib

TSIG = b'TSIG'
TRST = b'TRST'


class FormatError(Exception):
    pass


def read_signals_bin(data):
    """taiwan-signals.bin → (header dict, [(lat_e6, lon_e6, flags)])；格式錯丟 FormatError"""
    if len(data) < 32:
        raise FormatError('紅綠燈檔太短')
    if data[:4] != TSIG:
        raise FormatError('紅綠燈檔魔術字不符')
    ver, hsize, count, rsize, cell, crc, ts = struct.unpack_from('<HHIHHII', data, 4)
    if ver != 1 or hsize < 32 or rsize != 9:
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
        rows.append((lat, lon, fl))
    return dict(version=ver, count=count, osm_ts=ts, cell=cell), rows


def write_signals_bin(rows, osm_ts, cell=10000):
    """rows：[(lat_e6, lon_e6, flags)]；依 (緯度格, 經度格, 緯度, 經度) 排序後寫出（與 build_signals.py 相同）"""
    def c(v):
        return v // cell
    rows = sorted(rows, key=lambda r: (c(r[0]), c(r[1]), r[0], r[1], r[2]))
    body = b''.join(struct.pack('<iiB', a, b, f) for a, b, f in rows)
    head = TSIG + struct.pack('<HHIHHII', 1, 32, len(rows), 9, cell, zlib.crc32(body) & 0xFFFFFFFF, int(osm_ts))
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
    if name == 'taiwan-reststops':
        return len(read_reststops_bin(data)[1])
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
