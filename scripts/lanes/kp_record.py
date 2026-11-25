"""把判讀結果（以標籤 A### 記）寫進 <LANES_WORKDIR>/work/kp_judgments.json。用法：python kp_record.py <判讀批次.json>
批次檔格式：{"A001": {"a": {"lanes": 2, "tl": "", "vis": "none|partial|clear"}, "b": {...}, "note": "..."}, ...}
lanes 看不清就填 null；tl 只記看得到的箭頭（OSM turn:lanes 格式），看不到留空。vis=clear 表示該張所有車道的箭頭都看得到。"""
import sys, os, json, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kp import MAN, JUD, jload, jsave
man = jload(MAN, {"approaches": {}})
lab2key = {a["label"]: k for k, a in man["approaches"].items() if a.get("label")}
J = jload(JUD, {}); n = 0
for lab, v in json.load(open(sys.argv[1], encoding="utf-8")).items():
    k = lab2key.get(lab)
    if not k: print("找不到標籤", lab); continue
    if man["approaches"][k].get("status") != "ready" and k not in J: print("略過（非待判讀狀態，例如已作廢的高架照片）", lab); continue
    v["label"] = lab; v["judged_on"] = datetime.date.today().isoformat(); J[k] = v; n += 1
jsave(JUD, J)
print("寫入", n, "筆，累計", len(J))
