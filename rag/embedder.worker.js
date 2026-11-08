// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's embedder (rag/DESIGN.md section 3). A module worker that runs a sentence-embedding model with
// Transformers.js 4.3.2 (vendor/transformers/transformers.min.js: the build that has ONNX Runtime inside it) on the processor
// (WebAssembly, one thread: the apps send no cross-origin isolation headers). The model is read from vendor/models only;
// nothing goes online.
//
// Messages: request {id, op, args}; reply {id, ok: true, result} or {id, ok: false, error: {name, message, code, stack}}.
// Events carry no id: {event: 'hello', worker: 'embedder'}, and while a model loads
// {event: 'progress', stage: 'library' | 'tokenizer' | 'model' | 'warm-up' | 'ready', modelId, ms}. (There is no byte count: asking
// Transformers.js for one makes it read config.json and the model file twice, a cost that grows with the model; the files are on
// this computer, so a stage is enough.)
//
// Ops
//   init({modelId = 'Xenova/bge-small-en-v1.5', dtype = 'q8', device = 'wasm', vendorBase, pooling = 'mean', batchSize = 16,
//         queryPrefix, passagePrefix}) -> {modelId, dtype, device, pooling, key, dims, maxTokens, ...}
//        A second init for the model that is loaded changes nothing and loads nothing. A different model is loaded first and
//        replaces the old one only when it has loaded, so a model that is not on this computer leaves the working one in place.
//   count({texts, special = true}) -> [tokens]    the model's own tokenizer; with special = true the two special tokens
//        ([CLS], [SEP]) are counted, so the number is what the model receives; a text is never cut short here
//   embed({texts, kind = 'passage' | 'query'}) -> [Float32Array]    mean pooling, then normalised to length 1; batches of
//        16 (the batch size); a query gets the model's instruction ahead of it; each vector has a buffer of its own
//   info() -> {modelId, dims, maxTokens, calls, embedded, batches, counted, atLimit, ...}
//        calls = embed requests served, embedded = texts embedded, batches = model runs; all count from the init that loaded
//        the model (a reusing init does not reset them), and a request that fails changes none of them
//   dispose() -> frees the model

const DEFAULT_MODEL = 'Xenova/bge-small-en-v1.5';
const BGE_QUERY_PREFIX = 'Represent this sentence for searching relevant passages: ';
const DEFAULT_BATCH = 16;
// DEFAULT_DTYPE_SUFFIX_MAPPING of Transformers.js: the model file is onnx/model<suffix>.onnx
const ONNX_SUFFIX = { fp32: '', fp16: '_fp16', int8: '_int8', uint8: '_uint8', q8: '_quantized', q4: '_q4', q4f16: '_q4f16', bnb4: '_bnb4' };

class EmbedError extends Error {
  constructor(code, message) { super(message); this.name = 'EmbedError'; this.code = code; }
}
const bad = (m) => new EmbedError('BAD_ARGS', m);

let T = null;        // the Transformers.js module
let cur = null;      // the loaded model
let loads = 0;

// The instruction a retrieval model wants ahead of a search query. The English BGE models (small, base, large) take the
// sentence below; the passages stay as they are. Other models are told through init({queryPrefix, passagePrefix}).
function defaultPrefixes(modelId) {
  if (/(^|\/)bge-(small|base|large)-en/i.test(modelId)) return { query: BGE_QUERY_PREFIX, passage: '' };
  return { query: '', passage: '' };
}

function configure(vendorBase) {
  const env = T.env;
  if (new URL(vendorBase).origin !== self.location.origin) throw bad('vendorBase must be on the page\'s own address');
  env.allowRemoteModels = false;   // the model comes from vendor/models and from nowhere else
  env.allowLocalModels = true;
  // Transformers.js looks at local files only when localModelPath is a path, not a full address (its file check treats an
  // address as "remote" and finds nothing), so the path of vendor/models/ on this origin is given.
  env.localModelPath = new URL(vendorBase + 'models/').pathname;
  env.useBrowserCache = false;     // the files are on this computer already: no second copy in the browser's cache
  env.useWasmCache = false;        // likewise for ONNX Runtime's own files
  const wasm = env.backends.onnx.wasm;
  // The runtime's JavaScript and WebAssembly files, named in full: with only a folder given it asks for the ".asyncify" pair,
  // which is not shipped. The plain pair is the CPU build (the one Transformers.js itself picks for Safari before 26).
  wasm.wasmPaths = { mjs: vendorBase + 'ort/ort-wasm-simd-threaded.mjs', wasm: vendorBase + 'ort/ort-wasm-simd-threaded.wasm' };
  wasm.numThreads = 1;
}

// A model that is not on this computer would otherwise fail inside Transformers.js with a message about "tokenizer_class".
async function checkInstalled(vendorBase, modelId, dtype) {
  if (!Object.prototype.hasOwnProperty.call(ONNX_SUFFIX, dtype)) throw bad('dtype ' + dtype + ' is not one of ' + Object.keys(ONNX_SUFFIX).join(', '));
  if (!/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(modelId) || modelId.includes('..')) throw bad('modelId must look like owner/name');
  const base = vendorBase + 'models/' + modelId + '/';
  const files = ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'onnx/model' + ONNX_SUFFIX[dtype] + '.onnx'];
  const missing = [];
  for (const f of files) {
    let r;
    try { r = await fetch(base + f, { method: 'HEAD', cache: 'no-store' }); }
    catch (e) { throw new EmbedError('MODEL_UNREADABLE', 'the model files at ' + base + ' could not be read: ' + String((e && e.message) || e)); }
    if (r.status === 404) missing.push(f);
  }
  if (missing.length) throw new EmbedError('MODEL_NOT_INSTALLED', 'the model ' + modelId + ' (' + dtype + ') is not on this computer: ' + missing.join(', ') + ' missing from ' + base);
}

function finiteMax(...vals) {
  const ok = vals.filter((v) => Number.isFinite(v) && v > 0 && v < 1e6);
  return ok.length ? Math.min(...ok) : null;
}

function tensorDispose(t) { try { if (t && typeof t.dispose === 'function') t.dispose(); } catch (_) { /* nothing to free */ } }

// One batch through the model: tokenise (padded to the longest, cut at the model's limit), run, pool, normalise.
async function runBatch(m, strings) {
  const enc = m.tokenizer(strings, { padding: true, truncation: true });
  const out = await m.model(enc);
  const hidden = out.last_hidden_state || out.token_embeddings || out.logits;
  if (!hidden) throw new EmbedError('MODEL_OUTPUT', 'the model gave no hidden states (its outputs: ' + Object.keys(out).join(', ') + ')');
  let pooled = m.pooling === 'cls' ? hidden.slice(null, 0) : T.mean_pooling(hidden, enc.attention_mask);
  pooled = pooled.normalize(2, -1);
  const dims = pooled.dims[pooled.dims.length - 1];
  const data = pooled.data;
  const vecs = [];
  for (let i = 0; i < strings.length; i++) vecs.push(data.slice(i * dims, (i + 1) * dims));   // a copy, with a buffer of its own
  let atLimit = 0;
  const len = enc.input_ids.dims[1];
  if (len >= m.maxTokens) {   // some text reached the model's limit and may have been cut
    const mask = enc.attention_mask.data;
    for (let i = 0; i < strings.length; i++) { let n = 0; for (let j = 0; j < len; j++) if (Number(mask[i * len + j]) !== 0) n++; if (n >= m.maxTokens) atLimit++; }
  }
  tensorDispose(hidden); tensorDispose(pooled); tensorDispose(enc.input_ids); tensorDispose(enc.attention_mask);
  return { vecs, dims, atLimit };
}

function summary() {
  if (!cur) return { modelId: null, ready: false, loads, calls: 0, embedded: 0, batches: 0, counted: 0, atLimit: 0 };
  return {
    modelId: cur.modelId, dtype: cur.dtype, device: cur.device, pooling: cur.pooling, key: cur.modelId + '|' + cur.dtype + '|' + cur.pooling,
    dims: cur.dims, maxTokens: cur.maxTokens, batchSize: cur.batchSize,
    calls: cur.calls, embedded: cur.embedded, batches: cur.batches, counted: cur.counted, atLimit: cur.atLimit,
    loads, loadMs: cur.loadMs, ready: true,
  };
}

async function init(a) {
  const modelId = a.modelId || DEFAULT_MODEL;
  const dtype = a.dtype || 'q8';
  const device = a.device || 'wasm';
  const pooling = a.pooling || 'mean';
  const batchSize = a.batchSize == null ? DEFAULT_BATCH : a.batchSize;
  if (typeof modelId !== 'string') throw bad('modelId must be a string');
  if (pooling !== 'mean' && pooling !== 'cls') throw bad("pooling must be 'mean' or 'cls'");
  if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 64) throw bad('batchSize must be a whole number from 1 to 64');
  if (cur && cur.modelId === modelId && cur.dtype === dtype && cur.device === device && cur.pooling === pooling && cur.batchSize === batchSize) {
    return summary();   // the model is loaded already
  }
  if (typeof a.vendorBase !== 'string' || !/\/$/.test(a.vendorBase)) throw bad('vendorBase must be the URL of the vendor folder, ending in /');
  const vendorBase = a.vendorBase;
  // checked before anything is asked of that address (the check in configure() came after the first requests)
  let vb;
  try { vb = new URL(vendorBase); } catch (_) { throw bad('vendorBase must be a full address'); }
  if (vb.origin !== self.location.origin) throw bad('vendorBase must be on the page\'s own address');
  await checkInstalled(vendorBase, modelId, dtype);
  const t0 = performance.now();
  if (!T) T = await import(vendorBase + 'transformers/transformers.min.js');
  configure(vendorBase);

  const stage = (name) => self.postMessage({ event: 'progress', stage: name, modelId, ms: Math.round(performance.now() - t0) });
  stage('library');
  const tokenizer = await T.AutoTokenizer.from_pretrained(modelId);
  stage('tokenizer');
  const model = await T.AutoModel.from_pretrained(modelId, { dtype, device });
  stage('model');
  const prefixes = defaultPrefixes(modelId);
  const next = {
    modelId, dtype, device, pooling, batchSize, tokenizer, model,
    queryPrefix: a.queryPrefix != null ? String(a.queryPrefix) : prefixes.query,
    passagePrefix: a.passagePrefix != null ? String(a.passagePrefix) : prefixes.passage,
    maxTokens: finiteMax(tokenizer.model_max_length, model.config && model.config.max_position_embeddings) || 512,
    dims: 0, calls: 0, embedded: 0, batches: 0, counted: 0, atLimit: 0, loadMs: 0,
  };
  // a first run: finds the vector length (the model's own, not an assumption) and takes the start-up cost off the first request
  const probe = await runBatch(next, ['warm up']);
  next.dims = probe.dims;
  next.loadMs = Math.round(performance.now() - t0);
  stage('warm-up');
  const old = cur;
  cur = next; loads++;
  if (old) { try { await old.model.dispose(); } catch (_) { /* the old model is gone either way */ } }
  stage('ready');
  return summary();
}

function needModel() { if (!cur) throw new EmbedError('NOT_READY', 'no model is loaded: call init first'); return cur; }
function texts(a) {
  if (!a || !Array.isArray(a.texts)) throw bad('texts must be an array of strings');
  a.texts.forEach((t, i) => { if (typeof t !== 'string') throw bad('texts[' + i + '] must be a string'); });
  return a.texts;
}

const OPS = {
  init,
  info: () => summary(),
  ops: () => Object.keys(OPS),

  count(a) {
    const m = needModel(), list = texts(a);
    if (!list.length) return [];
    const enc = m.tokenizer(list, { add_special_tokens: a.special !== false, padding: false, truncation: false, return_tensor: false });
    m.counted += list.length;
    return enc.input_ids.map((ids) => ids.length);
  },

  async embed(a) {
    const m = needModel(), list = texts(a);
    const kind = a.kind == null ? 'passage' : a.kind;
    if (kind !== 'passage' && kind !== 'query') throw bad("kind must be 'passage' or 'query'");
    if (!list.length) return [];
    const prefix = kind === 'query' ? m.queryPrefix : m.passagePrefix;
    const inputs = prefix ? list.map((t) => prefix + t) : list;
    const out = new Array(list.length);
    let batches = 0, atLimit = 0;
    for (let i = 0; i < inputs.length; i += m.batchSize) {
      const r = await runBatch(m, inputs.slice(i, i + m.batchSize));
      for (let j = 0; j < r.vecs.length; j++) out[i + j] = r.vecs[j];
      batches++; atLimit += r.atLimit;
    }
    m.calls++; m.embedded += list.length; m.batches += batches; m.atLimit += atLimit;
    return out;
  },

  async dispose() {
    if (cur) { const m = cur; cur = null; try { await m.model.dispose(); } catch (_) { /* freed or not, it is let go */ } }
    return { disposed: true };
  },
};

function serializeError(e) {
  return { name: (e && e.name) || 'Error', message: String((e && e.message) || e), code: (e && e.code) || null, stack: e && e.stack ? String(e.stack) : null };
}

function collectTransfer(result) {
  const seen = new Set(), list = [];
  if (Array.isArray(result)) for (const v of result) {
    if (v instanceof Float32Array && v.byteOffset === 0 && v.byteLength === v.buffer.byteLength && !seen.has(v.buffer)) { seen.add(v.buffer); list.push(v.buffer); }
  }
  return list;
}

let chain = Promise.resolve();
async function handle(m) {
  const { id, op, args } = m || {};
  try {
    if (!Object.prototype.hasOwnProperty.call(OPS, op)) throw new EmbedError('UNKNOWN_OP', 'unknown op ' + op);
    const result = await OPS[op](args || {});
    self.postMessage({ id, ok: true, result }, collectTransfer(result));
  } catch (e) {
    try { self.postMessage({ id, ok: false, error: serializeError(e) }); } catch (_) { /* the page is gone */ }
  }
}
self.onmessage = (e) => { chain = chain.then(() => handle(e.data)); };
self.postMessage({ event: 'hello', worker: 'embedder' });
