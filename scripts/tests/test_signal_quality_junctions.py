# -*- coding: utf-8 -*-
"""路口庫不存在（CI 沒有本機 資料\\搜尋\\touror_junctions.sqlite）時，號誌信心旁檔與 publish 號誌群組照常產出，
只有疑似未收錄號誌路口清單留空；Mapillary 補點候選（candidates）則必須有路口庫，不能默默算成 0 筆。"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from common import formats, paths  # noqa: E402
from signals import signal_quality as sq  # noqa: E402
import publish  # noqa: E402


class MissingJunctionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old = sq.JUNCTIONS
        sq.JUNCTIONS = os.path.join(self.tmp.name, 'no_such_junctions.sqlite')
        self.addCleanup(setattr, sq, 'JUNCTIONS', self.old)

    def test_junctions_required_raises_optional_returns_none(self):
        with self.assertRaises(FileNotFoundError):
            sq.junctions()
        self.assertIsNone(sq.junctions(required=False))

    def test_build_without_junctions_writes_sidecar_with_empty_suspects(self):
        rows = [(25_040_000, 121_510_000, 1), (25_050_000, 121_520_000, 8)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            data, st = sq.build(rows, None)
        self.assertIn('::warning::', out.getvalue())
        self.assertEqual(st['suspects'], 0)
        self.assertFalse(st['junctions_available'])
        formats.read_signal_quality_bin(data)

    def test_publish_group_signals_no_osm_without_junctions(self):
        prev = os.path.join(paths.ROOT, publish.FILES['taiwan-signals'][0])
        if not os.path.exists(prev):
            self.skipTest('沒有 data/signals/taiwan-signals.bin')
        report = {'checks': {}}
        with contextlib.redirect_stdout(io.StringIO()):
            res = publish.group_signals(report, None, True)
        self.assertIn('taiwan-signals', res)
        self.assertIn('taiwan-signals-quality', res)
        self.assertEqual(res['taiwan-signals-quality'][2]['suspects'], 0)
        self.assertFalse(report['signals_quality']['junctions_available'])


if __name__ == '__main__':
    unittest.main()
