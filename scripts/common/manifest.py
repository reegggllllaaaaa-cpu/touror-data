# -*- coding: utf-8 -*-
"""manifest.json、版本號（yyyymmdd.rev）與 CHANGELOG。

版本規則（App 端 DataVersion 同規則比大小）：
- 內容（SHA-256）沒變 → 沿用上一版版本號。
- 內容有變 → 今天日期；若上一版（或資料本身內建的版本）日期 ≥ 今天，rev＝上一版 rev＋1，否則 rev＝1。
  「資料本身內建的版本」例如測速打包檔的 20261119.3：發布版本一定比 App 內建版大，App 才會下載。
"""
import datetime
import hashlib
import json
import os
import re

SCHEMA = 1
VERSION_RE = re.compile(r'^(\d{8})(?:\.(\d+))?$')
TAIPEI = datetime.timezone(datetime.timedelta(hours=8))


def today_taipei(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(TAIPEI).strftime('%Y%m%d')


def parse_version(v):
    """'20261119.3' → (20261119, 3)；'20261119' → (20261119, 0)；格式錯回 None"""
    m = VERSION_RE.match(str(v or '').strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def format_version(t):
    return '%08d.%d' % t


def next_version(prev, floor, changed, today):
    """prev：上一版發布版本（可 None）；floor：資料內建版本（可 None，例如 App 包內同類檔的版本）；
    changed：內容是否變了；today：'yyyymmdd'。"""
    p = parse_version(prev)
    if p and not changed:
        return format_version(p)
    base = max([x for x in (p, parse_version(floor)) if x] or [(0, 0)])
    t = int(today)
    if base[0] >= t:
        return format_version((base[0], base[1] + 1))
    return format_version((t, 1))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def entry(name, path, root, version, count, min_app, license_text, attribution, sources, date=None):
    full = os.path.join(root, path)
    v = parse_version(version)
    return dict(
        name=name, path=path.replace(os.sep, '/'), version=version,
        date=date or ('%04d-%02d-%02d' % (v[0] // 10000, v[0] // 100 % 100, v[0] % 100) if v else ''),
        count=count, sha256=sha256_file(full), bytes=os.path.getsize(full), minAppVersion=min_app,
        license=license_text, attribution=attribution, sources=sources)


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def previous_entry(prev_manifest, name):
    for e in (prev_manifest or {}).get('files', []):
        if e.get('name') == name:
            return e
    return None


def write(path, files, generated=None):
    doc = dict(schema=SCHEMA, generated=generated or datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
               files=sorted(files, key=lambda e: e['name']))
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write('\n')
    return doc


def validate(doc, root=None):
    """回傳問題清單（空＝通過）。root 有給就核對檔案雜湊與大小。App 端 DataManifest.parse 同規則。"""
    problems = []
    if doc.get('schema') != SCHEMA:
        problems.append('schema 應為 %d' % SCHEMA)
    names = set()
    for e in doc.get('files', []):
        n = e.get('name', '')
        if not n or n in names:
            problems.append('名稱重複或空白：%r' % n)
        names.add(n)
        p = e.get('path', '')
        if not p or p.startswith('/') or '\\' in p or '://' in p or any(s in ('', '.', '..') for s in p.split('/')):
            problems.append('%s 路徑不安全：%s' % (n, p))
        if not parse_version(e.get('version')) or '.' not in str(e.get('version')):
            problems.append('%s 版本不是 yyyymmdd.rev：%s' % (n, e.get('version')))
        if not re.fullmatch(r'[0-9a-f]{64}', str(e.get('sha256', ''))):
            problems.append('%s sha256 格式錯' % n)
        if not isinstance(e.get('bytes'), int) or e['bytes'] <= 0:
            problems.append('%s bytes 錯' % n)
        for k in ('count', 'minAppVersion', 'license', 'attribution', 'sources', 'date'):
            if k not in e:
                problems.append('%s 缺欄位 %s' % (n, k))
        if root and not problems:
            full = os.path.join(root, p)
            if not os.path.exists(full):
                problems.append('%s 檔案不存在：%s' % (n, p))
            elif os.path.getsize(full) != e['bytes'] or sha256_file(full) != e['sha256']:
                problems.append('%s 雜湊或大小與檔案不符' % n)
    return problems
