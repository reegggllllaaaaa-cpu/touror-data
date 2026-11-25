# -*- coding: utf-8 -*-
"""紅綠燈 TSIG v2 發布格式測試：編解碼、旗標保留、發布管線與本機 build_signals（含 snap_signals）同輸入結果一致。
吸附測試需要 osmium＋numpy（weekly-osm 有裝；沒裝就跳過）。"""
import csv
import datetime
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

from common import formats, paths  # noqa: E402
from signals import gov_signals  # noqa: E402

VENDOR_SIG = os.path.join(paths.VENDOR, '資料', '紅綠燈')


def load_vendor(name):
    spec = importlib.util.spec_from_file_location('vendor_' + name, os.path.join(VENDOR_SIG, name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def have_snap_deps():
    try:
        import numpy  # noqa: F401
        import osmium  # noqa: F401
        return True
    except ImportError:
        return False


class SignalsV2FormatTests(unittest.TestCase):
    ROWS = [(25040000, 121510000, 1 | 64), (22600000, 120300000, 2 | 4 | 128), (25040100, 121510100, 1 | 8 | 16),
            (23000000, 120200000, 1 | 8 | 32)]

    def test_v2_roundtrip_keeps_high_flags(self):
        data = formats.write_signals_bin(self.ROWS, 1794950670)
        self.assertEqual(struct.unpack_from('<H', data, 4)[0], 2)
        head, back = formats.read_signals_bin(data)
        self.assertEqual(head['version'], 2)
        self.assertEqual(sorted(back), sorted(self.ROWS))

    def test_v1_write_and_read_mask_high_bits(self):
        data = formats.write_signals_bin(self.ROWS, 1794950670, version=1)
        head, back = formats.read_signals_bin(data)
        self.assertEqual(head['version'], 1)
        self.assertEqual(sorted(back), sorted((a, b, f & 0x0F) for a, b, f in self.ROWS))
        with self.assertRaises(formats.FormatError):
            formats.write_signals_bin(self.ROWS, 0, version=3)
        bad = bytearray(data)
        bad[4] = 3
        with self.assertRaises(formats.FormatError):
            formats.read_signals_bin(bytes(bad))

    def test_bytes_identical_to_vendor_encoder(self):
        bs = load_vendor('build_signals')
        self.assertEqual(bs.VERSION, formats.SIGNALS_VERSION)
        ts = 1794950670
        ours = formats.write_signals_bin(self.ROWS, ts)
        theirs = bs.encode(self.ROWS, datetime.datetime.fromtimestamp(ts, datetime.timezone.utc))
        self.assertEqual(ours, theirs)
        self.assertEqual(sorted(bs.decode(ours)), sorted(self.ROWS))

    def test_vendor_copy_matches_local_build_scripts(self):
        local = os.path.join(paths.ROOT, '..', '資料', '紅綠燈')
        if not os.path.isdir(local):
            self.skipTest('沒有本機 資料/紅綠燈 工作區')
        for name in ('build_signals.py', 'snap_signals.py'):
            a = open(os.path.join(local, name), 'rb').read().replace(b'\r\n', b'\n')
            b = open(os.path.join(VENDOR_SIG, name), 'rb').read().replace(b'\r\n', b'\n')
            self.assertEqual(a, b, '%s 與本機 資料/紅綠燈 不同，請重新複製到 scripts/vendor' % name)


# 合成小路網（緯度 23.6 附近）：平面十字路口 n2、高架國道、各種號誌點
OSM_XML = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6" generator="test" timestamp="2026-11-20T00:00:00Z">
  <node id="1" version="1" lat="23.600000" lon="120.900000"/>
  <node id="2" version="1" lat="23.600000" lon="120.902000"/>
  <node id="3" version="1" lat="23.600000" lon="120.904000"/>
  <node id="4" version="1" lat="23.599000" lon="120.902000"/>
  <node id="5" version="1" lat="23.601000" lon="120.902000"/>
  <node id="6" version="1" lat="23.605000" lon="120.900000"/>
  <node id="7" version="1" lat="23.605000" lon="120.904000"/>
  <node id="101" version="1" lat="23.600010" lon="120.902000"><tag k="highway" v="traffic_signals"/></node>
  <node id="102" version="1" lat="23.605010" lon="120.902000"><tag k="highway" v="traffic_signals"/></node>
  <node id="103" version="1" lat="23.603000" lon="120.903000"><tag k="highway" v="traffic_signals"/></node>
  <node id="104" version="1" lat="23.600100" lon="120.902000"><tag k="highway" v="crossing"/><tag k="crossing" v="traffic_signals"/></node>
  <node id="105" version="1" lat="23.600270" lon="120.903000"><tag k="highway" v="traffic_signals"/></node>
  <node id="106" version="1" lat="23.600500" lon="120.901000"><tag k="highway" v="traffic_signals"/><tag k="traffic_signals" v="blinker"/></node>
  <way id="201" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="residential"/></way>
  <way id="202" version="1"><nd ref="4"/><nd ref="2"/><nd ref="5"/><tag k="highway" v="residential"/></way>
  <way id="203" version="1"><nd ref="6"/><nd ref="7"/><tag k="highway" v="motorway"/><tag k="bridge" v="yes"/><tag k="layer" v="1"/></way>
</osm>
"""
GOV_CSV_ROWS = [
    dict(lat='23.6000000', lon='120.9035000', source='user_verified', name='使用者確認'),
    dict(lat='23.5980000', lon='120.8950000', source='tainan_shp_137239', name='臺南 SHP'),
]


@unittest.skipUnless(have_snap_deps(), '需要 osmium＋numpy')
class SignalsSnapPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.osm = os.path.join(self.tmp, 'test.osm')
        with open(self.osm, 'w', encoding='utf-8') as f:
            f.write(OSM_XML)
        self.gov_csv = os.path.join(self.tmp, 'gov_signals_extra.csv')
        with open(self.gov_csv, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=['lat', 'lon', 'source', 'name'])
            w.writeheader()
            w.writerows(GOV_CSV_ROWS)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, *extra):
        out = os.path.join(self.tmp, 'out-%d.bin' % len(extra))
        r = subprocess.run([sys.executable, '-I', os.path.join(VENDOR_SIG, 'build_signals.py'), '--pbf', self.osm,
                            '--out', out, '--copy-to', '', '--stats', os.path.join(self.tmp, 'stats.json'),
                            '--snap-stats', os.path.join(self.tmp, 'snap.json'), '--taichung-csv', ''] + list(extra),
                           capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return formats.read_signals_bin(open(out, 'rb').read())[1]

    def pipeline(self, osm_rows):
        import publish
        srcs = [dict(key=k, dataset='', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='t', csv_source=k,
                     lat=('lat',), lon=('lon',), name=('name',)) for k in ('tainan_shp_137239', 'user_verified')]
        rows, st = gov_signals.merge(osm_rows, self.tmp, sources=srcs)
        self.assertEqual(sum(s['added'] for s in st), 2)
        report = {'checks': {}}
        snapped = publish.snap_rows(report, rows, self.osm)
        return snapped, report

    def test_pipeline_matches_local_build(self):
        osm_rows = self.build('--no-snap', '--extra-csv', '')
        self.assertTrue(all(f < 16 for _, _, f in osm_rows))
        self.assertEqual(len(osm_rows), 5)  # 閃光號誌排除
        snapped, report = self.pipeline(osm_rows)
        local = self.build('--extra-csv', self.gov_csv)
        self.assertEqual(sorted(snapped), sorted(local))
        data = formats.write_signals_bin(snapped, 0)
        head, back = formats.read_signals_bin(data)
        self.assertEqual(head['version'], 2)
        self.assertEqual(sorted(back), sorted(snapped))

        def flags_at(lat, lon):
            return [f for a, b, f in snapped if abs(a - lat) < 60 and abs(b - lon) < 60]
        self.assertTrue(flags_at(23600010, 120902000)[0] & formats.SIG_FLAG_AT_JUNCTION)
        self.assertTrue(flags_at(23605010, 120902000)[0] & formats.SIG_FLAG_ELEVATED)
        self.assertTrue(flags_at(23603000, 120903000)[0] & formats.SIG_FLAG_OFFROAD)
        self.assertTrue(flags_at(23600100, 120902000)[0] & formats.SIG_FLAG_MERGED)
        self.assertEqual(flags_at(23600000, 120903000), [1])  # 偏 30 m 的點吸附到路上
        self.assertEqual(flags_at(23600000, 120903500), [1 | 8])  # user_verified 保留 bit3
        self.assertEqual(flags_at(23598000, 120895000), [1 | 8 | formats.SIG_FLAG_OFFROAD])  # 政府補點保留 bit3
        fc = report['signals_snap']['flag_counts']
        self.assertEqual((fc['gov'], fc['elevated'], fc['offroad'], fc['merged']), (2, 1, 2, 1))

    def test_resnap_is_stable(self):
        snapped, _ = self.pipeline(self.build('--no-snap', '--extra-csv', ''))
        import publish
        again = publish.snap_rows({'checks': {}}, snapped, self.osm)
        self.assertEqual(sorted(again), sorted(snapped))


if __name__ == '__main__':
    unittest.main()
