# -*- coding: utf-8 -*-
"""警政署事故補點與疑似漏燈多線索升級（accident_signals.py）規則測試，不需網路與大檔。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from signals import accident_signals as acc  # noqa: E402
from signals import signal_quality as sq  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES  # noqa: E402

M_LAT = 1 / 110_574.0  # 1 m 緯度


def row(kind='行車管制號誌', road='交岔路', lat='22.630000', lon='120.300000', t='120000', date='20260101', place='x'):
    return {'號誌-號誌種類名稱': kind, '道路型態大類別名稱': road, '緯度': lat, '經度': lon,
            '發生時間': t, '發生日期': date, '發生地點': place}


class FilterTests(unittest.TestCase):
    def test_flashing_and_no_signal_excluded(self):
        self.assertTrue(acc.is_signal_junction(row()))
        self.assertTrue(acc.is_signal_junction(row(kind='行車管制號誌(附設行人專用號誌)')))
        self.assertFalse(acc.is_signal_junction(row(kind='閃光號誌')))
        self.assertFalse(acc.is_signal_junction(row(kind='無號誌')))
        self.assertFalse(acc.is_signal_junction(row(road='單路部分')))
        self.assertFalse(acc.is_signal_junction(row(road='圓環廣場')))

    def test_party_rows_deduped_and_filtered(self):
        rows = [row(), row(), row(t='130000'), row(t='140000', kind='閃光號誌'), row(t='150000', lat='', lon='')]
        pts, st = acc.accidents(rows)
        self.assertEqual(len(pts), 2)               # 同一件兩位當事者只算一件；閃光不收
        self.assertEqual(st['accidents'], 4)
        self.assertEqual(st['bad_coord'], 1)


class ClusterSnapTests(unittest.TestCase):
    J = [(22.63, 120.30, 1, '甲路／乙路')]

    def pts(self, n, base_lat=22.63, step_m=5):
        return [(base_lat + i * step_m * M_LAT, 120.30) for i in range(n)]

    def test_min_three_accidents(self):
        two = acc.cluster(self.pts(2))
        self.assertEqual([c[2] for c in two], [2])
        added, st = acc.snap(two, self.J, existing=[])
        self.assertEqual(added, [])
        added, st = acc.snap(acc.cluster(self.pts(3)), self.J, existing=[])
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0][2], 1)
        self.assertEqual(added[0][4], 3)

    def test_cluster_radius_30m(self):
        far = [(22.63, 120.30), (22.63 + 40 * M_LAT, 120.30)]
        self.assertEqual(len(acc.cluster(far)), 2)
        near = [(22.63, 120.30), (22.63 + 20 * M_LAT, 120.30)]
        self.assertEqual(len(acc.cluster(near)), 1)

    def test_snap_within_30m_only(self):
        cl = [(22.63 + 25 * M_LAT, 120.30, 5)]
        self.assertEqual(len(acc.snap(cl, self.J, existing=[])[0]), 1)
        cl = [(22.63 + 35 * M_LAT, 120.30, 5)]
        added, st = acc.snap(cl, self.J, existing=[])
        self.assertEqual(added, [])
        self.assertEqual(st['no_junction'], 1)

    def test_existing_signal_within_40m_skipped(self):
        cl = [(22.63, 120.30, 5)]
        added, st = acc.snap(cl, self.J, existing=[(22.63 + 35 * M_LAT, 120.30)])
        self.assertEqual(added, [])
        self.assertEqual(st['has_signal'], 1)
        added, _ = acc.snap(cl, self.J, existing=[(22.63 + 45 * M_LAT, 120.30)])
        self.assertEqual(len(added), 1)


class UpgradeTests(unittest.TestCase):
    def test_single_clue_not_upgraded(self):
        sus = [(22630000, 120300000, sq.R_MAJOR), (22640000, 120300000, sq.R_MLY), (22650000, 120300000, sq.R_OSM_NODE)]
        up, combos = acc.upgrade(acc.suspect_clues(sus, acc_clusters=[]))
        self.assertEqual(up, [])
        self.assertEqual(combos, {})

    def test_two_clues_upgraded_including_accident(self):
        sus = [(22630000, 120300000, sq.R_MAJOR | sq.R_MLY), (22640000, 120300000, sq.R_MAJOR)]
        clusters = [(22.64 + 10 * M_LAT, 120.30, 2)]
        up, combos = acc.upgrade(acc.suspect_clues(sus, clusters))
        self.assertEqual(len(up), 2)
        self.assertEqual(combos, {'mly+major': 1, 'major+accident': 1})
        # 只有 1 件事故不算一類線索（CLUE_MIN_ACCIDENTS=2，主管 11-28 裁示）
        up1, combos1 = acc.upgrade(acc.suspect_clues(sus, [(22.64 + 10 * M_LAT, 120.30, 1)]))
        self.assertEqual(len(up1), 1)
        self.assertEqual(combos1, {'mly+major': 1})

    def test_accident_clue_needs_30m(self):
        sus = [(22640000, 120300000, sq.R_MAJOR)]
        up, _ = acc.upgrade(acc.suspect_clues(sus, [(22.64 + 35 * M_LAT, 120.30, 4)]))
        self.assertEqual(up, [])

    def test_upgrade_skips_existing(self):
        sus = [(22630000, 120300000, sq.R_MAJOR | sq.R_MLY)]
        up, combos = acc.upgrade(acc.suspect_clues(sus, []), existing=[(22.63, 120.30)])
        self.assertEqual(up, [])


class SourcesTests(unittest.TestCase):
    def test_sources_registered_medium(self):
        by = {s['key']: s for s in GOV_SIGNAL_SOURCES}
        for k, cs in (('npa_accident', 'npa-accident'), ('suspect_multi', 'suspect-multi')):
            self.assertEqual(by[k]['csv_source'], cs)
            self.assertEqual(by[k]['confidence'], 'medium')
            self.assertEqual(by[k]['fmt'], 'POINTS_CSV')


if __name__ == '__main__':
    unittest.main()
