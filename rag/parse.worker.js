// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's parse worker (a module worker; rag/DESIGN.md sections 3 and 5). It turns the bytes of one file into blocks,
// chunks and hashes: .txt .md .html .htm .json .csv with the code in chunker.js, .pdf with pdf.js run in THIS worker (no nested
// worker: globalThis.pdfjsWorker is set before pdf.js starts), .docx with mammoth (fetched and run with new Function, because a
// module worker cannot importScripts) and then the HTML reader.
//
// Messages (one protocol, as every worker of the folder search):
//   request  {id, op, args}            reply {id, ok: true, result} | {id, ok: false, error}
//   ops      init({vendorBase})        optional: where rag/vendor/ is (default: beside this file)
//            open({ext, path, size?, maxTokens?, modelKey?, overlap?, slim?, maxBytes?, textOnly?}) -> {session}   overlap: 0.125 or [0.10, 0.15] (the default)
//                                      textOnly (phase 3, the source viewer): finish gives the file's text (and a PDF's pages) without
//                                      cutting it into chunks, so no token count is asked for
//            feed({session, bytes})    one piece of the file in order (an ArrayBuffer or Uint8Array; send it in the transfer list);
//                                      text files are decoded piece by piece with one TextDecoder (stream: true)
//            finish({session})         -> {fileHash, ext, size, text?, pages?, blocks?, chunks, warnings, meta, stats, mem}
//            abort({session})
//            parse({bytes, ext, path, maxTokens?, slim?})   open + feed + finish in one call
//            counts({cid, counts} | {cid, error})           the page's answer to a count request (below)
//            info()                    -> {version, pdfjs, mammoth, sessions}
//   events   {event: 'count', cid, texts}   the worker asks the page for token counts (the page answers with op "counts", the
//                                           numbers coming from the embedder's tokenizer: chunk sizes are measured, never guessed)
//            {event: 'stage', session, stage, done?, of?}  progress (reading, pdf pages, units, counting, packing, checking)
//
// result.chunks: [{chunk_index, heading_path, char_start, char_end, page, content_hash, text, input, tokens, overlap}]; slim: true leaves
// out text, pages and blocks (the indexer needs only the chunks and the file's hash).

import { VERSION, parseTextFile, blocksFromHtml, blocksFromPdfPages, chunkDocument, sha256Hex } from './chunker.js';

// WebKit (the Mac apps' web view; checked on GitHub's macOS 15.8 machines, 1 November 2026) cannot loop over a ReadableStream with
// "for await", which pdf.js 6.4's getTextContent does: every PDF failed there with "undefined is not a function (near '...t of
// e...')". When the web view lacks it, the stream is given the standard async iterator (values()), built on its own reader.
(function streamsCanBeLooped() {
  if (typeof ReadableStream !== 'function' || typeof Symbol !== 'function' || !Symbol.asyncIterator) return;
  const proto = ReadableStream.prototype;
  if (typeof proto[Symbol.asyncIterator] === 'function') return;
  function values(options) {
    const preventCancel = !!(options && options.preventCancel);
    const reader = this.getReader();
    let finished = false;
    return {
      async next() {
        if (finished) return { done: true, value: undefined };
        try {
          const r = await reader.read();
          if (r.done) { finished = true; reader.releaseLock(); }
          return r;
        } catch (e) { finished = true; reader.releaseLock(); throw e; }
      },
      async return(value) {
        if (!finished) {
          finished = true;
          if (!preventCancel) { const c = reader.cancel(value); reader.releaseLock(); await c; } else reader.releaseLock();
        }
        return { done: true, value };
      },
      [Symbol.asyncIterator]() { return this; },
    };
  }
  if (typeof proto.values !== 'function') Object.defineProperty(proto, 'values', { value: values, writable: true, configurable: true });
  Object.defineProperty(proto, Symbol.asyncIterator, { value: proto.values, writable: true, configurable: true });
})();

const realPost = self.postMessage.bind(self);
const post = (m, t) => realPost(m, t || []);
// pdf.js's worker module announces itself on the global when it is imported ({sourceName: 'worker', targetName: 'main'}); that is not ours
self.postMessage = (m, t) => { if (m && typeof m === 'object' && 'sourceName' in m && 'targetName' in m) return; realPost(m, t || []); };

const S = { overhead: undefined, modelKey: undefined, vendorBase: new URL('vendor/', self.location.href).href, pdfjs: null, pdfjsVersion: null, mammoth: null, sessions: new Map(), nextSession: 1, nextCid: 1, pending: new Map() };
const TEXT_EXTS = new Set(['txt', 'md', 'markdown', 'html', 'htm', 'json', 'csv']);
const normExt = (e) => { e = String(e || '').toLowerCase().replace(/^\./, ''); return e === 'htm' ? 'html' : e === 'markdown' ? 'md' : e; };
const MB = 1048576;
const fmtBytes = (n) => (n >= MB ? (n / MB).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' kB');

// ------------------------------------------------------------------------------------------------ token counts, from the page
function pageCount(texts) {
  return new Promise((resolve, reject) => { const cid = S.nextCid++; S.pending.set(cid, { resolve, reject }); post({ event: 'count', cid, texts }); });
}

// -------------------------------------------------------------------------------------------------------------- the readers
async function loadPdfjs() {
  if (S.pdfjs) return S.pdfjs;
  globalThis.pdfjsWorker = await import(S.vendorBase + 'pdfjs/pdf.worker.min.mjs');
  S.pdfjs = await import(S.vendorBase + 'pdfjs/pdf.min.mjs');
  S.pdfjsVersion = S.pdfjs.version || null;
  return S.pdfjs;
}

async function loadMammoth() {
  if (S.mammoth) return S.mammoth;
  const r = await fetch(S.vendorBase + 'mammoth/mammoth.browser.min.js');
  if (!r.ok) throw new Error('could not load mammoth (' + r.status + ')');
  new Function(await r.text()).call(self);
  if (!self.mammoth || typeof self.mammoth.convertToHtml !== 'function') throw new Error('mammoth did not start');
  S.mammoth = self.mammoth;
  return S.mammoth;
}

async function readPdf(bytes, stage) {
  const pdfjs = await loadPdfjs();
  const task = pdfjs.getDocument({ data: bytes, isEvalSupported: false, useSystemFonts: false, disableFontFace: true, verbosity: 0, useWorkerFetch: false });
  const warnings = []; let doc;
  try { doc = await task.promise; } catch (e) {
    const name = e && e.name || '';
    throw new Error(name === 'PasswordException' ? 'the PDF is password protected' : name === 'InvalidPDFException' ? 'not a readable PDF' : 'PDF: ' + (e && e.message || e));
  }
  const pages = []; let noText = 0;
  try {
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const view = page.view, h = view[3] - view[1];
      const tc = await page.getTextContent();
      const items = [];
      for (const it of tc.items) {
        if (typeof it.str !== 'string') continue;
        const tr = it.transform; if (!tr) continue;
        if (Math.abs(tr[1]) > 0.1 && Math.abs(tr[1]) > Math.abs(tr[0]) * 0.3) continue; // rotated text (margins, stamps)
        items.push({ str: it.str, x: tr[4] - view[0], y: tr[5] - view[1], w: it.width || 0, h: it.height || Math.hypot(tr[2], tr[3]) });
      }
      if (!items.some((x) => /\S/.test(x.str))) noText++;
      pages.push({ page: i, height: h, items });
      page.cleanup();
      if (i % 10 === 0 || i === doc.numPages) stage('pdf', i, doc.numPages);
    }
  } finally { try { await task.destroy(); } catch (e) { /* closing only */ } }
  if (noText) warnings.push('no text on ' + noText + ' of ' + pages.length + ' pages (scanned images?)');
  const r = blocksFromPdfPages(pages);
  return { ...r, warnings, meta: { ...r.meta, numPages: pages.length, pagesWithoutText: noText } };
}

async function readDocx(bytes) {
  const mammoth = await loadMammoth();
  const ab = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
  let res;
  try {
    res = await mammoth.convertToHtml({ arrayBuffer: ab }, {
      styleMap: ["p[style-name='Title'] => h1.ing-title:fresh", "p[style-name='Subtitle'] => p:fresh"],
      convertImage: mammoth.images.imgElement(() => ({ src: '' })), // pictures are not read
    });
  } catch (e) { throw new Error('not a readable Word file: ' + (e && e.message || e)); }
  const r = blocksFromHtml(res.value);
  const warnings = (res.messages || []).filter((m) => m.type === 'warning' && !/Unrecognised (paragraph|run) style/.test(m.message)).slice(0, 5).map((m) => 'docx: ' + m.message);
  return { ...r, warnings };
}

// ----------------------------------------------------------------------------------------------------------------- sessions
function openSession(a) {
  const ext = normExt(a.ext);
  if (!TEXT_EXTS.has(ext) && ext !== 'pdf' && ext !== 'docx') throw new Error('unsupported file type: .' + ext);
  const maxBytes = a.maxBytes || 64 * MB;
  if (a.size > maxBytes) throw new Error('file too large (' + fmtBytes(a.size) + '; the limit is ' + fmtBytes(maxBytes) + ')');
  if (a.modelKey !== undefined && a.modelKey !== S.modelKey) { S.modelKey = a.modelKey; S.overhead = undefined; } // another model: measure its special tokens again
  const id = S.nextSession++;
  const ov = Array.isArray(a.overlap) ? a.overlap : (typeof a.overlap === 'number' ? [a.overlap * 0.8, a.overlap * 1.2] : null); // 0.125 means 10 to 15%
  const s = { id, ext, path: a.path || '', maxTokens: a.maxTokens || 512, limits: ov ? { overlapLo: ov[0], overlapHi: ov[1] } : undefined, slim: !!a.slim, textOnly: !!a.textOnly, maxBytes, pieces: [], total: 0, parts: [], dec: null, legacy: false, utf16: null, t0: Date.now(), count: 0 };
  S.sessions.set(id, s);
  return s;
}

function textDecoderFor(s, first) {
  if (first.length >= 2 && ((first[0] === 0xff && first[1] === 0xfe) || (first[0] === 0xfe && first[1] === 0xff))) s.utf16 = first[0] === 0xff ? 'utf-16le' : 'utf-16be';
  return new TextDecoder(s.utf16 || 'utf-8', { fatal: true });
}

function feedSession(s, bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  s.total += u8.length; s.count++;
  if (s.total > s.maxBytes) throw new Error('file too large (more than ' + fmtBytes(s.maxBytes) + ')');
  s.pieces.push(u8);
  if (TEXT_EXTS.has(s.ext) && !s.legacy) { // decoded piece by piece; an invalid UTF-8 sequence means a legacy file, decoded as a whole later
    try { if (!s.dec) s.dec = textDecoderFor(s, u8); s.parts.push(s.dec.decode(u8, { stream: true })); } catch (e) { s.legacy = true; s.parts = []; }
  }
  return { received: s.total };
}

async function finishSession(s) {
  const stage = (stage, done, of) => post({ event: 'stage', session: s.id, stage, done, of });
  try {
    stage('reading');
    let all;
    if (s.pieces.length === 1) all = s.pieces[0];
    else { all = new Uint8Array(s.total); let p = 0; for (const x of s.pieces) { all.set(x, p); p += x.length; } }
    const fileHash = await sha256Hex(all);
    const out = { fileHash, ext: s.ext, size: s.total, pieces: s.count, warnings: [], meta: {} };
    let doc;
    if (TEXT_EXTS.has(s.ext)) {
      let text = '';
      if (!s.legacy) { try { if (s.dec) s.parts.push(s.dec.decode()); text = s.parts.join(''); } catch (e) { s.legacy = true; } }
      if (s.legacy) { text = new TextDecoder('windows-1252').decode(all); out.warnings.push('not valid UTF-8: read as Windows-1252'); }
      s.parts = []; s.pieces = [];
      if (!text.trim()) { out.warnings.push('empty file'); doc = { text, blocks: [], meta: {} }; }
      else { doc = parseTextFile(s.ext, text); out.warnings.push(...doc.warnings); }
    } else if (s.ext === 'pdf') {
      s.pieces = []; doc = await readPdf(all.slice(), stage); out.warnings.push(...doc.warnings);
    } else {
      s.pieces = []; doc = await readDocx(all); out.warnings.push(...doc.warnings);
    }
    out.meta = { ...doc.meta, blocks: doc.blocks.length };
    if (!doc.blocks.length && !out.warnings.some((w) => /empty|no text/.test(w))) out.warnings.push('no text found');
    if (s.textOnly) { out.text = doc.text; if (doc.pages) out.pages = doc.pages; out.chunks = []; out.version = VERSION; out.parseMs = Date.now() - s.t0; return out; }
    const { chunks, stats } = await chunkDocument(doc, { count: pageCount, maxTokens: s.maxTokens, limits: s.limits, overhead: S.overhead, onStage: (n) => stage(n) });
    S.overhead = stats.overhead; // the special tokens' cost is the same for every file
    out.chunks = chunks; out.stats = stats; out.version = VERSION; out.parseMs = Date.now() - s.t0;
    if (!s.slim) { out.text = doc.text; out.blocks = doc.blocks; if (doc.pages) out.pages = doc.pages; }
    try { if (performance.memory) out.mem = { used: performance.memory.usedJSHeapSize, total: performance.memory.totalJSHeapSize, limit: performance.memory.jsHeapSizeLimit }; } catch (e) { /* not in every engine */ }
    return out;
  } finally { S.sessions.delete(s.id); }
}

// ------------------------------------------------------------------------------------------------------------------ requests
async function handle(op, a) {
  switch (op) {
    case 'init':
      if (a.vendorBase) {
        // only a folder on the page's own address: nothing is fetched or imported from anywhere else
        let vb;
        try { vb = new URL(a.vendorBase, self.location.href); } catch (_) { throw new Error('vendorBase must be an address'); }
        if (vb.origin !== self.location.origin) throw new Error("vendorBase must be on the page's own address");
        S.vendorBase = vb.href.endsWith('/') ? vb.href : vb.href + '/';
      }
      return { ready: true, version: VERSION };
    case 'open': return { session: openSession(a).id };
    case 'feed': { const s = S.sessions.get(a.session); if (!s) throw new Error('no such session'); return feedSession(s, a.bytes); }
    case 'finish': { const s = S.sessions.get(a.session); if (!s) throw new Error('no such session'); return await finishSession(s); }
    case 'abort': S.sessions.delete(a.session); return true;
    case 'parse': { const s = openSession({ ...a, size: a.bytes && a.bytes.byteLength }); feedSession(s, a.bytes); return await finishSession(s); }
    case 'info': return { version: VERSION, pdfjs: S.pdfjsVersion, mammoth: !!S.mammoth, sessions: S.sessions.size, vendorBase: S.vendorBase };
    default: throw new Error('unknown op ' + op);
  }
}

self.onmessage = async (ev) => {
  const m = ev.data;
  if (!m || typeof m !== 'object' || m.op === undefined) return; // not ours (pdf.js's own chatter)
  if (m.op === 'counts') { // the page's answer to a count request
    const a = m.args || {}, p = S.pending.get(a.cid); S.pending.delete(a.cid);
    if (p) { if (a.error) p.reject(new Error(String(a.error))); else p.resolve(a.counts); }
    if (m.id !== undefined) post({ id: m.id, ok: true, result: true });
    return;
  }
  try {
    const result = await handle(m.op, m.args || {});
    post({ id: m.id, ok: true, result });
  } catch (e) {
    post({ id: m.id, ok: false, error: String(e && e.message || e) });
  }
};
