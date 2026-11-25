"""抓 7-ELEVEN（emap.pcsc.com.tw 公開 SDK）與 OK mart（okmart.com.tw 門市查詢）官方門市名冊。
無金鑰、單執行緒、每次請求間隔 0.7 秒，只抓公開頁面；輸出 UTF-8 CSV 到 候選-分店名/名冊/。
用法：python -I fetch_chain_rosters.py [--out DIR]
"""
import argparse, csv, datetime, html, http.cookiejar, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120 touror-roster-fetch'
CITIES = ['台北市', '基隆市', '新北市', '宜蘭縣', '新竹縣', '新竹市', '桃園市', '苗栗縣', '台中市', '彰化縣', '南投縣',
          '雲林縣', '嘉義縣', '嘉義市', '台南市', '高雄市', '屏東縣', '台東縣', '花蓮縣', '澎湖縣', '金門縣', '連江縣']
DELAY = 0.7
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def req(url, data=None, headers=None, tries=3):
    for k in range(tries):
        try:
            time.sleep(DELAY)
            r = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None,
                                       headers=dict({'User-Agent': UA}, **(headers or {})))
            return op.open(r, timeout=40).read().decode('utf-8', 'replace')
        except Exception as e:
            err = e; time.sleep(3 * (k + 1))
    raise err

def fetch_711(out):
    rows = {}
    miss = []
    import importlib.util, collections
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location('build_addr', os.path.join(here, 'build_addr.py'))
    ba = importlib.util.module_from_spec(spec); sys.modules['build_addr'] = ba; spec.loader.exec_module(ba)
    pk = ba.AddrPack(os.environ.get('TOUROR_ADDR') or os.path.join(here, 'touror_addr.bin'))  # CI：TOUROR_ADDR 指向線上門牌包
    city_of = collections.defaultdict(set)
    for c, d, _, _ in pk.dists: city_of[d].add(c)
    for i in range(1, 31):  # emap 的 cityid 順序與縣市表不同：用鄉鎮名對門牌包的行政區反推縣市
        tx = ET.fromstring(req('https://emap.pcsc.com.tw/EMapSDK.aspx', {'commandid': 'GetTown', 'cityid': '%02d' % i}).encode('utf-8'))
        towns = [g.findtext('TownName') for g in tx.iter('GeoPosition')]
        if not towns: continue
        votes = collections.Counter(c for t in towns for c in city_of.get(t.replace('臺', '台'), ()))
        top = votes.most_common(1)[0][1]; cands = [c for c, v in votes.items() if v == top]
        for town in towns:
            n = 0
            for city in cands:
                xml = req('https://emap.pcsc.com.tw/EMapSDK.aspx', {'commandid': 'SearchStore', 'city': city, 'town': town,
                          'roadname': '', 'ID': '', 'StoreName': '', 'SpecialStore_Kind': '', 'leftMenuChecked': '', 'address': ''})
                for g in ET.fromstring(xml.encode('utf-8')).iter('GeoPosition'):
                    pid = (g.findtext('POIID') or '').strip()
                    if not pid: continue
                    n += 1
                    rows[pid] = (pid, (g.findtext('POIName') or '').strip(), (g.findtext('Address') or '').strip(),
                                 float(g.findtext('Y')) / 1e6, float(g.findtext('X')) / 1e6, city, town)
                pass
            if n == 0: miss.append((cands, town))
        print('7-11', cands, len(towns), '區，累計', len(rows), flush=True)
    with open(os.path.join(out, '7-11_emap.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['id', 'name', 'address', 'lat', 'lon', 'city', 'town']); w.writerows(rows.values())
    return len(rows), miss

def fetch_ok(out):
    ref = {'Referer': 'https://www.okmart.com.tw/convenient_shopSearch', 'X-Requested-With': 'XMLHttpRequest'}
    req('https://www.okmart.com.tw/convenient_shopSearch')
    rows = {}
    pat = re.compile(r"<h2>(.*?)</h2><span>(.*?)</span>.*?showshop\('(\d+)'", re.S)
    def q(city, area):
        url = 'https://www.okmart.com.tw/convenient_shopSearch_Result.aspx?' + urllib.parse.urlencode(
            {'city': city, 'zipcode': area, 'key': '', 'service': '', 'service2': ''})
        for m in pat.finditer(req(url, headers=ref)):
            rows[m.group(3)] = (m.group(3), html.unescape(m.group(1)).strip(), html.unescape(m.group(2)).strip(), city, area)
    import json
    for city in CITIES:
        q(city, '')
        areas = json.loads(req('https://www.okmart.com.tw/GetZipCode?' + urllib.parse.urlencode({'city': city, 'ajax': 'true'}), headers=ref))
        for a in areas: q(city, a['Area'])
        print('OK', city, len(areas), '區，累計', len(rows), flush=True)
    with open(os.path.join(out, 'OK_okmart.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['id', 'name', 'address', 'city', 'area']); w.writerows(rows.values())
    return len(rows)

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument('--out', default=os.path.join(here, '候選-分店名', '名冊'))
    a = ap.parse_args(); os.makedirs(a.out, exist_ok=True)
    n, miss = fetch_711(a.out)
    k = fetch_ok(a.out)
    with open(os.path.join(a.out, '抓取紀錄.txt'), 'w', encoding='utf-8') as f:
        f.write('抓取日期 %s\n7-ELEVEN emap.pcsc.com.tw EMapSDK.aspx SearchStore：%d 筆；無結果的鄉鎮 %s\nOK mart okmart.com.tw convenient_shopSearch_Result.aspx：%d 筆\n'
                % (datetime.date.today(), n, miss, k))
    print('done', n, k, miss)

