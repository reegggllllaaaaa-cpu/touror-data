# -*- coding: utf-8 -*-
"""測速來源清單：直接讀 vendor 的 建置.py／建置_移動式.py 的 SOURCES，不另抄一份（避免兩邊不一致）。"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import paths  # noqa: E402

VENDOR_SPEEDCAM = os.path.join(paths.VENDOR, '資料', '測速')


def load_vendor_module(name, filename, folder=VENDOR_SPEEDCAM):
    """以檔案路徑載入 vendor 腳本（只執行模組頂層的常數定義，不跑 main）。"""
    spec = importlib.util.spec_from_file_location(name, os.path.join(folder, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_targets():
    """回傳可由 data.gov.tw 資料集 API 更新的檔：dataset 是數字、檔案是 CSV。
    其他（國道 ZIP、高雄 PDF、參考文件）留在 raw/speedcams 固定版本，人工更新。"""
    build = load_vendor_module('touror_speedcam_build', '建置.py')
    out = []
    for src in list(build.SOURCES) + list(getattr(build, 'EXTRA_SOURCES', [])):
        ds = str(src.get('dataset', ''))
        if not ds.isdigit() or not src['file'].lower().endswith('.csv') or src.get('raw_extra'):
            continue
        if src['key'] == 'highway':  # ZIP 內 CSV，要解壓，人工更新
            continue
        out.append(dict(key=src['key'], dataset=ds, file=src['file'], url=src.get('url'), fmt='CSV'))
    try:
        mobile = load_vendor_module('touror_speedcam_mobile', '建置_移動式.py')
        for src in mobile.SOURCES:
            out.append(dict(key='mobile_' + src['key'], dataset=str(src['dataset']),
                            file=os.path.join('移動式', src['file']), fmt='CSV'))
    except Exception as e:  # noqa: BLE001
        print('讀 建置_移動式.py 失敗：%s' % e, file=sys.stderr)
    return out
