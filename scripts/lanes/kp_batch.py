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
        for side in ("a", "b"):
            p = v.get(side)
            if not isinstance(p, dict): errs.append(f"{lab}.{side} 缺漏"); continue
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
