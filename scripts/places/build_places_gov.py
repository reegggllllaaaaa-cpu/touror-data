"""政府分庫試作：中油、郵局、觀光景點及旅宿。執行 python build_places_gov.py。

首次下載原始檔，之後使用 gov_raw 快照重建；不會自動更新快照。
公廁需金鑰未取得；警察資料尚未換算。
"""
import collections
import csv
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import urllib.request
import zipfile

from build_places import norm_name, grams

ROOT = Path(os.environ.get("TOUROR_PLACES_WORK") or Path(__file__).resolve().parent)  # CI：工作資料夾（內含兩庫、gov_raw、真值）
RAW = ROOT / "gov_raw"
SOURCES = {
    "中油": ("cpc.json", "https://vipmbr.cpc.com.tw/openData/getStationInfo",
           "https://data.gov.tw/dataset/6065"),
    "郵局": ("post.csv", "https://www.post.gov.tw/post/internet/Templates/getOpenDataFile.jsp?vkey=B484EB27-B3F0-4D02-8F62-047D20C64078",
           "https://data.gov.tw/dataset/5950"),
}
TOURISM = {
    "景點": ("Attraction", "Attractions", "https://data.gov.tw/dataset/7777"),
    "旅宿": ("Hotel", "Hotels", "https://data.gov.tw/dataset/7780"),
}


def load():
    RAW.mkdir(exist_ok=True)
    out = []
    for category, (filename, url, _) in SOURCES.items():
        path = RAW / filename
        if not path.exists():
            with urllib.request.urlopen(url, timeout=90) as response:
                path.write_bytes(response.read())
        if category == "中油":
            records = json.loads(path.read_text(encoding="utf-8-sig"))
        else:
            with path.open(encoding="utf-8-sig", newline="") as f:
                records = list(csv.DictReader(f))
        for d in records:
            if category == "中油":
                if str(d.get("營業中")) != "1" or d.get("類別") == "漁船站":
                    continue
                name = "中油 " + str(d.get("站名") or "")
                kind, weight = "fuel", 400
                sub = str(d.get("縣市") or "") + str(d.get("鄉鎮區") or "")
                phone, hours = d.get("電話"), d.get("營業時間")
            else:
                name = d.get("局名", "")
                kind, weight = "service", 350
                sub = str(d.get("縣市") or "") + str(d.get("鄉鎮市區") or "")
                phone, hours = d.get("郵務電話"), d.get("營業時間備註")
            try:
                lat, lon = float(d["緯度"]), float(d["經度"])
            except (KeyError, ValueError, TypeError):
                continue
            name = norm_name(name)
            if not name or not grams(name) or not (21 <= lat <= 27 and 118 <= lon <= 123):
                continue
            out.append((name, kind, norm_name(sub), round(lat, 6), round(lon, 6),
                        weight, str(d.get("地址") or ""), str(phone or ""),
                        str(hours or ""), category))
    for category, (prefix, key, _) in TOURISM.items():
        path = ROOT / "真值" / f"{prefix}-json.zip"
        with zipfile.ZipFile(path) as z:
            records = json.loads(z.read(f"{prefix}List.json"))[key]
        seen = collections.defaultdict(list)
        for d in records:
            name = d.get(prefix + "Name") or ""
            try:
                lat, lon = float(d["PositionLat"]), float(d["PositionLon"])
            except (KeyError, ValueError, TypeError):
                continue
            if not name.strip() or not grams(name) or not (21 <= lat <= 27 and 118 <= lon <= 123):
                continue
            # Exact official name, within 50 m, including adjacent grid cells.
            x, y = math.floor(lat * 1000), math.floor(lon * 1000)
            nearby = (p for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                      for p in seen[(name, x + dx, y + dy)])
            if any(((lat-a)*111195)**2 + ((lon-b)*111195*math.cos(math.radians(lat)))**2 <= 50**2
                   for a, b in nearby):
                continue
            seen[(name, x, y)].append((lat, lon))
            addr = d.get("PostalAddress") or {}
            sub = (addr.get("City") or "") + (addr.get("Town") or "")
            address = sub + (addr.get("StreetAddress") or "")
            phones = d.get("Telephones") or []
            phone = phones[0].get("Tel", "") if phones else ""
            out.append((name, category, sub, round(lat, 6), round(lon, 6), 300,
                        address, phone or "", d.get("ServiceTimeInfo") or "", category))
    return sorted(out, key=lambda r: (-r[5], r[0], r[3], r[4], r[9], r[6]))


def similar(a, b):
    a = re.sub(r"[\W_]+", "", norm_name(a).lower())
    b = re.sub(r"[\W_]+", "", norm_name(b).lower())
    return bool(a and b) and (a == b or difflib.SequenceMatcher(None, a, b).ratio() >= .7)


def compare(rows, file):
    grid = collections.defaultdict(list)
    con = sqlite3.connect(f"file:{file.as_posix()}?mode=ro", uri=True)
    for name, lat, lon in con.execute("SELECT name,lat,lon FROM place"):
        grid[(math.floor(lat * 500), math.floor(lon * 500))].append((name, lat, lon))
    con.close()
    matches = []
    for name, _, _, lat, lon, *_ in rows:
        x, y = math.floor(lat * 500), math.floor(lon * 500)
        hit = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other, la, lo in grid.get((x + dx, y + dy), ()):
                    north = (la - lat) * 111195
                    east = (lo - lon) * 111195 * math.cos(math.radians((la + lat) / 2))
                    if north*north + east*east <= 150**2 and name == other:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
        matches.append(hit)
    return matches


def main():
    rows = load()
    if not rows:
        raise RuntimeError("無有效政府資料，不建立空庫")
    osm = compare(rows, ROOT / "touror_places.sqlite")
    overture = compare(rows, ROOT / "touror_places_overture.sqlite")
    target = ROOT / "touror_places_gov.sqlite"
    tmp = ROOT / "touror_places_gov.sqlite.tmp"
    if tmp.exists():
        raise RuntimeError(f"暫存檔已存在，請先確認處理：{tmp}")
    con = sqlite3.connect(tmp)
    con.executescript("""CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, sub TEXT, lat REAL NOT NULL, lon REAL NOT NULL, weight INTEGER NOT NULL);
CREATE VIRTUAL TABLE search USING fts5(grams, tokenize='unicode61');
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE detail(place_id INTEGER PRIMARY KEY, address TEXT, phone TEXT, hours TEXT, source TEXT);
PRAGMA user_version=1;""")
    for i, r in enumerate(rows, 1):
        con.execute("INSERT INTO place VALUES(?,?,?,?,?,?,?)", (i, *r[:6]))
        con.execute("INSERT INTO search(rowid,grams) VALUES(?,?)", (i, grams(r[0])))
        source = (SOURCES if r[9] in SOURCES else TOURISM)[r[9]][2]
        con.execute("INSERT INTO detail VALUES(?,?,?,?,?)", (i, *r[6:9], source))
    meta = {"version": "1", "built_at": "快照重建", "source": "政府開放資料（未完成試作）",
            "attribution": "台灣中油股份有限公司、中華郵政股份有限公司、交通部觀光署；政府資料開放授權條款第1版 https://data.gov.tw/license",
            "row_count": str(len(rows))}
    con.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    con.execute("INSERT INTO search(search) VALUES('optimize')")
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, target)
    lines = ["# 政府分庫報告（試作）", "",
             "快照不自動更新；重跑：`python build_places_gov.py`。", "",
             "|類別|入庫|OSM 覆蓋|Overture 覆蓋|兩者均未匹配|",
             "|---|---:|---:|---:|---:|"]
    for cat in (*SOURCES, *TOURISM):
        ix = [i for i, r in enumerate(rows) if r[9] == cat]
        n = len(ix)
        a = sum(osm[i] for i in ix)
        b = sum(overture[i] for i in ix)
        neither = sum(not osm[i] and not overture[i] for i in ix)
        lines.append(f"|{cat}|{n}|{a}（{a/n:.1%}）|{b}（{b/n:.1%}）|{neither}|")
    lines += ["", "重複判定：名稱完全相同且 150 公尺內，未限制類別；僅量測、不刪除。", "",
              "公廁（30794）未取得：官方端點需金鑰；未使用金鑰。警察、健保診所、國小未完成。", "",
              "授權：政府資料開放授權條款第1版，須顯名台灣中油、中華郵政及交通部觀光署。觀光資料標準 V2.1 指定 WGS84；保留官方名稱、排除空座標及範圍外，同名 50 公尺內去重。", "",
              "觀光資料下載 2026-11-24，來源更新 2026-11-23；原有快照下載日期未記錄："]
    for cat, (filename, url, page) in SOURCES.items():
        digest = hashlib.sha256((RAW / filename).read_bytes()).hexdigest()
        lines.append(f"- {cat}：{page}；{url}；SHA-256 `{digest}`")
    for cat, (prefix, key, page) in TOURISM.items():
        path = ROOT / "真值" / f"{prefix}-json.zip"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        url = f"https://media.taiwan.net.tw/XMLReleaseAll_public/v2.0/Zh_tw/{prefix}-json.zip"
        lines.append(f"- {cat}：{page}；{url}；SHA-256 `{digest}`")
    lines.append("\n抽樣縣市核對：尚未完成，不能視為驗證通過。")
    (ROOT / "政府分庫報告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:9]))


if __name__ == "__main__":
    main()
