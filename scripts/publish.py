# -*- coding: utf-8 -*-
"""建置、驗證、合併人工建檔、決定版本、寫 manifest.json／CHANGELOG.md／site/（GitHub Pages）。

用法（倉庫根目錄；GitHub Actions 與本機相同）：
  python -I scripts/publish.py speedcams                   # 每日：測速＋常出沒（用 raw/speedcams）
  python -I scripts/publish.py signals reststops --pbf raw/osm/taiwan-latest.osm.pbf   # 每週：OSM 重建＋政府號誌補點
  python -I scripts/publish.py signals --no-osm            # 不重跑 OSM，只用目前發布檔的 OSM 部分重新併政府補點
  python -I scripts/publish.py timing speedlimits          # 每月：臺北／澎湖時制、國道速限
  python -I scripts/publish.py all --pbf ... --dry-run     # 全部建一次、驗證，不寫 data/
選項：
  --max-drop 0.05   筆數比上一版少超過 5% 就不發布（結束碼 3，Actions 開 issue）
  --today yyyymmdd  版本日期（預設台灣時間今天）
結束碼：0＝成功（有沒有變更看 build/publish-report.json 的 changed）、2＝驗證失敗、3＝筆數驟減、4＝缺輸入。
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from common import curated, formats, manifest, paths  # noqa: E402
from signals import build_timing, gov_signals  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES  # noqa: E402
from speedcams import road_anchors  # noqa: E402
from speedlimits import build_speed_limits  # noqa: E402

WORK = os.path.join(paths.BUILD, 'work')
W_SPEED = os.path.join(WORK, '資料', '測速')
W_SIGNAL = os.path.join(WORK, '資料', '紅綠燈')
W_REST = os.path.join(WORK, '資料', '休息點')
W_APPRES = os.path.join(WORK, 'ios', 'App', 'Resources')
MIN_APP = '0.0.1'
GOV_LICENSE = '政府資料開放授權條款－第1版'
ODBL = 'Open Database License（ODbL）1.0'
DATAGOV = 'https://data.gov.tw/dataset/'

# 每個發布檔：(manifest name, data/ 內路徑, 授權, 標示)
FILES = {
    'speedcams': ('data/speedcams/speedcams.json', GOV_LICENSE, '資料來源：政府資料開放平臺（內政部警政署及各縣市警察局）'),
    'speedcams-full': ('data/speedcams/speedcams-full.csv', GOV_LICENSE, '資料來源：政府資料開放平臺（內政部警政署及各縣市警察局）'),
    'mobile-hotspots': ('data/speedcams/mobile-hotspots.json', GOV_LICENSE, '資料來源：政府資料開放平臺（高雄市政府警察局、臺中市政府警察局）；參考點 © OpenStreetMap contributors'),
    'taiwan-signals': ('data/signals/taiwan-signals.bin', ODBL, '© OpenStreetMap contributors；政府號誌位置補點：臺北市、彰化縣、澎湖縣、臺南市、臺中市、高雄市、新竹市政府（政府資料開放授權條款第1版）；使用者實地確認補點'),
    'taiwan-reststops': ('data/reststops/taiwan-reststops.bin', ODBL, '© OpenStreetMap contributors'),
    'signal-timing-taipei': ('data/signals/signal-timing-taipei.json', GOV_LICENSE, '資料來源：臺北市政府交通局（臺北市路口號誌時制計畫）'),
    'signal-timing-penghu': ('data/signals/signal-timing-penghu.json', GOV_LICENSE, '資料來源：澎湖縣政府（紅綠燈號誌位置一覽表）'),
    'speed-limits-national': ('data/speedlimits/speed-limits-national.json', GOV_LICENSE, '資料來源：交通部高速公路局（國道各主要路段速限表）'),
}
GROUPS = {
    'speedcams': ['speedcams', 'speedcams-full', 'mobile-hotspots'],
    'signals': ['taiwan-signals'],
    'reststops': ['taiwan-reststops'],
    'timing': ['signal-timing-taipei', 'signal-timing-penghu'],
    'speedlimits': ['speed-limits-national'],
}
# 筆數驟減檢查：只看這些（自動抓取、會每日變動的）
DROP_CHECKED = {'speedcams', 'mobile-hotspots', 'taiwan-signals', 'taiwan-reststops', 'signal-timing-taipei'}


class PublishError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def run(cmd, **kw):
    print('$ ' + ' '.join(os.path.relpath(c, paths.ROOT) if os.path.isabs(c) and c.startswith(paths.ROOT) else c for c in cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
                       env=dict(os.environ, PYTHONIOENCODING='utf-8'), **kw)
    out = (r.stdout or '') + (r.stderr or '')
    tail = '\n'.join(out.strip().splitlines()[-15:])
    if tail:
        print('  ' + tail.replace('\n', '\n  '))
    return r.returncode, out


def fresh_work():
    if os.path.exists(WORK):
        shutil.rmtree(WORK)
    shutil.copytree(paths.VENDOR, WORK)
    os.makedirs(W_APPRES, exist_ok=True)


def copy_tree(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


# ------------------------------------------------------------------ 各群組建置 → 候選檔 {name: (bytes, floor_version, info)}

def group_speedcams(report):
    if not os.path.exists(os.path.join(paths.RAW_SPEEDCAMS, 'national_7320.csv')):
        raise PublishError(4, '缺少 raw/speedcams/national_7320.csv')
    copy_tree(paths.RAW_SPEEDCAMS, os.path.join(W_SPEED, '原始'))
    shutil.copyfile(os.path.join(paths.CURATED, 'section-corrections.json'), os.path.join(W_SPEED, '區間修正.json'))
    road_anchors.restore(os.path.join(WORK, '資料', '搜尋', 'touror_places.sqlite'))
    py = sys.executable
    code, out = run([py, '-I', os.path.join(W_SPEED, '建置.py')])
    if code != 0:
        raise PublishError(2, '測速建置失敗：\n' + out[-2000:])
    code, out = run([py, '-I', os.path.join(W_SPEED, '驗證.py')])
    report['checks']['speedcams 驗證.py'] = out.strip().splitlines()[-1] if out.strip() else ''
    if code != 0:
        raise PublishError(2, '測速驗證失敗（座標、欄位或筆數檢查）：\n' + out[-3000:])
    code, out = run([py, '-I', os.path.join(WORK, 'ios', 'tools', 'pack_speedcams.py')])
    if code != 0:
        raise PublishError(2, '測速打包失敗：\n' + out[-2000:])
    code, out = run([py, '-I', os.path.join(W_SPEED, '建置_移動式.py')])
    if code != 0:
        raise PublishError(2, '常出沒路段建置失敗：\n' + out[-2000:])
    tables, problems = curated.load_all(paths.CURATED)
    if problems:
        raise PublishError(2, '人工建檔表有錯：\n' + '\n'.join(problems[:30]))
    packed = json.load(open(os.path.join(W_APPRES, 'speedcams.json'), encoding='utf-8'))
    merged, st1 = curated.merge_speedcams(packed, tables)
    hot = json.load(open(os.path.join(W_APPRES, 'mobile-hotspots.json'), encoding='utf-8'))
    hmerged, st2 = curated.merge_hotspots(hot, tables)
    report['curated'] = dict(speedcams=st1, hotspots=st2)
    if st1['missing_ref'] or st2['missing_ref']:
        raise PublishError(2, '人工建檔表的 ref_id 找不到：%s' % (st1['missing_ref'] + st2['missing_ref']))
    csv_bytes = open(os.path.join(W_SPEED, 'speedcams.csv'), 'rb').read()
    return {
        'speedcams': (merged, packed.get('version'), dict(count=merged['count'])),
        'mobile-hotspots': (hmerged, hot.get('version'), dict(count=len(hmerged['spots']))),
        'speedcams-full': (csv_bytes, packed.get('version'), dict(count=csv_bytes.count(b'\n') - 1)),
    }


def find_pbf(arg):
    for p in [arg, os.path.join(paths.RAW_OSM, 'taiwan-latest.osm.pbf')]:
        if p and os.path.exists(p):
            return p
    return None


def snap_rows(report, rows, pbf):
    """OSM＋政府補點合併後，用 vendor 的 snap_signals.process（與本機同一支）吸附並算 v2 高位旗標。
    政府補點的 bit3 與 user_verified 點都保留（process 只重算高 4 位、不刪點）。"""
    snap_signals = load_snap_module()
    snapped, st = snap_signals.process(rows, pbf, log=lambda *a: None)
    if len(snapped) != len(rows):
        raise PublishError(2, '紅綠燈吸附後筆數改變 %d → %d' % (len(rows), len(snapped)))
    st['flag_counts'] = signal_flag_counts(snapped)
    report['signals_snap'] = st
    return snapped


def load_snap_module():
    import importlib.util
    p = os.path.join(paths.VENDOR, '資料', '紅綠燈', 'snap_signals.py')
    spec = importlib.util.spec_from_file_location('snap_signals', p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def signal_flag_counts(rows):
    names = dict(signal=1, crossing=2, highway_crossing=4, gov=8, elevated=16, offroad=32, at_intersection=64, merged=128)
    return {k: sum(1 for r in rows if r[2] & b) for k, b in names.items()}


def group_signals(report, pbf, no_osm):
    if no_osm:
        prev = os.path.join(paths.ROOT, FILES['taiwan-signals'][0])
        if not os.path.exists(prev):
            raise PublishError(4, '--no-osm 需要目前的 data/signals/taiwan-signals.bin')
        head, rows = formats.read_signals_bin(open(prev, 'rb').read())
        osm_rows = [r for r in rows if not r[2] & gov_signals.FLAG_GOV]
        existing_gov_rows = [r for r in rows if r[2] & gov_signals.FLAG_GOV]
        osm_ts = head['osm_ts']
        report['checks']['signals OSM 來源'] = '沿用目前發布檔的 OSM 部分（%d 點）與既有政府補點（%d 點）' % (len(osm_rows), len(existing_gov_rows))
        base_rows = osm_rows + existing_gov_rows
        taichung_src = [s for s in GOV_SIGNAL_SOURCES if s.get('static_path')]  # 倉庫內靜態點位（臺中＋前處理補點）
        rows, stats = gov_signals.merge(base_rows, paths.RAW_SIGNALS, sources=taichung_src)
        if pbf:
            # 有 PBF 就整份重算吸附旗標；沒有就沿用發布檔既有旗標（新補點暫無高位旗標）
            rows = snap_rows(report, rows, pbf)
        else:
            report['checks']['signals 吸附旗標'] = '沒有 PBF：沿用發布檔既有旗標，新增補點未吸附'
    else:
        if not pbf:
            raise PublishError(4, '缺少 OSM PBF（--pbf 或 raw/osm/taiwan-latest.osm.pbf）')
        os.makedirs(W_SIGNAL, exist_ok=True)
        out = os.path.join(W_SIGNAL, 'taiwan-signals.bin')
        # 只抽 OSM 節點（--no-snap、不讀本機臺中／補點 CSV）；政府補點由 gov_signals.merge 併入後再整份吸附，
        # 與本機 資料/紅綠燈/build_signals.py「OSM＋補點 → snap_signals.process」同一順序與規則
        code, log = run([sys.executable, '-I', os.path.join(W_SIGNAL, 'build_signals.py'), '--pbf', pbf, '--out', out,
                         '--copy-to', os.path.join(W_APPRES, 'taiwan-signals.bin'), '--no-snap',
                         '--taichung-csv', '', '--extra-csv', ''])
        if code != 0:
            raise PublishError(2, '紅綠燈建置失敗：\n' + log[-2000:])
        head, osm_rows = formats.read_signals_bin(open(out, 'rb').read())
        osm_ts = head['osm_ts']
        base_rows = osm_rows
        rows, stats = gov_signals.merge(osm_rows, paths.RAW_SIGNALS)
        rows = snap_rows(report, rows, pbf)
    report['gov_signals'] = stats
    data = formats.write_signals_bin(rows, osm_ts)
    formats.read_signals_bin(data)
    floor = datetime.datetime.fromtimestamp(osm_ts, manifest.TAIPEI).strftime('%Y%m%d') if osm_ts else None
    return {'taiwan-signals': (data, floor, dict(count=len(rows), osm=len(osm_rows), gov_added=len(rows) - len(base_rows), gov_existing=len(existing_gov_rows) if no_osm else 0))}


def group_reststops(report, pbf):
    if not pbf:
        raise PublishError(4, '缺少 OSM PBF（--pbf 或 raw/osm/taiwan-latest.osm.pbf）')
    os.makedirs(W_REST, exist_ok=True)
    out = os.path.join(W_REST, 'taiwan-reststops.bin')
    code, log = run([sys.executable, '-I', os.path.join(W_REST, 'build_rest_stops.py'), '--pbf', pbf, '--out', out,
                     '--copy-to', os.path.join(W_APPRES, 'taiwan-reststops.bin'), '--stats', os.path.join(W_REST, 'build_stats.json')])
    if code != 0:
        raise PublishError(2, '休息點建置失敗：\n' + log[-2000:])
    data = open(out, 'rb').read()
    head, rows = formats.read_reststops_bin(data)
    floor = datetime.datetime.fromtimestamp(head['osm_ts'], manifest.TAIPEI).strftime('%Y%m%d') if head['osm_ts'] else None
    return {'taiwan-reststops': (data, floor, dict(count=len(rows)))}


def group_timing(report):
    t = paths.RAW_TIMING
    need = ['taipei_timing_plan_table.json', 'taipei_timing_plan.json', 'taipei_timing_ic.csv']
    missing = [n for n in need if not os.path.exists(os.path.join(t, n))]
    if missing:
        raise PublishError(4, '缺少時制原始檔：%s（先跑 scripts/fetch.py timing）' % missing)
    doc, st = build_timing.build_taipei(*[os.path.join(t, n) for n in need], version='')
    tables, problems = curated.load_all(paths.CURATED)
    if problems:
        raise PublishError(2, '人工建檔表有錯：\n' + '\n'.join(problems[:30]))
    doc, cst = curated.merge_timing(doc, tables)
    report['timing'] = dict(taipei=st, curated=cst)
    problems = build_timing.validate(doc)
    out = {'signal-timing-taipei': (doc, None, dict(count=len(doc['junctions'])))}
    penghu = os.path.join(t, 'penghu_signals_113153.csv')
    if os.path.exists(penghu):
        pdoc, pst = build_timing.build_penghu(penghu, version='')
        report['timing']['penghu'] = pst
        problems += build_timing.validate(pdoc)
        out['signal-timing-penghu'] = (pdoc, None, dict(count=len(pdoc['junctions'])))
    if problems:
        raise PublishError(2, '時制驗證失敗：\n' + '\n'.join(problems[:20]))
    return out


def group_speedlimits(report):
    raw = os.path.join(paths.RAW_SPEEDCAMS, 'freeway_limits_40476.csv')
    if not os.path.exists(raw):
        raise PublishError(4, '缺少 raw/speedcams/freeway_limits_40476.csv')
    tmp = os.path.join(WORK, 'speed-limits-national.json')
    st = build_speed_limits.build(raw, '', tmp)
    report['speedlimits'] = st
    problems = build_speed_limits.validate(tmp)
    if problems:
        raise PublishError(2, '國道速限驗證失敗：\n' + '\n'.join(problems[:20]))
    doc = json.load(open(tmp, encoding='utf-8'))
    return {'speed-limits-national': (doc, None, dict(count=len(doc['segments'])))}


# ------------------------------------------------------------------ 版本、寫檔

def normalized(obj):
    """比較內容用：JSON 拿掉 version 欄；bytes 原樣"""
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    o = dict(obj)
    o.pop('version', None)
    return curated.dumps(o)


def finalize(name, obj, version):
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    o = dict(obj)
    o['version'] = version
    return curated.dumps(o)


def sources_for(name):
    if name in ('speedcams', 'speedcams-full'):
        meta_path = os.path.join(W_SPEED, 'meta.json')
        if os.path.exists(meta_path):
            meta = json.load(open(meta_path, encoding='utf-8'))
            return [dict(title=s['title'], url=s.get('dataset_page') or s.get('download_url'),
                         dataset=str(s.get('dataset_page', '')).rsplit('/', 1)[-1]) for s in meta.get('sources', [])]
    if name == 'mobile-hotspots':
        p = os.path.join(W_APPRES, 'mobile-hotspots.json')
        if os.path.exists(p):
            return [dict(title=s['title'], url=DATAGOV + s['dataset'], dataset=s['dataset'])
                    for s in json.load(open(p, encoding='utf-8')).get('sources', [])]
    if name == 'taiwan-signals':
        return [dict(title='OpenStreetMap（Geofabrik taiwan-latest.osm.pbf）', url='https://download.geofabrik.de/asia/taiwan.html', dataset='')] + \
               [dict(title=s['title'], url=s.get('url', DATAGOV + s['dataset']), dataset=s['dataset']) for s in GOV_SIGNAL_SOURCES]
    if name == 'taiwan-reststops':
        return [dict(title='OpenStreetMap（Geofabrik taiwan-latest.osm.pbf）', url='https://download.geofabrik.de/asia/taiwan.html', dataset='')]
    if name == 'signal-timing-taipei':
        return [dict(title='臺北市路口號誌時制計畫', url=DATAGOV + '145720', dataset='145720')]
    if name == 'signal-timing-penghu':
        return [dict(title='紅綠燈號誌位置一覽表', url=DATAGOV + '113153', dataset='113153')]
    if name == 'speed-limits-national':
        return [dict(title='國道各主要路段速限表', url=DATAGOV + '40476', dataset='40476')]
    return []


def decide(name, obj, floor, info, prev_manifest, today, max_drop, report):
    """回傳 (最終 bytes, 版本, 是否變更, diff)"""
    path = os.path.join(paths.ROOT, FILES[name][0])
    prev_entry = manifest.previous_entry(prev_manifest, name)
    prev_bytes = open(path, 'rb').read() if os.path.exists(path) else None
    prev_norm = None
    if prev_bytes is not None:
        try:
            prev_norm = normalized(prev_bytes if path.endswith(('.bin', '.csv')) else formats.load_json(prev_bytes))
        except Exception:  # noqa: BLE001
            prev_norm = None
    changed = prev_entry is None or prev_norm != normalized(obj)
    version = manifest.next_version(prev_entry and prev_entry.get('version'), floor, changed, today)
    data = finalize(name, obj, version) if changed else prev_bytes
    added = removed = modified = 0
    if prev_bytes is not None and name != 'speedcams-full':
        try:
            added, removed, modified = formats.diff(formats.record_keys(name, prev_bytes), formats.record_keys(name, data))
        except Exception as e:  # noqa: BLE001
            report['warnings'].append('%s 比對上一版失敗：%s' % (name, e))
    prev_count = (prev_entry or {}).get('count')
    count = info['count']
    if name in DROP_CHECKED and prev_count and count < prev_count * (1 - max_drop):
        raise PublishError(3, '%s 筆數 %d 比上一版 %d 少 %.1f%%（上限 %.0f%%），不發布' % (
            name, count, prev_count, (1 - count / prev_count) * 100, max_drop * 100))
    return data, version, changed, dict(added=added, removed=removed, modified=modified, first=prev_bytes is None)


def write_site(doc):
    """site/ = GitHub Pages 內容：manifest.json＋data/＋首頁"""
    if os.path.exists(paths.SITE):
        shutil.rmtree(paths.SITE)
    os.makedirs(paths.SITE)
    shutil.copyfile(paths.MANIFEST, os.path.join(paths.SITE, 'manifest.json'))
    if os.path.exists(paths.OFFLINE_MANIFEST):
        # App 的遠端下載清單（OfflineManifestLogic.remoteFileName）；沒有這份時 App 一律用內建清單
        shutil.copyfile(paths.OFFLINE_MANIFEST, os.path.join(paths.SITE, 'offline-manifest.json'))
    for e in doc['files']:
        dst = os.path.join(paths.SITE, e['path'])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(paths.ROOT, e['path']), dst)
    open(os.path.join(paths.SITE, '.nojekyll'), 'w').close()
    rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td><a href="%s">下載</a></td></tr>' % (
        e['name'], e['version'], e['count'], e['path']) for e in doc['files'])
    html = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Touror 公開資料</title><body style="font-family:sans-serif;max-width:720px;margin:24px auto;padding:0 16px">'
            '<h1>Touror 公開資料</h1><p>測速、紅綠燈、休息點等離線導航用的小型資料。來源與授權見 '
            '<a href="manifest.json">manifest.json</a>：政府資料開放授權條款第1版、OpenStreetMap（ODbL 1.0）。</p>'
            '<table border="1" cellpadding="6" style="border-collapse:collapse"><tr><th>資料</th><th>版本</th><th>筆數</th><th></th></tr>'
            + rows + '</table><p>產生時間（UTC）：%s</p></body>' % doc['generated'])
    with open(os.path.join(paths.SITE, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)


def changelog_section(today_str, results):
    lines = ['## %s-%s-%s' % (today_str[:4], today_str[4:6], today_str[6:]), '']
    lines.append('| 資料 | 版本 | 筆數 | 新增 | 移除 | 修改 |')
    lines.append('|---|---|---|---|---|---|')
    for r in results:
        if r['changed']:
            if r.get('first'):
                lines.append('| %s | %s | %s | 首次發布 | | |' % (r['name'], r['version'], r['count']))
            else:
                lines.append('| %s | %s | %s | %d | %d | %d |' % (r['name'], r['version'], r['count'], r['added'], r['removed'], r['modified']))
    return '\n'.join(lines) + '\n'


def update_changelog(section):
    head = '# 更新紀錄（CHANGELOG）\n\n每次發布自動產生：新增／移除／修改筆數以每筆的識別碼比對（測速 id、路口 id、座標）。\n\n'
    old = open(paths.CHANGELOG, encoding='utf-8').read() if os.path.exists(paths.CHANGELOG) else head
    body = old[len(head):] if old.startswith(head) else old
    with open(paths.CHANGELOG, 'w', encoding='utf-8', newline='\n') as f:
        f.write(head + section + '\n' + body)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('groups', nargs='+')
    ap.add_argument('--pbf')
    ap.add_argument('--no-osm', action='store_true')
    ap.add_argument('--today')
    ap.add_argument('--max-drop', type=float, default=0.05)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--report', default=os.path.join(paths.BUILD, 'publish-report.json'))
    a = ap.parse_args(argv)
    groups = list(GROUPS) if 'all' in a.groups else a.groups
    for g in groups:
        if g not in GROUPS:
            raise SystemExit('不認得的群組：%s' % g)
    today = a.today or manifest.today_taipei()
    report = dict(groups=groups, today=today, checks={}, warnings=[], results=[], changed=False, error=None)
    os.makedirs(paths.BUILD, exist_ok=True)
    curated.write_empty_tables(paths.CURATED)
    code = 0
    try:
        fresh_work()
        pbf = find_pbf(a.pbf)
        candidates = {}
        for g in groups:
            if g == 'speedcams':
                candidates.update(group_speedcams(report))
            elif g == 'signals':
                candidates.update(group_signals(report, pbf, a.no_osm))
            elif g == 'reststops':
                candidates.update(group_reststops(report, pbf))
            elif g == 'timing':
                candidates.update(group_timing(report))
            elif g == 'speedlimits':
                candidates.update(group_speedlimits(report))
        prev = manifest.load(paths.MANIFEST)
        decided = {}
        for name, (obj, floor, info) in candidates.items():
            data, version, changed, d = decide(name, obj, floor, info, prev, today, a.max_drop, report)
            decided[name] = (data, version, changed, info)
            report['results'].append(dict(name=name, version=version, count=info['count'], bytes=len(data), changed=changed, **d))
        # 寫檔前用讀取器把最終檔再驗一次（含 version 欄）
        for name, (data, version, _, info) in decided.items():
            if name not in ('speedcams-full',):
                n = formats.count_records(name, data)
                if n != info['count']:
                    raise PublishError(2, '%s 最終檔筆數 %d 與建置 %d 不符' % (name, n, info['count']))
        report['changed'] = any(v[2] for v in decided.values())
        if a.dry_run:
            print('乾跑：不寫 data/、manifest.json')
        elif report['changed'] or prev is None:
            files = {e['name']: e for e in (prev or {}).get('files', [])}
            for name, (data, version, changed, info) in decided.items():
                path = FILES[name][0]
                full = os.path.join(paths.ROOT, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                if changed:
                    with open(full, 'wb') as f:
                        f.write(data)
                lic, attr = FILES[name][1], FILES[name][2]
                files[name] = manifest.entry(name, path, paths.ROOT, version, info['count'], MIN_APP, lic, attr, sources_for(name))
            doc = manifest.write(paths.MANIFEST, list(files.values()))
            problems = manifest.validate(doc, paths.ROOT)
            if problems:
                raise PublishError(2, 'manifest 驗證失敗：\n' + '\n'.join(problems))
            update_changelog(changelog_section(today, report['results']))
            write_site(doc)
        else:
            print('內容沒有變更，不發布')
            if os.path.exists(paths.MANIFEST):
                write_site(manifest.load(paths.MANIFEST))
    except PublishError as e:
        report['error'] = str(e)
        code = e.code
        print('失敗（%d）：%s' % (e.code, e), file=sys.stderr)
    with open(a.report, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    for r in report['results']:
        print('%-22s %-12s %7s 筆  %s  +%d -%d ~%d' % (r['name'], r['version'], r['count'], '變更' if r['changed'] else '未變',
                                                       r['added'], r['removed'], r['modified']))
    return code


if __name__ == '__main__':
    sys.exit(main())
