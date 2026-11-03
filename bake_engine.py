#!/usr/bin/env python3
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# bake_engine.py - puts the wllama local inference engine (llama.cpp compiled
# to WebAssembly) INSIDE ingenium.html, so the page carries its own engine and
# runs GGUF models entirely offline, with nothing to add by hand and nothing
# fetched while the app runs.
#
#   python3 bake_engine.py <path-to-html> [variant]
#
# The variant argument is accepted for the older build scripts' sake and does
# nothing: every bake carries all four parts (the fast engine for Chromium web
# views, plus the "compat" engine that Safari's WebKit needs).
#
# Where each part comes from, in this order:
#   1. A folder named "engine" beside the HTML holding the file under its
#      offline name (wllama.esm.js, wllama.wasm, wllama.compat.js,
#      wllama.compat.wasm). No network at all.
#   2. Otherwise it is downloaded from the jsDelivr CDN.
#
# Every part, from either source, must match the SHA-256 checksum pinned below
# for wllama 3.6.1, or it is refused. That makes the download safe even on a
# machine whose Python cannot check certificates: a tampered or truncated file
# cannot get into the app.
#
# R64: the two wasm parts are stored gzip-compressed (slot types ending in
# "-gz", with data-bytes giving the unpacked size). The page unpacks them on
# first use. The baked page is about 10 MB instead of 33 MB, which matters in
# Safari and on tablets, where a 33 MB page took about 20 seconds to open.

import base64
import gzip
import hashlib
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

WLLAMA_VER = "3.6.1"
CDN = "https://cdn.jsdelivr.net/npm"

# slot id, slot type, url, offline name, binary, sha256
PARTS = [
    ("ingenium-engine-js", "ingenium/engine",
     "%s/@wllama/wllama@%s/esm/index.min.js" % (CDN, WLLAMA_VER),
     "wllama.esm.js", False,
     "097cc5ef9f12ed17c11b2f19150eda87b196dd47218e7e386cd4809ec44340dd"),
    ("ingenium-engine-wasm", "ingenium/engine-wasm-gz",
     "%s/@wllama/wllama@%s/esm/wasm/wllama.wasm" % (CDN, WLLAMA_VER),
     "wllama.wasm", True,
     "6ca9fdd1b6c03206cd3a04e359b52c8f539896d6c5fb5d36243dded4a689f0ad"),
    ("ingenium-engine-compat-worker", "ingenium/engine-compat-worker",
     "%s/@wllama/wllama-compat@%s/wasm/wllama.js" % (CDN, WLLAMA_VER),
     "wllama.compat.js", False,
     "97f88f7bf26b17ead6be6e7c0c39d20e21661e71827eb5e3c9a468667fc249f5"),
    ("ingenium-engine-compat-wasm", "ingenium/engine-compat-wasm-gz",
     "%s/@wllama/wllama-compat@%s/wasm/wllama.wasm" % (CDN, WLLAMA_VER),
     "wllama.compat.wasm", True,
     "3c447fe82f5376b1202f909a414348421fe196ae069e1504010f55c0454f85f4"),
]
SLOT_IDS = [p[0] for p in PARTS]


def _ssl_contexts():
    """System trust store first, then certifi if present, then (only because
    every byte is checked against a pinned checksum) an unverified context."""
    yield ssl.create_default_context()
    try:
        import certifi
        yield ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    yield ctx


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Ingenium-build"})
    last = None
    for ctx in _ssl_contexts():
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=300) as r:
                return r.read()
        except (urllib.error.URLError, ssl.SSLError, OSError) as e:
            last = e
    raise last


def get_part(engine_dir, url, offline_name, sha256):
    local = os.path.join(engine_dir, offline_name)
    if os.path.isfile(local):
        with open(local, "rb") as fh:
            data, source = fh.read(), "engine/%s" % offline_name
    else:
        data, source = download(url), "download"
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise ValueError("%s from %s does not match its pinned checksum (got %s...)"
                         % (offline_name, source, got[:16]))
    return data, source


def esc_js(txt):
    # keep the payload from ever closing its own <script> tag, at any case
    return re.sub(r"</script", "<\\/script", txt, flags=re.I)


def main():
    html_path = sys.argv[1]
    engine_dir = os.path.join(os.path.dirname(os.path.abspath(html_path)), "engine")
    with open(html_path, encoding="utf-8") as fh:
        current = fh.read()
    if "--force" not in sys.argv and all(
            re.search(r'<script id="%s"[^>]*>(?:(?!</script)[\s\S]){100}' % sid, current, flags=re.I) for sid in SLOT_IDS):
        print("The engine is already inside %s; nothing to do. (Use --force to bake it again.)"
              % os.path.basename(html_path))
        return 0
    print("Baking the wllama engine %s into %s" % (WLLAMA_VER, html_path))

    block, labels, missing = "", [], []
    for slot_id, slot_type, url, name, binary, sha in PARTS:
        try:
            data, source = get_part(engine_dir, url, name, sha)
        except Exception as e:
            missing.append((name, url))
            print("  MISSING %-20s (%s)" % (name, str(e)[:120]))
            continue
        if binary:
            payload = base64.b64encode(gzip.compress(data, 9, mtime=0)).decode("ascii")
            block += '<script id="%s" type="%s" data-bytes="%d">%s</script>' % (
                slot_id, slot_type, len(data), payload)
        else:
            block += '<script id="%s" type="%s">%s</script>' % (
                slot_id, slot_type, esc_js(data.decode("utf-8")))
        labels.append(name)
        print("  baked %-20s from %s (checksum ok)" % (name, source))

    if len(missing) == len(PARTS) or "wllama.esm.js" in [m[0] for m in missing]:
        print("\nEngine bake SKIPPED: the engine could not be obtained. Nothing was changed.")
        _print_manual(missing)
        return 1

    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    # remove any previously baked engine (re-running is safe)
    for sid in SLOT_IDS:
        html = re.sub(r'\n?<script id="%s"[\s\S]*?</script>' % sid, "", html, flags=re.I)
    # function replacement so the payload's own backslashes stay literal
    new_html, n = re.subn(r"(<body[^>]*>)", lambda m: m.group(1) + "\n" + block,
                          html, count=1, flags=re.I)
    if n != 1:
        print("Engine bake SKIPPED: no <body> tag found. Nothing was changed.")
        return 1
    if not new_html.rstrip().endswith("</html>"):
        print("Engine bake SKIPPED: the result no longer ends with </html>. Nothing was changed.")
        return 1

    tmp = html_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new_html)
    with open(tmp, encoding="utf-8") as fh:
        if fh.read() != new_html:
            os.remove(tmp)
            print("Engine bake SKIPPED: the written file did not read back the same.")
            return 1
    os.replace(tmp, html_path)
    print("\nEngine baked: %s. The page is now %.1f MB." % (", ".join(labels), len(new_html) / 1e6))
    if missing:
        _print_manual(missing)
    return 0


def _print_manual(missing):
    if not missing:
        return
    print("\nTo add a missing part with no network trouble at all:")
    print("  1. Make a folder named 'engine' beside ingenium.html.")
    print("  2. Open each address below in a web browser and save the file into")
    print("     that folder under the name shown.")
    for name, url in missing:
        print("       %-20s <-  %s" % (name, url))
    print("  3. Run the bake again.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("Engine bake SKIPPED: %s" % e)
        sys.exit(1)
