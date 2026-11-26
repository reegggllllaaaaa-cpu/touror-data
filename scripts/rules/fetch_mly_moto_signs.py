"""（相容入口）機車標誌（遵20、禁行機車）下載已併入共用下載器 scripts/common/mly_features.py。

  python scripts/rules/fetch_mly_moto_signs.py [download|images|export|all] [--workers 12]
等同 python -I scripts/common/mly_features.py ...（預設 all）；輸出 data/rules/mapillary_moto_signs.csv。
類別（2026-11-26 抽樣臺北忠孝東路、仁愛路實照驗證）：
  regulatory--two-stage-right-turn-for-mopeds--g1 → 台灣「遵20 機慢車兩段左轉」（Mapillary 用日本類別名，圖形相同）
  regulatory--no-motorcycles--g1 / --g2           → 「禁行機車」
Mapillary 沒有「機車待轉區」地面標線類別，不抓。
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'common'))
import mly_features  # noqa: E402

if __name__ == '__main__':
    if len(sys.argv) == 1 or sys.argv[1].startswith('-'):
        sys.argv.insert(1, 'all')
    sys.exit(mly_features.main())
