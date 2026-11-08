// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The page's side of the optional reranker (rag/reranker.worker.js; rag/DESIGN.md section 13). Nothing starts until init() is asked for:
// the chat's reranking is off by default, and the worker and its model are loaded the first time it is switched on and a question comes.
//   const rr = createReranker(); await rr.init(); const scores = await rr.score({query, texts}); rr.terminate();

// the same name as the worker's (never imported here: the worker's file sets its own message handler wherever it runs)
export const RERANK_MODEL = 'Xenova/ms-marco-MiniLM-L-6-v2';
export const WORKER_URL = new URL('./reranker.worker.js', import.meta.url).href;
export const VENDOR_BASE = new URL('./vendor/', import.meta.url).href;

export function createReranker(options = {}) {
  let worker = null, seq = 0;
  const pending = new Map();
  function start() {
    if (worker) return worker;
    worker = new Worker(options.workerUrl || WORKER_URL, { type: 'module' });
    worker.onmessage = (e) => {
      const m = e.data || {};
      if (m.id == null || !pending.has(m.id)) return;
      const p = pending.get(m.id); pending.delete(m.id);
      if (m.ok) p.resolve(m.result);
      else { const err = new Error((m.error && m.error.message) || 'the reranker failed'); err.code = (m.error && m.error.code) || 'ERROR'; p.reject(err); }
    };
    worker.onerror = (e) => {
      const err = new Error('the reranker could not start: ' + ((e && e.message) || 'its worker failed')); err.code = 'WORKER_FAILED';
      pending.forEach((p) => p.reject(err)); pending.clear(); try { worker.terminate(); } catch (_) { /* gone */ } worker = null;
    };
    return worker;
  }
  function request(op, args) {
    const w = start();
    return new Promise((resolve, reject) => { const id = ++seq; pending.set(id, { resolve, reject }); w.postMessage({ id, op, args: args || {} }); });
  }
  return {
    init: (a = {}) => request('init', Object.assign({ vendorBase: options.vendorBase || VENDOR_BASE }, a)),
    score: (a) => request('score', a),
    info: () => request('info'),
    terminate() {
      if (worker) { try { worker.terminate(); } catch (_) { /* gone */ } }
      worker = null; const err = new Error('the reranker was stopped'); err.code = 'WORKER_STOPPED'; pending.forEach((p) => p.reject(err)); pending.clear();
    },
  };
}

export default createReranker;
