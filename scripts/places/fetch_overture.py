"""從 Overture S3（公開、匿名）抓台灣 places，一次下載、本機衍生三個檔：
  raw.parquet            全欄位（同 資料/搜尋/路網來源/腳本/fetch_overture_raw.py）
  taiwan_places.parquet  build_places_overture.py 的輸入（同 驗證/overture-probe-1/scripts/download_taiwan.py 的欄位與區域框）
  flat.parquet           build_name_completion.py / build_place_addr.py 的輸入（同 flatten_overture_raw.py）
用法：python fetch_overture.py <outdir> [--release 2026-11-18.0]   不給 release 就用 S3 上最新一版。
最後一行印出 JSON：{"release":..., "raw":筆數, "taiwan":筆數}
"""
import argparse, json, os, re, time
import duckdb

REGIONS = [(21.8, 25.4, 119.9, 122.1), (23.15, 23.85, 119.3, 119.75), (24.35, 24.55, 118.2, 118.55), (25.9, 26.4, 119.85, 120.55)]


def latest_release(con):
    rows = con.execute("SELECT DISTINCT regexp_extract(file, 'release/([0-9]{4}-[0-9]{2}-[0-9]{2}\\.[0-9]+)/', 1) AS r "
                       "FROM glob('s3://overturemaps-us-west-2/release/*/theme=places/type=place/*.parquet')").fetchall()
    rels = sorted({r[0] for r in rows if r[0] and re.match(r'^\d{4}-\d{2}-\d{2}\.\d+$', r[0])})
    if not rels:
        raise SystemExit('找不到 Overture release')
    return rels[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('outdir'); ap.add_argument('--release')
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    d = os.path.abspath(a.outdir).replace('\\', '/')
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    rel = a.release or latest_release(con)
    print('Overture release', rel, flush=True)
    t0 = time.time()
    src = f"s3://overturemaps-us-west-2/release/{rel}/theme=places/type=place/*"
    con.execute(f"""COPY (SELECT id, names, brand, addresses, basic_category, taxonomy, confidence, operating_status, phones, websites, sources,
      bbox.xmin AS lon, bbox.ymin AS lat, bbox.xmax AS xmax, bbox.ymax AS ymax
      FROM read_parquet('{src}', hive_partitioning=1)
      WHERE bbox.xmin BETWEEN 118 AND 123 AND bbox.ymin BETWEEN 21.5 AND 26.5) TO '{d}/raw.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    print('下載完成 %.0f 秒' % (time.time() - t0), flush=True)
    where = ' OR '.join(f'(lon >= {b} AND xmax <= {c} AND lat >= {la} AND ymax <= {lb})' for la, lb, b, c in REGIONS)
    con.execute(f"""COPY (SELECT id, names.primary AS name, basic_category, taxonomy.primary AS taxonomy_primary, taxonomy.hierarchy AS taxonomy_hierarchy,
      confidence, lat, lon,
      (phones IS NOT NULL AND len(phones) > 0) AS has_phone, (websites IS NOT NULL AND len(websites) > 0) AS has_website,
      addresses[1].freeform AS address_freeform, addresses[1].locality AS address_locality, addresses[1].country AS address_country,
      sources[1].provider AS source_provider, sources[1].dataset AS source_dataset, sources[1].license AS source_license
      FROM '{d}/raw.parquet' WHERE {where}) TO '{d}/taiwan_places.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    con.execute(f"""COPY (SELECT id, lat, lon,
     names.primary AS primary_name,
     names.common['zh'] AS c_zh, names.common['zh-Hant'] AS c_zhhant, names.common['zh-TW'] AS c_zhtw, names.common['zh-Hans'] AS c_zhhans, names.common['en'] AS c_en,
     list_transform(names.rules, r -> r.variant || '|' || coalesce(r.language,'') || '|' || r.value) AS rules,
     brand.names.primary AS brand_primary, brand.names.common['zh'] AS brand_zh, brand.names.common['zh-Hant'] AS brand_zhhant, brand.names.common['zh-TW'] AS brand_zhtw,
     addresses[1].freeform AS addr, addresses[1].locality AS locality, addresses[1].region AS region, addresses[1].postcode AS postcode,
     basic_category, taxonomy.primary AS tax_primary, confidence, operating_status, phones[1] AS phone, websites[1] AS website
     FROM '{d}/raw.parquet') TO '{d}/flat.parquet' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    n_raw = con.execute(f"SELECT count(*) FROM '{d}/raw.parquet'").fetchone()[0]
    n_tw = con.execute(f"SELECT count(*) FROM '{d}/taiwan_places.parquet'").fetchone()[0]
    print(json.dumps({'release': rel, 'raw': n_raw, 'taiwan': n_tw}))


if __name__ == '__main__':
    main()
