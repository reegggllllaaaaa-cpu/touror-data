# -*- coding: utf-8 -*-
"""Touror Overture Maps 台灣店家資料建庫工具（NAV-OVERTURE-D1）

用法（務必使用 overture_venv）：
  python -I build_places_overture.py --sample 50000            # 小樣本試建（confidence≥0.9 前 5 萬筆）
  python -I build_places_overture.py                           # 全量（預設 confidence≥0.6，超商/加油站≥0.5）
  python -I build_places_overture.py --out xxx.sqlite --verify # 只對既有庫跑驗證

輸出：與 touror_places.sqlite 相同 schema 的獨立 touror_places_overture.sqlite，
不與 OSM 資料合併，避免 ODbL 衍生資料庫問題。
"""
import argparse
import datetime
import json
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata

# 重用現有建庫的 norm_name()/grams()/fts_query()，規則不可改
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_places import norm_name, grams, fts_query

SCHEMA_VERSION = 1
RELEASE = "2026-11-18.0"
SOURCE = "Overture Maps Places release 2026-11-18.0 (theme=places/type=place), filtered for Taiwan"
ATTRIBUTION = (
    "Overture Maps Foundation, overturemaps.org; "
    "Foursquare Labs, Inc. (Apache 2.0); AllThePlaces (CC0)"
)
LICENSE = "CDLA-Permissive-2.0 / Apache-2.0 / CC0-1.0 (依上游來源)"

# 150 m 對應的緯度差（度）
DEDUP_CELL_DEG = 150.0 / 111_000.0
NEAR_DEDUP_M = 30.0
OSM_DEDUP_M = 150.0

# 預設與放寬 confidence 門檻
DEFAULT_CONF = 0.6
LOOSE_CONF = 0.5
LOOSE_CATEGORIES = {"convenience_store", "gas_station", "fueling_station"}

# 明顯非店面的類別：直接排除
DROP_CATEGORIES = {
    "public_restroom",
    "parking",
    "ev_charging_station",
    "cemetery",
    "jail_or_prison",
    "military_site",
    "military_base",
}

# basic_category / taxonomy_primary → (kind, OSM 同類最大 weight)
CATEGORY_TO_KIND = {
    # 超商
    "convenience_store": ("shop", 350),
    # 加油站
    "gas_station": ("fuel", 450),
    "fueling_station": ("fuel", 450),
    # 餐飲
    "restaurant": ("food", 300),
    "casual_eatery": ("food", 300),
    "coffee_shop": ("food", 300),
    "cafe": ("food", 300),
    "food_truck_stand": ("food", 300),
    "fast_food_restaurant": ("food", 300),
    "bakery": ("food", 300),
    "bar": ("food", 300),
    "food_and_drink": ("food", 300),
    "food_and_beverage_store": ("food", 300),
    "non_alcoholic_beverage_venue": ("food", 300),
    "smoothie_juice_bar": ("food", 300),
    "dessert_shop": ("food", 300),
    "ice_cream_shop": ("food", 300),
    "alcoholic_beverage_venue": ("food", 300),
    "food_service": ("food", 300),
    "food_court": ("food", 300),
    "delicatessen": ("food", 300),
    "diner": ("food", 300),
    "tea_room": ("food", 300),
    "lounge": ("food", 300),
    "brewery": ("food", 300),
    "winery": ("food", 300),
    # 住宿
    "hotel": ("lodging", 450),
    "lodging": ("lodging", 450),
    "bed_and_breakfast": ("lodging", 450),
    "private_lodging": ("lodging", 450),
    "resort": ("lodging", 450),
    "inn": ("lodging", 450),
    "campground": ("scenic", 650),
    "rv_park": ("scenic", 650),
    # 宗教場所
    "religious_organization": ("worship", 400),
    "christian_place_of_worship": ("worship", 400),
    "buddhist_place_of_worship": ("worship", 400),
    "hindu_place_of_worship": ("worship", 400),
    "muslim_place_of_worship": ("worship", 400),
    "place_of_worship": ("worship", 400),
    # 景點/戶外
    "park": ("scenic", 650),
    "mountain": ("peak", 750),
    "historic_site": ("scenic", 650),
    "beach": ("scenic", 650),
    "lake": ("scenic", 650),
    "river": ("scenic", 650),
    "national_park": ("scenic", 650),
    "playground": ("scenic", 650),
    "public_plaza": ("scenic", 650),
    "garden": ("scenic", 650),
    "nature_reserve": ("scenic", 650),
    "waterfall": ("scenic", 650),
    "pier": ("scenic", 650),
    "lighthouse": ("scenic", 650),
    "fort": ("scenic", 650),
    "castle": ("scenic", 650),
    "monument": ("scenic", 650),
    "sculpture_statue": ("scenic", 650),
    "amusement_park": ("scenic", 650),
    "zoo": ("scenic", 650),
    "aquarium": ("scenic", 650),
    "golf_course": ("scenic", 650),
    "hot_springs": ("scenic", 650),
    "fairgrounds": ("scenic", 650),
    "recreational_trail_or_path": ("scenic", 650),
    "sport_or_fitness_facility": ("scenic", 650),
    "sports_and_recreation": ("scenic", 650),
    "sport_or_recreation_club": ("scenic", 650),
    "stadium_arena": ("scenic", 650),
    "sport_court": ("scenic", 650),
    "sport_field": ("scenic", 650),
    "swimming_pool": ("scenic", 650),
    "skating_rink": ("scenic", 650),
    "dog_park": ("scenic", 650),
    "skate_park": ("scenic", 650),
    "campground": ("scenic", 650),
    "rv_park": ("scenic", 650),
    # 市場
    "market": ("market", 550),
    "farmers_market": ("market", 550),
    # 交通
    "train_station": ("transport", 750),
    "airport": ("transport", 750),
    "public_transit_facility_or_service": ("transport", 750),
    "air_transport_facility_or_service": ("transport", 750),
    "taxi_or_ride_share_service": ("transport", 750),
    "marina": ("transport", 750),
    "b2b_transportation_and_storage_service": ("transport", 750),
    "travel_and_transportation": ("transport", 750),
    "bridge": ("transport", 750),
    # 商店（非超商）
    "shopping": ("shop", 350),
    "fashion_and_apparel_store": ("shop", 350),
    "electronics_store": ("shop", 350),
    "books_music_and_video_store": ("shop", 350),
    "sporting_goods_store": ("shop", 350),
    "flowers_and_gifts_store": ("shop", 350),
    "arts_crafts_and_hobby_store": ("shop", 350),
    "specialty_store": ("shop", 350),
    "toys_and_games_store": ("shop", 350),
    "musical_instrument_and_pro_audio_store": ("shop", 350),
    "second_hand_store": ("shop", 350),
    "department_store": ("shop", 350),
    "shopping_mall": ("shop", 350),
    "warehouse_club_store": ("shop", 350),
    "superstore": ("shop", 350),
    "discount_store": ("shop", 350),
    "office_supply_store": ("shop", 350),
    "pet_store": ("shop", 350),
    "animal_and_pet_store": ("shop", 350),
    "personal_care_and_beauty_store": ("shop", 350),
    "pharmacy_and_drug_store": ("service", 500),  # 藥局偏服務
    "hardware_home_and_garden_store": ("shop", 350),
    "vehicle_parts_store": ("shop", 350),
    "auto_dealer": ("shop", 350),
    "vehicle_dealer": ("shop", 350),
    # 公共/專業服務
    "professional_service": ("service", 500),
    "personal_or_beauty_service": ("shop", 350),  # 美容美髮等個人服務：App 端 service 會顯示「公共服務」，改歸商店
    "automotive_service": ("service", 500),
    "hospital": ("service", 500),
    "dental_clinic": ("service", 500),
    "wellness_service": ("service", 500),
    "social_or_community_service": ("service", 500),
    "event_or_party_service": ("service", 500),
    "home_service": ("service", 500),
    "real_estate_service": ("service", 500),
    "corporate_or_business_office": ("service", 500),
    "vehicle_service": ("service", 500),
    "place_of_learning": ("service", 500),
    "education": ("service", 500),
    "atm": ("service", 500),
    "financial_service": ("service", 500),
    "government_office": ("service", 500),
    "bank_or_credit_union": ("service", 500),
    "family_service": ("service", 500),
    "technical_service": ("service", 500),
    "community_and_government": ("service", 500),
    "medical_service": ("service", 500),
    "police_station": ("service", 500),
    "library": ("service", 500),
    "health_care": ("service", 500),
    "primary_care_or_general_clinic": ("service", 500),
    "specialized_health_care": ("service", 500),
    "behavioral_or_mental_health_clinic": ("service", 500),
    "vision_or_eye_care_clinic": ("service", 500),
    "complementary_and_alternative_medicine": ("service", 500),
    "pharmacy_and_drug_store": ("service", 500),
    "reproductive_perinatal_and_womens_care": ("service", 500),
    "diagnostics_imaging_or_lab_service": ("service", 500),
    "outpatient_care_facility": ("service", 500),
    "specialized_medical_facility": ("service", 500),
    "emergency_department": ("service", 500),
    "emergency_or_urgent_care_facility": ("service", 500),
    "pediatric_clinic": ("service", 500),
    "surgery": ("service", 500),
    "fire_station": ("service", 500),
    "courthouse": ("service", 500),
    "government_department": ("service", 500),
    "post_office": ("service", 500),
    "laundry_service": ("service", 500),
    "printing_service": ("service", 500),
    "design_service": ("service", 500),
    "legal_service": ("service", 500),
    "attorney_or_law_firm": ("service", 500),
    "b2b_service": ("service", 500),
    "b2b_office_and_professional_service": ("service", 500),
    "b2b_industrial_and_machine_service": ("service", 500),
    "b2b_science_and_technology_service": ("service", 500),
    "b2b_energy_and_utility_service": ("service", 500),
    "research_institute": ("service", 500),
    "labor_union": ("service", 500),
    "civic_organization": ("service", 500),
    "political_organization": ("service", 500),
    "youth_organization": ("service", 500),
    "community_center": ("service", 500),
    "senior_living_facility": ("service", 500),
    "animal_or_pet_service": ("service", 500),
    "tutoring_service": ("service", 500),
    "educational_service": ("service", 500),
    "college_university": ("service", 500),
    "elementary_school": ("service", 500),
    "middle_school": ("service", 500),
    "high_school": ("service", 500),
    "preschool": ("service", 500),
    "specialty_school": ("service", 500),
    "educational_facility": ("service", 500),
    "academy": ("service", 500),
    "campus_building": ("service", 500),
    "public_utility": ("service", 500),
    "electric_utility_provider": ("service", 500),
    "water_utility_provider": ("service", 500),
    "telecommunications_service": ("service", 500),
    "storage_facility": ("service", 500),
    "rental_service": ("service", 500),
    "travel_service": ("service", 500),
    "fitness_studio": ("service", 500),
    "gym": ("service", 500),
    "massage_therapy": ("service", 500),
    "spa": ("service", 500),
    "barber": ("service", 500),
    "nail_salon": ("service", 500),
    "skin_care_and_makeup": ("service", 500),
    "hair_salon": ("service", 500),
    "beauty_salon": ("service", 500),
    # 地點/其他
    "manufacturer": ("poi", 450),
    "supplier_or_distributor": ("poi", 450),
    "wholesaler": ("poi", 450),
    "industrial_facility_or_service": ("poi", 450),
    "farm": ("poi", 450),
    "geographic_entities": ("poi", 450),
    "land_feature": ("poi", 450),
    "built_feature": ("poi", 450),
    "island": ("poi", 450),
    "natural_gas_utility_provider": ("poi", 450),
    "utility_energy_infrastructure": ("poi", 450),
    "memorial_site": ("poi", 450),
    "science_attraction": ("poi", 450),
    "performing_arts_venue": ("poi", 450),
    "art_gallery": ("poi", 450),
    "museum": ("poi", 450),
    "theatre_venue": ("movie_theater", 450),
    "movie_theater": ("poi", 450),
    "music_venue": ("poi", 450),
    "gaming_venue": ("poi", 450),
    "arcade": ("poi", 450),
    "dance_club": ("poi", 450),
    "comedy_club": ("poi", 450),
    "adult_entertainment_venue": ("poi", 450),
    "nightlife_venue": ("poi", 450),
    "casino": ("poi", 450),
    "winery": ("poi", 450),
    "brewery": ("poi", 450),
    "distillery": ("poi", 450),
    "radio_station": ("poi", 450),
    "television_station": ("poi", 450),
    "ticket_office_or_booth": ("poi", 450),
    "amusement_attraction": ("poi", 450),
    "rural_attraction": ("poi", 450),
    "animal_attraction": ("poi", 450),
    "forest": ("poi", 450),
    "apartment": ("poi", 450),
    "condominium": ("poi", 450),
    "housing_or_property_service": ("poi", 450),
    "real_estate_agent": ("poi", 450),
}

_KIND_MAX = {
    "place": 1050,
    "transport": 800,
    "road": 800,
    "peak": 750,
    "scenic": 650,
    "market": 600,
    "service": 500,
    "poi": 450,
    "lodging": 450,
    "fuel": 450,
    "worship": 400,
    "shop": 350,
    "food": 300,
}

_DIGITS_RE = re.compile(r"^[0-9]+$")
_SEVEN_RE = re.compile(r"^7[\s\-]?(eleven|11)|seven[\s\-]?eleven|統一超商", re.IGNORECASE)


def log(s):
    print(time.strftime("%H:%M:%S"), s, flush=True)


def haversine_m(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) *
         math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742000 * math.asin(math.sqrt(a))


def cell(lat, lon, cell_deg):
    return int(math.floor(lat / cell_deg)), int(math.floor(lon / cell_deg))


class NameGridIndex:
    """依「正規化名稱」建立的格網索引，用來快速做同名鄰近去重。"""
    def __init__(self, cell_deg):
        self.cell_deg = cell_deg
        self.by_name = {}  # name_norm -> {cell -> [(lat, lon, ...)]}

    def add(self, name_norm, lat, lon, payload=None):
        d = self.by_name.setdefault(name_norm, {})
        c = cell(lat, lon, self.cell_deg)
        d.setdefault(c, []).append((lat, lon, payload))

    def has_nearby(self, name_norm, lat, lon, radius_m, exclude_self=False):
        d = self.by_name.get(name_norm)
        if not d:
            return False
        gy, gx = cell(lat, lon, self.cell_deg)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for lat2, lon2, _ in d.get((gy + dy, gx + dx), ()):
                    if haversine_m(lat, lon, lat2, lon2) <= radius_m:
                        return True
        return False



# ---- 純英文品牌名處理（2026-11-24）----
# Overture 只有單一 name 欄（無 name:zh）。規則：
#  1. 純英文（不含中日文字）且符合下表品牌 → 若 EN_DUP_M 內已有「含中文、同品牌」的店（OSM 庫或本庫其他列）→ 丟棄英文版（留中文版）
#  2. 否則改成中文品牌名（不編造分店名；分店區別靠 sub 行政區）
#  3. 不在品牌表的純英文名（Adidas、Curves…）原樣保留
EN_DUP_M = 50.0
BRANDS = [  # (key, 比對 regex（對小寫 norm 名稱 search）, 中文品牌名)
    ("711", r"7[\s\-]?(eleven|11)|seven[\s\-]?eleven|統一超商|小七", "統一超商"),
    ("fami", r"family[\s\-]?mart|全家", "全家"),
    ("hilife", r"hi[\s\-]?life|萊爾富", "萊爾富"),
    ("ok", r"^ok[\s\-]?mart|^ok$|ok超商", "OK超商"),
    ("watsons", r"watsons|屈臣氏", "屈臣氏"),
    ("starbucks", r"starbucks|星巴克", "星巴克"),
    ("mcd", r"mcdonald|麥當勞", "麥當勞"),
    ("kfc", r"^kfc|肯德基", "肯德基"),
    ("pizzahut", r"pizza hut|必勝客", "必勝客"),
    ("louisa", r"louisa|路易莎", "路易莎咖啡"),
    ("cosmed", r"^cosmed|康是美", "康是美"),
    ("mos", r"mos burger|摩斯", "摩斯漢堡"),
]
_BRANDS_RE = [(k, re.compile(rx, re.IGNORECASE), zh) for k, rx, zh in BRANDS]
_CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


def brand_of(name_norm):
    for k, rx, zh in _BRANDS_RE:
        if rx.search(name_norm):
            return k, zh
    return None


def has_cjk(s):
    return bool(_CJK_RE.search(s))


class BrandIndex:
    """含中文、符合品牌表的店的格網索引（key -> cell -> [(lat,lon)]）。"""
    def __init__(self):
        self.d = {}

    def add(self, key, lat, lon):
        self.d.setdefault(key, {}).setdefault(cell(lat, lon, DEDUP_CELL_DEG), []).append((lat, lon))

    def near(self, key, lat, lon, radius_m):
        dd = self.d.get(key)
        if not dd:
            return False
        gy, gx = cell(lat, lon, DEDUP_CELL_DEG)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for la, lo in dd.get((gy + dy, gx + dx), ()):
                    if haversine_m(lat, lon, la, lo) <= radius_m:
                        return True
        return False


def resolve_english_brands(rows, osm_path):
    """rows: (name_norm,name,kind,sub,lat,lon,weight)。回傳新 rows 與統計。"""
    bi = BrandIndex()
    con = sqlite3.connect(osm_path)
    for name, lat, lon in con.execute("SELECT name, lat, lon FROM place"):
        if name and has_cjk(name):
            b = brand_of(norm_name(name))
            if b:
                bi.add(b[0], lat, lon)
    con.close()
    for r in rows:
        if has_cjk(r[1]):
            b = brand_of(r[0])
            if b:
                bi.add(b[0], r[4], r[5])
    out, st = [], {"en_brand_total": 0, "en_dropped_dup_zh": 0, "en_renamed_zh": 0}
    for r in rows:
        if has_cjk(r[1]):
            out.append(r)
            continue
        b = brand_of(r[0])
        if not b:
            out.append(r)
            continue
        st["en_brand_total"] += 1
        if bi.near(b[0], r[4], r[5], EN_DUP_M):
            st["en_dropped_dup_zh"] += 1
            continue
        st["en_renamed_zh"] += 1
        out.append((norm_name(b[1]), b[1], r[2], r[3], r[4], r[5], r[6]))
    # 改名後同品牌 30 m 內重複再收斂一次
    seen, final = NameGridIndex(DEDUP_CELL_DEG), []
    for r in out:
        if r[1] in {z for _, _, z in BRANDS} and seen.has_nearby(r[0], r[4], r[5], NEAR_DEDUP_M):
            st["en_dropped_dup_zh"] += 1
            continue
        seen.add(r[0], r[4], r[5])
        final.append(r)
    return final, st


def load_osm_names(osm_path):
    """載入 OSM 地名庫所有名稱與座標，建立同名格網索引。"""
    log("載入 OSM 庫去重索引：%s" % osm_path)
    con = sqlite3.connect(osm_path)
    rows = con.execute("SELECT name, lat, lon FROM place").fetchall()
    con.close()
    idx = NameGridIndex(DEDUP_CELL_DEG)
    empty = 0
    for name, lat, lon in rows:
        if not name:
            empty += 1
            continue
        n = norm_name(name)
        if not n:
            continue
        idx.add(n, lat, lon)
    log("  OSM 索引：%d 筆有名稱，%d 筆無名稱" % (len(rows) - empty, empty))
    return idx


def map_kind(name_norm, basic_category, taxonomy_primary, confidence):
    """回傳 (kind, weight_cap) 或 None（要排除）。"""
    cat = basic_category or ""
    tax = taxonomy_primary or ""

    # 名稱救援：中油/台灣中油 不論分類為何，低門檻視為加油站
    if confidence >= LOOSE_CONF and ("中油" in name_norm or "台灣中油" in name_norm):
        if cat != "convenience_store":
            return "fuel", _KIND_MAX["fuel"]

    # 品牌過濾：7-ELEVEN / 7-Eleven / 統一超商 必須是超商或加油站類
    if _SEVEN_RE.search(name_norm):
        if cat not in ("convenience_store", "gas_station", "fueling_station"):
            return None

    # 品牌過濾：「全家」非 convenience_store 則排除（全家福鞋業/牙醫等）
    if "全家" in name_norm and cat != "convenience_store":
        return None

    kind, cap = CATEGORY_TO_KIND.get(cat) or CATEGORY_TO_KIND.get(tax) or (None, 0)
    return (kind, cap) if kind else None


def make_rows(df, osm_index):
    """從 pandas DataFrame 產出 (name_norm, name, kind, sub, lat, lon, weight) 列表，已去重。"""
    rows = []
    kept_index = NameGridIndex(DEDUP_CELL_DEG)
    dropped = {"conf": 0, "name": 0, "category": 0, "osm_dup": 0, "ovt_dup": 0, "kind": 0}
    kind_stat = {}

    for _, r in df.iterrows():
        conf = float(r.get("confidence", 0) or 0)
        cat = (r.get("basic_category") or "").strip()
        tax = (r.get("taxonomy_primary") or "").strip()
        name = str(r.get("name") or "").strip()
        if not name:
            dropped["name"] += 1
            continue

        # confidence 門檻
        threshold = LOOSE_CONF if cat in LOOSE_CATEGORIES or tax in LOOSE_CATEGORIES else DEFAULT_CONF
        if conf < threshold:
            dropped["conf"] += 1
            continue

        # 排除明顯非店面類別
        if cat in DROP_CATEGORIES or tax in DROP_CATEGORIES:
            dropped["category"] += 1
            continue

        name_norm = norm_name(name)
        if not name_norm or _DIGITS_RE.match(name_norm) or not grams(name_norm):
            dropped["name"] += 1
            continue

        mapped = map_kind(name_norm, cat, tax, conf)
        if not mapped:
            dropped["kind"] += 1
            continue
        kind, cap = mapped

        # 與 OSM 庫去重
        lat = float(r.get("lat"))
        lon = float(r.get("lon"))
        if osm_index.has_nearby(name_norm, lat, lon, OSM_DEDUP_M):
            dropped["osm_dup"] += 1
            continue

        # Overture 內部同名近座標去重（保留 confidence 較高者，因 df 已按 confidence 降冪）
        if kept_index.has_nearby(name_norm, lat, lon, NEAR_DEDUP_M):
            dropped["ovt_dup"] += 1
            continue

        weight = min(cap, int(round(conf * 100)))
        sub = str(r.get("address_locality") or "").strip() or None
        rows.append((name_norm, name, kind, sub, round(lat, 6), round(lon, 6), weight))
        kept_index.add(name_norm, lat, lon)
        kind_stat[kind] = kind_stat.get(kind, 0) + 1

    log("Overture 篩選：保留 %d，丟棄 %s" % (len(rows), json.dumps(dropped, ensure_ascii=False)))
    log("kind 統計：%s" % json.dumps(kind_stat, ensure_ascii=False))
    return rows


def build(rows, out_path):
    tmp = out_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute(
        "CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, sub TEXT, "
        "lat REAL NOT NULL, lon REAL NOT NULL, weight INTEGER NOT NULL)"
    )
    con.execute("CREATE VIRTUAL TABLE search USING fts5(grams, tokenize='unicode61')")
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")

    rows.sort(key=lambda r: (-r[6], r[1]))
    con.executemany(
        "INSERT INTO place(id,name,kind,sub,lat,lon,weight) VALUES(?,?,?,?,?,?,?)",
        ((i + 1, r[1], r[2], r[3], r[4], r[5], r[6]) for i, r in enumerate(rows))
    )
    con.executemany(
        "INSERT INTO search(rowid, grams) VALUES(?,?)",
        ((i + 1, grams(r[1])) for i, r in enumerate(rows))
    )
    con.execute("INSERT INTO search(search) VALUES('optimize')")

    meta = {
        "version": str(SCHEMA_VERSION),
        "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "release": RELEASE,
        "row_count": str(len(rows)),
    }
    con.executemany("INSERT INTO meta VALUES(?,?)", meta.items())
    con.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    con.commit()
    con.execute("VACUUM")
    con.close()
    if os.path.exists(out_path):
        os.remove(out_path)
    os.replace(tmp, out_path)
    return os.path.getsize(out_path)


VERIFY_QUERIES = [
    ("小七", "7-ELEVEN"),
    ("全家", "全家"),
    ("萊爾富", "萊爾富"),
    ("OK", "OK"),
    ("星巴克", "星巴克"),
    ("中油", "中油"),
    ("仁武區", "仁武"),
    ("八卦寮", "八卦寮"),
]


def verify(db_path):
    log("驗證 %s" % db_path)
    con = sqlite3.connect(db_path)
    results = []
    for label, q in VERIFY_QUERIES:
        m = fts_query(q)
        n = con.execute("SELECT COUNT(*) FROM (SELECT rowid FROM search WHERE search MATCH ? LIMIT 2000)", (m,)).fetchone()[0]
        results.append((label, q, m, n))
        log("  %s `%s` (%s) → %d" % (label, q, m, n))

    # 八卦寮 2 km 內超商
    bagua_lat, bagua_lon = 22.688, 120.337
    near = con.execute(
        "SELECT name, lat, lon FROM place WHERE kind='shop' AND "
        "lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (bagua_lat - 0.02, bagua_lat + 0.02, bagua_lon - 0.02, bagua_lon + 0.02)
    ).fetchall()
    conv = [r for r in near if haversine_m(bagua_lat, bagua_lon, r[1], r[2]) <= 2000]
    log("  八卦寮 2 km 內 shop（超商）%d 筆" % len(conv))
    con.close()
    return results, conv


def main():
    ap = argparse.ArgumentParser(description="建置 Overture Maps 台灣店家資料庫")
    ap.add_argument("--parquet", default=r"C:\Users\REG Windows\資料中心\專案\重機導航\驗證\overture-probe-1\output\taiwan_places.parquet")
    ap.add_argument("--osm-db", default=r"C:\Users\REG Windows\資料中心\專案\重機導航\資料\搜尋\touror_places.sqlite")
    ap.add_argument("--out", default=r"C:\Users\REG Windows\資料中心\專案\重機導航\資料\搜尋\touror_places_overture.sqlite")
    ap.add_argument("--sample", type=int, default=0, help="小樣本試建：取 confidence≥0.9 的前 N 筆")
    ap.add_argument("--verify", action="store_true", help="只對 --out 跑驗證")
    args = ap.parse_args()

    if args.verify:
        verify(args.out)
        return

    log("讀取 Parquet：%s" % args.parquet)
    import pandas as pd
    df = pd.read_parquet(args.parquet)
    log("  原始 %d 筆" % len(df))

    if args.sample:
        df = df[df["confidence"] >= 0.9].sort_values("confidence", ascending=False).head(args.sample).reset_index(drop=True)
        log("  小樣本試建：confidence≥0.9 前 %d 筆" % len(df))

    osm_index = load_osm_names(args.osm_db)
    rows = make_rows(df, osm_index)
    n0 = len(rows)
    en0 = sum(1 for r in rows if not has_cjk(r[1]))
    rows, st = resolve_english_brands(rows, args.osm_db)
    log('英文品牌處理：筆數 %d -> %d；純英文名 %d -> %d；%s' % (n0, len(rows), en0, sum(1 for r in rows if not has_cjk(r[1])), json.dumps(st, ensure_ascii=False)))
    size = build(rows, args.out)
    log("建庫完成：%d 筆，%.1f MB，%s" % (len(rows), size / 1024 ** 2, args.out))
    verify(args.out)


if __name__ == "__main__":
    main()
