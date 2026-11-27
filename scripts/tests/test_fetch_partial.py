# -*- coding: utf-8 -*-
"""fetch.py 號誌來源部分失敗政策：單一來源失敗略過並警告；過半或全部失敗仍讓步驟失敗。不連網。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import fetch  # noqa: E402


def fake_targets(n):
    def targets(group):
        return [dict(dataset=str(100 + i), file='src%d_signals_%d.csv' % (i, 100 + i), fmt='CSV', dir=TMP[0])
                for i in range(n)]
    return targets


TMP = ['']


def fake_fetch(bad):
    def fetch_dataset(dataset_id, dest, **kw):
        if dataset_id in bad:
            return dict(dataset=dataset_id, file=os.path.basename(dest), status='failed',
                        reason='下載失敗且無舊檔：timed out')
        return dict(dataset=dataset_id, file=os.path.basename(dest), status='updated')
    return fetch_dataset


class PartialFailureTest(unittest.TestCase):
    def setUp(self):
        TMP[0] = tempfile.mkdtemp()
        self.report = os.path.join(TMP[0], 'report.json')
        self.summary = os.path.join(TMP[0], 'summary.md')

    def tearDown(self):
        shutil.rmtree(TMP[0], ignore_errors=True)

    def run_fetch(self, n, bad, group='signals'):
        env = dict(os.environ, GITHUB_STEP_SUMMARY=self.summary)
        with mock.patch.object(fetch, 'targets', fake_targets(n)), \
                mock.patch.object(fetch.datagov, 'fetch_dataset', fake_fetch(bad)), \
                mock.patch.dict(os.environ, env), \
                mock.patch('builtins.print') as pr:
            code = fetch.main([group, '--report', self.report])
        out = '\n'.join(' '.join(str(x) for x in c.args) for c in pr.call_args_list)
        with open(self.report, encoding='utf-8') as f:
            rep = json.load(f)
        return code, out, rep

    def test_one_source_failed_is_skipped_with_warning(self):
        code, out, rep = self.run_fetch(4, {'102'})
        self.assertEqual(code, 0)
        self.assertIn('::warning', out)
        self.assertIn('102', out)
        self.assertEqual([r['status'] for r in rep], ['updated', 'updated', 'skipped', 'updated'])
        with open(self.summary, encoding='utf-8') as f:
            s = f.read()
        self.assertIn('略過的來源', s)
        self.assertIn('102', s)

    def test_half_failed_still_passes(self):
        code, _, _ = self.run_fetch(4, {'100', '101'})
        self.assertEqual(code, 0)

    def test_more_than_half_failed_fails(self):
        code, out, rep = self.run_fetch(4, {'100', '101', '102'})
        self.assertEqual(code, 1)
        self.assertIn('::error', out)
        self.assertEqual(sum(r['status'] == 'failed' for r in rep), 3)

    def test_all_failed_fails(self):
        code, _, _ = self.run_fetch(1, {'100'})
        self.assertEqual(code, 1)

    def test_other_groups_keep_strict_policy(self):
        code, _, _ = self.run_fetch(4, {'102'}, group='timing')
        self.assertEqual(code, 1)

    def test_kept_old_file_is_reported(self):
        def fetch_dataset(dataset_id, dest, **kw):
            st = 'kept' if dataset_id == '101' else 'updated'
            return dict(dataset=dataset_id, file=os.path.basename(dest), status=st, reason='下載失敗，沿用舊檔')
        with mock.patch.object(fetch, 'targets', fake_targets(3)), \
                mock.patch.object(fetch.datagov, 'fetch_dataset', fetch_dataset), \
                mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': self.summary}), \
                mock.patch('builtins.print') as pr:
            code = fetch.main(['signals', '--report', self.report])
        out = '\n'.join(' '.join(str(x) for x in c.args) for c in pr.call_args_list)
        self.assertEqual(code, 0)
        self.assertIn('沿用舊檔', out)
        with open(self.summary, encoding='utf-8') as f:
            self.assertIn('沿用舊檔的來源', f.read())


if __name__ == '__main__':
    unittest.main()
