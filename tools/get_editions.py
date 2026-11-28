# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# 7.45: the Full and Gift editions' pages for the Windows build, so each can be
# installed as an app of its own (her words, 19 November 2026: "I want to be
# able to download and install the Gift and Full editions of Ingenium on my HP
# Windows computer and other devices").
#
# The editions are made with each release in the Mac mini repository's
# editions/ (source/build/release.py editions). This reads them from that
# repository at the same branch, and keeps each only when
#   * editions/README.txt names the same version as this repository's page, and
#   * the page's SHA-256 is the one that README lists for it.
# The README is pushed with the editions, a moment after this repository's
# page, so it is asked again for up to 15 minutes before the build gives up.
#   python tools/get_editions.py <out folder> <branch> [<owner>]
import hashlib
import os
import re
import sys
import time
import urllib.request

out, branch = sys.argv[1], sys.argv[2]
owner = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("GITHUB_REPOSITORY_OWNER", "DevGroup2313")
base = "https://raw.githubusercontent.com/%s/Ingenium-Mac-Mini/%s/editions/" % (owner, branch)
EDITIONS = ("full", "gift", "demo")  # 8.11: and the Demo edition

page = open("ingenium.html", encoding="utf-8").read()
m = re.search(r'var ING_VERSION="([0-9.]+)"', page)
if not m:
    sys.exit("::error::this repository's page names no version")
want = m.group(1)


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "ingenium-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


sums, readme = {}, ""
for attempt in range(31):
    try:
        readme = get(base + "README.txt?t=%d" % time.time())
        first = readme.splitlines()[0] if readme else ""
        if first.startswith("Ingenium %s " % want):
            sums = {n: s for s, n in re.findall(r"^\s*([0-9a-f]{64})\s+(ingenium_\w+\.html)", readme, re.M)}
            if all("ingenium_%s.html" % e in sums for e in EDITIONS):
                break
        print("the editions' README names %r, not version %s yet; asking again in 30 s" % (first[:60], want))
    except Exception as e:
        print("the editions' README could not be read (%s); asking again in 30 s" % e)
    time.sleep(30)
else:
    sys.exit("::error::the Mac mini repository has no editions of version %s on %s" % (want, branch))

os.makedirs(out, exist_ok=True)
for e in EDITIONS:
    n = "ingenium_%s.html" % e
    data = b""
    for attempt in range(4):
        try:
            data = get(base + n, binary=True)
            if hashlib.sha256(data).hexdigest() == sums[n]:
                break
        except Exception as ex:
            print("%s could not be read (%s)" % (n, ex))
        time.sleep(20)
    got = hashlib.sha256(data).hexdigest()
    if got != sums[n]:
        sys.exit("::error::%s has the fingerprint %s, not the %s its README lists" % (n, got, sums[n]))
    v = re.search(rb'var ING_VERSION="([0-9.]+)"', data)
    if not v or v.group(1).decode() != want:
        sys.exit("::error::%s is not version %s" % (n, want))
    open(os.path.join(out, n), "wb").write(data)
    print("%s: version %s, %d bytes, fingerprint %s (as its README lists)" % (n, want, len(data), got[:16]))
