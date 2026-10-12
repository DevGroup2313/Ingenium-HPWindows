#!/usr/bin/env python3
# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# Gets the Windows app's native engine and packs what the app needs:
#   python tools/get_engine_windows.py [engine\windows-x64.zip]
#
# The engine is llama.cpp's own published build for Windows, named in
# engine_windows.json. It is downloaded once (kept in a cache folder, by
# default ~/.cache/ingenium-engine) and refused unless its SHA-256 matches.
# The pack holds llama-server.exe, the libraries beside it, the version and
# llama.cpp's licence; the app unpacks it into its data folder on first use.
import hashlib
import json
import os
import sys
import time
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url, dest):
    tmp = dest + ".part"
    for attempt in range(1, 6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Ingenium-build"})
            with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as out:
                while True:
                    chunk = r.read(1 << 22)
                    if not chunk:
                        break
                    out.write(chunk)
            os.replace(tmp, dest)
            return
        except Exception as e:   # network trouble: wait and try again
            print("  download attempt %d failed: %s" % (attempt, e), flush=True)
            time.sleep(5 * attempt)
    raise SystemExit("could not download %s" % url)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "engine", "windows-x64.zip")
    cfg = json.load(open(os.path.join(ROOT, "engine_windows.json"), encoding="utf-8"))
    cache = os.environ.get("INGENIUM_ENGINE_CACHE", os.path.expanduser(os.path.join("~", ".cache", "ingenium-engine")))
    os.makedirs(cache, exist_ok=True)
    got = os.path.join(cache, cfg["asset"])
    want = cfg.get("sha256")
    if not (os.path.isfile(got) and (not want or sha256_of(got) == want)):
        print("Downloading the engine: llama.cpp %s for Windows (about 40 MB, once)..." % cfg["tag"], flush=True)
        download(cfg["url"], got)
    digest = sha256_of(got)
    if want and digest != want:
        os.remove(got)
        raise SystemExit("the engine download does not match its checksum (got %s); refusing it" % digest)
    if not want:
        print("NOTE: engine_windows.json pins no checksum yet; this download's SHA-256 is %s" % digest, flush=True)
    else:
        print("Engine verified: %s" % cfg["asset"], flush=True)

    with zipfile.ZipFile(got) as src:
        names = src.namelist()
        server = [n for n in names if n.replace("\\", "/").split("/")[-1].lower() == "llama-server.exe"]
        if not server:
            raise SystemExit("llama-server.exe is not in %s" % cfg["asset"])
        base = server[0][: -len("llama-server.exe")]
        keep = []
        for n in names:
            if not n.startswith(base) or n.endswith("/"):
                continue
            rel = n[len(base):]
            if "/" in rel.replace("\\", "/"):
                continue
            low = rel.lower()
            # the server and the libraries it loads; the other tools' parts stay out
            if low.startswith("llama-") and low.endswith("-impl.dll") and low != "llama-server-impl.dll":
                continue
            if low == "llama-server.exe" or low.endswith(".dll") or low.startswith("license"):
                keep.append((n, rel))
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        tmp = out + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as dst:
            total = 0
            for n, rel in sorted(keep, key=lambda x: x[1].lower()):
                data = src.read(n)
                total += len(data)
                dst.writestr(rel, data)
                print("  %-40s %8.1f MB" % (rel, len(data) / 1e6), flush=True)
            dst.writestr("VERSION.txt", "llama.cpp %s\n" % cfg["tag"])
            lic = os.path.join(ROOT, "licences", "MIT-llama.cpp.txt")
            if not any(r.lower() in ("license", "license.txt", "license-llama.cpp.txt") for _, r in keep) and os.path.isfile(lic):
                dst.write(lic, "LICENSE-llama.cpp.txt")
    os.replace(tmp, out)
    with zipfile.ZipFile(out) as z:
        count = len(z.namelist())
    print("Engine packed: %s (%d files, %.0f MB unpacked, %.0f MB packed)"
          % (out, count, total / 1e6, os.path.getsize(out) / 1e6), flush=True)


if __name__ == "__main__":
    main()
