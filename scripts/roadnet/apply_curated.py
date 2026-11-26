#!/usr/bin/env python
"""套用 curated/way覆蓋.csv（採用=是）到 OSM PBF，輸出新 PBF；不改原檔。
用法：python -I apply_curated.py <in.pbf> <out.pbf> [way覆蓋.csv] [類型覆蓋.csv]
車道覆蓋.csv（同資料夾，沒有就略過）：依 way_id 改 lanes（turn_lanes 有值且段數相符才改 turn:lanes），原值存 touror:orig_lanes，標 touror:lanes_override=<來源>；路網與底圖吃同一份 final.pbf。
路名覆蓋.csv（同資料夾，沒有就略過）：依 way_id 改 name，原名放進「原名放入」欄指定的標籤（old_name／alt_name，已有值則以 ; 串接），
  標 touror:name_override=<來源>；tiles（播報路名）與底圖標籤都吃 final.pbf，故一處修正兩邊生效。
類型覆蓋.csv（預設取 way覆蓋.csv 同資料夾的 類型覆蓋.csv，沒有就略過）：依條件（ref＋原 highway）把整類 way 改 highway，
保留其他標籤並加 touror:override；例如國3甲（ref=3甲、highway=motorway）改 trunk，使黃紅牌的 exclude_highways 不再排除它。輸出被改 way 清單（id、長度）。
動作：解除private＝移除 access 並標 touror:override=rode；捷西路升級＝highway=tertiary、maxspeed=50、touror:override=jiexi。
選用 --moto-bans <禁行機車.csv> --moto-out <資料夾>：政府禁行機車路段（osm_way_id 對位，來源 資料/車道/moto_restrictions_for_routing.csv）。
  牌照處理照規則表慣例（tiles 只有一套，四種牌照共用）：
  ・四牌皆禁（applies_yellow_red=yes）且方向與單行一致 → 寫 motorcycle=no 進 tiles（硬避開）。
  ・只禁綠白（applies_yellow_red=no…，禁3「大型重型機車以外之機車」）→ 不能寫進 tiles（會連黃紅一起擋），
    改標 touror:moto_ban（Valhalla 不讀）並輸出 禁行機車走廊.json（rules_with_plate 格式＋corridor），由 App 依牌照事後排除重算。
  每列都寫進 禁行機車對應清單.csv（成功／對不到＋原因），不默默丟掉；統計寫 禁行機車統計.json。"""
import csv, sys, time, os, math, re, json, osmium

RESTRICT = {"private", "no", "permit", "customers", "destination", "delivery"}

def load(csvp):
    acts = {}
    with open(csvp, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["採用"].strip() == "是":
                acts[int(r["way_id"])] = r["動作"]
    return acts

def load_type_rules(csvp):
    rules = []
    if csvp and os.path.exists(csvp):
        with open(csvp, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["採用"].strip() == "是":
                    rules.append((r["條件_ref"].strip(), r["條件_highway"].strip(), r["改成_highway"].strip(), r["override標記"].strip(), r["id"].strip()))
    return rules

def load_lanes(csvp):
    out = {}
    if csvp and os.path.exists(csvp):
        with open(csvp, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r["採用"].strip() == "是" and r["lanes"].strip().isdigit():
                    out[int(r["way_id"])] = (r["lanes"].strip(), r.get("turn_lanes", "").strip(), r.get("來源", "").strip() or "curated")
    return out

def load_names(csvp):
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
    new, keep, src = ov
    old = t.get("name")
    if old and old != new:
        t[keep] = old if not t.get(keep) else (t[keep] if old in t[keep].split(";") else t[keep] + ";" + old)
    t["name"] = new; t["touror:name_override"] = src
    return t

def lanes_fix(tags, ov):
    t = {k: v for k, v in tags}
    n, tl, src = ov
    if "lanes" in t: t["touror:orig_lanes"] = t["lanes"]
    t["lanes"] = n
    if tl and len(tl.split("|")) == int(n): t["turn:lanes"] = tl
    t["touror:lanes_override"] = src
    return t

def match_type(rules, tags):
    for ref, hw, new, mark, rid in rules:
        if tags.get("highway") == hw and ref in [x.strip() for x in tags.get("ref", "").split(";")]:
            return new, mark, rid
    return None

ONEWAY_FWD = {"yes", "1", "true"}
PLATE_ALL = {"green": "prohibited", "white": "prohibited", "yellow": "prohibited", "red": "prohibited"}
PLATE_WG = {"green": "prohibited", "white": "prohibited", "yellow": "allowed", "red": "allowed"}

def load_bans(p):
    with open(p, encoding="utf-8-sig") as f:
        return [dict(r, _line=i) for i, r in enumerate(csv.DictReader(f), 2)]

def road_key(source_text):
    """'內湖區 堤頂大道1段 港墘路-南京東路 速限70' -> '堤頂大道'"""
    parts = source_text.split()
    tok = parts[1] if len(parts) > 1 else source_text
    return re.sub(r"([0-9一二三四五六七八九十]+段)$", "", tok)

def judge_ban(r, tags):
    """回傳 (狀態, 原因, 牌照map或None, tiles處理)；tags=None 表示 OSM 沒有此 way。"""
    if r.get("採用", "是").strip() != "是": return "未採用", "採用≠是", None, "無"
    if r.get("restriction", "").strip() != "motorcycle=no": return "對不到", "不支援的限制 " + str(r.get("restriction")), None, "無"
    if tags is None: return "對不到", "OSM 已無此 way（可能被刪除或改號，需重新對位）", None, "無"
    if "highway" not in tags: return "對不到", "此 way 不是道路（無 highway）", None, "無"
    key = road_key(r.get("source_text", ""))
    names = " ".join(tags.get(k, "") for k in ("name", "alt_name", "old_name", "official_name", "name:zh"))
    if key and key not in names: return "對不到", "路名不符：公告 %s，OSM name=%s" % (key, tags.get("name", "（無）")), None, "無"
    d, ow = r.get("direction", "").strip(), tags.get("oneway", "no")
    if (d == "backward" and ow in ONEWAY_FWD) or (d == "forward" and ow == "-1"):
        return "成功", "OSM 為反向單行，該方向本就不可通行，不需另外處理", None, "無"
    yr, wg = r.get("applies_yellow_red", ""), r.get("applies_white_green", "")
    if yr.startswith("yes"):
        if (d == "forward" and ow in ONEWAY_FWD) or (d == "backward" and ow == "-1") or d == "both":
            return "成功", "", PLATE_ALL, "motorcycle=no"
        return "成功", "四牌皆禁但為雙向道單方向，Valhalla 方向性機車標籤未驗證，改走走廊排除", PLATE_ALL, "走廊"
    if wg.startswith("yes"): return "成功", "", PLATE_WG, "走廊"
    return "對不到", "牌照欄位無法判讀 white_green=%s yellow_red=%s" % (wg, yr), None, "無"

def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2-la1)/2)**2 + math.cos(la1)*math.cos(la2)*math.sin((lo2-lo1)/2)**2
    return 12742000*math.asin(math.sqrt(h))

def main():
    argv = list(sys.argv); opt = {}
    for k in ("--moto-bans", "--moto-out"):
        if k in argv:
            i = argv.index(k); opt[k] = argv[i + 1]; del argv[i:i + 2]
    src, dst = argv[1], argv[2]
    csvp = argv[3] if len(argv) > 3 else __file__.rsplit("apply_curated.py", 1)[0] + "curated/way覆蓋.csv"
    acts = load(csvp)
    tcsv = argv[4] if len(argv) > 4 else os.path.join(os.path.dirname(os.path.abspath(csvp)), "類型覆蓋.csv")
    trules = load_type_rules(tcsv)
    lov = load_lanes(os.path.join(os.path.dirname(os.path.abspath(csvp)), "車道覆蓋.csv")); ldone = []
    nov = load_names(os.path.join(os.path.dirname(os.path.abspath(csvp)), "路名覆蓋.csv")); ndone = []
    t0 = time.time(); done = {}
    if os.path.exists(dst): os.remove(dst)
    # 預掃：解除private 的 way 上的節點（閘門/車擋 barrier 的 access=private 會讓 Valhalla 加 private_access_penalty，一併解除）
    pn = set()
    bans = load_bans(opt["--moto-bans"]) if "--moto-bans" in opt else []
    ban_ids = {int(r["osm_way_id"]) for r in bans if r.get("osm_way_id", "").strip().isdigit()}
    ban_tags, ban_nodes, bnodes = {}, {}, set()
    for o in osmium.FileProcessor(src, osmium.osm.WAY):
        if acts.get(o.id) == "解除private":
            pn.update(n.ref for n in o.nodes)
        if o.id in ban_ids:
            ban_tags[o.id] = dict(o.tags); ban_nodes[o.id] = [n.ref for n in o.nodes]; bnodes.update(ban_nodes[o.id])
    judged = []  # (row, 狀態, 原因, plate, tiles處理)
    for r in bans:
        wid = int(r["osm_way_id"]) if r.get("osm_way_id", "").strip().isdigit() else None
        judged.append((r,) + (judge_ban(r, ban_tags.get(wid)) if wid else ("對不到", "osm_way_id 空白或非數字", None, "無")))
    tile_ban = {int(j[0]["osm_way_id"]) for j in judged if j[4] == "motorcycle=no"}
    mark_ban = {}
    for j in judged:
        if j[1] == "成功" and j[3]:
            mark_ban.setdefault(int(j[0]["osm_way_id"]), set()).add(("all" if j[3] == PLATE_ALL else "white_green", j[0]["direction"]))
    bcoords = {}
    tnodes = set()
    if trules:
        for o in osmium.FileProcessor(src, osmium.osm.WAY):
            if match_type(trules, o.tags): tnodes.update(n.ref for n in o.nodes)
    coords = {}; tdone = []
    nodefix = []
    w = osmium.SimpleWriter(dst)
    _add_way = w.add_way
    def add_way(o):  # 路名、車道覆蓋最後套用（其他覆蓋已改好的標籤保留）
        nv = nov.get(o.id)
        if nv is not None:
            ndone.append((o.id, o.tags.get("name"), nv[0])); o = o.replace(tags=name_fix({k: v for k, v in o.tags}, nv))
        ov = lov.get(o.id)
        if ov is None: return _add_way(o)
        ldone.append((o.id, o.tags.get("lanes"), ov[0])); _add_way(o.replace(tags=lanes_fix(o.tags, ov)))
    for o in osmium.FileProcessor(src):
        if o.is_node():
            if o.id in tnodes: coords[o.id] = (o.location.lat, o.location.lon)
            if o.id in bnodes: bcoords[o.id] = (o.location.lat, o.location.lon)
            if o.id in pn and "barrier" in o.tags and any(o.tags.get(k) in RESTRICT for k in ("access", "motor_vehicle", "motorcycle")):
                t = {k: v for k, v in o.tags if k not in ("access", "motor_vehicle", "motorcycle", "barrier")}
                t["touror:override"] = "rode"; t["touror:barrier_removed"] = o.tags.get("barrier", "")
                nodefix.append((o.id, dict(o.tags)))
                w.add_node(o.replace(tags=t))
            else:
                w.add_node(o)
        elif o.is_way():
            a = acts.get(o.id); pre = None
            if o.id in mark_ban:
                t = {k: v for k, v in o.tags}
                if o.id in tile_ban: t["motorcycle"] = "no"
                t["touror:moto_ban"] = ";".join(sorted(p + ":" + d for p, d in mark_ban[o.id]))
                if a is None:
                    add_way(o.replace(tags=t)); continue
                pre = t
            if a is None:
                m = match_type(trules, o.tags) if trules else None
                if m:
                    t = {k: v for k, v in o.tags}
                    t["touror:orig_highway"] = t["highway"]; t["highway"] = m[0]; t["touror:override"] = m[1]
                    pts = [coords[n.ref] for n in o.nodes if n.ref in coords]
                    tdone.append((o.id, m[2], sum(hav(pts[i], pts[i+1]) for i in range(len(pts)-1))))
                    add_way(o.replace(tags=t))
                else:
                    add_way(o)
                continue
            t = dict(pre) if pre is not None else {k: v for k, v in o.tags}
            before = {k: v for k, v in o.tags}
            if a == "解除private":
                t.pop("access", None); t["touror:override"] = "rode"
            elif a == "捷西路升級":
                t["highway"] = "tertiary"; t["maxspeed"] = "50"; t["touror:override"] = "jiexi"
            done[o.id] = (before, t)
            add_way(o.replace(tags=t))
        else:
            w.add_relation(o)
    w.close()
    miss = set(acts) - set(done)
    for i, t in nodefix: print("節點", i, t)
    print(f"套用 {len(done)}/{len(acts)} 條，耗時 {time.time()-t0:.0f}s；缺：{sorted(miss)}")
    for rid in sorted({x[1] for x in tdone}):
        L = [x for x in tdone if x[1] == rid]
        print(f"類型覆蓋 {rid}：改 {len(L)} 條 way，共 {sum(x[2] for x in L)/1000:.2f} km")
        for i, _, l in L: print("類型覆蓋way", rid, i, f"{l:.0f}m")
    if lov: print(f"車道覆蓋：套用 {len(ldone)}/{len(lov)} 條；缺：{sorted(set(lov) - {x[0] for x in ldone})}")
    for i, a, b in ldone: print("車道覆蓋way", i, a, "->", b)
    if nov: print(f"路名覆蓋：套用 {len(ndone)}/{len(nov)} 條；缺：{sorted(set(nov) - {x[0] for x in ndone})}")
    for i, a, b in ndone: print("路名覆蓋way", i, a, "->", b)
    if trules and not tdone: print("警示：類型覆蓋規則沒有命中任何 way")
    if "--moto-bans" in opt: write_bans(judged, ban_nodes, bcoords, opt.get("--moto-out", os.path.dirname(os.path.abspath(dst))))
    for i, (b, t) in done.items():
        print(i, {k: b.get(k) for k in ("highway", "access", "maxspeed", "oneway")}, "->", {k: t.get(k) for k in ("highway", "access", "maxspeed", "oneway")})

def write_bans(judged, ban_nodes, bcoords, outd):
    os.makedirs(outd, exist_ok=True)
    with open(os.path.join(outd, "禁行機車對應清單.csv"), "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f); wr.writerow(["CSV列", "osm_way_id", "方向", "來源路段", "狀態", "原因", "綠白", "黃紅", "tiles處理"])
        for r, st, why, plate, tp in judged:
            wr.writerow([r["_line"], r.get("osm_way_id"), r.get("direction"), r.get("source_text"), st, why,
                         plate["white"] if plate else "", plate["yellow"] if plate else "", tp])
    rules = {}
    for r, st, why, plate, tp in judged:
        if st != "成功" or not plate or tp != "走廊": continue
        wid = int(r["osm_way_id"]); pts = [bcoords[n] for n in ban_nodes.get(wid, []) if n in bcoords]
        if len(pts) < 2: continue
        if r["direction"] == "backward": pts = pts[::-1]
        key = (r.get("source", ""), r.get("source_text", ""))
        g = rules.setdefault(key, {"id": "moto-ban-%03d" % (len(rules) + 1), "road": road_key(key[1]), "segment": key[1],
                                   "status": "prohibited" if plate == PLATE_ALL else "partial",
                                   "applies_to": "機車（大型重型機車除外，禁3）" if plate == PLATE_WG else "所有機車", "plate": plate,
                                   "plate_note": r.get("legal_basis", ""), "source": r.get("source", ""), "confidence": r.get("confidence", ""),
                                   "corridor": {"width_m": 15, "source": "curated/禁行機車.csv osm_way_id（每週對位）", "osm_way_ids": [], "lines": [], "directions": []}})
        c = g["corridor"]; c["osm_way_ids"].append(wid); c["directions"].append(r["direction"])
        c["lines"].append([[round(lon, 6), round(lat, 6)] for lat, lon in pts])
    json.dump(list(rules.values()), open(os.path.join(outd, "禁行機車走廊.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = len(judged); ok = sum(1 for j in judged if j[1] == "成功"); miss = sum(1 for j in judged if j[1] == "對不到")
    st = {"總筆數": n, "成功對應": ok, "對不到": miss, "未採用": n - ok - miss,
          "寫入tiles_motorcycle_no": sum(1 for j in judged if j[4] == "motorcycle=no"),
          "走廊_依牌照由App排除": sum(1 for j in judged if j[4] == "走廊"), "走廊規則數": len(rules)}
    json.dump(st, open(os.path.join(outd, "禁行機車統計.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("禁行機車：總 %d、成功 %d、對不到 %d；tiles 寫 motorcycle=no %d 條，走廊 %d 條（%d 段）" % (n, ok, miss, st["寫入tiles_motorcycle_no"], st["走廊_依牌照由App排除"], len(rules)))
    for r, s_, why, _, _ in judged:
        if s_ == "對不到": print("禁行機車對不到", r.get("osm_way_id"), r.get("source_text"), why)

main()
