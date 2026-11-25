import json,sys
d=json.load(open(sys.argv[1],encoding="utf-8"))["stats"]
S=("osm","gov","mly","inferred")
def agg(cs, hws=None):
    t={s:0.0 for s in S}
    for c in cs:
        for hw,v in d[c].items():
            if hws and hw not in hws: continue
            for s in S: t[s]+=v.get(s,[0,0])[1]
    return t
def row(name,t):
    T=sum(t.values()) or 1
    return f"| {name} | {T/1000:,.0f} | {t['osm']/T*100:.1f}% | {t['gov']/T*100:.1f}% | {t['mly']/T*100:.2f}% | {t['inferred']/T*100:.1f}% |"
cs=sorted(d, key=lambda c:-sum(sum(v.get(s,[0,0])[1] for s in S) for v in d[c].values()))
print("| 範圍 | 主要道路 km | OSM lanes | 政府資料 | Mapillary AI | 只能推定 |\n|---|---:|---:|---:|---:|---:|")
print(row("**全台**",agg(cs)))
for hw in ("trunk","primary","secondary","tertiary"): print(row(f"全台 {hw}",agg(cs,{hw})))
print()
print("| 縣市 | 主要道路 km | OSM lanes | 政府資料 | Mapillary AI | 只能推定 |\n|---|---:|---:|---:|---:|---:|")
for c in cs: print(row(c,agg([c])))
