"""把 add_gov_gaps.py 補入的合成 ID（node 9e12 起、way 9.1e12 起）改成接在真實 OSM ID 後面的小 ID，
planetiler 的節點索引（依最大 ID 配置）才不會爆掉。只給底圖用，路網照用原檔。
用法：python -I renumber_synthetic.py <in.pbf> <out.pbf>
"""
import sys
import osmium

NODE_SYN, WAY_SYN = 9_000_000_000_000, 9_100_000_000_000
NODE_BASE, WAY_BASE = 20_000_000_000, 2_000_000_000  # OSM 目前 node 約 1.3e10、way 約 1.5e9


def nmap(i):
    return NODE_BASE + (i - NODE_SYN) if i >= NODE_SYN else i


def wmap(i):
    return WAY_BASE + (i - WAY_SYN) if i >= WAY_SYN else i


class H(osmium.SimpleHandler):
    def __init__(self, w):
        super().__init__()
        self.w, self.n = w, [0, 0]

    def node(self, n):
        if n.id >= NODE_SYN:
            self.n[0] += 1
            self.w.add_node(n.replace(id=nmap(n.id)))
        else:
            self.w.add_node(n)

    def way(self, w):
        if w.id >= WAY_SYN or any(r.ref >= NODE_SYN for r in w.nodes):
            self.n[1] += 1
            self.w.add_way(w.replace(id=wmap(w.id), nodes=[nmap(r.ref) for r in w.nodes]))
        else:
            self.w.add_way(w)

    def relation(self, r):
        self.w.add_relation(r)


src, dst = sys.argv[1:3]
wr = osmium.SimpleWriter(dst, overwrite=True)
h = H(wr)
h.apply_file(src)
wr.close()
assert NODE_BASE > 1.5e10 and WAY_BASE > 1.6e9
print(f"合成節點 {h.n[0]}、合成或含合成節點的 way {h.n[1]} 已改號")
