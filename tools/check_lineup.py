#!/usr/bin/env python3
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# Checks that the models an app is built with (mobile_models.json, or the
# desktop apps' desktop_model.json) are the ones Ingenium's lineup in the page
# names (ING_LINEUP in ingenium.html): the same file, size, SHA-256 and address.
# The page uses the lineup to put each built-in model in its house and to
# install the others, so the two must agree. Run by the cloud builds:
#   python3 tools/check_lineup.py
# A model the person chose themselves (one the lineup does not name, and with
# no "lineup" key) is built in as it is; only the lineup's own models must agree.
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def lineup(page):
    text = open(page, encoding="utf-8").read()
    start = text.find("var ING_LINEUP={")
    if start < 0:
        return None
    block = text[start:text.find("devices:{", start)]
    out = {}
    for m in re.finditer(r'file:"([^"]+)",size:(\d+),sha256:"([0-9a-f]{64})",\s*url:"([^"]+)"', block):
        out[m.group(1)] = {"size": int(m.group(2)), "sha256": m.group(3), "url": m.group(4)}
    return out


def built_in():
    found = []
    mm = os.path.join(ROOT, "mobile_models.json")
    if os.path.isfile(mm):
        for plat, v in json.load(open(mm, encoding="utf-8")).items():
            if plat.startswith("_") or not v:
                continue
            found += [(plat, m) for m in (v if isinstance(v, list) else [v])]
    dm = os.path.join(ROOT, "desktop_model.json")
    if os.path.isfile(dm):
        d = json.load(open(dm, encoding="utf-8"))
        for plat, v in (d.get("models") or {}).items():
            found += [(plat, m) for m in (v or [])]
        if d.get("model"):
            found.append(("desktop", d["model"]))
    return found


def main():
    page = os.path.join(ROOT, "ingenium.html")
    lu = lineup(page)
    models = built_in()
    lu = lu or {}
    bad = own = 0
    for plat, m in models:
        want = lu.get(m["file"])
        if not want:
            if m.get("lineup"):
                print("%s: %s says it belongs to the lineup (%s), but the page's lineup does not name it" % (plat, m["file"], m["lineup"]))
                bad += 1
            else:
                print("%s: %s is a model of your own choosing, built in as it is" % (plat, m["file"]))
                own += 1
            continue
        for k in ("size", "sha256", "url"):
            if want[k] != m[k]:
                print("%s: %s has %s %r in the build file and %r in the page" % (plat, m["file"], k, m[k], want[k]))
                bad += 1
    if bad:
        sys.exit("the built-in models and the page's lineup disagree (%d difference%s)" % (bad, "" if bad == 1 else "s"))
    n = len(models) - own
    print("Lineup checked: %d built-in model%s agree with the page (%d models in the lineup)%s." % (n, "" if n == 1 else "s", len(lu), (", and %d of your own" % own) if own else ""))


if __name__ == "__main__":
    main()
