"""把測速照相正本（資料/測速/speedcams.json，NAV-DATA-1 產出）精簡成 App 內資源 App/Resources/speedcams.json（NAV-CAM-1）。
用法（在 ios/ 目錄下）：python tools/pack_speedcams.py          產生／覆寫 App 內副本
                      python tools/pack_speedcams.py --check  只比對，不一致就結束碼 1（local_check 也會比對）
可重複執行：同一份正本一定產生位元組完全相同的輸出（排序固定、不含建置時間）。只用標準庫。

輸出格式（UTF-8、無 BOM、無縮排）：
  {"version": 正本版本, "license": 授權標示, "count": 筆數,
   "cameras": [{"i": id, "la": 緯度, "lo": 經度, "t": "f|r|rs|s|u", "s": 速限 km/h（未知省略）,
                "b": 被拍車輛行進方位角（單向才有，雙向／未知省略）, "a": 設置地點（最多 24 字）,
                "len": 區間長度公尺（只有區間點、有長度才有；lenSrc=est／unverified 時是估計值）,
                "lenSrc": "text|official|est|unverified"（只有區間點）,
                "st": "sus"（官方註記維護中，App 預設不提醒；使用中省略）}, ...]}
  version：正本 version，正本 meta 有 revision ≥ 2 時接「.revision」（例 20261119.3）
  t：f＝固定測速 fixed、r＝闖紅燈 redlight、rs＝闖紅燈兼超速 redlight_speed（App 視為闖紅燈且有速限）、
     s＝區間測速起點 section、u＝類型不明 unknown
  正本 status＝retired（官方已汰除）、merged（重複點，已併入另一筆）的不打包（NAV-CAM-DATA-2）。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent          # ios/
SOURCE = ROOT.parent / "資料" / "測速" / "speedcams.json"
META = ROOT.parent / "資料" / "測速" / "meta.json"
TARGET = ROOT / "App" / "Resources" / "speedcams.json"
MAX_BYTES = 400 * 1024
ADDRESS_MAX = 24
TYPE_CODE = {"fixed": "f", "redlight": "r", "redlight_speed": "rs", "section": "s", "unknown": "u"}
# 打包進 App 的狀態：使用中與維護中（維護中帶 "st":"sus"，App 預設不提醒）；汰除與重複點留在正本追蹤
PACKED_STATUS = ("active", "suspended")
LEN_SRCS = ("text", "official", "est", "unverified")
# 設置地點或拍攝方向原文寫了這些字＝雙向，不帶方位角（不過濾對向）。
# 原因：建置.py 判方向時先拿掉括號註記，「南向北（雙向）」「大義路與學林路口（雙向）」會被判成單向；
# 這些點 150 公尺內都沒有反方向的另一筆（不是「每個方向各一筆」），所以「雙向」才是可信的資訊。
BOTH_WORDS = ("雙向", "相向")


def is_both_ways(c):
    text = str(c.get("address") or "") + str(c.get("direct_raw") or "")
    return any(w in text for w in BOTH_WORDS)


def direction_stats(cameras):
    """方向分類統計：單向（帶方位角）、雙向、未知，以及「原本單向但文字寫雙向」被改成雙向的筆數與 id"""
    stats = {"one": 0, "both": 0, "unknown": 0, "both_conflict_fixed": []}
    for c in cameras:
        if c.get("direction") == "one" and c.get("bearing") is not None:
            if is_both_ways(c):
                stats["both_conflict_fixed"].append(c["id"])
                stats["both"] += 1
            else:
                stats["one"] += 1
        elif c.get("direction") == "both":
            stats["both"] += 1
        else:
            stats["unknown"] += 1
    return stats


def live(cameras):
    """要打包進 App 的點（status 沒寫的舊正本當使用中）"""
    return [c for c in cameras if c.get("status", "active") in PACKED_STATUS]


def pack(cameras, meta):
    out = []
    for c in sorted(live(cameras), key=lambda c: c["id"]):
        item = {"i": c["id"], "la": round(float(c["lat"]), 6), "lo": round(float(c["lon"]), 6),
                "t": TYPE_CODE[c["type"]]}
        if c.get("limit_kmh"):
            item["s"] = int(c["limit_kmh"])
        # 方向：只有單向（direction=one）且有方位角、文字也沒寫雙向才留；雙向、未知不過濾對向
        if c.get("direction") == "one" and c.get("bearing") is not None and not is_both_ways(c):
            item["b"] = int(round(float(c["bearing"]))) % 360
        address = " ".join(str(c.get("address") or "").split())
        if len(address) > ADDRESS_MAX:
            address = address[:ADDRESS_MAX - 1] + "…"
        if address:
            item["a"] = address
        # 區間長度（NAV-CAM-HUD-1、NAV-CAM-DATA-2）：App 算區間平均速度與建議時速用。
        # text／official＝已知長度；est／unverified＝估計（有 len 用它估計，沒有 App 以 1.0 km 估計）
        if c["type"] == "section":
            src = c.get("len_src") if c.get("len_src") in LEN_SRCS else ("text" if c.get("len_m") else "est")
            if c.get("len_m"):
                item["len"] = int(round(float(c["len_m"])))
            item["lenSrc"] = src
        if c.get("status") == "suspended":
            item["st"] = "sus"
        out.append(item)
    version = str(meta.get("version", ""))
    if int(meta.get("revision") or 1) >= 2:
        version += ".%d" % int(meta["revision"])
    doc = {"version": version, "license": meta.get("license", ""), "count": len(out),
           "cameras": out}
    return (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def load_source():
    return json.loads(SOURCE.read_text(encoding="utf-8-sig"))


def build():
    cameras = load_source()
    meta = json.loads(META.read_text(encoding="utf-8-sig")) if META.is_file() else {}
    return pack(cameras, meta)


def validate(data, cameras):
    """打包結果驗證：回傳錯誤訊息清單（空＝通過）"""
    errors = []
    doc = json.loads(data)
    by_id = {c["id"]: c for c in cameras}
    for item in doc["cameras"]:
        src = by_id.get(item["i"])
        if "b" in item and (src is None or is_both_ways(src)):
            errors.append(f"{item['i']} 文字寫雙向卻帶方位角")
    if doc["count"] != len(live(cameras)):
        errors.append(f"筆數 {doc['count']} 與正本使用中 {len(live(cameras))} 不同")
    for item in doc["cameras"]:
        src = by_id.get(item["i"]) or {}
        if item["t"] == "rs" and "s" not in item:
            errors.append(f"{item['i']} 闖紅燈兼超速沒有速限")
        if src.get("len_src") in ("text", "official") and "len" not in item:
            errors.append(f"{item['i']} 已知長度卻沒有 len")
    return errors


def main(argv):
    if not SOURCE.is_file():
        print(f"SKIP 找不到正本 {SOURCE}")
        return 0
    cameras = load_source()
    data = build()
    stats = direction_stats(live(cameras))
    print(f"方向：單向 {stats['one']}、雙向 {stats['both']}（其中原判單向但文字寫雙向而改正 "
          f"{len(stats['both_conflict_fixed'])} 筆）、未知 {stats['unknown']}")
    errors = validate(data, cameras)
    if errors:
        print("FAIL 打包驗證：" + "；".join(errors[:10]))
        return 1
    if len(data) > MAX_BYTES:
        print(f"FAIL 輸出 {len(data)} bytes，超過 {MAX_BYTES}")
        return 1
    if "--check" in argv:
        same = TARGET.is_file() and TARGET.read_bytes() == data
        print(("OK   " if same else "FAIL ") + "App/Resources/speedcams.json 與正本打包結果一致")
        return 0 if same else 1
    TARGET.write_bytes(data)
    doc = json.loads(data)
    from collections import Counter
    types = Counter(c["t"] for c in doc["cameras"])
    print(f"寫入 {TARGET}（{len(data)} bytes，{doc['count']} 筆，版本 {doc['version']}；類型 {dict(sorted(types.items()))}；"
          f"維護中 {sum(1 for c in doc['cameras'] if c.get('st') == 'sus')}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
