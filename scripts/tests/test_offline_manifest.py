# -*- coding: utf-8 -*-
"""App 離線大檔下載清單（offline-manifest.json）：格式檢查、倉庫現況、發布時會複製到網站。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
sys.path.insert(0, SCRIPTS)

from common import paths  # noqa: E402
import publish  # noqa: E402
import validate  # noqa: E402

SHA = 'a' * 64


def good_file(kind='places', name='touror_places.sqlite'):
    return {'name': name, 'kind': kind, 'url': 'https://example.com/x', 'bytes': 10, 'sha256': SHA, 'version': '20261123'}


def good_doc():
    return {'schema': 1, 'version': '20261123', 'files': [good_file()]}


class OfflineManifestValidation(unittest.TestCase):
    def test_good_doc_has_no_problems(self):
        self.assertEqual(validate.offline_manifest_problems(good_doc()), [])

    def test_unknown_kind_is_a_problem(self):
        d = good_doc()
        d['files'].append(good_file(kind='mystery', name='x.bin'))
        self.assertTrue(any('未知的 kind' in p for p in validate.offline_manifest_problems(d)))

    def test_known_overture_kind_is_ok(self):
        d = good_doc()
        d['files'].append(good_file(kind='places_overture', name='touror_places_overture.sqlite'))
        self.assertEqual(validate.offline_manifest_problems(d), [])

    def test_known_gov_and_junctions_kinds_are_ok(self):
        d = good_doc()
        d['files'].append(good_file(kind='places_gov', name='touror_places_gov.sqlite'))
        d['files'].append(good_file(kind='junctions', name='touror_junctions.sqlite'))
        self.assertEqual(validate.offline_manifest_problems(d), [])

    def test_duplicate_kind_and_bad_fields(self):
        d = good_doc()
        bad = good_file()
        bad.update(url='http://insecure', bytes=0, sha256='xyz', version='2026')
        d['files'].append(bad)
        problems = ' | '.join(validate.offline_manifest_problems(d))
        for key in ('kind 重複', 'https', 'bytes', 'sha256', 'version'):
            self.assertIn(key, problems)

    def test_bad_version_and_schema(self):
        d = good_doc()
        d.update(schema=2, version='nope')
        problems = ' | '.join(validate.offline_manifest_problems(d))
        self.assertIn('schema', problems)
        self.assertIn('yyyymmdd', problems)

    def test_filename_with_path_separator_rejected(self):
        d = good_doc()
        d['files'][0]['name'] = '../evil.sqlite'
        self.assertTrue(any('檔名' in p for p in validate.offline_manifest_problems(d)))


class RepoOfflineManifest(unittest.TestCase):
    def test_repo_file_is_valid_and_has_overture(self):
        with open(paths.OFFLINE_MANIFEST, encoding='utf-8') as f:
            doc = json.load(f)
        self.assertEqual(validate.offline_manifest_problems(doc), [])
        kinds = {x['kind'] for x in doc['files']}
        self.assertTrue({'tiles', 'basemap', 'places', 'addr', 'places_overture'} <= kinds)


class WriteSiteCopiesOfflineManifest(unittest.TestCase):
    def test_copied_to_site_root(self):
        tmp = tempfile.mkdtemp()
        old_site, old_root, old_manifest, old_offline = paths.SITE, paths.ROOT, paths.MANIFEST, paths.OFFLINE_MANIFEST
        try:
            paths.SITE = os.path.join(tmp, 'site')
            paths.ROOT = tmp
            paths.MANIFEST = os.path.join(tmp, 'manifest.json')
            paths.OFFLINE_MANIFEST = os.path.join(tmp, 'offline-manifest.json')
            with open(paths.MANIFEST, 'w', encoding='utf-8') as f:
                json.dump({'files': [], 'generated': '2026-11-23T00:00:00Z'}, f)
            with open(paths.OFFLINE_MANIFEST, 'w', encoding='utf-8') as f:
                json.dump(good_doc(), f)
            publish.write_site({'files': [], 'generated': '2026-11-23T00:00:00Z'})
            self.assertTrue(os.path.exists(os.path.join(paths.SITE, 'offline-manifest.json')))
            os.remove(paths.OFFLINE_MANIFEST)
            publish.write_site({'files': [], 'generated': '2026-11-23T00:00:00Z'})
            self.assertFalse(os.path.exists(os.path.join(paths.SITE, 'offline-manifest.json')), '沒有清單檔時不應產生')
        finally:
            paths.SITE, paths.ROOT, paths.MANIFEST, paths.OFFLINE_MANIFEST = old_site, old_root, old_manifest, old_offline
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
