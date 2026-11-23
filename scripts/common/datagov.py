# -*- coding: utf-8 -*-
"""政府資料開放平臺（data.gov.tw）下載工具（只用 Python 標準庫）。

不寫死下載網址：每次先讀資料集 API（/api/v2/rest/dataset/<id>）拿最新的 resourceDownloadUrl，
再依「已知網址 → 格式＋關鍵字 → 唯一一個該格式」的順序挑資源。挑不到就保留快取並回報，不亂猜。

禮貌與容錯規則：
- 對 data.gov.tw 資料集 API 的請求間隔至少 MIN_INTERVAL 秒（預設 40 秒，可用環境變數
  TOUROR_FETCH_INTERVAL 調整；測試用 0）。下載實際檔案（不同主機）不套用此間隔。
- 每個來源重試 1 次（間隔 30 秒）；單一來源的整個 GET 過程（含 API 與下載、含重試）
  總時間上限 MAX_GET_SECONDS 秒，防慢速滴流占滿排程。
- 任一來源失敗或逾時，只要本地已有舊檔就保留並標「沿用舊檔」，整體不失敗。
- User-Agent 用通用名稱，不帶任何個人資訊；不需要金鑰的來源才用。
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = 'https://data.gov.tw/api/v2/rest/dataset/%s'
USER_AGENT = 'touror-data-builder/1.0 (+open data pipeline)'
MIN_INTERVAL = float(os.environ.get('TOUROR_FETCH_INTERVAL', '40'))
RETRY_WAITS = (30,)
MAX_GET_SECONDS = 120
_last_api_request = [0.0]


def _polite_wait():
    """對 data.gov.tw API 請求前的間隔（下載實際檔案不套用）。"""
    gap = time.time() - _last_api_request[0]
    if _last_api_request[0] and gap < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - gap)
    _last_api_request[0] = time.time()


class _Deadline:
    def __init__(self, seconds):
        self.deadline = time.time() + seconds if seconds else None

    def remaining(self):
        if self.deadline is None:
            return None
        r = self.deadline - time.time()
        return r if r > 0 else 0.0

    def check(self):
        if self.deadline and time.time() >= self.deadline:
            raise TimeoutError('單一來源總時間逾時（約 %d 秒）' % int(MAX_GET_SECONDS))


def http_get(url, timeout=120, retries=RETRY_WAITS, opener=None, api_call=False, max_seconds=MAX_GET_SECONDS):
    """GET 一個網址，回傳 bytes。opener 可替換（測試用）。

    api_call=True 時套用對 data.gov.tw API 的禮貌間隔；下載檔案設 False。
    max_seconds 限制整個 GET（含重試）的總耗時，防慢速滴流。
    """
    opener = opener or urllib.request.urlopen
    deadline = _Deadline(max_seconds)
    last = None
    for attempt in range(len(retries) + 1):
        if api_call:
            _polite_wait()
        else:
            # 下載檔案與 API 不同主機，不重啟 API 間隔計時
            pass
        deadline.check()
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            socket_timeout = min(timeout, deadline.remaining()) if deadline.remaining() is not None else timeout
            with opener(req, timeout=socket_timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < len(retries):
                wait = retries[attempt] if MIN_INTERVAL > 0 else 0
                remain = deadline.remaining()
                if remain is not None:
                    wait = min(wait, remain)
                if wait > 0:
                    print('  重試 %d：%s（%s）' % (attempt + 1, url, e), file=sys.stderr)
                    time.sleep(wait)
    raise RuntimeError('下載失敗：%s（%s）' % (url, last))


_info_cache = {}


def dataset_info(dataset_id, getter=http_get, use_cache=True):
    """讀資料集 API，回傳 result（含 distribution、modifiedDate、updateFrequency）。同一次執行同一資料集只讀一次。"""
    key = (str(dataset_id), id(getter))
    if use_cache and key in _info_cache:
        return _info_cache[key]
    raw = getter(API % dataset_id, api_call=True)
    doc = json.loads(raw.decode('utf-8-sig'))
    if not doc.get('success'):
        raise RuntimeError('資料集 %s API 回傳失敗' % dataset_id)
    _info_cache[key] = doc['result']
    return doc['result']


def choose_resource(distribution, known_url=None, fmt=None, match=None):
    """從 distribution 挑一個資源。回傳 (resource, 原因)；挑不到回傳 (None, 原因)。

    known_url：上次用的網址（完全相同就用它）。
    fmt：要的格式（CSV、JSON…，不分大小寫）。
    match：關鍵字（正規式），比對 resourceDescription 或網址。
    """
    items = [d for d in distribution or [] if d.get('resourceDownloadUrl')]
    if known_url:
        for d in items:
            if d['resourceDownloadUrl'] == known_url:
                return d, '網址與上次相同'
    pool = items
    if fmt:
        pool = [d for d in pool if str(d.get('resourceFormat', '')).strip().upper() == fmt.upper()]
    if match:
        rx = re.compile(match)
        hit = [d for d in pool if rx.search(str(d.get('resourceDescription', ''))) or rx.search(d['resourceDownloadUrl'])]
        if len(hit) == 1:
            return hit[0], '關鍵字 %s' % match
        if len(hit) > 1:
            return None, '關鍵字 %s 符合 %d 個資源，無法決定' % (match, len(hit))
        return None, '沒有資源符合關鍵字 %s' % match
    if len(pool) == 1:
        return pool[0], '唯一一個 %s 資源' % (fmt or '')
    return None, '%s 資源有 %d 個，無法決定' % (fmt or '全部', len(pool))


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def fetch_dataset(dataset_id, dest, known_url=None, fmt='CSV', match=None, getter=http_get, min_bytes=16):
    """抓資料集最新檔案寫到 dest（原子替換）。回傳狀態 dict：
    status＝updated／unchanged／kept（挑不到資源或下載失敗，保留舊檔）。"""
    rec = dict(dataset=str(dataset_id), file=os.path.basename(dest))
    try:
        info = dataset_info(dataset_id, getter)
    except Exception as e:  # noqa: BLE001
        if os.path.exists(dest):
            rec.update(status='kept', reason='讀 API 失敗，沿用舊檔：%s' % e)
        else:
            rec.update(status='failed', reason='讀 API 失敗且無舊檔：%s' % e)
        return rec
    rec['modified'] = info.get('modifiedDate', '')
    res, why = choose_resource(info.get('distribution'), known_url, fmt, match)
    if res is None:
        if os.path.exists(dest):
            rec.update(status='kept', reason=why + '，沿用舊檔')
        else:
            rec.update(status='failed', reason=why + '且無舊檔')
        return rec
    url = res['resourceDownloadUrl']
    rec.update(url=url, choose=why)
    try:
        body = getter(url, api_call=False)
    except Exception as e:  # noqa: BLE001
        if os.path.exists(dest):
            rec.update(status='kept', reason='下載失敗，沿用舊檔：%s' % e)
        else:
            rec.update(status='failed', reason='下載失敗且無舊檔：%s' % e)
        return rec
    if len(body) < min_bytes:
        if os.path.exists(dest):
            rec.update(status='kept', reason='檔案太小（%d bytes），沿用舊檔' % len(body))
        else:
            rec.update(status='failed', reason='檔案太小（%d bytes）且無舊檔' % len(body))
        return rec
    new_hash = sha256_bytes(body)
    old_hash = sha256_bytes(open(dest, 'rb').read()) if os.path.exists(dest) else ''
    rec.update(sha256=new_hash, bytes=len(body))
    if new_hash == old_hash:
        rec['status'] = 'unchanged'
        return rec
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + '.part'
    with open(tmp, 'wb') as f:
        f.write(body)
    os.replace(tmp, dest)
    rec['status'] = 'updated'
    return rec
