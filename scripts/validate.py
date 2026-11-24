# -*- coding: utf-8 -*-
"""驗證倉庫現況：manifest.json（格式、雜湊、大小）、每個資料檔用讀取器解析一次並核對筆數與檔內版本、人工建檔表。
用法：python -I scripts/validate.py      全部通過結束碼 0，否則 1"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import curated, formats, manifest, paths  # noqa: E402
from signals import build_timing  # noqa: E402
from speedlimits import build_speed_limits  # noqa: E402

# 測速打包檔（App 格式）的基本規則；細部規則在 vendor 的 驗證.py（建置時跑）
CAM_TYPES = {'f', 'r', 'rs', 's', 'u'}


def check_speedcams(doc):
    p = []
    cams = doc.get('cameras', [])
    if doc.get('count') != len(cams):
        p.append('speedcams count 與陣列長度不符')
    ids = [c.get('i') for c in cams]
    if len(set(ids)) != len(ids):
        p.append('speedcams id 重複')
    for c in cams:
        if c.get('t') not in CAM_TYPES:
            p.append('speedcams %s 類型錯' % c.get('i'))
        if not (21.5 <= c.get('la', 0) <= 26.5 and 118 <= c.get('lo', 0) <= 123):
            p.append('speedcams %s 座標不在台灣' % c.get('i'))
        if c.get('t') == 'rs' and 's' not in c:
            p.append('speedcams %s 闖紅燈兼超速沒有速限' % c.get('i'))
    if '政府資料開放授權條款' not in doc.get('license', ''):
        p.append('speedcams 缺授權標示')
    return p


OFFLINE_KINDS = ('tiles', 'basemap', 'places', 'places_overture', 'places_gov', 'junctions', 'addr')


def offline_manifest_problems(doc):
    """App 離線大檔下載清單（offline-manifest.json）的格式檢查；規則對齊 App\\OfflineDownload.swift 的 problem(of:)。
    注意：App 新版對不認得的 kind 會略過，但舊版會整份拒絕，所以新增 kind 前要先確認 App 已上線支援。"""
    out = []
    if doc.get('schema') != 1:
        out.append('offline-manifest schema 必須是 1')
    ver = str(doc.get('version', ''))
    if not (len(ver) == 8 and ver.isdigit()):
        out.append('offline-manifest version 須為 yyyymmdd：%r' % ver)
    files = doc.get('files')
    if not isinstance(files, list) or not files:
        return out + ['offline-manifest 沒有 files']
    seen = set()
    for f in files:
        name = str(f.get('name', ''))
        kind = f.get('kind')
        if kind not in OFFLINE_KINDS:
            out.append('%s：未知的 kind %r' % (name, kind))
        if kind in seen:
            out.append('kind 重複：%s' % kind)
        seen.add(kind)
        if not name or '/' in name or '\\' in name:
            out.append('檔名不正確：%r' % name)
        if not str(f.get('url', '')).startswith('https://'):
            out.append('%s：網址必須是 https' % name)
        if not (isinstance(f.get('bytes'), int) and f['bytes'] > 0):
            out.append('%s：bytes 不正確' % name)
        sha = str(f.get('sha256', ''))
        if not (len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)):
            out.append('%s：sha256 格式不正確' % name)
        v = str(f.get('version', ''))
        if not (len(v) == 8 and v.isdigit()):
            out.append('%s：version 須為 yyyymmdd' % name)
    return out


def main():
    problems = []
    doc = manifest.load(paths.MANIFEST)
    if doc is None:
        print('沒有 manifest.json')
        return 1
    problems += manifest.validate(doc, paths.ROOT)
    if os.path.exists(paths.OFFLINE_MANIFEST):
        with open(paths.OFFLINE_MANIFEST, encoding='utf-8') as fh:
            problems += offline_manifest_problems(json.load(fh))
    for e in doc['files']:
        full = os.path.join(paths.ROOT, e['path'])
        if not os.path.exists(full):
            continue
        data = open(full, 'rb').read()
        name = e['name']
        try:
            if name != 'speedcams-full':
                n = formats.count_records(name, data)
                if n != e['count']:
                    problems.append('%s 筆數 %d 與 manifest %d 不符' % (name, n, e['count']))
            if full.endswith('.json'):
                j = formats.load_json(data)
                if j.get('version') != e['version']:
                    problems.append('%s 檔內版本 %s 與 manifest %s 不符' % (name, j.get('version'), e['version']))
                if name == 'speedcams':
                    problems += check_speedcams(j)
                elif name.startswith('signal-timing'):
                    problems += build_timing.validate(j)
                elif name == 'speed-limits-national':
                    problems += build_speed_limits.validate(full)
        except Exception as ex:  # noqa: BLE001
            problems.append('%s 讀取失敗：%s' % (name, ex))
    _, cp = curated.load_all(paths.CURATED)
    problems += cp
    for p in problems:
        print('[失敗] ' + p)
    print('檢查 %d 個檔，%s' % (len(doc['files']), '全部通過' if not problems else '%d 個問題' % len(problems)))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
