// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's clock, phase 4 (rag/DESIGN.md section 11). Her task: "Use setInterval in a Worker. Default interval 20 minutes,
// configurable. It runs only while the app is open ... Do not use continuous file watchers." This worker keeps time and nothing else:
// it never touches a file or the index. The page (rag/scheduler.js, rag/main.js) does the reading when it hears a tick.
//
// Messages (one protocol, as every worker of the folder search):
//   request {id, op, args}   reply {id, ok: true, result} | {id, ok: false, error}
//   ops     start({everyMs, firstMs?})   a tick every everyMs (at least 1,000), and one at firstMs first when it is sooner
//           stop()                       no more ticks
//           state()                      -> {everyMs, next, ticks}
//   events  {event: 'tick', at, n}

let timer = null, first = null, everyMs = 0, next = 0, ticks = 0;

function tick() {
  ticks++;
  next = everyMs ? Date.now() + everyMs : 0;
  self.postMessage({ event: 'tick', at: Date.now(), n: ticks });
}

function stop() {
  if (timer !== null) clearInterval(timer);
  if (first !== null) clearTimeout(first);
  timer = first = null; everyMs = 0; next = 0;
}

function start(args) {
  const every = Number(args && args.everyMs), firstMs = args && args.firstMs != null ? Number(args.firstMs) : null;
  if (!Number.isFinite(every) || every < 1000) throw new Error('everyMs must be at least 1000');
  stop();
  everyMs = every;
  timer = setInterval(tick, everyMs);
  next = Date.now() + everyMs;
  if (firstMs !== null && Number.isFinite(firstMs) && firstMs >= 0 && firstMs < everyMs) {
    first = setTimeout(() => { first = null; tick(); }, firstMs);
    next = Date.now() + firstMs;
  }
  return { everyMs, next };
}

self.onmessage = (e) => {
  const m = e.data || {};
  if (m.op === undefined) return;
  let result = null;
  try {
    if (m.op === 'start') result = start(m.args || {});
    else if (m.op === 'stop') { stop(); result = { stopped: true }; }
    else if (m.op === 'state') result = { everyMs, next, ticks };
    else throw new Error('unknown op ' + m.op);
    self.postMessage({ id: m.id, ok: true, result });
  } catch (err) {
    self.postMessage({ id: m.id, ok: false, error: String((err && err.message) || err) });
  }
};
