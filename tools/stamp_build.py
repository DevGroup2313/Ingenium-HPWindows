#!/usr/bin/env python3
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# Stamps the build number into the page (<meta name="ingenium-build">), so the
# app shows which build it is in About, on the welcome card and in the footer.
# The cloud build runs it just before it builds the app; a page that was not
# stamped shows the date it was written instead.
#   python3 tools/stamp_build.py ingenium.html 2.1.20261002.7
import re
import sys

if len(sys.argv) != 3:
    sys.exit("usage: stamp_build.py <page> <build number, for example 2.1.20261002.7>")
page, build = sys.argv[1], sys.argv[2]
if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.\-]{0,39}", build):
    sys.exit("the build number takes letters, digits, dots and dashes, for example 2.1.20261002.7")
with open(page, encoding="utf-8", newline="") as f:
    html = f.read()
html, n = re.subn(r'<meta name="ingenium-build" content="[^"]*">', '<meta name="ingenium-build" content="%s">' % build, html, count=1)
if n != 1:
    sys.exit("the page has no build-number tag; nothing was changed")
with open(page, "w", encoding="utf-8", newline="") as f:
    f.write(html)
print("%s: build %s" % (page, build))
