# -*- coding: utf-8 -*-
"""下載原始資料（data.gov.tw 資料集 API → 最新 resourceDownloadUrl）。

用法（在倉庫根目錄）：
  python -I scripts/fetch.py speedcams            # 全部測速來源（向後相容）
  python -I scripts/fetch.py speedcams-core       # 每日：全國固定測速 7320、全國闖紅燈 100856
  python -I scripts/fetch.py speedcams-counties   # 每週：各縣市與移動式測速
  python -I scripts/fetch.py signals timing       # 號誌位置補點、時制
  python -I scripts/fetch.py speedlimits
  python -I scripts/fetch.py all --dry-run        # 只列出會抓哪些資料集，不連網
對 data.gov.tw API 請求間隔 ≥ 40 秒（TOUROR_FETCH_INTERVAL）；下載實際檔案不套用。
每個來源重試 1 次（30 秒）；單一來源總時間上限 120 秒。抓不到就保留舊檔，無舊檔才失敗。
CI 上 raw/signals 不進版控，「舊檔」來自 actions/cache 還原的上一次成功下載（weekly-osm.yml）。
號誌補點群組（PARTIAL_OK_GROUPS）：單一來源下載失敗且無舊檔 → 略過該來源（status＝skipped，印警告），
步驟仍成功；但連網來源全部失敗或失敗超過一半 → 仍然失敗，不讓殘缺資料悄悄上線。
結果寫 build/fetch-report.json（Actions 用來決定要不要開 issue）；有 GITHUB_STEP_SUMMARY 時另寫摘要。
"""
import argparse
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import datagov, paths  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES, SPEED_LIMIT_SOURCES, TIMING_SOURCES  # noqa: E402
from speedcams.sources import fetch_core_targets, fetch_county_targets, fetch_targets as speedcam_targets  # noqa: E402


def targets(group):
    if group == 'speedcams':
        return [dict(t, dir=paths.RAW_SPEEDCAMS) for t in speedcam_targets()]
    if group == 'speedcams-core':
        return [dict(t, dir=paths.RAW_SPEEDCAMS) for t in fetch_core_targets()]
    if group == 'speedcams-counties':
        return [dict(t, dir=paths.RAW_SPEEDCAMS) for t in fetch_county_targets()]
    if group == 'signals':
        return [dict(t, dir=paths.RAW_SIGNALS) for t in GOV_SIGNAL_SOURCES]
    if group == 'timing':
        return [dict(t, dir=paths.RAW_TIMING) for t in TIMING_SOURCES]
    if group == 'speedlimits':
        return [dict(t, dir=paths.RAW_SPEEDCAMS) for t in SPEED_LIMIT_SOURCES]
    raise SystemExit('不認得的群組：%s' % group)


# 可容許部分來源缺席的群組：gov_signals.merge 對缺檔來源只記 missing 不中斷，略過一個來源只少那一區補點
PARTIAL_OK_GROUPS = {'signals'}


def apply_partial_policy(results):
    """對 PARTIAL_OK_GROUPS 的失敗來源改判 skipped。回傳 (仍算失敗的數量, 訊息 list)。
    連網來源全部失敗或失敗數超過一半 → 該群組維持失敗。"""
    failed, msgs = 0, []
    groups = []
    for r in results:
        if r['group'] not in groups:
            groups.append(r['group'])
    for g in groups:
        net = [r for r in results if r['group'] == g and r['status'] in ('updated', 'unchanged', 'kept', 'failed')]
        bad = [r for r in net if r['status'] == 'failed']
        if not bad:
            continue
        if g in PARTIAL_OK_GROUPS and len(bad) < len(net) and len(bad) * 2 <= len(net):
            for r in bad:
                r['status'] = 'skipped'
                r['reason'] = '略過此來源（下載失敗且無舊檔）：%s' % r.get('reason', '')
                msgs.append('::warning title=號誌來源略過::%s 資料集 %s（%s）下載失敗且無舊檔，本次略過此來源'
                            % (g, r['dataset'], r['file']))
        else:
            failed += len(bad)
            msgs.append('::error title=來源失敗過多::%s 群組 %d／%d 個連網來源失敗（超過一半或全部），停止'
                        % (g, len(bad), len(net)))
    for r in results:
        if r['status'] == 'kept':
            msgs.append('::warning title=沿用舊檔::%s 資料集 %s（%s）%s'
                        % (r['group'], r['dataset'], r['file'], r.get('reason', '')))
    return failed, msgs


def write_summary(results, failed, path):
    skipped = [r for r in results if r['status'] == 'skipped']
    kept = [r for r in results if r['status'] == 'kept']
    lines = ['### 政府資料下載：%s' % ('失敗' if failed else ('部分略過' if skipped else '完成')), '']
    for title, rows in (('略過的來源', skipped), ('沿用舊檔的來源', kept),
                        ('失敗的來源', [r for r in results if r['status'] == 'failed'])):
        if rows:
            lines.append('**%s**' % title)
            lines += ['- %s 資料集 %s（%s）：%s' % (r['group'], r['dataset'], r['file'], r.get('reason', ''))
                      for r in rows]
            lines.append('')
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('groups', nargs='+', help='speedcams／speedcams-core／speedcams-counties／signals／timing／speedlimits／all')
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
                print('[乾跑] %s 資料集 %s → %s' % (g, t['dataset'], os.path.relpath(dest, paths.ROOT)), flush=True)
                results.append(dict(group=g, dataset=t['dataset'], file=t['file'], status='dry-run'))
                continue
            start = time.time()
            print('開始 %s 資料集 %s → %s' % (g, t['dataset'], t['file']), flush=True)
            if t.get('fmt') in ('TAICHUNG_CSV', 'POINTS_CSV'):  # 倉庫內靜態點位，直接複製不連網
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
            elapsed = time.time() - start
            rec['group'] = g
            rec['elapsed_sec'] = round(elapsed, 2)
            results.append(rec)
            print('結束 %s %s %s 耗時 %.1fs：%s' % (
                rec['status'], t['dataset'], t['file'], elapsed,
                rec.get('reason') or rec.get('choose', '')), flush=True)
    failed, msgs = apply_partial_policy(results)
    for m in msgs:
        print(m, flush=True)
    os.makedirs(os.path.dirname(a.report), exist_ok=True)
    with open(a.report, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    kept = [r for r in results if r['status'] == 'kept']
    print('合計 %d 個；更新 %d、未變 %d、保留舊檔 %d、略過 %d、失敗 %d' % (
        len(results), sum(r['status'] == 'updated' for r in results),
        sum(r['status'] == 'unchanged' for r in results), len(kept),
        sum(r['status'] == 'skipped' for r in results), failed), flush=True)
    if os.environ.get('GITHUB_STEP_SUMMARY') and not a.dry_run:
        write_summary(results, failed, os.environ['GITHUB_STEP_SUMMARY'])
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
