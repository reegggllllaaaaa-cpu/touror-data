# -*- coding: utf-8 -*-
"""（相容入口）Mapillary 號誌燈頭偵測下載已併入共用下載器 scripts/common/mly_features.py。

  python -I scripts/signals/fetch_mly_signals.py [download|images|export|all] [--workers 12]
等同 python -I scripts/common/mly_features.py ...（預設 all）。號誌與機車標誌一次下載、可續跑。
"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'common'))
import mly_features  # noqa: E402

if __name__ == '__main__':
    if len(sys.argv) == 1 or sys.argv[1].startswith('-'):
        sys.argv.insert(1, 'all')
    sys.exit(mly_features.main())
