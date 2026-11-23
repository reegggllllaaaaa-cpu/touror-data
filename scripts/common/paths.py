# -*- coding: utf-8 -*-
"""倉庫內路徑（全部相對於倉庫根目錄，GitHub Actions 乾淨環境與本機都一樣）。"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.join(ROOT, 'scripts')
VENDOR = os.path.join(SCRIPTS, 'vendor')
DATA = os.path.join(ROOT, 'data')
CURATED = os.path.join(DATA, 'curated')
RAW = os.path.join(ROOT, 'raw')
RAW_SPEEDCAMS = os.path.join(RAW, 'speedcams')
RAW_SIGNALS = os.path.join(RAW, 'signals')
RAW_TIMING = os.path.join(RAW, 'timing')
RAW_OSM = os.path.join(RAW, 'osm')
BUILD = os.path.join(ROOT, 'build')
SITE = os.path.join(ROOT, 'site')
MANIFEST = os.path.join(ROOT, 'manifest.json')
OFFLINE_MANIFEST = os.path.join(ROOT, 'offline-manifest.json')   # App 的離線大檔下載清單（遠端版，網站根目錄）
CHANGELOG = os.path.join(ROOT, 'CHANGELOG.md')
