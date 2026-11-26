# -*- coding: utf-8 -*-
"""機車左轉規則 decide()：證據衝突時信心高者勝（2026-11-26 主管決定）。"""
import unittest

from rules.build_moto_left_turn import decide

ZUN20_OLD = ("two_stage", "mly_sign_zun20:1", "low")        # 2022 年以前最後拍到的遵20
ZUN20_NEW = ("two_stage", "mly_sign_zun20:2", "medium")     # 2022 年以後仍拍到
TP_2LANE = ("direct", "taipei_rule_2lane", "medium")
NO_MOTO = ("two_stage", "mly_sign_no_moto:3", "low")


class DecideTest(unittest.TestCase):
    def test_old_zun20_does_not_override_taipei_2lane(self):
        self.assertEqual(decide([ZUN20_OLD, TP_2LANE]), TP_2LANE)

    def test_recent_zun20_wins_tie_by_order(self):
        self.assertEqual(decide([ZUN20_NEW, TP_2LANE]), ZUN20_NEW)

    def test_no_moto_sign_alone_stays_low(self):
        self.assertEqual(decide([NO_MOTO]), NO_MOTO)

    def test_no_moto_sign_loses_to_taipei_rule(self):
        self.assertEqual(decide([TP_2LANE, NO_MOTO]), TP_2LANE)

    def test_high_beats_medium(self):
        hi = ("two_stage", "osm_sign_zun20", "high")
        self.assertEqual(decide([TP_2LANE, hi]), hi)

    def test_banned_approach_is_unknown(self):
        self.assertEqual(decide([TP_2LANE], approach_banned=True), ("unknown", "approach_moto_banned", "none"))

    def test_no_evidence(self):
        self.assertEqual(decide([]), ("unknown", "no_evidence", "none"))


if __name__ == "__main__":
    unittest.main()
