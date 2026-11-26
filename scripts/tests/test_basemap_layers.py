# -*- coding: utf-8 -*-
"""底圖 roads 立體分層與顯示平滑測試（只用標準庫）。"""
import math
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(SCRIPTS, "basemap"))

import smooth_display as sd  # noqa: E402

BASEMAP = os.path.join(SCRIPTS, "basemap")


def _read(*p):
    with open(os.path.join(BASEMAP, *p), encoding="utf-8") as f:
        return f.read()


def _arc(n=7, radius_m=120.0, sweep_deg=60.0, lon0=120.30, lat0=22.67):
    """稀疏節點的匝道弧線（折角明顯）。"""
    k = 111320 * math.cos(math.radians(lat0))
    pts = []
    for i in range(n):
        a = math.radians(sweep_deg * i / (n - 1))
        x, y = radius_m * math.sin(a), radius_m * (1 - math.cos(a))
        pts.append((lon0 + x / k, lat0 + y / 110540))
    return pts


def _xy(ll, lat0=22.67):
    return (ll[0] * 111320 * math.cos(math.radians(lat0)), ll[1] * 110540)


class EligibleTests(unittest.TestCase):
    def test_targets(self):
        self.assertTrue(sd.eligible({"highway": "motorway_link"}))
        self.assertTrue(sd.eligible({"highway": "trunk"}))
        self.assertTrue(sd.eligible({"highway": "primary", "bridge": "yes"}))
        self.assertTrue(sd.eligible({"highway": "primary", "bridge": "viaduct"}))
        self.assertFalse(sd.eligible({"highway": "primary"}))
        self.assertFalse(sd.eligible({"highway": "residential", "bridge": "no"}))
        self.assertFalse(sd.eligible({"railway": "rail", "bridge": "yes"}))
        self.assertFalse(sd.eligible({"highway": "footway", "bridge": "yes"}))
        self.assertFalse(sd.eligible({"highway": "pedestrian", "bridge": "yes", "area": "yes"}))


class SmoothTests(unittest.TestCase):
    def _run(self, ll, fixed):
        res = sd.smooth_way(ll, fixed)
        geo = [ll[i] if p is None else p for p, i in res]
        return res, geo

    def test_endpoints_fixed_and_offset_bounded(self):
        ll = _arc()
        res, geo = self._run(ll, [False] * len(ll))
        self.assertEqual(res[0], (None, 0))
        self.assertEqual(res[-1], (None, len(ll) - 1))
        self.assertGreater(len(res), len(ll))  # 有重取樣
        self.assertLessEqual(sd.max_offset_m(ll, geo), sd.MAX_OFFSET_M + 1e-6)

    def test_shared_interior_node_kept(self):
        ll = _arc(n=9)
        fixed = [False] * 9
        fixed[4] = True  # 與其他道路共用的節點
        res, geo = self._run(ll, fixed)
        kept = [i for p, i in res if p is None]
        self.assertIn(4, kept)
        self.assertEqual(geo[[i for _, i in res].index(4)], ll[4])  # 座標完全不動

    def test_offset_clamped_on_sharp_corner(self):
        # 90 度直角（節點稀疏），平滑會想切角，但不能超過 1.5 m
        lat0 = 22.67
        k = 111320 * math.cos(math.radians(lat0))
        ll = [(120.3, lat0), (120.3 + 60 / k, lat0), (120.3 + 60 / k, lat0 + 60 / 110540)]
        _, geo = self._run(ll, [False] * 3)
        off = sd.max_offset_m(ll, geo)
        self.assertLessEqual(off, sd.MAX_OFFSET_M + 1e-6)
        self.assertGreater(off, 0.5)  # 角確實有被修圓

    def test_straight_two_point_segment_untouched(self):
        ll = [(120.30, 22.67), (120.301, 22.67)]
        res, _ = self._run(ll, [False, False])
        self.assertEqual(res, [(None, 0), (None, 1)])

    def test_smoothed_corners_gentler(self):
        ll = _arc()
        _, geo = self._run(ll, [False] * len(ll))

        def max_turn(pts):
            xy = [_xy(p) for p in pts]
            best = 0.0
            for a, b, c in zip(xy, xy[1:], xy[2:]):
                h1 = math.atan2(b[1] - a[1], b[0] - a[0])
                h2 = math.atan2(c[1] - b[1], c[0] - b[0])
                best = max(best, abs((h2 - h1 + math.pi) % (2 * math.pi) - math.pi))
            return math.degrees(best)

        self.assertLess(max_turn(geo), max_turn(ll))


class RoadAttrsSourceTests(unittest.TestCase):
    """Java 端（planetiler）無法在這裡編譯，檢查補丁與屬性規則的原始碼。"""

    def test_build_script_patches_roads(self):
        sh = _read("build_basemap.sh")
        self.assertIn("roads/TourorRoadAttrs.java", sh)
        self.assertIn("TourorRoadAttrs.apply(feat, sf);", sh)
        self.assertIn("smooth_display.py", sh)
        # 平滑接在改號之後、planetiler 之前
        self.assertLess(sh.index("renumber_synthetic.py"), sh.index("smooth_display.py"))
        self.assertLess(sh.index("smooth_display.py"), sh.index("java -Xmx"))

    def test_layer_attrs(self):
        java = _read("roads", "TourorRoadAttrs.java")
        # layer 在 z12 起寫入（與 is_bridge 同級），合併只併屬性相同的線 → 不跨 layer 合併
        self.assertRegex(java, r'setAttrWithMinzoom\("layer", layer, 12\)')
        self.assertRegex(java, r'setAttrWithMinzoom\("lanes", lanes, 14\)')
        self.assertRegex(java, r'setAttrWithMinzoom\("oneway", "yes", 14\)')
        self.assertRegex(java, r"return bridge \? 1 : tunnel \? -1 : 0;")
        self.assertIsNone(re.search(r"setAttr\(\"layer\"", java))

    def test_layer_tag_decimal_and_multi(self):
        # Furen-max 審查：layer='1;2'、'-1.5' 不可落回橋／隧道規則；取分號前、小數向零截斷
        java = _read("roads", "TourorRoadAttrs.java")
        self.assertIn('(int) Double.parseDouble(layerTag.trim().split(";")[0].trim())', java)
        for tag, want in (("1;2", 1), ("-1.5", -1), ("2.3", 2)):
            self.assertEqual(int(float(tag.split(";")[0])), want)

    def test_shared_count_includes_non_road_ways(self):
        # Furen-max 審查：與鐵路、建物等非道路 way 共用的節點也要計入共用數 → 固定不動
        src = _read("smooth_display.py")
        i = src.index("self.uses[r.ref]")  # 計共用數的那個 way handler
        way_handler = src[src.rindex("def way(self, w):", 0, i):i]
        self.assertNotIn('"highway" not in w.tags', way_handler)


if __name__ == "__main__":
    unittest.main()
