# -*- coding: utf-8 -*-
"""英文／中英混合地址 → 中文路名（供 build_place_addr.py；只在空間條件下採用，不硬猜）。

做法：
  1. 英文地址抽出：路名（Rd／Road→路、St／Street→街、Ave／Avenue／Blvd／Boulevard→大道；方位 E/W/S/N；序數 1st/2nd…）、
     Section N→N段、Lane／Ln→巷、Alley／Aly→弄、No. N-M／N號→門牌（N之M；同位數且 M>N≥10 視為門牌範圍，取 N）。
  2. 店家座標 300 m 內的 OSM 道路（extract_osm_road_names.py 產物）：每條路的中文名先正規化並去掉段，得 road_base；
     比對鍵＝(拼音鬆鍵, 方位, 序數, 路別)。中文側由 pypinyin（含破音字組合）產生，英文側也收 OSM name:en／old_name:en／alt_name:en 等。
     拼音鬆鍵把漢語拼音／通用拼音／威妥瑪的差異收斂（zh/jh/ch/j/q/c/z/ts/tz→q、x/hs/sh/s→s、ung→ong、ih→i、k→g、t→d、p→b…），
     Zhongxiao／Jhongsiao／Chunghsiao 得同一鍵。
  3. 300 m 內「恰好一個」中文 road_base 符合才採用；0 個或多個都列缺口（原因寫明）。
"""
import itertools, math, pickle, re, unicodedata

KIND_EN = {'rd': '路', 'road': '路', 'st': '街', 'street': '街', 'ave': '大道', 'avenue': '大道', 'blvd': '大道', 'boulevard': '大道'}
DIR_EN = {'e': '東', 'east': '東', 'w': '西', 'west': '西', 's': '南', 'south': '南', 'n': '北', 'north': '北'}
CN_NUM = {c: i + 1 for i, c in enumerate('一二三四五六七八九十')}
ROAD_RE = re.compile(r"((?:(?:[A-Za-z][A-Za-z']*|\d+(?:st|nd|rd|th))\.?[\s\-]+){1,5})(Boulevard|Avenue|Street|Road|Blvd|Ave|Rd|St)\b\.?", re.I)
NUM_EN = re.compile(r'\bNo\.?\s*(\d+)(?:\s*[-之]\s*(\d+))?', re.I)
NUM_CN = re.compile(r'(\d+)(?:\s*[-之]\s*(\d+))?\s*號')
SEC_RE = [re.compile(r'\b(?:Section|Sec)\.?\s*(\d+)', re.I), re.compile(r'(\d+)段')]
LANE_RE = [re.compile(r'\b(?:Lane|Ln)\.?\s*(\d+)(?:\s*-\s*(\d+))?', re.I), re.compile(r'(\d+)(?:之(\d+))?巷')]
ALLEY_RE = [re.compile(r'\b(?:Alley|Aly)\.?\s*(\d+)(?:\s*-\s*(\d+))?', re.I), re.compile(r'(\d+)(?:之(\d+))?弄')]
CN_BASE = re.compile(r'^[一-鿿]{1,12}(大道|路|街)$')
LOOSE = [('tsz', 'q'), ('ts', 'q'), ('tz', 'q'), ('hs', 's'), ('jh', 'q'), ('ch', 'q'), ('zh', 'q'), ('sh', 's'),
         ('c', 'q'), ('z', 'q'), ('j', 'q'), ('x', 's'), ('iung', 'iong'), ('ung', 'ong'), ('ih', 'i'), ('yi', 'i'),
         ('wu', 'u'), ('wun', 'wen'), ('w', 'u'), ('y', 'i'), ('k', 'g'), ('t', 'd'), ('p', 'b'), ('ii', 'i')]


def loose(s):
    s = re.sub(r'[^a-z]', '', unicodedata.normalize('NFKD', s.lower()))
    s = re.sub(r'(?<=[bpmfw])ong', 'eng', s)          # 通用拼音 fong/bong/mong/wong＝feng/beng/meng/weng
    for a, b in LOOSE:
        s = s.replace(a, b)
    return s


def en_road_keys(text):
    """英文路名字串 → set((鬆鍵, 方位, 序數, 路別))（可多個路名）。"""
    out = []
    for m in ROAD_RE.finditer(text):
        toks = [t.lower().rstrip('.') for t in re.findall(r"[A-Za-z][A-Za-z']*\.?|\d+(?:st|nd|rd|th)", m.group(1), re.I)]
        kind = KIND_EN[m.group(2).lower()]
        d = o = None
        for _ in range(2):
            if len(toks) > 1 and toks[-1] in DIR_EN and d is None:
                d = DIR_EN[toks.pop()]
            elif len(toks) > 1 and re.match(r'^\d+(st|nd|rd|th)$', toks[-1]) and o is None:
                o = int(re.match(r'\d+', toks.pop()).group())
        if not toks or any(re.match(r'^\d', t) or t in ('no', 'lane', 'ln', 'alley', 'aly', 'section', 'sec', 'dist', 'city') for t in toks):
            continue
        out.append((loose(''.join(toks)), d, o, kind))
    return out


def cn_keys(base, pinyin_fn):
    m = re.search(r'(大道|路|街)$', base)
    kind = m.group(1); rest = base[:-len(kind)]
    variants = [(rest, None, None)]
    if len(rest) > 1 and rest[-1] in CN_NUM:
        variants.append((rest[:-1], None, CN_NUM[rest[-1]]))
    for st, d, o in list(variants):
        if len(st) > 1 and st[-1] in '東西南北':
            variants.append((st[:-1], st[-1], o))
    keys = set()
    for st, d, o in variants:
        for combo in itertools.islice(itertools.product(*pinyin_fn(st)), 16):
            keys.add((loose(''.join(combo)), d, o, kind))
    return keys


class EnRoad:
    def __init__(self, pkl, normalize):
        import numpy as np
        from scipy.spatial import cKDTree
        from pypinyin import pinyin, Style
        pf = lambda s: [list(dict.fromkeys(p)) for p in pinyin(s, style=Style.NORMAL, heteronym=True)]
        d = pickle.load(open(pkl, 'rb'))
        self.kx = 111000.0 * math.cos(math.radians(23.7))
        self.base, self.keys = [], []
        kcache = {}
        for name, ens in d['roads']:
            nm = normalize(name)
            nm = re.sub(r'[0-9]+段$', '', nm)
            if not CN_BASE.match(nm):
                self.base.append(None); self.keys.append(None); continue
            if nm not in kcache:
                kcache[nm] = cn_keys(nm, pf)
            ks = set(kcache[nm])
            for e in ens:
                ks.update(en_road_keys(e + ' '))
            self.base.append(nm); self.keys.append(ks)
        pts = np.array([p for p in d['pts'] if self.base[p[2]] is not None])
        self.pidx = pts[:, 2].astype(np.int64)
        self.tree = cKDTree(np.c_[pts[:, 0] * 111000.0, pts[:, 1] * self.kx])

    def near_roads(self, lat, lon, r=300.0):
        return {int(self.pidx[j]) for j in self.tree.query_ball_point((lat * 111000.0, lon * self.kx), r)}

    @staticmethod
    def _num(rx_list, s):
        for rx in rx_list:
            ms = {(m.group(1), m.groups()[1] if len(m.groups()) > 1 else None) for m in rx.finditer(s)}
            if len(ms) == 1:
                return ms.pop()
            if len(ms) > 1:
                return 'AMBIG'
        return None

    def convert(self, text, lat, lon, normalize):
        """→ (dict|None, 原因)。dict 欄位與 build_place_addr.parse_loose 相同。"""
        if not re.search(r'[A-Za-z]', text or ''):
            return None, '非英文'
        s0 = unicodedata.normalize('NFKC', text)            # 英文部分不能走 normalize（會去空白）
        keys = set(en_road_keys(s0 + ' '))
        s = s0 + ' ' + normalize(text)                       # 中文段／巷／弄／號（二段→2段）用正規化版
        if not keys:
            return None, '英文地址無可辨識路名'
        if len({k for k in keys}) > 1:
            return None, '英文地址含多個路名'
        k = keys.pop()
        hit = {self.base[i] for i in self.near_roads(lat, lon) if k in self.keys[i]}
        if not hit:
            return None, '英文路名300m內無對應中文道路'
        if len(hit) > 1:
            return None, '英文路名300m內對應多條中文道路'
        base = hit.pop()
        sec = self._num(SEC_RE, s)
        if sec == 'AMBIG':
            return None, '英文地址段號矛盾'
        section = int(sec[0]) if sec else None
        num = None
        for rx in (NUM_EN, NUM_CN):
            ms = {(m.group(1), m.group(2)) for m in rx.finditer(s)}
            if ms:
                if len(ms) == 1:
                    a, b = ms.pop()
                    if b and len(a) == len(b) and int(b) > int(a) >= 10:
                        b = None                              # No. 301-311 是門牌範圍
                    num = str(int(a)) + ('之%d' % int(b) if b and int(b) else '')
                break
        if num and re.search(r'(^|[,\s])之\s*\d', s0):
            num = None                                        # 「之1, No. 280號」：之號沒跟在門牌後，門牌不確定，只留路名
        def ln(rxs):
            v = self._num(rxs, s)
            if v == 'AMBIG': return 'AMBIG'
            return (str(int(v[0])) + ('之%d' % int(v[1]) if v[1] and int(v[1]) else '')) if v else None
        lane, alley = ln(LANE_RE), ln(ALLEY_RE)
        if 'AMBIG' in (lane, alley):
            return None, '英文地址巷弄矛盾'
        if alley and not lane:
            alley = None
        if num == '0' or lane == '0':
            return None, '門牌為 0'
        kind = re.search(r'(大道|路|街)$', base).group(1)
        return dict(city=None, district=None, road=base + ('%d段' % section if section else ''), road_base=base, section=section,
                    road_kind=kind, lane=lane, alley=alley, number=num), ''
