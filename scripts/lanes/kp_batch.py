"""每週判讀用的小工具（給 run-weekly-lanes.ps1 呼叫）。
  python kp_batch.py todo [N]              # 列出待判讀（ready 且未判讀、已有拼圖）的方向，最多 N 個；每行「S###.jpg<TAB>A001,A002」
  python kp_batch.py sample                # 乾跑用：取最後一個已有拼圖的方向，輸出一行同上格式（只含 1 個標籤）
  python kp_batch.py check <批次.json> <A001,A002,...>   # 檢查批次 JSON 是否符合 kp_record.py 格式；不符則 exit 1
"""
import sys, os, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kp import MAN, JUD, jload, needs_judge

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def todo(limit):
    man = jload(MAN, {"approaches": {}}); J = jload(JUD, {})
    by = collections.OrderedDict(); n = 0
    items = [a for k, a in man["approaches"].items() if needs_judge(k, a, J) and a.get("sheet") and a.get("label")]
    items.sort(key=lambda a: (a["sheet"], a["label"]))
    for a in items:
        if n >= limit: break
        by.setdefault(a["sheet"], []).append(a["label"]); n += 1
    for s, labs in by.items(): print(f"{s}\t{','.join(labs)}")


def sample():
    man = jload(MAN, {"approaches": {}})
    items = [a for a in man["approaches"].values() if a.get("sheet") and a.get("label")]
    if not items: return
    a = max(items, key=lambda a: (a["sheet"], a["label"]))
    print(f"{a['sheet']}\t{a['label']}")


def check(path, labels):
    want = [x for x in labels.split(",") if x]; errs = []
    raw = open(path, encoding="utf-8-sig").read()
    try:
        d = json.loads(raw)
    except Exception as e:
        # 外部模型（codex -o）可能包 markdown 或前後加說明：取第一個 { 到最後一個 }，成功就改寫成乾淨 JSON
        i, j = raw.find("{"), raw.rfind("}")
        try:
            d = json.loads(raw[i:j + 1]) if 0 <= i < j else None
        except Exception:
            d = None
        if d is None: print("JSON 無法解析:", e); sys.exit(1)
        json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1); print("已清掉 JSON 外的文字")
    if not isinstance(d, dict): print("頂層不是物件"); sys.exit(1)
    for lab in want:
        if lab not in d: errs.append(f"{lab} 缺漏")
    for lab, v in d.items():
        if lab not in want: errs.append(f"{lab} 不在這批標籤內"); continue
        if not isinstance(v, dict): errs.append(f"{lab} 不是物件"); continue
        extra = set(v) - {"a", "b", "note"}
        if extra: errs.append(f"{lab} 多出欄位 {','.join(sorted(extra))}")
        for side in ("a", "b"):
            if lab.startswith("B") and isinstance(v.get(side), dict):   # 路段點欄位嚴格：五個欄位缺一不可、不可多
                ks = set(v[side]); need = {"lanes", "tl", "vis", "moto", "median"}
                if ks != need: errs.append(f"{lab}.{side} 欄位應為 {','.join(sorted(need))}，實得 {','.join(sorted(ks))}")
            p = v.get(side)
            if not isinstance(p, dict): errs.append(f"{lab}.{side} 缺漏"); continue
            if lab.startswith("B"):   # 路段點：不判箭頭；要有 moto（機慢車道）、median（分隔島）
                if p.get("tl"): errs.append(f"{lab}.{side}.tl 路段點不判箭頭，須留空")
                for f in ("moto", "median"):
                    if p.get(f) not in ("yes", "no", "unknown"): errs.append(f"{lab}.{side}.{f} 不合法")
            else:   # 路口：欄位嚴格，五個缺一不可、不可多；two_stage／moto_ban_inner 只能 yes/no/unclear
                ks = set(p); need = {"lanes", "tl", "vis", "two_stage", "moto_ban_inner"}
                if ks != need: errs.append(f"{lab}.{side} 欄位應為 {','.join(sorted(need))}，實得 {','.join(sorted(ks))}")
                for f in ("two_stage", "moto_ban_inner"):
                    if f in p and p[f] not in ("yes", "no", "unclear"): errs.append(f"{lab}.{side}.{f} 不合法（須 yes/no/unclear）")
            if p.get("lanes") is not None and not (isinstance(p["lanes"], int) and 1 <= p["lanes"] <= 12): errs.append(f"{lab}.{side}.lanes 不合法")
            if not isinstance(p.get("tl", ""), str): errs.append(f"{lab}.{side}.tl 不是字串")
            if p.get("vis") not in ("none", "partial", "clear"): errs.append(f"{lab}.{side}.vis 不合法")
            tl = p.get("tl") or ""
            if tl and isinstance(p.get("lanes"), int) and len(tl.split("|")) != p["lanes"]: errs.append(f"{lab}.{side}.tl 段數與 lanes 不符")
    for e in errs: print(e)
    print("OK" if not errs else f"錯誤 {len(errs)} 項", f"（{len(d)} 筆）")
    sys.exit(1 if errs else 0)


if __name__ == "__main__":
    m = sys.argv[1]
    if m == "todo": todo(int(sys.argv[2]) if len(sys.argv) > 2 else 150)
    elif m == "sample": sample()
    elif m == "check": check(sys.argv[2], sys.argv[3])
