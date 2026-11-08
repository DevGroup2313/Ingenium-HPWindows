// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's indexer: the page's side of reading a folder into the index (rag/DESIGN.md sections 2, 3, 5, 6).
//
//   import { createIndexer } from './indexer.js';
//   const indexer = createIndexer({ store, embedder });             // store.js and embedder.js clients, the embedder already init'd
//   const summary = await indexer.indexFolder(folderId, { onProgress, signal, force });
//
// For each file of the folder (listed through rag/folders.js, the app's bridge): a file whose size and modification time are those
// in the index is left alone; otherwise it is read in pieces of 1 MB, sent to the parse worker (rag/parse.worker.js: decoding,
// parsing, chunking, hashing; the worker asks THIS page for token counts, answered by the embedder's own tokenizer), the vectors of
// chunks the index already holds (the same content hash) are reused, only the others are embedded, and the file goes into the
// index in ONE call (store.file.replace: its old chunks go and the new ones come in one transaction). Files that have gone from the
// folder are taken out. One file failing is reported and the rest go on.
//
// Progress events (onProgress receives one object each, {event, ...}):
//   folder-start  {folderId, name, path, total, changed}
//   file-start    {index, total, path, size}
//   file-stage    {path, stage}   stage: reading | pdf | units | counting | splitting | packing | checking | embedding | writing
//                                 (pdf and embedding carry done and of)
//   file-done     {path, chunks, embedded, reused, ms, warnings}
//   file-skip     {path, reason: 'unchanged'}
//   file-error    {path, error}
//   file-deleted  {path, chunks}
//   folder-done   {summary}
// The summary: {folderId, files: {total, indexed, skipped, failed, deleted}, chunks, embedded, reused, ms, failures: [{path, error}],
// warnings, stopped, notes: [...]}. A run that is stopped through {signal} rejects with an AbortError after the file in hand.

import * as bridge from './folders.js';

export const PARSE_WORKER_URL = new URL('./parse.worker.js', import.meta.url).href;
export const VENDOR_BASE = new URL('./vendor/', import.meta.url).href;
const MB = 1048576;
const FATAL = new Set(['WORKER_FAILED', 'WORKER_STOPPED', 'NOT_OPEN', 'OPFS_LOCKED', 'OPFS_UNAVAILABLE', 'NOT_READY', 'MODEL_NOT_INSTALLED', 'MODEL_UNREADABLE']);

const fmtBytes = (n) => (n >= MB ? (n / MB).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' kB');
const abortError = () => { const e = new Error('stopped'); e.name = 'AbortError'; return e; };
const extOf = (rel) => { const m = /\.([A-Za-z0-9]+)$/.exec(rel); return m ? m[1].toLowerCase() : ''; };

// ------------------------------------------------------------------------------------------------- the parse worker's client
/** The page's side of rag/parse.worker.js. parse(...) reads a file's pieces (an async iterable of Uint8Array) into the worker and
 *  returns what finish() returns. The worker's token-count requests are answered with count(texts) -> Promise<number[]>. */
export class ParseClient {
  constructor(options = {}) {
    this.workerUrl = options.workerUrl || PARSE_WORKER_URL;
    this.vendorBase = options.vendorBase || VENDOR_BASE;
    this.count = options.count;
    this.worker = null; this.seq = 0; this.pending = new Map(); this.errors = []; this.stageListener = null; this.inited = false;
  }
  _start() {
    if (this.worker) return;
    const w = new Worker(this.workerUrl, { type: 'module' });
    w.onmessage = (e) => this._message(e.data);
    w.onerror = (e) => {
      const msg = 'parse worker failed: ' + (e && e.message ? e.message : 'it could not start');
      this.errors.push(msg); const err = new Error(msg); err.code = 'PARSE_WORKER_FAILED';
      this.pending.forEach((p) => p.reject(err)); this.pending.clear(); this._drop();
    };
    this.worker = w; this.inited = false;
  }
  _drop() { if (this.worker) { try { this.worker.terminate(); } catch (e) { /* gone already */ } } this.worker = null; this.inited = false; }
  _message(m) {
    if (!m || typeof m !== 'object') return;
    if (m.event === 'count') { // the worker needs token counts: the embedder's tokenizer measures
      const w = this.worker;
      Promise.resolve().then(() => this.count(m.texts)).then(
        (counts) => { if (this.worker === w) w.postMessage({ op: 'counts', args: { cid: m.cid, counts } }); },
        (err) => { if (this.worker === w) w.postMessage({ op: 'counts', args: { cid: m.cid, error: String(err && err.message || err) } }); });
      return;
    }
    if (m.event === 'stage') { if (this.stageListener) { try { this.stageListener(m); } catch (e) { /* the listener's own */ } } return; }
    if (m.id != null && this.pending.has(m.id)) {
      const p = this.pending.get(m.id); this.pending.delete(m.id);
      if (m.ok) p.resolve(m.result); else p.reject(new Error(m.error || 'the parse worker failed'));
    } // anything else is not ours (pdf.js talks on the worker's port when it starts)
  }
  _request(op, args, transfer) {
    this._start();
    return new Promise((resolve, reject) => {
      const id = ++this.seq; this.pending.set(id, { resolve, reject });
      try { this.worker.postMessage({ id, op, args: args || {} }, transfer || []); } catch (e) { this.pending.delete(id); reject(e); }
    });
  }
  async init() { if (!this.inited) { await this._request('init', { vendorBase: this.vendorBase }); this.inited = true; } }
  /** pieces: async iterable of Uint8Array (each is handed over, its buffer emptied). opts: {ext, path, size, maxTokens, modelKey, onStage, signal,
   *  timeoutMs = 10 minutes, slim = true, maxBytes, textOnly}. Resolves with the worker's result. */
  async parse(pieces, opts) {
    await this.init();
    let timer = null, session = null;
    const stop = new Promise((_, reject) => {
      if (opts.timeoutMs !== 0) timer = setTimeout(() => { this._drop(); const e = new Error('reading the file took more than ' + Math.round((opts.timeoutMs || 600000) / 60000) + ' minutes'); e.code = 'PARSE_TIMEOUT'; this.pending.forEach((p) => p.reject(e)); this.pending.clear(); reject(e); }, opts.timeoutMs || 600000);
    });
    stop.catch(() => {});
    this.stageListener = (m) => { if (opts.onStage && (session === null || m.session === session)) opts.onStage(m); };
    const run = async () => {
      const r = await this._request('open', { ext: opts.ext, path: opts.path, size: opts.size, maxTokens: opts.maxTokens, modelKey: opts.modelKey, overlap: opts.overlap, slim: opts.slim !== false, maxBytes: opts.maxBytes, textOnly: !!opts.textOnly });
      session = r.session;
      try {
        for await (const u8 of pieces) {
          if (opts.signal && opts.signal.aborted) throw abortError();
          await this._request('feed', { session, bytes: u8 }, [u8.buffer]);
        }
        if (opts.signal && opts.signal.aborted) throw abortError();
        return await this._request('finish', { session });
      } catch (e) { if (session !== null && this.worker) { try { this.worker.postMessage({ id: ++this.seq, op: 'abort', args: { session } }); } catch (x) { /* gone */ } } throw e; }
    };
    try { return await Promise.race([run(), stop]); } finally { clearTimeout(timer); this.stageListener = null; }
  }
  terminate() { this._drop(); const e = new Error('parse worker was stopped'); e.code = 'WORKER_STOPPED'; this.pending.forEach((p) => p.reject(e)); this.pending.clear(); }
}

// --------------------------------------------------------------------------------------------------------------- the indexer
/** options: {store, embedder, bridge = rag/folders.js, workerUrl, vendorBase, maxFileBytes = 64 MB, embedBatch = 32, parseTimeoutMs, overlap}. */
export function createIndexer(options) {
  const { store, embedder } = options;
  if (!store || !embedder) throw new Error('createIndexer needs a store and an embedder');
  const br = options.bridge || bridge;
  const maxFileBytes = options.maxFileBytes || 64 * MB;
  const embedBatch = Math.max(1, options.embedBatch || 32);
  const running = new Set();
  const parser = new ParseClient({ workerUrl: options.workerUrl, vendorBase: options.vendorBase, count: (texts) => embedder.count({ texts }) });

  async function modelInfo() {
    let info = null;
    try { info = await embedder.info(); } catch (e) { info = null; }
    if (!info || !info.modelId) info = await embedder.init();
    return { model: info.key || info.modelId, maxTokens: info.maxTokens || 512, dims: info.dims };
  }

  // One file: read, parse, reuse or embed, write. Returns {chunks, embedded, reused, warnings}.
  async function indexOne(folder, f, ctx, emit) {
    const t0 = Date.now(), { model, maxTokens, signal } = ctx;
    emit({ event: 'file-stage', path: f.rel, stage: 'reading' });
    const parsed = await parser.parse(br.readPieces(folder.path, f.rel, { signal }), {
      ext: extOf(f.rel), path: f.rel, size: f.size, maxTokens, modelKey: model, signal, slim: true, maxBytes: maxFileBytes, timeoutMs: options.parseTimeoutMs, overlap: options.overlap,
      onStage: (m) => emit({ event: 'file-stage', path: f.rel, stage: m.stage, done: m.done, of: m.of }),
    });
    const chunks = parsed.chunks, vecs = new Map();      // content hash -> vector
    // vectors the index already holds for the same input are reused
    const hashes = [...new Set(chunks.map((c) => c.content_hash))];
    let reused = 0;
    for (let i = 0; i < hashes.length; i += 4000) {
      const got = await store.emb.lookup({ model, hashes: hashes.slice(i, i + 4000) });
      for (const h of Object.keys(got)) vecs.set(h, got[h]);
    }
    reused = vecs.size;
    // the others are embedded (shortest first, so a batch pads little), each distinct input once
    const todo = []; const seen = new Set();
    for (const c of chunks) if (!vecs.has(c.content_hash) && !seen.has(c.content_hash)) { seen.add(c.content_hash); todo.push(c); }
    todo.sort((a, b) => a.tokens - b.tokens);
    let embedded = 0, lastSaved = 0;
    try {
      for (let i = 0; i < todo.length; i += embedBatch) {
        if (signal && signal.aborted) throw abortError();
        const batch = todo.slice(i, i + embedBatch);
        emit({ event: 'file-stage', path: f.rel, stage: 'embedding', done: embedded, of: todo.length });
        const out = await embedder.embed({ texts: batch.map((c) => c.input), kind: 'passage' });
        if (!Array.isArray(out) || out.length !== batch.length) throw new Error('the embedder returned ' + (out && out.length) + ' vectors for ' + batch.length + ' texts');
        batch.forEach((c, k) => vecs.set(c.content_hash, out[k]));
        embedded += batch.length;
        if (embedded - lastSaved >= 200 && embedded < todo.length) { // a long file: what is done is kept, so a stop or a crash costs little
          await saveCache(model, todo.slice(lastSaved, embedded), vecs, false); lastSaved = embedded;   // only the new ones
        }
      }
    } catch (e) {
      if (embedded > lastSaved) { try { await saveCache(model, todo.slice(lastSaved, embedded), vecs, false); } catch (x) { /* the original error is the one to report */ } }
      throw e;
    }
    emit({ event: 'file-stage', path: f.rel, stage: 'writing' });
    const used = new Set();
    const rows = chunks.map((c) => {
      let v = vecs.get(c.content_hash);
      if (used.has(c.content_hash)) v = new Float32Array(v);   // the same input twice in one file: a copy, for the transfer
      used.add(c.content_hash);
      return { chunk_index: c.chunk_index, heading_path: c.heading_path, char_start: c.char_start, char_end: c.char_end, page: c.page, content_hash: c.content_hash, text: c.text, embedding: v };
    });
    await store.file.replace({ folderId: folder.id, path: f.rel, size: f.size, mtime: Math.floor(f.mtime), contentHash: parsed.fileHash, model, chunks: rows });
    return { chunks: chunks.length, embedded, reused: chunks.length - embedded, warnings: parsed.warnings || [], ms: Date.now() - t0 };
  }

  async function saveCache(model, items, vecs, transfer) {
    await store.emb.put({ model, items: items.map((c) => ({ hash: c.content_hash, embedding: vecs.get(c.content_hash) })) }, { transfer });
  }

  /** Reads a folder into the index. opts: {onProgress, signal, force = false (read every file again), only: [rel] (just these files),
   *  confirmEmpty = false (a folder that lists no files while the index holds some is taken as a drive that is not there, unless this is true)}. */
  async function indexFolder(folderId, opts = {}) {
    if (running.has(folderId)) { const e = new Error('this folder is being read already'); e.code = 'ALREADY_RUNNING'; throw e; }
    running.add(folderId);
    try { return await indexFolderRun(folderId, opts); } finally { running.delete(folderId); }
  }

  async function indexFolderRun(folderId, opts) {
    const t0 = Date.now(), signal = opts.signal;
    const emit = (e) => { if (opts.onProgress) { try { opts.onProgress(e); } catch (x) { /* the listener's own */ } } };
    const { model, maxTokens } = await modelInfo();
    const folder = (await store.folder.list()).find((x) => x.id === folderId);
    if (!folder) { const e = new Error('no such folder in the index: ' + folderId); e.code = 'NO_SUCH_FOLDER'; throw e; }
    const summary = { folderId, files: { total: 0, indexed: 0, skipped: 0, failed: 0, deleted: 0 }, chunks: 0, embedded: 0, reused: 0, ms: 0, failures: [], warnings: [], stopped: false, notes: [] };
    // what the app lists now, what the index holds
    let listing;
    try { listing = await br.listFiles(folder.path); } catch (e) {
      // the app no longer has the folder on its list (NOT_CHOSEN) is not the same as a drive that is not connected
      const code = e && e.code === 'NOT_CHOSEN' ? 'NOT_CHOSEN' : 'FOLDER_UNAVAILABLE';
      const err = new Error(code === 'NOT_CHOSEN' ? 'the app does not have this folder on its list: ' + (e && e.message || e) : 'the folder cannot be read (is its drive connected?): ' + (e && e.message || e));
      err.code = code; err.cause = e; throw err;
    }
    const existing = new Map((await store.file.list({ folderId })).map((x) => [x.path, x]));
    const only = opts.only ? new Set(opts.only) : null;
    const wanted = listing.filter((f) => !only || only.has(f.rel));
    const changed = wanted.filter((f) => {
      const old = existing.get(f.rel);
      return opts.force || !old || old.size !== f.size || old.mtime !== Math.floor(f.mtime);
    });
    summary.files.total = wanted.length;
    emit({ event: 'folder-start', folderId, name: folder.name, path: folder.path, total: wanted.length, changed: changed.length });
    const ctx = { model, maxTokens, signal };
    const changedSet = new Set(changed.map((f) => f.rel));
    let n = 0;
    try {
      for (const f of wanted) {
        n++;
        if (signal && signal.aborted) throw abortError();
        if (!changedSet.has(f.rel)) { summary.files.skipped++; emit({ event: 'file-skip', path: f.rel, reason: 'unchanged' }); continue; }
        emit({ event: 'file-start', index: n, total: wanted.length, path: f.rel, size: f.size });
        try {
          if (f.size > maxFileBytes) throw new Error('file too large (' + fmtBytes(f.size) + '; the limit is ' + fmtBytes(maxFileBytes) + ')');
          const r = await indexOne(folder, f, ctx, emit);
          summary.files.indexed++; summary.chunks += r.chunks; summary.embedded += r.embedded; summary.reused += r.reused;
          if (r.warnings.length) summary.warnings.push(...r.warnings.map((w) => f.rel + ': ' + w));
          emit({ event: 'file-done', path: f.rel, chunks: r.chunks, embedded: r.embedded, reused: r.reused, ms: r.ms, warnings: r.warnings });
        } catch (e) {
          if (e && e.name === 'AbortError') throw e;
          if (e && FATAL.has(e.code)) throw e;      // the store or the embedder is gone: every other file would fail the same way
          const msg = String(e && e.message || e);
          summary.files.failed++; summary.failures.push({ path: f.rel, error: msg });
          emit({ event: 'file-error', path: f.rel, error: msg });
        }
      }
      // files that have gone from the folder
      if (!only) {
        const now = new Set(listing.map((x) => x.rel)), gone = [...existing.keys()].filter((p) => !now.has(p));
        const cap = br.LIST_CAP || bridge.LIST_CAP, capped = listing.truncated !== undefined ? !!listing.truncated : listing.length >= cap;
        if (capped) summary.notes.push('This folder has more files than Ingenium reads at once (at most ' + cap.toLocaleString('en-GB') + '). The others are left out of the search, and nothing was taken out.');
        else if (!listing.length && existing.size && !opts.confirmEmpty) {
          // the card asks the person, in plain words, and reads again with confirmEmpty when she says the folder really is empty
          summary.emptyNow = existing.size;
          summary.notes.push('This folder looks empty now, so nothing was taken out of the search, in case its drive is not connected.');
        }
        else for (const p of gone) {
          if (signal && signal.aborted) throw abortError();
          try { const r = await store.file.delete({ folderId, path: p }); summary.files.deleted++; emit({ event: 'file-deleted', path: p, chunks: r && r.chunkCount || 0 }); }
          catch (e) { if (e && FATAL.has(e.code)) throw e; summary.failures.push({ path: p, error: String(e && e.message || e) }); emit({ event: 'file-error', path: p, error: String(e && e.message || e) }); }
        }
      }
      await store.folder.touch({ folderId, indexedAt: Date.now() });
    } catch (e) {
      if (e && e.name === 'AbortError') { summary.stopped = true; summary.ms = Date.now() - t0; emit({ event: 'folder-done', summary }); e.summary = summary; }
      throw e;
    }
    summary.ms = Date.now() - t0;
    emit({ event: 'folder-done', summary });
    return summary;
  }

  return { indexFolder, parser, stop() { parser.terminate(); } };
}

export default createIndexer;
