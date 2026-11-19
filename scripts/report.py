# -*- coding: utf-8 -*-
"""把 build/publish-report.json 印成幾行人看得懂的摘要（Actions 記錄用）。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import paths  # noqa: E402


def main():
    p = os.path.join(paths.BUILD, 'publish-report.json')
    if not os.path.exists(p):
        print('沒有 publish-report.json')
        return 0
    r = json.load(open(p, encoding='utf-8'))
    if r.get('error'):
        print('未發布：' + r['error'].splitlines()[0])
    for s in r.get('gov_signals', []):
        print('%s 號誌補點 %d（原始 %d、與 OSM 重複 %d、閃光 %d、座標錯 %d）' % (
            s['city'], s['added'], s['rows'], s['dup_osm'], s['flashing'], s['bad_coord']))
    for x in r.get('results', []):
        print('%s %s：%s 筆（%s）' % (x['name'], x['version'], x['count'], '變更' if x['changed'] else '未變'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
