// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's scheduler, phase 4 (rag/DESIGN.md section 11): the page's side of rag/scheduler.worker.js.
//
//   const s = createScheduler({ onTick: async (tick) => { ... } });
//   await s.start(20 * 60000, { firstMs: 120000 })   a tick every 20 minutes, the first after 2 minutes
//   await s.stop(); s.state(); await s.tickNow(); s.terminate();
//
// onTick is the reconcile (rag/main.js, reconcile()): what it does with each folder is decided there, from the sizes and modification
// times the app lists, so a folder where nothing changed costs one listing and the reading model is not even loaded. A tick that comes
// while the last one's work is still going on is let pass (it is counted as skipped), so two runs never overlap. Nothing watches the
// files between ticks, as her task asks; the ticks stop when the page closes, because the worker goes with it.

export const DEFAULT_MINUTES = 20;
export const WORKER_URL = new URL('./scheduler.worker.js', import.meta.url).href;

export function createScheduler(options = {}) {
  const onTick = options.onTick;
  if (typeof onTick !== 'function') throw new TypeError('createScheduler needs onTick');
  let worker = null, seq = 0, busy = false, everyMs = 0, next = 0, ticks = 0, skipped = 0, lastError = null;
  const pending = new Map();

  function ensure() {
    if (worker) return worker;
    worker = new Worker(options.workerUrl || WORKER_URL, { type: 'module' });
    worker.onmessage = (e) => {
      const m = e.data || {};
      if (m.event === 'tick') { next = everyMs ? Date.now() + everyMs : 0; run(m); return; }
      if (m.id != null && pending.has(m.id)) {
        const p = pending.get(m.id); pending.delete(m.id);
        if (m.ok) p.resolve(m.result); else p.reject(new Error(m.error || 'the clock failed'));
      }
    };
    worker.onerror = (e) => {
      const err = new Error('the folder search\'s clock stopped: ' + ((e && e.message) || 'it could not start')); err.code = 'WORKER_FAILED';
      pending.forEach((p) => p.reject(err)); pending.clear(); lastError = err.message;
      try { worker.terminate(); } catch (_) { /* gone */ }
      worker = null; everyMs = 0; next = 0;
    };
    return worker;
  }

  function request(op, args) {
    const w = ensure();
    return new Promise((resolve, reject) => {
      const id = ++seq; pending.set(id, { resolve, reject });
      try { w.postMessage({ id, op, args: args || {} }); } catch (e) { pending.delete(id); reject(e); }
    });
  }

  async function run(tick) {
    if (busy) { skipped++; return null; }
    busy = true; ticks++;
    try { lastError = null; return await onTick(tick || { at: Date.now(), n: ticks, now: true }); }
    catch (e) { lastError = String((e && e.message) || e); return null; }
    finally { busy = false; }
  }

  return {
    async start(ms, opts = {}) {
      const r = await request('start', { everyMs: ms, firstMs: opts.firstMs });
      everyMs = r.everyMs; next = r.next; return r;
    },
    async stop() {
      if (!worker) { everyMs = 0; next = 0; return { stopped: true }; }
      const r = await request('stop'); everyMs = 0; next = 0; return r;
    },
    /** A tick now (the tests, or a press on the card); it waits for its work and returns what onTick returned. */
    tickNow() { return run({ at: Date.now(), n: ticks + 1, now: true }); },
    state() { return { everyMs, minutes: everyMs ? everyMs / 60000 : 0, next, running: busy, ticks, skipped, lastError }; },
    terminate() {
      if (worker) { try { worker.terminate(); } catch (_) { /* gone */ } }
      worker = null; everyMs = 0; next = 0;
      const e = new Error('the folder search\'s clock was stopped'); e.code = 'WORKER_STOPPED';
      pending.forEach((p) => p.reject(e)); pending.clear();
    },
  };
}

export default createScheduler;
