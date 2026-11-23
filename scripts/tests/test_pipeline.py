# -*- coding: utf-8 -*-
"""資料發布管線單元測試（只用標準庫）：python -I -m unittest discover -s scripts/tests -t scripts"""
import csv
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

from common import curated, datagov, formats, manifest, paths  # noqa: E402
from signals import build_timing, gov_signals  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES  # noqa: E402
from speedlimits import build_speed_limits  # noqa: E402
import fetch  # noqa: E402

datagov.MIN_INTERVAL = 0
datagov.RETRY_WAITS = ()


class VersionTests(unittest.TestCase):
    def test_next_version(self):
        nv = manifest.next_version
        self.assertEqual(nv(None, None, True, '20261120'), '20261120.1')
        self.assertEqual(nv('20261120.1', None, False, '20261121'), '20261120.1', '內容沒變沿用')
        self.assertEqual(nv('20261120.1', None, True, '20261120'), '20261120.2', '同一天再發布 rev+1')
        self.assertEqual(nv('20261119.4', None, True, '20261120'), '20261120.1')
        self.assertEqual(nv(None, '20261119.3', True, '20261119'), '20261119.4', '要比內建（來源）版本新')
        self.assertEqual(nv(None, '20261117', True, '20261119'), '20261119.1')
        self.assertEqual(nv('20261121.2', None, True, '20261120'), '20261121.3', '時鐘倒退也不會變小')

    def test_parse(self):
        self.assertEqual(manifest.parse_version('20261119.3'), (20261119, 3))
        self.assertEqual(manifest.parse_version('20261119'), (20261119, 0))
        self.assertIsNone(manifest.parse_version('2026-11-19'))

    def test_manifest_validate(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, 'data'))
            with open(os.path.join(tmp, 'data', 'a.json'), 'wb') as f:
                f.write(b'{"version":"20261120.1"}')
            e = manifest.entry('speedcams', 'data/a.json', tmp, '20261120.1', 1, '0.0.1', 'l', 'a', [])
            doc = dict(schema=1, generated='x', files=[e])
            self.assertEqual(manifest.validate(doc, tmp), [])
            bad = dict(e, path='../x')
            self.assertTrue(manifest.validate(dict(schema=1, files=[bad])))
            bad = dict(e, version='20261120')
            self.assertTrue(manifest.validate(dict(schema=1, files=[bad])), '版本要有 .rev')
            with open(os.path.join(tmp, 'data', 'a.json'), 'wb') as f:
                f.write(b'changed')
            self.assertTrue(manifest.validate(doc, tmp), '檔案被改過要抓到')
        finally:
            shutil.rmtree(tmp)


class FakeGetter:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append(url)
        v = self.routes.get(url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise RuntimeError('404 ' + url)
        return v


def api(dist, modified='2026-11-20 10:00:00'):
    return json.dumps(dict(success=True, result=dict(modifiedDate=modified, distribution=dist))).encode()


class DataGovTests(unittest.TestCase):
    def test_choose_resource(self):
        d = [dict(resourceFormat='CSV', resourceDownloadUrl='https://a/1.csv', resourceDescription='左半邊'),
             dict(resourceFormat='JSON', resourceDownloadUrl='https://a/2.json', resourceDescription='右半邊'),
             dict(resourceFormat='JSON', resourceDownloadUrl='https://a/3.json', resourceDescription='左半邊 JSON')]
        self.assertEqual(datagov.choose_resource(d, known_url='https://a/2.json')[0]['resourceDownloadUrl'], 'https://a/2.json')
        self.assertEqual(datagov.choose_resource(d, fmt='CSV')[0]['resourceDownloadUrl'], 'https://a/1.csv')
        self.assertEqual(datagov.choose_resource(d, fmt='JSON', match='左半邊')[0]['resourceDownloadUrl'], 'https://a/3.json')
        self.assertIsNone(datagov.choose_resource(d, fmt='JSON')[0], '兩個 JSON 無法決定，不亂猜')

    def test_fetch_dataset_updated_unchanged_kept(self):
        tmp = tempfile.mkdtemp()
        try:
            dest = os.path.join(tmp, 'x.csv')
            g = FakeGetter({datagov.API % '1': api([dict(resourceFormat='CSV', resourceDownloadUrl='https://new/x.csv')]),
                            'https://new/x.csv': b'a,b\n1,2\n' * 5})
            datagov._info_cache.clear()
            r = datagov.fetch_dataset('1', dest, known_url='https://old/x.csv', getter=g)
            self.assertEqual(r['status'], 'updated')
            self.assertEqual(r['url'], 'https://new/x.csv', '網址換了也抓得到（不寫死）')
            datagov._info_cache.clear()
            self.assertEqual(datagov.fetch_dataset('1', dest, getter=g)['status'], 'unchanged')
            datagov._info_cache.clear()
            g2 = FakeGetter({datagov.API % '1': RuntimeError('down')})
            r = datagov.fetch_dataset('1', dest, getter=g2)
            self.assertEqual(r['status'], 'kept')
            self.assertTrue(os.path.exists(dest), '失敗保留舊檔')
            datagov._info_cache.clear()
            g3 = FakeGetter({datagov.API % '1': api([dict(resourceFormat='CSV', resourceDownloadUrl='https://new/x.csv')]),
                             'https://new/x.csv': b'x'})
            self.assertEqual(datagov.fetch_dataset('1', dest, getter=g3)['status'], 'kept', '太小的檔不換')
        finally:
            shutil.rmtree(tmp)

    def test_http_get_retries_then_fails(self):
        calls = []

        def opener(req, timeout):
            calls.append(req.full_url)
            self.assertNotIn('Cookie', req.headers)
            self.assertTrue(req.get_header('User-agent').startswith('touror-data-builder'))
            raise OSError('boom')
        with self.assertRaises(RuntimeError):
            datagov.http_get('https://x/y', retries=(0,), opener=opener)
        self.assertEqual(len(calls), 2, '初始 1 次 + 重試 1 次')

    def test_http_get_total_time_budget(self):
        """驗證總時間上限會把 socket timeout 壓縮到預算內（防慢速滴流）。"""
        timeouts = []

        def opener(req, timeout):
            timeouts.append(timeout)
            raise OSError('boom')
        with self.assertRaises(RuntimeError):
            datagov.http_get('https://x/y', retries=(0,), opener=opener, max_seconds=1)
        self.assertEqual(len(timeouts), 2, '初始 1 次 + 重試 1 次')
        self.assertLessEqual(timeouts[0], 1, 'socket timeout 被壓在總預算內')

    def test_fetch_dataset_fails_without_old_file(self):
        tmp = tempfile.mkdtemp()
        try:
            dest = os.path.join(tmp, 'missing.csv')
            g = FakeGetter({datagov.API % '1': api([dict(resourceFormat='CSV', resourceDownloadUrl='https://new/x.csv')]),
                            'https://new/x.csv': RuntimeError('down')})
            datagov._info_cache.clear()
            r = datagov.fetch_dataset('1', dest, getter=g)
            self.assertEqual(r['status'], 'failed')
            self.assertFalse(os.path.exists(dest))
        finally:
            shutil.rmtree(tmp)


def write_csv(path, header, rows, encoding='utf-8-sig', delimiter=','):
    with open(path, 'w', encoding=encoding, newline='') as f:
        w = csv.writer(f, delimiter=delimiter, lineterminator='\n')
        w.writerow(header)
        w.writerows(rows)


def cur_row(**kw):
    base = dict(id='', ref_id='', lat='25.04', lon='121.51', bearing='-', direction='both', type='redlight', limit_kmh='-',
                len_m='-', road='測試路口', source='現地查核', source_url='IMG_0001', checked_date='2026-11-20',
                checker='測試', status='active', note='')
    base.update(kw)
    return base


class CuratedTests(unittest.TestCase):
    def test_check_rows(self):
        self.assertEqual(curated.check_rows('redlight-additions.csv', [cur_row(id='a')]), [])
        probs = curated.check_rows('redlight-additions.csv', [cur_row(id='a', checker='', lat='99')])
        self.assertTrue(any('checker' in p for p in probs))
        self.assertTrue(any('座標' in p for p in probs))
        self.assertTrue(curated.check_rows('redlight-additions.csv', [cur_row(id='a', status='gone')]))
        self.assertTrue(curated.check_rows('redlight-additions.csv', [cur_row(id='a', type='redlight_speed')]),
                        '闖紅燈兼超速要有速限')
        self.assertTrue(curated.check_rows('section-ends.csv', [cur_row(type='section_end')]), '區間終點要 ref_id')
        self.assertTrue(curated.check_rows('redlight-additions.csv', [cur_row(id='a', direction='one')]), 'one 要方位角')
        self.assertTrue(curated.check_rows('redlight-additions.csv', [cur_row(id='a'), cur_row(id='a')]), 'id 重複')

    def test_merge_speedcams(self):
        doc = dict(version='20261119.3', count=3, cameras=[
            dict(i='tw-1', la=25.0, lo=121.5, t='r'), dict(i='tw-2', la=25.1, lo=121.5, t='f', s=50),
            dict(i='tw-s', la=25.2, lo=121.5, t='s', s=60, lenSrc='est')])
        tables = {
            'redlight-additions.csv': [cur_row(id='new1'), cur_row(ref_id='tw-1', type='redlight_speed', limit_kmh='60',
                                                                    direction='one', bearing='90'),
                                       cur_row(ref_id='tw-2', status='retired')],
            'section-ends.csv': [cur_row(ref_id='tw-s', type='section_end', len_m='1300', lat='25.21', lon='121.5')],
        }
        out, st = curated.merge_speedcams(doc, tables)
        ids = [c['i'] for c in out['cameras']]
        self.assertEqual(ids, ['cur-new1', 'tw-1', 'tw-s'], '人工新增、撤除生效')
        self.assertEqual(out['count'], 3)
        tw1 = next(c for c in out['cameras'] if c['i'] == 'tw-1')
        self.assertEqual((tw1['t'], tw1['s'], tw1['b']), ('rs', 60, 90), '人工覆蓋自動資料')
        sec = next(c for c in out['cameras'] if c['i'] == 'tw-s')
        self.assertEqual((sec['len'], sec['lenSrc'], sec['e']), (1300, 'official', [25.21, 121.5]))
        self.assertEqual((st['added'], st['updated'], st['retired'], st['section_ends']), (1, 1, 1, 1))
        sus, _ = curated.merge_speedcams(doc, {'redlight-additions.csv': [cur_row(ref_id='tw-1', status='suspended')]})
        self.assertEqual(next(c for c in sus['cameras'] if c['i'] == 'tw-1').get('st'), 'sus')
        _, st2 = curated.merge_speedcams(doc, {'redlight-additions.csv': [cur_row(ref_id='nope')]})
        self.assertEqual(st2['missing_ref'], ['nope'])

    def test_merge_hotspots_and_timing(self):
        hot = dict(version='20261119', spots=[dict(c='台中市', d='北區', r='中清路一段', p=[[24.16, 120.67]])])
        rows = [dict(cur_row(type='mobile_hotspot', road='文心路'), city='臺中市', district='北屯區'),
                dict(cur_row(type='mobile_hotspot', status='retired', ref_id='台中市|北區|中清路一段'), city='臺中市', district='北區')]
        out, st = curated.merge_hotspots(hot, {'mobile-segments.csv': rows})
        self.assertEqual([s['r'] for s in out['spots']], ['文心路'])
        self.assertEqual((st['added'], st['retired']), (1, 1))
        tdoc = dict(junctions=[dict(id='I1', la=25.0, lo=121.5, p=[dict(w=127, s=0, e=1440, c=100, r=[40, 60])])])
        trow = dict(cur_row(type='signal_timing', ref_id='I1'), cycle_s='90', red_s='45', days='31', time_from='07:00', time_to='09:00')
        self.assertEqual(curated.check_rows('signal-timing.csv', [trow]), [])
        tout, tst = curated.merge_timing(tdoc, {'signal-timing.csv': [trow]})
        self.assertEqual(tout['junctions'][0]['p'][0], dict(w=31, s=420, e=540, c=90, r=[45]))
        self.assertEqual(tst['updated'], 1)

    def test_empty_tables_have_required_headers(self):
        tmp = tempfile.mkdtemp()
        try:
            curated.write_empty_tables(tmp)
            for t in curated.TABLES:
                head = open(os.path.join(tmp, t), encoding='utf-8-sig').readline().strip().split(',')
                for k in curated.REQUIRED:
                    self.assertIn(k, head, t)
            tables, problems = curated.load_all(tmp)
            self.assertEqual(problems, [])
            self.assertTrue(all(v == [] for v in tables.values()))
        finally:
            shutil.rmtree(tmp)

    def test_repo_curated_tables_valid(self):
        _, problems = curated.load_all(paths.CURATED)
        self.assertEqual(problems, [])


class FetchGroupTests(unittest.TestCase):
    def test_speedcam_groups_partition(self):
        from speedcams.sources import fetch_core_targets, fetch_county_targets, fetch_targets
        all_targets = fetch_targets()
        core_targets = fetch_core_targets()
        county_targets = fetch_county_targets()
        all_ds = {t['dataset'] for t in all_targets}
        core_ds = {t['dataset'] for t in core_targets}
        county_ds = {t['dataset'] for t in county_targets}
        self.assertEqual(core_ds, {'7320', '100856'}, '每日核心為全國固定測速與全國闖紅燈')
        self.assertEqual(core_ds | county_ds, all_ds)
        self.assertEqual(core_ds & county_ds, set())
        self.assertEqual(len(core_targets), 2)
        self.assertEqual(len(county_targets), 25, '縣市與移動式來源應為 25 個（含重複 dataset）')

    def test_fetch_targets_command_groups(self):
        from fetch import targets
        core = targets('speedcams-core')
        county = targets('speedcams-counties')
        self.assertEqual({t['dataset'] for t in core}, {'7320', '100856'})
        self.assertEqual(len(county), 25)


class FormatTests(unittest.TestCase):
    def test_signals_roundtrip_and_crc(self):
        rows = [(25040000, 121510000, 1), (22600000, 120300000, 2), (25040100, 121510100, 9)]
        data = formats.write_signals_bin(rows, 1794950670)
        head, back = formats.read_signals_bin(data)
        self.assertEqual(head['count'], 3)
        self.assertEqual(head['osm_ts'], 1794950670)
        self.assertEqual(sorted(back), sorted(rows))
        bad = bytearray(data)
        bad[-1] ^= 1
        with self.assertRaises(formats.FormatError):
            formats.read_signals_bin(bytes(bad))
        with self.assertRaises(formats.FormatError):
            formats.read_signals_bin(data[:40])

    def test_diff(self):
        self.assertEqual(formats.diff({'a': 1, 'b': 2}, {'b': 3, 'c': 4}), (1, 1, 1))

    def test_bundled_app_files_readable(self):
        res = os.path.join(paths.ROOT, '..', 'ios', 'App', 'Resources')
        p = os.path.join(res, 'taiwan-signals.bin')
        if not os.path.exists(p):
            self.skipTest('沒有 ios 工作區')
        head, rows = formats.read_signals_bin(open(p, 'rb').read())
        self.assertGreater(len(rows), 50000)
        _, rest = formats.read_reststops_bin(open(os.path.join(res, 'taiwan-reststops.bin'), 'rb').read())
        self.assertGreater(len(rest), 20000)


class GovSignalTests(unittest.TestCase):
    def test_merge_rules(self):
        tmp = tempfile.mkdtemp()
        try:
            src = [dict(key='tp', dataset='1', file='tp.csv', city='臺北市', title='t', lat=('WGS緯度座標',), lon=('WGS經度座標',)),
                   dict(key='ch', dataset='2', file='ch.csv', city='彰化縣', title='c'),
                   dict(key='none', dataset='3', file='missing.csv', city='x', title='x')]
            write_csv(os.path.join(tmp, 'tp.csv'), ['流水號', '地點', 'WGS經度座標', 'WGS緯度座標'],
                      [['1', '近 OSM', '121.510200', '25.040000'],      # 20 m 內有 OSM 號誌 → 重複
                       ['2', '新路口', '121.530000', '25.050000'],      # 新增
                       ['3', '同上', '121.530100', '25.050050'],        # 與 2 相距約 12 m → 政府點互相去重
                       ['4', '經緯顛倒', '25.060000', '121.540000'],    # 對調後新增
                       ['5', '壞座標', '0', '0']], encoding='cp950', delimiter='\t')
            write_csv(os.path.join(tmp, 'ch.csv'), ['編號', '路口名稱', '號誌種類', '緯度', '經度'],
                      [['1', 'a', '閃光', '24.07', '120.54'], ['2', 'b', '三色', '24.08', '120.55']])
            osm = [(25040000, 121510000, 1), (24000000, 120000000, 2)]
            rows, st = gov_signals.merge(osm, tmp, sources=src)
            by = {s['key']: s for s in st}
            self.assertEqual((by['tp']['dup_osm'], by['tp']['added'], by['tp']['dup_gov'], by['tp']['bad_coord']), (1, 2, 1, 1))
            self.assertEqual((by['ch']['flashing'], by['ch']['added']), (1, 1))
            self.assertTrue(by['none']['missing'])
            added = rows[len(osm):]
            self.assertEqual(len(added), 3)
            self.assertTrue(all(f == 9 for _, _, f in added), '補點旗標＝路口號誌＋政府來源')
            self.assertIn((25060000, 121540000, 9), added)
        finally:
            shutil.rmtree(tmp)

    def test_taichung_filter_and_merge(self):
        tmp = tempfile.mkdtemp()
        static = tempfile.mkdtemp()
        try:
            # 臺中 CSV 篩選：light=三色號誌、kind≠行人號誌桿；閃光號誌與純行人桿排除
            write_csv(os.path.join(static, 'taichung_signals.csv'),
                      ['lat', 'lon', 'layer', 'kind', 'comboroad', 'num', 'control', 'light'],
                      [['24.2411757', '120.7154192', '8010504', '三色號誌', 'A路口', 'GT1', '', '三色號誌'],
                       ['24.1900688', '120.6808608', '8010504', '行人號誌桿', 'B路口', 'N2', '', '三色號誌'],
                       ['24.3243441', '120.7390241', '8010504', '閃紅燈號誌', 'C路口', 'N3', '', '閃光號誌'],
                       ['24.1875581', '120.6787138', '8010504', '行人行車號誌共桿', 'D路口', 'N4', '', '三色號誌']])
            coords, st = gov_signals.read_taichung(os.path.join(static, 'taichung_signals.csv'))
            self.assertEqual((st['rows'], st['flag1'], st['flasher'], st['pedestrian_or_blank']), (4, 2, 1, 1))
            self.assertEqual(len(coords), 2)

            # merge：透過 static_path 找到臺中 CSV；D 路口與 OSM 點在 40 m 內 → 去重
            src = [dict(key='taichung', dataset='103743', file='taichung_signals.csv', fmt='TAICHUNG_CSV',
                        city='臺中市', static_path=static, title='臺中市號誌燈圖資', lat=('lat',), lon=('lon',))]
            osm = [(24187550, 120678710, 1)]  # 與 D 路口約 1 m
            rows, mst = gov_signals.merge(osm, tmp, sources=src)
            by = {s['key']: s for s in mst}
            self.assertEqual((by['taichung']['rows'], by['taichung']['flag1'], by['taichung']['added'], by['taichung']['dup_osm']),
                             (4, 2, 1, 1))
            added = rows[len(osm):]
            self.assertEqual(len(added), 1)
            self.assertEqual(added[0][2], 9)
        finally:
            shutil.rmtree(tmp)
            shutil.rmtree(static)


class FetchTests(unittest.TestCase):
    def test_signals_targets_include_taichung(self):
        keys = [t['key'] for t in fetch.targets('signals')]
        self.assertIn('taichung', keys)
        taichung = next(t for t in fetch.targets('signals') if t['key'] == 'taichung')
        self.assertEqual(taichung['fmt'], 'TAICHUNG_CSV')
        self.assertEqual(taichung['static_path'], 'data/gov-signals')

    def test_taichung_copied_from_static(self):
        """fetch.py signals 對臺中來源不連網，直接從 static_path 複製到 raw/signals/"""
        tmp = tempfile.mkdtemp()
        raw_signals = os.path.join(tmp, 'raw', 'signals')
        build_dir = os.path.join(tmp, 'build')
        static = os.path.join(tmp, 'data', 'gov-signals')
        os.makedirs(static)
        write_csv(os.path.join(static, 'taichung_signals.csv'),
                  ['lat', 'lon', 'layer', 'kind', 'comboroad', 'num', 'control', 'light'],
                  [['24.2411757', '120.7154192', '8010504', '三色號誌', 'A路口', 'GT1', '', '三色號誌']])
        try:
            old_root, old_raw, old_build = paths.ROOT, paths.RAW_SIGNALS, paths.BUILD
            paths.ROOT = tmp
            paths.RAW_SIGNALS = raw_signals
            paths.BUILD = build_dir
            # 只保留臺中來源，避免測試真的連網下載其他資料集
            old_sources = list(GOV_SIGNAL_SOURCES)
            GOV_SIGNAL_SOURCES[:] = [t for t in old_sources if t['key'] == 'taichung']

            def fake_fetch(*a, **kw):
                raise AssertionError('不應連網下載臺中')

            old_fetch_dataset = datagov.fetch_dataset
            datagov.fetch_dataset = fake_fetch
            try:
                self.assertEqual(fetch.main(['signals']), 0)
                copied = os.path.join(raw_signals, 'taichung_signals.csv')
                self.assertTrue(os.path.exists(copied), '臺中 CSV 應複製到 raw/signals/')
                self.assertIn('三色號誌', open(copied, encoding='utf-8-sig').read())
            finally:
                datagov.fetch_dataset = old_fetch_dataset
                GOV_SIGNAL_SOURCES[:] = old_sources
                paths.ROOT, paths.RAW_SIGNALS, paths.BUILD = old_root, old_raw, old_build
        finally:
            shutil.rmtree(tmp)


class TimingTests(unittest.TestCase):
    def test_plan_reds(self):
        p = dict(cycletime=30, subplan=[dict(green=10, yellow=3, allred=2), dict(green=10, yellow=3, allred=2)])
        self.assertEqual(build_timing.plan_reds(p), (30, (17, 17)))
        self.assertIsNone(build_timing.plan_reds(dict(cycletime=100, subplan=p['subplan'])), '分相總和與週期不符')
        self.assertIsNone(build_timing.plan_reds(dict(cycletime=0, subplan=p['subplan'])))

    def test_day_segments_merge(self):
        plans = {'1': (30, (17, 17)), '2': (30, (17, 17)), '3': (60, (30, 25))}
        subs = [{'time': '0000', 'planid（SeqNo）': '1'}, {'time': '0600', 'planid（SeqNo）': '2'},
                {'time': '0700', 'planid（SeqNo）': '3'}, {'time': '0900', 'planid（SeqNo）': '9'}]
        self.assertEqual(build_timing.day_segments(subs, plans), [(0, 420, 30, (17, 17)), (420, 540, 60, (30, 25))])

    def test_build_taipei_small(self):
        tmp = tempfile.mkdtemp()
        try:
            table = [dict(icid='I1', segmenttype=st, subsegment=[{'time': '0000', 'planid（SeqNo）': '1'},
                                                                  {'time': '0700', 'planid（SeqNo）': '2' if st <= 5 else '1'}])
                     for st in range(1, 8)]
            plans = [dict(icid='I1', **{'planid（SeqNo）': 1}, cycletime=30, subplan=[dict(green=10, yellow=3, allred=2)] * 2),
                     dict(icid='I1', **{'planid（SeqNo）': 2}, cycletime=60, subplan=[dict(green=30, yellow=3, allred=2),
                                                                                 dict(green=20, yellow=3, allred=2)])]
            json.dump(table, open(os.path.join(tmp, 't.json'), 'w', encoding='utf-8'))
            json.dump(plans, open(os.path.join(tmp, 'p.json'), 'w', encoding='utf-8'))
            write_csv(os.path.join(tmp, 'ic.csv'), ['icid', '設備編號', '路口名稱', '群組', '經度', '緯度'],
                      [['I1', 'S1', '中山路　　民生路', 'G', '121.52', '25.05']])
            doc, st = build_timing.build_taipei(os.path.join(tmp, 't.json'), os.path.join(tmp, 'p.json'),
                                                os.path.join(tmp, 'ic.csv'), '20261120.1')
            self.assertEqual(build_timing.validate(doc), [])
            j = doc['junctions'][0]
            self.assertEqual(j['n'], '中山路 民生路')
            self.assertEqual(j['p'], [dict(w=31, s=0, e=420, c=30, r=[17, 17]), dict(w=31, s=420, e=1440, c=60, r=[27, 37]),
                                      dict(w=96, s=0, e=1440, c=30, r=[17, 17])])
        finally:
            shutil.rmtree(tmp)

    def test_penghu_greens(self):
        self.assertEqual(build_timing.parse_penghu_greens('50.2'), [50, 20])
        self.assertEqual(build_timing.parse_penghu_greens('30.15'), [30, 15])
        self.assertEqual(build_timing.parse_penghu_greens(''), [])
        self.assertEqual(build_timing.parse_penghu_greens('a.b'), [])


class SpeedLimitTests(unittest.TestCase):
    def test_km_range(self):
        self.assertEqual(build_speed_limits.km_range('大安溪橋(北向155K處、南向155K+600處)至楠梓交流道(356K)'), (155.6, 356.0))
        self.assertEqual(build_speed_limits.km_range('楠梓交流道(356K)以南'), (None, None))
        segs, rej = build_speed_limits.parse_rows('路線,路段,速限\n國道1號,A(1K)至B(2K),110\n國道1號,x,abc\n'.encode('cp950'))
        self.assertEqual(len(segs), 1)
        self.assertEqual(len(rej), 1)


class WorkflowTests(unittest.TestCase):
    """workflow 檔的基本守則（不用 yaml 套件，只做文字檢查）"""

    def read(self, name):
        return open(os.path.join(paths.ROOT, '.github', 'workflows', name), encoding='utf-8').read()

    def test_schedules_and_commands(self):
        daily = self.read('daily-speedcams.yml')
        self.assertIn("cron: '0 19 * * *'", daily, '每日 03:00 台灣時間＝19:00 UTC')
        self.assertIn('scripts/fetch.py speedcams-core', daily)
        self.assertIn('scripts/publish.py speedcams', daily)
        self.assertIn('timeout-minutes: 20', daily, '每日 fetch 步驟限 20 分鐘')
        self.assertIn('timeout-minutes: 40', daily, '每日 job 限 40 分鐘')
        self.assertIn('PYTHONUNBUFFERED', daily)
        self.assertIn('issues: write', daily)
        weekly = self.read('weekly-osm.yml')
        self.assertIn('download.geofabrik.de/asia/taiwan-latest.osm.pbf', weekly)
        self.assertIn('scripts/fetch.py signals', weekly)
        self.assertIn('scripts/fetch.py speedcams-counties', weekly)
        monthly = self.read('monthly-timing.yml')
        self.assertIn('scripts/fetch.py timing', monthly)
        pr = self.read('validate-on-push.yml')
        self.assertIn('unittest', pr)

    def test_no_secrets_besides_github_token(self):
        for name in os.listdir(os.path.join(paths.ROOT, '.github', 'workflows')):
            text = self.read(name)
            for line in text.splitlines():
                if 'secrets.' in line:
                    self.assertIn('secrets.GITHUB_TOKEN', line, '%s 只能用內建 GITHUB_TOKEN' % name)


if __name__ == '__main__':
    unittest.main()
