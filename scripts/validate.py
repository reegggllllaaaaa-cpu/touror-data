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


def main():
    problems = []
    doc = manifest.load(paths.MANIFEST)
    if doc is None:
        print('沒有 manifest.json')
        return 1
    problems += manifest.validate(doc, paths.ROOT)
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
