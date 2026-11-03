# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# R78: "Train a small model from zero", tested through the app's own code on
# the build machine: a vocabulary learned from text, a new model with random
# numbers, two passes of llama.cpp's trainer, then the app's engine loads the
# trained model and continues a sentence. Prints a summary in Markdown.
#
#   python tools/train_smoke.py
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ingenium_app as a  # noqa: E402


def say(t):
    print(t, flush=True)


def main():
    av = a._train_available()
    say("* Trainer in the app's engine folder: %s" % ("yes" if av.get("trainer") else "NO"))
    if not av.get("trainer"):
        return 1
    parts = []
    for f in ("LICENCE.txt", "THIRD_PARTY_NOTICES.txt", "README.md", "WINDOWS_README.txt", "ENGINE.txt"):
        p = os.path.join(ROOT, f)
        if os.path.isfile(p):
            with open(p, encoding="utf-8", errors="replace") as fh:
                parts.append(fh.read())
    text = "\n\n".join(parts)
    while len(text) < 20000:
        text += "\n\n" + text
    t0 = time.time()
    r = a._train_start({"name": "Build test", "text": text, "size": "tiny", "passes": 2})
    if not r.get("ok"):
        say("* Training could not start: %s" % r.get("error"))
        return 1
    st = {}
    while time.time() - t0 < 900:
        time.sleep(2)
        st = a._train_status()
        if st.get("state") != "running":
            break
    secs = time.time() - t0
    hist = st.get("history") or []
    say("* Training: %s in %.0f s on %d characters (%s numbers); loss after each pass: %s" % (
        st.get("state"), secs, len(text), st.get("params"),
        ", ".join("%.2f (held back %.2f)" % (h.get("loss") or 0, h.get("val") or 0) for h in hist) or "none"))
    if st.get("state") != "done":
        say("* The trainer said: %s" % (st.get("error") or "nothing"))
        return 1
    m = st.get("model") or {}
    res = a._engine_start(m.get("id"), 2048, None)
    if not res or not res.get("ok"):
        say("* The engine could not load the trained model: %s" % (res or {}).get("error"))
        return 1
    es = {}
    for _ in range(120):
        time.sleep(1)
        es = a._engine_status()
        if es.get("state") in ("ready", "error", "off"):
            break
    if es.get("state") != "ready":
        say("* The engine did not get ready with the trained model: %s" % es.get("error"))
        a._engine_stop()
        return 1
    req = urllib.request.Request("http://127.0.0.1:%s/completion" % es["port"],
                                 data=json.dumps({"prompt": "The app", "n_predict": 30, "temperature": 0.7}).encode(),
                                 headers={"content-type": "application/json", "authorization": "Bearer " + (es.get("key") or "")})
    try:
        out = json.load(a._NO_PROXY.open(req, timeout=120)).get("content") or ""
    finally:
        a._engine_stop()
    say("* The trained model continues \"The app\": %s" % json.dumps(out[:160]))
    return 0 if out.strip() else 1


if __name__ == "__main__":
    sys.exit(main())
