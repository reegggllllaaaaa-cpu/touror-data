#!/usr/bin/env python
"""只改 offline-manifest.json 的 tiles 項（version、bytes、sha256、url）與整體 version；其他項原樣保留。
用法：python -I update_manifest.py <manifest> <tiles.tar> <tag offline-YYYYMMDD> <version YYYYMMDD> <owner/repo>
整體 version 取「原整體版本」與「新版本」較大者（不倒退）。"""
import hashlib, json, os, sys, datetime
mp, tar, tag, ver, repo = sys.argv[1:6]
m = json.load(open(mp, encoding="utf-8"))
h = hashlib.sha256()
with open(tar, "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
t = next(x for x in m["files"] if x["kind"] == "tiles")
t.update({"version": ver, "bytes": os.path.getsize(tar), "sha256": h.hexdigest(),
          "url": f"https://github.com/{repo}/releases/download/{tag}/valhalla_tiles.tar"})
m["version"] = max(m["version"], ver)
m["generated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
with open(mp, "w", encoding="utf-8", newline="\n") as f:
    json.dump(m, f, ensure_ascii=False, indent=1); f.write("\n")
print(json.dumps({"bytes": t["bytes"], "sha256": t["sha256"], "version": ver, "overall": m["version"]}))
