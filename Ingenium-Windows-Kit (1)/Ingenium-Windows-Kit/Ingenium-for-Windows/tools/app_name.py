# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# Prints one field of app.json, cleaned the way the app cleans it, for the
# build file:  python tools/app_name.py file_name|name|app_id
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    with open(os.path.join(ROOT, "app.json"), encoding="utf-8") as fh:
        c = json.load(fh)
except Exception:
    c = {}
field = sys.argv[1] if len(sys.argv) > 1 else "file_name"
if field == "name":
    print(re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ._-]", "", str(c.get("name") or ""))).strip() or "Ingenium")
elif field == "app_id":
    print(str(c.get("app_id") or "com.yannieforest.ingenium"))
else:
    print(re.sub(r"[^A-Za-z0-9._-]", "", str(c.get("file_name") or "")) or "Ingenium")
