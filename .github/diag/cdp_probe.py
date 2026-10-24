# INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
# NO LICENCE IS GRANTED. See LICENCE.txt.
#
# 24 October 2026: what Ingenium's window holds, asked of its web view (Edge WebView2 started with
# --remote-debugging-port, a check only): every page it has, how far each got, which of the page's own parts started and
# never finished, the last error it kept, and a picture of it described in words (its colours).
#   python .github/diag/cdp_probe.py <port> <picture.png>
import base64, json, sys, time, urllib.request
import websocket  # websocket-client

port, shot = int(sys.argv[1]), sys.argv[2]
targets = []
for _ in range(60):
    try:
        targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=5))
        break
    except Exception as e:
        time.sleep(1)
print("targets:", json.dumps([{k: t.get(k) for k in ("type", "title", "url")} for t in targets], indent=1)[:3000])
EXPR = r"""(function(){var o={};try{o.href=location.href;o.ready=document.readyState;o.title=document.title;
o.bytes=document.documentElement?document.documentElement.outerHTML.length:-1;o.body=document.body?document.body.children.length:-1;
o.ingReady=window.__ingReady===true;o.booted=!!window.__INGENIUM_BOOTED;o.lastError=String(window.__INGENIUM_LAST_ERROR||'');
var p=window.__ingParts||{},st=[];for(var k in p)if(p[k]!=='running'&&p[k]!=='done')st.push(k+':'+p[k]);o.partsNotRunning=st;o.parts=Object.keys(p).length;
var n=performance.getEntriesByType('navigation')[0];if(n)o.nav={responseEnd:Math.round(n.responseEnd),dcl:Math.round(n.domContentLoadedEventEnd),load:Math.round(n.loadEventEnd),size:n.transferSize,decoded:n.decodedBodySize};
o.now=Math.round(performance.now());o.scripts=document.scripts.length;o.pywebview=!!window.pywebview;
var r=performance.getEntriesByType('resource');o.resources=r.slice(-15).map(function(x){return x.name.slice(0,90)+' '+Math.round(x.duration)+'ms';});
var lp=document.getElementById('ing-painting');o.loadingPage=lp?{shown:getComputedStyle(lp).display,text:(lp.textContent||'').replace(/\s+/g,' ').slice(0,200)}:null;
o.errors=(window.__ingErrLog||window.__ingErrors||[]).slice?((window.__ingErrLog||window.__ingErrors||[]).slice(-8)):null;
o.visibleText=(document.body?document.body.innerText:'').replace(/\s+/g,' ').slice(0,400);}catch(e){o.probeError=String(e);}return JSON.stringify(o);})()"""
for t in targets:
    if t.get("type") != "page" or not t.get("webSocketDebuggerUrl"):
        continue
    ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
    def call(i, method, params=None):
        ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == i:
                return m
    r = call(1, "Runtime.evaluate", {"expression": EXPR, "returnByValue": True, "timeout": 20000})
    print("page", t.get("url"), "->", json.dumps(json.loads(r.get("result", {}).get("result", {}).get("value") or "{}"), indent=1)[:6000] if r.get("result", {}).get("result", {}).get("value") else r)
    try:
        s = call(2, "Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(s["result"]["data"])
        open(shot, "wb").write(data)
        from PIL import Image
        im = Image.open(shot).convert("RGB")
        w, h = im.size
        small = im.resize((32, 20))
        cols = {}
        for px in small.getdata():
            q = tuple(c // 32 * 32 for c in px)
            cols[q] = cols.get(q, 0) + 1
        top = sorted(cols.items(), key=lambda kv: -kv[1])[:6]
        print("picture: %dx%d; commonest colours (of 640 cells): %s" % (w, h, ", ".join("#%02x%02x%02x x%d" % (c[0], c[1], c[2], n) for c, n in top)))
    except Exception as e:
        print("picture: none (%s)" % e)
    ws.close()
