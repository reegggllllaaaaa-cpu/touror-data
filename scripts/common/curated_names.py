# -*- coding: utf-8 -*-
"""curated/路名覆蓋.csv 的讀取與套用（單一真實來源）。

路網（roadnet/apply_curated.py）、播報錨點庫（anchors/build_anchors.py）、地名／店家庫（places/build_places_v2.py、
places/extract_osm_road_names.py）都從這裡讀，一處修正全部生效。
欄位：way_id、name（正確路名）、原名放入（old_name／alt_name）、採用（是＝套用）、來源。
"""
import csv
import os

DEFAULT_CSV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                           'curated', '路名覆蓋.csv')


def load_names(csvp=DEFAULT_CSV):
    """{way_id: (新路名, 原名放入的標籤, 來源)}；檔案不存在回空 dict。"""
    out = {}
    if csvp and os.path.exists(csvp):
        with open(csvp, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["採用"].strip() == "是" and r["name"].strip():
                    keep = r.get("原名放入", "").strip() or "old_name"
                    if keep not in ("old_name", "alt_name"): keep = "old_name"
                    out[int(r["way_id"])] = (r["name"].strip(), keep, r.get("來源", "").strip() or "curated")
    return out


def name_fix(t, ov):
    """t：標籤 dict（會被改並回傳）；ov：load_names 的值。原名移到 old_name／alt_name，標 touror:name_override。"""
    new, keep, src = ov
    old = t.get("name")
    if old and old != new:
        t[keep] = old if not t.get(keep) else (t[keep] if old in t[keep].split(";") else t[keep] + ";" + old)
    t["name"] = new; t["touror:name_override"] = src
    return t


def way_tags(way_id, tags, ovs):
    """osmium 標籤 → 套用覆蓋後的 dict（沒有覆蓋就原樣轉 dict）。"""
    t = {k: v for k, v in tags} if not isinstance(tags, dict) else dict(tags)
    ov = ovs.get(way_id) if ovs else None
    return name_fix(t, ov) if ov else t
