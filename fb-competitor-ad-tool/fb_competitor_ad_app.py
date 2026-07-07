# -*- coding: utf-8 -*-
"""
Streamlit Cloud / 本地入口 — 启动 NuageWears 工具组。
业务逻辑在 fb_competitor_ad_core.py 与 marketing_suite_app.py。
"""

from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
_FB_DIR = Path(__file__).resolve().parent
for path in (_ROOT, _FB_DIR):
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)

from marketing_suite_app import main

main()
