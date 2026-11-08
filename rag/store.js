// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The page's side of the folder search's store (rag/store.worker.js; DESIGN section 3). Every method returns a Promise.
//
//   import { createStore } from './store.js';
//   const store = createStore();                       // starts the worker on the first call
//   await store.open();                                // {schemaVersion, vfs, sqlite, vec, dims, dbName, bytes, persisted, storage}
//   const { id } = await store.principal.ensure({ name: 'me' });
//   const folder = await store.folder.add({ name: 'Letters', path: '/Users/me/Letters' });
//   await store.acl.grant({ folderId: folder.id, principalId: id });
//   await store.file.replace({ folderId: folder.id, path: 'a.md', size, mtime, contentHash, model, chunks });
//
// The worker and the vendor folder are found from this file's own address (they sit beside it: <page folder>/rag/), so the same
// code works in the apps and in the test harness. Errors reject with an Error that has .code (BAD_ARGS, NOT_OPEN, OPFS_LOCKED,
// OPFS_UNAVAILABLE, SCHEMA_TOO_NEW, DIMS_MISMATCH, NO_SUCH_FOLDER, ...), .op, and .resultCode for SQLite errors.
//
// Float32Array embeddings handed to file.replace and emb.put are TRANSFERRED to the worker (their buffers are emptied here)
// unless they are views of a larger buffer or the call is made with { transfer: false }.

export const DEFAULT_DIMS = 384;
export const STORE_WORKER_URL = new URL('./store.worker.js', import.meta.url).href;
export const VENDOR_BASE = new URL('./vendor/', import.meta.url).href;

// What an index takes on the computer's disk, for the settings page and for choosing a model. Measured on 31 October 2026
// (SQLite 3.45.3 in OPFS, 4,096 chunks of 1,800 characters of English each, with their FTS5 rows, vectors and cache entries;
// the growth from 2,048 to 4,096 chunks): 8,650 bytes a chunk with 384-value vectors, 12,700 with 768, 17,800 with 1024. The
// vectors count twice (the vec table and the cache), and a cache row of more than half a database page (8 KB) leaves most of
// that page empty, which is why 1024 costs more than twice 384. Each folder also leaves about half a block of 1,024 vectors
// unused. The index lives in the browser's private storage on the computer's own disk, not on a drive the folders are on.
const PER_CHUNK = [[384, 8650], [768, 12700], [1024, 17800]];
export function estimateIndexBytes({ chunks, dims = DEFAULT_DIMS, folders = 1 }) {
  let lo = PER_CHUNK[0], hi = PER_CHUNK[1];
  if (dims > PER_CHUNK[1][0]) { lo = PER_CHUNK[1]; hi = PER_CHUNK[2]; }
  const per = lo[1] + ((dims - lo[0]) * (hi[1] - lo[1])) / (hi[0] - lo[0]);
  return Math.round(chunks * per + folders * dims * 4 * 512);
}

// ------------------------------------------------------------------------------------------------ the request/reply core
function remoteError(info, op) {
  const e = new Error(info && info.message ? info.message : 'the store failed');
  if (info) { e.name = info.name || 'Error'; e.code = info.code || null; e.resultCode = info.resultCode == null ? null : info.resultCode; e.workerStack = info.stack || null; }
  e.op = op;
  return e;
}

class Rpc {
  constructor(url, label) {
    this.url = url; this.label = label; this.worker = null; this.seq = 0; this.pending = new Map(); this.listeners = new Set();
    this.errors = [];     // uncaught errors of the worker, for tests and diagnostics
    this.dead = null;
  }
  start() {
    if (this.worker) return;
    const w = new Worker(this.url, { type: 'module' });
    w.onmessage = (e) => {
      const m = e.data || {};
      if (m.id != null && this.pending.has(m.id)) {
        const p = this.pending.get(m.id); this.pending.delete(m.id);
        if (m.ok) p.resolve(m.result); else p.reject(remoteError(m.error, p.op));
      } else if (m.event) this.listeners.forEach((f) => { try { f(m); } catch (_) { /* a listener's error is its own */ } });
    };
    w.onerror = (e) => {
      const msg = this.label + ' worker failed: ' + (e && e.message ? e.message : 'it could not start') + (e && e.filename ? ' (' + e.filename + ':' + e.lineno + ')' : '');
      this.errors.push(msg); this.dead = new Error(msg); this.dead.code = 'WORKER_FAILED';
      this.pending.forEach((p) => p.reject(this.dead)); this.pending.clear();
    };
    w.onmessageerror = () => this.errors.push(this.label + ' worker sent a message that could not be read');
    this.worker = w;
  }
  request(op, args, transfer) {
    if (this.dead) return Promise.reject(this.dead);
    this.start();
    return new Promise((resolve, reject) => {
      const id = ++this.seq;
      this.pending.set(id, { resolve, reject, op });
      try { this.worker.postMessage({ id, op, args: args || {} }, transfer || []); }
      catch (e) { this.pending.delete(id); reject(e); }
    });
  }
  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  terminate() {
    if (this.worker) { this.worker.terminate(); this.worker = null; }
    const gone = new Error(this.label + ' worker was stopped'); gone.code = 'WORKER_STOPPED';
    this.pending.forEach((p) => p.reject(gone)); this.pending.clear();
    this.dead = null;
  }
}

// Buffers that may travel: Float32Arrays that own their whole buffer, each buffer once.
function transferables(vectors, enabled) {
  if (enabled === false) return [];
  const seen = new Set(), list = [];
  for (const v of vectors) {
    if (v instanceof Float32Array && v.byteOffset === 0 && v.byteLength === v.buffer.byteLength && v.buffer instanceof ArrayBuffer && !seen.has(v.buffer)) { seen.add(v.buffer); list.push(v.buffer); }
  }
  return list;
}

// ------------------------------------------------------------------------------------------------ the client
export class Store {
  constructor(options = {}) {
    // debug: the tests' operations (debug.*) are served only by a worker started with ?debug=1; the app never asks for it
    this.workerUrl = options.workerUrl || STORE_WORKER_URL;
    if (options.debug) { const u = new URL(this.workerUrl, typeof location !== 'undefined' ? location.href : undefined); u.searchParams.set('debug', '1'); this.workerUrl = u.href; }
    this.vendorBase = options.vendorBase || VENDOR_BASE;
    this.rpc = new Rpc(this.workerUrl, 'store');
    const r = (op, transferOf) => (args, opts) => this.rpc.request(op, args, transferOf ? transferables(transferOf(args || {}), opts && opts.transfer) : undefined);
    this.principal = { ensure: r('principal.ensure'), find: r('principal.find') };
    this.folder = { add: r('folder.add'), list: r('folder.list'), remove: r('folder.remove'), touch: r('folder.touch') };
    this.acl = { grant: r('acl.grant'), revoke: r('acl.revoke'), allowed: r('acl.allowed') };
    this.file = {
      list: r('file.list'), get: r('file.get'), delete: r('file.delete'),
      replace: r('file.replace', (a) => (Array.isArray(a.chunks) ? a.chunks.map((c) => c && c.embedding) : [])),
    };
    this.emb = {
      lookup: r('emb.lookup'), prune: r('emb.prune'),
      put: r('emb.put', (a) => (Array.isArray(a.items) ? a.items.map((i) => i && (i.embedding != null ? i.embedding : i.vec)) : [])),
    };
    // phase 2: the two searches of retrieval and the passages they name (each answers only within the folders the principal
    // may read; rag/retriever.js fuses them). The question's vector is transferred to the worker.
    this.search = { fts: r('search.fts'), vec: r('search.vec', (a) => [a.embedding]) };
    this.chunk = { get: r('chunk.get') };
    this.debug = { rows: r('debug.rows'), sql: r('debug.sql'), fail: r('debug.fail'), migrate: r('debug.migrate'), wipe: r('debug.wipe') };
  }
  /**
   * Opens the database. Args: {dbName = 'rag.sqlite3', vfs = 'opfs-sahpool' | 'memory', dims = 384, lockWaitMs = 8000}.
   * For the private file system the page first asks the browser to keep this origin's storage (persist() exists only on the
   * page, not in workers); the answer is info().persisted.
   */
  async open(args = {}) {
    if (args.vfs !== 'memory' && typeof navigator !== 'undefined' && navigator.storage && typeof navigator.storage.persist === 'function') {
      try { await navigator.storage.persist(); } catch (_) { /* a refusal changes nothing here */ }
    }
    return this.rpc.request('open', Object.assign({ vendorBase: this.vendorBase }, args));
  }
  /** Gives the space of deleted rows back to the file system (after removing a big folder). -> {before, after, mode} in bytes. */
  compact() { return this.rpc.request('compact'); }
  /** Starts the index again from nothing, with the arguments of the last open (after it was found damaged: error code CORRUPT). */
  reset(args = {}) { return this.rpc.request('reset', Object.assign({ vendorBase: this.vendorBase }, args)); }
  close() { return this.rpc.request('close'); }
  info() { return this.rpc.request('info'); }
  stats() { return this.rpc.request('stats'); }
  ops() { return this.rpc.request('ops'); }
  /** Any op by its name, for callers that hold the name as a string. */
  call(op, args, transfer) { return this.rpc.request(op, args, transfer); }
  onEvent(fn) { return this.rpc.on(fn); }
  get errors() { return this.rpc.errors; }
  /** Stops the worker (it releases the database files; the data stays). */
  terminate() { this.rpc.terminate(); }
}

export function createStore(options) { return new Store(options); }
export default createStore;
