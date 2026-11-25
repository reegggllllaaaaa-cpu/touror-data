"""統計新底圖 lanes 圖層 lanes_source=mapillary_ai，並畫一張高雄路口預覽 PNG。用法：python preview_mly.py <pmtiles> <out.png> <lat> <lon>"""
import sys, gzip, math, collections
sys.path.insert(0, __import__("os").path.join(__import__("os").path.dirname(__import__("os").path.abspath(__file__)), "..", "basemap"))
from verify_basemap import Reader, MmapSource, mvt, lonlat_to_tile
from PIL import Image, ImageDraw
pm, out, lat, lon = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
r = Reader(MmapSource(open(pm, "rb")))
fx, fy = lonlat_to_tile(lat, lon, 14); tx, ty = int(fx), int(fy)
S = 512; img = Image.new("RGB", (S * 3, S * 3), (245, 245, 240)); d = ImageDraw.Draw(img)
cnt = collections.Counter(); col = {"osm": (40, 90, 200), "gov": (30, 150, 60), "mapillary_ai": (230, 120, 0), "inferred": (190, 190, 190)}
labels = []
for dx in (-1, 0, 1):
    for dy in (-1, 0, 1):
        b = r.get(14, tx + dx, ty + dy)
        if not b: continue
        if b[:2] == b"\x1f\x8b": b = gzip.decompress(b)
        t = mvt.decode(b); ext = 4096
        for lay in ("lanes", "lane_lines"):
            for ft in t.get(lay, {}).get("features", []):
                p = ft["properties"]; src = p.get("lanes_source", "?"); cnt[(lay, src)] += 1
                g = ft["geometry"]; parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"] if g["type"] == "MultiLineString" else []
                for part in parts:
                    pts = [((dx + 1) * S + x * S / ext, (dy + 1) * S + (ext - y) * S / ext) for x, y in part]
                    if len(pts) < 2: continue
                    if lay == "lanes":
                        d.line(pts, fill=col.get(src, (0, 0, 0)), width=7 if src == "mapillary_ai" else 3)
                        if src == "mapillary_ai": labels.append((pts[len(pts) // 2], f"AI lanes={p.get('lanes')} f={p.get('lanes_forward')} b={p.get('lanes_backward')}"))
                    else:
                        d.line(pts, fill=(120, 120, 120) if src != "mapillary_ai" else (255, 170, 60), width=1)
for (x, y), s in labels: d.text((x + 4, y + 4), s, fill=(160, 60, 0))
cx, cy = (fx - tx + 1) * S, (fy - ty + 1) * S; d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), outline=(255, 0, 0), width=2)
y0 = 6
for k, c in col.items(): d.rectangle((6, y0, 26, y0 + 10), fill=c); d.text((32, y0), "lanes_source=" + k, fill=(0, 0, 0)); y0 += 16
img.save(out); print("tile", tx, ty, dict(cnt))
