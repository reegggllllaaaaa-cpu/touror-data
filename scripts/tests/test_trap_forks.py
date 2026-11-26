"""C1 陷阱分岔表閘門：curated/cases.json 帶 expect_trap_fork 的案例（左營 Z1–Z6），data/lanes/trap_forks.json 必須有對應的誤上點，
左右順序、通往等級、車道守恆、不可回頭點／最後決策點一致（docs/複雜路口引導設計.md §4.3、§5.1、§5.2）。"""
import json
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
TRAPS = os.path.join(ROOT, "data", "lanes", "trap_forks.json")
CASES = os.path.join(ROOT, "curated", "cases.json")


@unittest.skipUnless(os.path.exists(TRAPS), "尚未產生 trap_forks.json")
class TrapForksTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = json.load(open(TRAPS, encoding="utf-8"))
        cls.cases = [c for c in json.load(open(CASES, encoding="utf-8"))["cases"] if c.get("expect_trap_fork")]
        cls.idx = {(f["node"], f["entry_way"]): f for f in cls.doc["forks"]}

    def test_cases_present(self):
        self.assertEqual(len(self.cases), 6)

    def test_each_case(self):
        for c in self.cases:
            exp = c["expect_trap_fork"]
            with self.subTest(case=c["id"]):
                f = self.idx.get((exp["node"], exp["entry_way"]))
                self.assertIsNotNone(f, "trap_forks.json 缺 %s" % c["id"])
                self.assertTrue(f["misentry"])
                got = [{"way": b["way"], "side": b["side"], "leads_to": b["leads_class"]} for b in f["branches"]]
                self.assertEqual(got, exp["branches"])
                if "lane_split" in exp:
                    self.assertEqual(f["lane_split"], exp["lane_split"])
                pts = [p for nr in f.get("no_return", []) for p in nr.get("points", [])]
                self.assertTrue(pts, "沒有不可回頭點")
                self.assertEqual(pts[0]["no_return"]["node"], exp["no_return"])
                self.assertEqual(pts[0]["last_decision"]["node"], exp["last_decision"])

    def test_no_negative_text_fields(self):
        """使用者裁決 2026-11-26：只做正向引導，資料不帶指認／否定文案欄位"""
        bad = {"warning", "avoid", "trap_text", "negative", "do_not"}
        f = self.doc["forks"][0]
        self.assertFalse(bad & set(f), sorted(bad & set(f)))
        self.assertFalse(bad & set(f["branches"][0]))

    def test_zuoying_count(self):
        """左營—鼎金範圍誤上點（不同節點）＝6（§4.3 試跑）"""
        s, w, n, e = 22.665, 120.285, 22.695, 120.335
        nodes = {f["node"] for f in self.doc["forks"] if f["misentry"] and s <= f["lat"] <= n and w <= f["lon"] <= e}
        self.assertEqual(len(nodes), 6, sorted(nodes))


if __name__ == "__main__":
    unittest.main()
