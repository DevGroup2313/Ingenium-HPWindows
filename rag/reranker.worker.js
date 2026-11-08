// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's optional reranker (rag/DESIGN.md section 13), a module worker. Her task (31 October 2026): "Reranker. Optional, off
// by default, loaded on demand. Use a small cross-encoder that has a working ONNX export for Transformers.js. Confirm this on Hugging
// Face before choosing a model." Confirmed on Hugging Face on 8 November 2026: Xenova/ms-marco-MiniLM-L-6-v2 (revision a0914435), tagged
// transformers.js, onnx and text-classification, its quantized export onnx/model_quantized.onnx; a conversion of
// cross-encoder/ms-marco-MiniLM-L6-v2 (Apache-2.0). It reads a question and a passage together and gives one number: how well the
// passage answers the question. It is loaded only when the chat asks for reranking, from vendor/models/ on the app's own address.
//
// Messages (the folder search's one protocol): request {id, op, args}; reply {id, ok: true, result} | {id, ok: false, error}
//   init({vendorBase, modelId = RERANK_MODEL, dtype = 'q8'})  -> {modelId, dtype, loadMs}
//   score({query, texts})                                      -> [number per text] (the model's logits: higher is a better answer)
//   info()                                                     -> {modelId, dtype, calls, scored}

export const RERANK_MODEL = 'Xenova/ms-marco-MiniLM-L-6-v2';
const ONNX_SUFFIX = { fp32: '', fp16: '_fp16', int8: '_int8', uint8: '_uint8', q8: '_quantized', q4: '_q4' };
const BATCH = 8, MAX_TOKENS = 512;
let T = null, cur = null;

class RerankError extends Error { constructor(code, message) { super(message); this.code = code; } }
const bad = (m) => new RerankError('BAD_ARGS', m);

function configure(vendorBase) {
  const env = T.env;
  env.allowRemoteModels = false;   // the model comes from vendor/models and from nowhere else
  env.allowLocalModels = true;
  env.localModelPath = new URL(vendorBase + 'models/').pathname;
  env.useBrowserCache = false;
  env.useWasmCache = false;
  const wasm = env.backends.onnx.wasm;
  wasm.wasmPaths = { mjs: vendorBase + 'ort/ort-wasm-simd-threaded.mjs', wasm: vendorBase + 'ort/ort-wasm-simd-threaded.wasm' };
  wasm.numThreads = 1;
}

async function init(a) {
  const vendorBase = a.vendorBase, modelId = a.modelId || RERANK_MODEL, dtype = a.dtype || 'q8';
  if (typeof vendorBase !== 'string' || !/\/$/.test(vendorBase)) throw bad('vendorBase must be the URL of the vendor folder, ending in /');
  let vb; try { vb = new URL(vendorBase); } catch (_) { throw bad('vendorBase must be a full address'); }
  if (vb.origin !== self.location.origin) throw bad('vendorBase must be on the page\'s own address');
  if (!Object.prototype.hasOwnProperty.call(ONNX_SUFFIX, dtype)) throw bad('dtype ' + dtype + ' is not one of ' + Object.keys(ONNX_SUFFIX).join(', '));
  if (!/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(modelId) || modelId.includes('..')) throw bad('modelId must look like owner/name');
  const base = vendorBase + 'models/' + modelId + '/', missing = [];
  for (const f of ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'onnx/model' + ONNX_SUFFIX[dtype] + '.onnx']) {
    let r; try { r = await fetch(base + f, { method: 'HEAD', cache: 'no-store' }); } catch (e) { throw new RerankError('MODEL_UNREADABLE', 'the reranker\'s files could not be read: ' + String((e && e.message) || e)); }
    if (r.status === 404) missing.push(f);
  }
  if (missing.length) throw new RerankError('MODEL_NOT_INSTALLED', 'the reranker ' + modelId + ' is not on this computer: ' + missing.join(', ') + ' missing');
  const t0 = performance.now();
  if (!T) T = await import(vendorBase + 'transformers/transformers.min.js');
  configure(vendorBase);
  const tokenizer = await T.AutoTokenizer.from_pretrained(modelId);
  const model = await T.AutoModelForSequenceClassification.from_pretrained(modelId, { dtype });
  cur = { modelId, dtype, tokenizer, model, calls: 0, scored: 0 };
  await scoreAll('warm up', ['warm up']);
  cur.calls = 0; cur.scored = 0;
  return { modelId, dtype, loadMs: Math.round(performance.now() - t0) };
}

async function scoreAll(query, texts) {
  const out = [];
  for (let i = 0; i < texts.length; i += BATCH) {
    const part = texts.slice(i, i + BATCH);
    const inputs = cur.tokenizer(part.map(() => query), { text_pair: part, padding: true, truncation: true, max_length: MAX_TOKENS });
    const { logits } = await cur.model(inputs);
    const data = Array.from(logits.data), per = data.length / part.length;   // one number for each pair (the ms-marco cross-encoders give one)
    for (let j = 0; j < part.length; j++) out.push(data[j * per]);
  }
  cur.calls++; cur.scored += texts.length;
  return out;
}

const OPS = {
  init,
  async score(a) {
    if (!cur) throw new RerankError('NOT_READY', 'the reranker is not loaded: call init first');
    if (typeof a.query !== 'string' || !Array.isArray(a.texts) || a.texts.some((t) => typeof t !== 'string')) throw bad('score needs a query and an array of texts');
    return scoreAll(a.query, a.texts);
  },
  info() { return cur ? { modelId: cur.modelId, dtype: cur.dtype, calls: cur.calls, scored: cur.scored } : null; },
};

let chain = Promise.resolve();
async function handle(m) {
  const { id, op, args } = m || {};
  try {
    if (!Object.prototype.hasOwnProperty.call(OPS, op)) throw new RerankError('UNKNOWN_OP', 'unknown op ' + op);
    self.postMessage({ id, ok: true, result: await OPS[op](args || {}) });
  } catch (e) {
    try { self.postMessage({ id, ok: false, error: { code: (e && e.code) || 'ERROR', message: String((e && e.message) || e) } }); } catch (_) { /* the page is gone */ }
  }
}
self.onmessage = (e) => { chain = chain.then(() => handle(e.data)); };
