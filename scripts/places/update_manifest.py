#!/usr/bin/env python
"""只改 offline-manifest.json 的店家三項（places、places_overture、places_gov）；其他項（tiles、號誌、junctions…）原樣保留。
用法：
  python update_manifest.py set <manifest> <資料夾(三個 sqlite)> <tag offline-YYYYMMDD> <version YYYYMMDD> <owner/repo>
  python update_manifest.py restore <manifest> <舊 manifest.json>   只把店家三項還原成舊檔的內容
  python update_manifest.py check <manifest> <資料夾>               核對 manifest 三項 sha256／bytes 與資料夾內檔案一致（不一致回傳 1）
整體 version 取「原整體版本」與「新版本」較大者（不倒退）。"""
import datetime, hashlib, json, os, sys

KINDS = {'places': 'touror_places.sqlite', 'places_overture': 'touror_places_overture.sqlite', 'places_gov': 'touror_places_gov.sqlite'}


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''): h.update(b)
    return h.hexdigest()


def save(mp, m):
    m['generated'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with open(mp, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(m, f, ensure_ascii=False, indent=1); f.write('\n')


def entry(m, kind):
    return next(x for x in m['files'] if x['kind'] == kind)


def main():
    mode, mp = sys.argv[1], sys.argv[2]
    m = json.load(open(mp, encoding='utf-8'))
    if mode == 'set':
        d, tag, ver, repo = sys.argv[3:7]
        out = {}
        for k, fn in KINDS.items():
            p = os.path.join(d, fn)
            entry(m, k).update({'version': ver, 'bytes': os.path.getsize(p), 'sha256': sha(p),
                                'url': f'https://github.com/{repo}/releases/download/{tag}/{fn}'})
            out[k] = entry(m, k)['sha256']
        m['version'] = max(m['version'], ver)
        save(mp, m); print(json.dumps({'version': ver, 'overall': m['version'], 'sha256': out}))
    elif mode == 'restore':
        old = json.load(open(sys.argv[3], encoding='utf-8'))
        for k in KINDS:
            e = entry(m, k); e.clear(); e.update(entry(old, k))
        save(mp, m); print('restored', list(KINDS))
    elif mode == 'check':
        d = sys.argv[3]; bad = []
        for k, fn in KINDS.items():
            e, p = entry(m, k), os.path.join(d, fn)
            if e['sha256'] != sha(p) or e['bytes'] != os.path.getsize(p): bad.append(k)
        print('mismatch' if bad else 'ok', bad); sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
