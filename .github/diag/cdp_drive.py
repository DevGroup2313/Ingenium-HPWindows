# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# 25 October 2026: the owner's words, "the app's page keeps on saying Not responding and crashes the whole page", on both
# HP laptops. This drives the app's own page through its web view's remote debugging port (a check only) and measures how
# long the page itself stays busy: how quickly it answers, sampled four times a second, while it starts, while each of its
# pages is opened in turn, and while a large file (as a kit or a backup is) is handed to the app through its bridge.
# hang_watch.ps1, beside it, records at the same time whether Windows marks the window "Not responding".
#   python .github/diag/cdp_drive.py <port> <out.json> [app name]
import json, sys, time, urllib.request
import websocket  # websocket-client

port, out = int(sys.argv[1]), sys.argv[2]
app = sys.argv[3] if len(sys.argv) > 3 else "app"
res = {"app": app, "phases": []}


def target():
    for _ in range(180):
        try:
            for t in json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=5)):
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl") and not t.get("url", "").startswith("devtools"):
                    return t
        except Exception:
            pass
        time.sleep(1)
    return None


t0 = time.time()
tg = target()
if not tg:
    res["error"] = "no page target on port %d within 180 s" % port
    json.dump(res, open(out, "w"), indent=1)
    print(res["error"])
    sys.exit(0)
res["found_after_s"] = round(time.time() - t0, 1)
ws = websocket.create_connection(tg["webSocketDebuggerUrl"], timeout=120, suppress_origin=True)
seq = [0]


def call(method, params=None, timeout=120):
    seq[0] += 1
    i = seq[0]
    ws.settimeout(timeout)
    ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == i:
            return m


def ev(expr, timeout=120):
    """Evaluate in the page; return (value, seconds the answer took)."""
    a = time.time()
    try:
        m = call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True, "timeout": int(timeout * 1000)}, timeout)
        v = m.get("result", {}).get("result", {}).get("value")
    except Exception as e:
        v = {"error": str(e)[:200]}
    return v, time.time() - a


def sample(name, seconds, then=None):
    """How long the page takes to answer '1', four times a second, for a while (after doing `then`, if given)."""
    ph = {"name": name}
    if then:
        v, took = ev(then, 300)
        ph["action_s"] = round(took, 2)
        ph["action"] = v if isinstance(v, (str, int, float, bool, type(None))) else json.dumps(v)[:300]
    lat, end = [], time.time() + seconds
    while time.time() < end:
        _, took = ev("1", 60)
        lat.append(took)
        time.sleep(max(0, 0.25 - took))
    lat_ms = sorted(round(x * 1000) for x in lat)
    ph.update({"samples": len(lat_ms), "median_ms": lat_ms[len(lat_ms) // 2] if lat_ms else None, "worst_ms": lat_ms[-1] if lat_ms else None,
               "over_200ms": sum(1 for x in lat_ms if x > 200), "over_1s": sum(1 for x in lat_ms if x > 1000), "over_5s": sum(1 for x in lat_ms if x > 5000)})
    res["phases"].append(ph)
    print(json.dumps(ph))
    json.dump(res, open(out, "w"), indent=1)
    return ph


# 1. starting: until the page says it is ready (or two minutes), sampled
# the Forge marks itself ready on its body (data-forge-ready="1"), as its own app reads it; window.__forgeReady is never set
ready_expr = "!!(window.__ingReady===true||window.__forgeReady===true||document.body&&document.body.getAttribute('data-forge-ready')==='1'||document.readyState==='complete'&&window.__INGENIUM_BOOTED)"
a = time.time()
lat = []
while time.time() - a < 120:
    v, took = ev(ready_expr, 60)
    lat.append(took)
    if v is True:
        break
    time.sleep(max(0, 0.25 - took))
res["ready_after_s"] = round(time.time() - t0, 1)
res["ready"] = v is True
lat_ms = sorted(round(x * 1000) for x in lat)
res["phases"].append({"name": "starting", "samples": len(lat_ms), "median_ms": lat_ms[len(lat_ms) // 2] if lat_ms else None,
                      "worst_ms": lat_ms[-1] if lat_ms else None, "over_1s": sum(1 for x in lat_ms if x > 1000)})
print("ready:", res["ready"], "after", res["ready_after_s"], "s")
ev("(function(){var b=[...document.querySelectorAll('button')].find(function(x){return x.textContent.trim()==='Enter'&&x.offsetParent});if(b)b.click();return !!b;})()")

# 2. a minute at rest, as a person reads (what the page does by itself after starting)
sample("at rest, the first minute", 60)

# 3. each of its pages, opened in turn
pages, _ = ev("(function(){return [...document.querySelectorAll('section.section[id^=\"page-\"]')].map(function(s){return s.id.slice(5);});})()")
res["pages"] = pages
for pg in (pages or [])[:24]:
    sample("open " + pg, 3, "(function(){var t=performance.now();try{go(%s);}catch(e){return 'go failed: '+e.message;}return Math.round(performance.now()-t)+' ms in go()';})()" % json.dumps(pg))

# 4. a large file handed to the app through its bridge, as a kit or a backup is (30 MB, held in memory by the app, never
# written): the window must keep answering while it goes
big = ("(function(){var api=window.pywebview&&window.pywebview.api;if(!api||!api.put_engine_part)return 'no bridge call to try';"
       "var s='QUJD'.repeat(7864320);var t=performance.now();return api.put_engine_part('diag_payload',s).then(function(r){"
       "return 'handed over '+(s.length/1048576).toFixed(0)+' MB of text in '+Math.round(performance.now()-t)+' ms: '+r;});})()")
sample("a 30 MB hand-over through the bridge, and after", 20, big)

# 5. Home again, and a last look
sample("Home again", 10, "(function(){try{go('home');}catch(e){}return 'home';})()")
res["total_s"] = round(time.time() - t0, 1)
json.dump(res, open(out, "w"), indent=1)
ws.close()
print("done in", res["total_s"], "s")
