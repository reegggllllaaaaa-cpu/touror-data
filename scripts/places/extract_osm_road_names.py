# -*- coding: utf-8 -*-
"""從 OSM pbf 抽出「有中文名的道路」與其英文名，供 build_place_addr.py 把英文地址轉中文路名（空間配對用）。

輸出 pickle：{'roads': [(name, (英文名們...))], 'pts': [(lat, lon, road_idx), ...]}；道路頂點另每 25 m 內插一點。
英文名取 name:en、old_name:en、alt_name:en、official_name:en、name:zh-Latn-pinyin、name:zh_pinyin。
用法（系統 python，需 osmium）：python -I extract_osm_road_names.py taiwan-latest.osm.pbf osm_road_names.pkl
"""
import math, os, pickle, re, sys
import osmium
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.curated_names import load_names, way_tags  # noqa: E402  curated 路名覆蓋

EN_KEYS = ('name:en', 'old_name:en', 'alt_name:en', 'official_name:en', 'short_name:en',
           'name:zh-Latn-pinyin', 'name:zh_pinyin', 'name:zh-Latn-tongyong', 'name:zh-Latn-wadegiles')
CJK = re.compile(r'[一-鿿]')


class H(osmium.SimpleHandler):
    def __init__(self, name_ov=None):
        super().__init__()
        self.name_ov = load_names() if name_ov is None else name_ov
        self.roads, self.rid, self.pts = [], {}, []

    def way(self, w):
        if 'highway' not in w.tags:
            return
        t = way_tags(w.id, w.tags, self.name_ov) if w.id in self.name_ov else w.tags
        nm = t.get('name')
        if not nm or not CJK.search(nm):
            return
        ens = tuple(sorted({t.get(k) for k in EN_KEYS if t.get(k)}))
        key = (nm, ens)
        i = self.rid.get(key)
        if i is None:
            i = self.rid[key] = len(self.roads); self.roads.append(key)
        prev = None
        for n in w.nodes:
            try:
                la, lo = n.location.lat, n.location.lon
            except Exception:
                prev = None; continue
            if prev:
                d = math.hypot((la - prev[0]) * 111000, (lo - prev[1]) * 101900)
                k = int(d // 25)
                for s in range(1, k + 1):
                    f = s / (k + 1)
                    self.pts.append((prev[0] + (la - prev[0]) * f, prev[1] + (lo - prev[1]) * f, i))
            self.pts.append((la, lo, i)); prev = (la, lo)


if __name__ == '__main__':
    h = H()
    h.apply_file(sys.argv[1], locations=True, idx='flex_mem')
    pickle.dump({'roads': h.roads, 'pts': h.pts}, open(sys.argv[2], 'wb'), protocol=4)
    print('roads', len(h.roads), 'with_en', sum(1 for r in h.roads if r[1]), 'pts', len(h.pts))
