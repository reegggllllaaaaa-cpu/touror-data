# -*- coding: utf-8 -*-
"""下載原始資料（data.gov.tw 資料集 API → 最新 resourceDownloadUrl）。

用法（在倉庫根目錄）：
  python -I scripts/fetch.py speedcams            # 測速：更新 raw/speedcams/ 裡能由資料集 API 解析的檔
  python -I scripts/fetch.py signals timing       # 號誌位置補點、時制
  python -I scripts/fetch.py speedlimits
  python -I scripts/fetch.py all --dry-run        # 只列出會抓哪些資料集，不連網
每個請求間隔 ≥ 40 秒（TOUROR_FETCH_INTERVAL）；失敗重試 3 次；抓不到就保留舊檔。
結果寫 build/fetch-report.json（Actions 用來決定要不要開 issue）。
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import datagov, paths  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES, SPEED_LIMIT_SOURCES, TIMING_SOURCES  # noqa: E402
from speedcams.sources import fetch_targets as speedcam_targets  # noqa: E402


def targets(group):
    if group == 'speedcams':
        return [dict(t, dir=paths.RAW_SPEEDCAMS) for t in speedcam_targets()]
    if group == 'signals':
        return [dict(t, dir=paths.RAW_SIGNALS) for t in GOV_SIGNAL_SOURCES]
    if group == 'timing':
        return [dict(t, dir=paths.RAW_TIMING) for t in TIMING_SOURCES]
    if group == 'speedlimits':
        return [dict(t, dir=paths.RAW_SPEEDCAMS) for t in SPEED_LIMIT_SOURCES]
    raise SystemExit('不認得的群組：%s' % group)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('groups', nargs='+', help='speedcams／signals／timing／speedlimits／all')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--report', default=os.path.join(paths.BUILD, 'fetch-report.json'))
    a = ap.parse_args(argv)
    groups = ['speedcams', 'signals', 'timing', 'speedlimits'] if 'all' in a.groups else a.groups
    seen, results = set(), []
    for g in groups:
        for t in targets(g):
            dest = os.path.join(t['dir'], t['file'])
            if dest in seen:
                continue
            seen.add(dest)
            if a.dry_run:
                print('[乾跑] %s 資料集 %s → %s' % (g, t['dataset'], os.path.relpath(dest, paths.ROOT)))
                results.append(dict(group=g, dataset=t['dataset'], file=t['file'], status='dry-run'))
                continue
            if t.get('fmt') == 'TAICHUNG_CSV':
                # 臺中 103743 圖資下載網址為 Google Drive，不經 data.gov.tw API；從版控 static 複製到 raw
                src = os.path.join(paths.ROOT, t['static_path'], t['file']) if t.get('static_path') else None
                if src and os.path.exists(src):
                    os.makedirs(t['dir'], exist_ok=True)
                    shutil.copyfile(src, dest)
                    rec = dict(status='copied', dataset=t['dataset'], file=t['file'], url=src)
                else:
                    rec = dict(status='missing', dataset=t['dataset'], file=t['file'],
                               reason='static file not found: %s' % src)
                rec['group'] = g
                results.append(rec)
                print('%s %s %s %s' % (rec['status'], t['dataset'], t['file'], rec.get('reason') or ''))
                continue
            rec = datagov.fetch_dataset(t['dataset'], dest, known_url=t.get('url'), fmt=t.get('fmt', 'CSV'),
                                        match=t.get('match'))
            rec['group'] = g
            results.append(rec)
            print('%s %s %s %s' % (rec['status'], t['dataset'], t['file'], rec.get('reason') or rec.get('choose', '')))
    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    kept = [r for r in results if r['status'] == 'kept']
    print('合計 %d 個；更新 %d、未變 %d、保留舊檔 %d' % (
        len(results), sum(r['status'] == 'updated' for r in results),
        sum(r['status'] == 'unchanged' for r in results), len(kept)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
