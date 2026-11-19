# -*- coding: utf-8 -*-
"""把開發工作區（專案\\重機導航\\資料、ios\\tools）裡的建置腳本與原始檔，原封不動複製進本倉庫。

為什麼原封不動：建置腳本仍由開發組在工作區修改（測速區間長度、闖紅燈擴充、限速推定…）。
本倉庫在 build/work/ 底下重建與工作區相同的資料夾結構（資料/測速、資料/搜尋、ios/tools…）再執行，
所以腳本裡的相對路徑（HERE、原始\\、..\\搜尋）不用改，工作區改完只要再跑一次本工具。

用法（只在本機開發機用；GitHub Actions 不需要，倉庫內已有複本）：
  python -I scripts/tools/sync_from_workspace.py --workspace "C:\\...\\專案\\重機導航"
  python -I scripts/tools/sync_from_workspace.py --workspace ... --check   # 只比對，不一致結束碼 1
"""
import argparse
import filecmp
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import paths  # noqa: E402

# (工作區相對路徑, 本倉庫目的地)
SCRIPTS = [
    ('資料/測速/建置.py', 'scripts/vendor/資料/測速/建置.py'),
    ('資料/測速/驗證.py', 'scripts/vendor/資料/測速/驗證.py'),
    ('資料/測速/建置_移動式.py', 'scripts/vendor/資料/測速/建置_移動式.py'),
    ('資料/測速/解析_高雄PDF.py', 'scripts/vendor/資料/測速/解析_高雄PDF.py'),
    ('資料/紅綠燈/build_signals.py', 'scripts/vendor/資料/紅綠燈/build_signals.py'),
    ('資料/休息點/build_rest_stops.py', 'scripts/vendor/資料/休息點/build_rest_stops.py'),
    ('ios/tools/pack_speedcams.py', 'scripts/vendor/ios/tools/pack_speedcams.py'),
]
# 人工建檔（區間修正）：倉庫內以 data/curated/ 為正本
CURATED = [
    ('資料/測速/區間修正.json', 'data/curated/section-corrections.json'),
]
# 原始檔資料夾（測速原始檔小，進倉庫；含非 data.gov.tw 的 PDF 與參考文件，每日只更新 API 解析得到的那些）
RAW_DIRS = [
    ('資料/測速/原始', 'raw/speedcams'),
]


def plan(workspace):
    items = []
    for src, dst in SCRIPTS + CURATED:
        items.append((os.path.join(workspace, src), os.path.join(paths.ROOT, dst)))
    for src_dir, dst_dir in RAW_DIRS:
        base = os.path.join(workspace, src_dir)
        for dirpath, _, files in os.walk(base):
            for fn in files:
                if fn.endswith(('.part', '.tmp')):
                    continue
                s = os.path.join(dirpath, fn)
                rel = os.path.relpath(s, base)
                items.append((s, os.path.join(paths.ROOT, dst_dir, rel)))
    return items


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--workspace', required=True, help='專案\\重機導航 資料夾')
    ap.add_argument('--check', action='store_true')
    a = ap.parse_args(argv)
    diffs = []
    for s, d in plan(a.workspace):
        if not os.path.exists(s):
            print('缺少來源：%s' % s)
            diffs.append(s)
            continue
        same = os.path.exists(d) and filecmp.cmp(s, d, shallow=False)
        if same:
            continue
        diffs.append(d)
        if a.check:
            print('不同：%s' % os.path.relpath(d, paths.ROOT))
        else:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copyfile(s, d)
            print('更新：%s' % os.path.relpath(d, paths.ROOT))
    print('共 %d 個檔%s' % (len(diffs), '不同' if a.check else '更新'))
    return 1 if (a.check and diffs) else 0


if __name__ == '__main__':
    sys.exit(main())
