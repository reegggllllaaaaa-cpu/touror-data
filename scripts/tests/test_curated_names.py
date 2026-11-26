# -*- coding: utf-8 -*-
"""N18b 回歸：curated/路名覆蓋.csv 也要進播報錨點庫與地名／店家庫（文前路案例，way 957963591 澄清巷→文前路）。"""
import importlib.util
import os
import shutil
import tempfile
import unittest

from common import curated_names

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 縮小版現場：文前路上坡段 215378083（n1-n2）、被 OSM 誤標澄清巷的 957963591（n2-n3，ref 高57）、
# 真正的澄清巷 service 小路 780459013（n2 往東 n4）。n3 是接大全一巷那端。
OSM = '''<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
 <node id="1" version="1" lat="22.6690500" lon="120.3459500"/>
 <node id="2" version="1" lat="22.6700000" lon="120.3450000"/>
 <node id="3" version="1" lat="22.6714800" lon="120.3442600"/>
 <node id="4" version="1" lat="22.6700000" lon="120.3470000"/>
 <node id="5" version="1" lat="22.6716000" lon="120.3438000"/>
 <way id="100" version="1"><nd ref="3"/><nd ref="5"/>
  <tag k="highway" v="residential"/><tag k="name" v="大全一巷"/></way>
 <way id="215378083" version="1"><nd ref="1"/><nd ref="2"/>
  <tag k="highway" v="tertiary"/><tag k="name" v="文前路"/><tag k="ref" v="高57"/></way>
 <way id="780459013" version="1"><nd ref="2"/><nd ref="4"/>
  <tag k="highway" v="service"/><tag k="name" v="澄清巷"/></way>
 <way id="957963591" version="1"><nd ref="2"/><nd ref="3"/>
  <tag k="highway" v="tertiary"/><tag k="name" v="澄清巷"/><tag k="ref" v="高57"/></way>
</osm>
'''


def load_mod(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, *rel.split('/')))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class CuratedNamesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.osm = os.path.join(cls.tmp, 'wenqian.osm')
        with open(cls.osm, 'w', encoding='utf-8') as f:
            f.write(OSM)
        cls.ov = curated_names.load_names()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_curated_csv_has_wenqian(self):
        self.assertEqual(self.ov[957963591][0], '文前路')
        t = curated_names.way_tags(957963591, {'name': '澄清巷', 'highway': 'tertiary'}, self.ov)
        self.assertEqual((t['name'], t['old_name']), ('文前路', '澄清巷'))

    def test_anchor_library_uses_curated_name(self):
        ba = load_mod('build_anchors_t', 'anchors/build_anchors.py')
        # 群 0：接大全一巷那端（n3），路口庫名「澄清巷／大全一巷」，附近的澄清巷只有被覆蓋的 way → 改文前路
        # 群 1：n2，真正的澄清巷 service 也接在這裡 → 澄清巷保留
        groups = [(22.67148, 120.34426, frozenset({'澄清巷', '大全一巷'})),
                  (22.6700, 120.3450, frozenset({'文前路', '澄清巷'}))]
        cells = {}
        for ci, (la, lo, full) in enumerate(groups):
            ki, kj = ba.key(la, lo)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    cells.setdefault((ki + di, kj + dj), []).append((la, lo, ci, full))
        names = set().union(*(g[2] for g in groups))
        hw, renames = ba.load_highway(cells, names, self.ov, pbf=self.osm)
        self.assertEqual(renames, {0: {'澄清巷': '文前路'}})
        self.assertIn((0, '文前路'), hw)
        joined = ba.rename_joined('澄清巷／大全一巷', renames[0])
        self.assertEqual(joined, '文前路／大全一巷')
        a, b, sa, sb = ba.cluster_names([joined])
        self.assertEqual({a, b}, {'文前路', '大全一巷'})
        self.assertNotIn('澄清巷', joined)

    def test_place_library_uses_curated_name(self):
        v2 = load_mod('build_places_v2_t', 'places/build_places_v2.py')
        work = os.path.join(self.tmp, 'work')
        os.makedirs(work, exist_ok=True)
        raw = v2.scan(self.osm, work, lambda *a: None, name_ov=self.ov)
        gao57 = [r for r in raw['roads'] if r[1] == '高57']
        self.assertEqual(len(gao57), 2)
        self.assertEqual({r[0] for r in gao57}, {'文前路'})
        # 真正的澄清巷 service 小路不受影響
        self.assertEqual([r[0] for r in raw['roads'] if r[1] == '' and r[0] == '澄清巷'], ['澄清巷'])

    def test_osm_road_names_for_address_uses_curated_name(self):
        ex = load_mod('extract_osm_road_names_t', 'places/extract_osm_road_names.py')
        h = ex.H(self.ov)
        h.apply_file(self.osm, locations=True, idx='flex_mem')
        idx = {r[0]: i for i, r in enumerate(h.roads)}
        # n3（接大全一巷那端）只屬於 957963591 與大全一巷：該點應歸文前路
        n3 = [p[2] for p in h.pts if abs(p[0] - 22.67148) < 1e-6 and abs(p[1] - 120.34426) < 1e-6]
        self.assertIn(idx['文前路'], n3)
        self.assertNotIn(idx['澄清巷'], n3)


if __name__ == '__main__':
    unittest.main()
