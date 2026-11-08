// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// Retrieval, phase 2 of the folder search (rag/DESIGN.md section 9).
//
//   import { retrieve } from './retriever.js';
//   const r = await retrieve({ store, embedder }, 'How much did the boiler repair cost?', 'me', { k: 5 });
//   r.passages   // [{id, folderId, folderName, folderPath, filePath, headingPath, charStart, charEnd, page, text,
//                //   score, ftsRank, vecRank, distance}], best first, at most k
//
// What it does, in order:
//   1. The user is found by name in the index (principal.find). A name the index does not know finds nothing.
//   2. The text search (FTS5, bm25) starts at once in the store's worker; the question's vector is made meanwhile in the reading
//      model's worker (bge's query form), and the vector search (vec0, cosine) follows. Each gives its PER_LIST (20) best, and each
//      answers only within the folders the user may read: the folder_acl condition is inside both SQL statements
//      (rag/store.worker.js, search.fts and search.vec), before anything is ranked or fused. opts.folderIds, the folders the chat
//      was given, can only narrow that set.
//   3. The two lists are fused by Reciprocal Rank Fusion: a passage scores the sum, over the lists it is in, of 1 / (RRF_K + its
//      rank there), ranks counted from 1, RRF_K = 60 (Cormack, Clarke and Buettcher, SIGIR 2009). Equal scores go to the passage
//      with the better single rank, then to the lower id, so the order never depends on chance.
//   4. The best TOP (5) are read back with what a citation needs (chunk.get, which checks the folders once more).
// Nothing leaves this computer: the store and the reading model are workers of this page.

export const RRF_K = 60;
export const PER_LIST = 20;
export const TOP = 5;
export const MAX_QUESTION = 4000;   // characters of a question kept (the reading model reads at most 512 tokens of it anyway)

/** Reciprocal Rank Fusion of lists of ids, each best first. -> [{id, score, bestRank, ranks: [rank or null per list]}], best first. */
export function rrf(lists, k = RRF_K) {
  if (!Array.isArray(lists)) throw new TypeError('lists must be an array of arrays of ids');
  if (!(typeof k === 'number' && Number.isFinite(k) && k >= 0)) throw new TypeError('k must be a number of at least 0');
  const by = new Map();
  lists.forEach((list, li) => {
    const seen = new Set();
    (list || []).forEach((id, i) => {
      if (seen.has(id)) return;          // an id twice in one list counts once, at its better rank
      seen.add(id);
      let e = by.get(id);
      if (!e) { e = { id, score: 0, bestRank: Infinity, ranks: lists.map(() => null) }; by.set(id, e); }
      e.score += 1 / (k + i + 1);
      e.ranks[li] = i + 1;
      if (i + 1 < e.bestRank) e.bestRank = i + 1;
    });
  });
  return Array.from(by.values()).sort((a, b) => b.score - a.score || a.bestRank - b.bestRank || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
}

function argError(message) { const e = new Error(message); e.code = 'BAD_ARGS'; return e; }

/**
 * The passages of the folders `userId` may read that answer `query` best.
 * opts: {k = TOP, perList = PER_LIST, rrfK = RRF_K, folderIds (narrows the user's folders)}.
 * -> {passages, principalId, match (the text search's MATCH, or null), fts: [hits], vec: [hits], ms}
 */
export async function retrieve(ctx, query, userId, opts = {}) {
  const store = ctx && ctx.store, embedder = ctx && ctx.embedder;
  if (!store || !embedder) throw argError('retrieve needs the store and the embedder');
  if (typeof query !== 'string') throw argError('the question must be text');
  if (typeof userId !== 'string' || !userId) throw argError('the user must be named');
  const top = opts.k == null ? TOP : opts.k, per = opts.perList == null ? PER_LIST : opts.perList;
  const k = opts.rrfK == null ? RRF_K : opts.rrfK;
  if (!Number.isInteger(top) || top < 1 || top > 50) throw argError('k must be a whole number from 1 to 50');
  if (!Number.isInteger(per) || per < top || per > 200) throw argError('perList must be a whole number from k to 200');
  const t0 = typeof performance !== 'undefined' ? performance.now() : Date.now();
  const q = query.trim().slice(0, MAX_QUESTION);
  const empty = (principalId, reason) => ({ passages: [], principalId, match: null, fts: [], vec: [], reason, ms: 0 });
  if (!q) return empty(null, 'EMPTY_QUESTION');
  const who = await store.principal.find({ name: userId });
  if (!who) return empty(null, 'NO_SUCH_USER');
  const args = { principalId: who.id, k: per };
  if (opts.folderIds != null) args.folderIds = opts.folderIds;
  // the text search starts now; the question's vector is made meanwhile, in the reading model's own worker
  const ftsP = store.search.fts(Object.assign({ query: q }, args));
  let vecHits = [];
  try {
    const [qv] = await embedder.embed({ texts: [q], kind: 'query' });
    vecHits = (await store.search.vec(Object.assign({ embedding: qv }, args))).hits;
  } catch (e) { ftsP.catch(() => undefined); throw e; }
  const fts = await ftsP;
  const fused = rrf([fts.hits.map((h) => h.id), vecHits.map((h) => h.id)], k).slice(0, top);
  const rows = fused.length ? await store.chunk.get({ ids: fused.map((f) => f.id), principalId: who.id }) : [];
  const byId = new Map(rows.map((r) => [r.id, r]));
  const dist = new Map(vecHits.map((h) => [h.id, h.distance]));
  const passages = fused.filter((f) => byId.has(f.id)).map((f) => Object.assign({}, byId.get(f.id), {
    score: f.score, ftsRank: f.ranks[0], vecRank: f.ranks[1], distance: dist.has(f.id) ? dist.get(f.id) : null }));
  const ms = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - t0;
  return { passages, principalId: who.id, match: fts.match, fts: fts.hits, vec: vecHits, ms };
}

export default retrieve;
