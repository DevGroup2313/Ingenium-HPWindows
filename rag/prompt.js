// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The grounding contract, phase 3 of the folder search (rag/DESIGN.md section 10; the owner's task of 31 October 2026, sent again on
// 8 November 2026). Pure functions: no worker, no network, no page. The chat (build/modules/r257.js) uses them through IngRag.prompt;
// the tests and the evaluation (phase 5) import them directly.
//
//   NOT_FOUND                              "I could not find that in the selected folders."
//   systemPrompt(passages, question)       the system prompt of her task, word for word, with the context numbered [1] to [n]
//   ground(result, question, budgetChars)  -> {notFound, passages, system, messages, dropped}: what one chat turn sends, or NOT_FOUND
//   isNotFound(answer)                     the model's reply is the NOT_FOUND sentence (quotes, spaces and case aside)
//   citations(text)                        the [n] marks of a reply: [{n, start, end, raw}] ([1], [1, 3], [2-4] and [1][2] are read)
//   sentences(text)                        a reply cut into sentences, each with the [n] marks it carries
//   checkGrounded(answer, count)           -> {ok, notFound, sentences, flagged, badRefs, cited}: every sentence carries a citation and
//                                          every citation points to one of the count passages given
//   sourceLabel(passage) / sourceList(passages)   how a passage is named in the prompt and under the reply
//
// A passage is what IngRag.retrieve gives (rag/retriever.js): {folderName, filePath, chunkIndex, headingPath, page, text, ...}. The
// chat keeps a smaller copy of it with the reply ({folder, file, chunk, heading, page, text, ...}); both are read here.

export const NOT_FOUND = 'I could not find that in the selected folders.';

// Her system prompt, as written in the task. Only the context and the question are filled in.
export const SYSTEM_LINES = Object.freeze([
  'You are answering strictly from the provided context.',
  'If the context does not contain the answer, respond exactly: "' + NOT_FOUND + '"',
  'Do not use outside knowledge.',
  'Support every claim with the context and cite it as [n].',
]);

const str = (x) => (x == null ? '' : String(x));
const field = (p, ...names) => { for (const n of names) if (p && p[n] != null && p[n] !== '') return p[n]; return null; };

/** The passage's path as the prompt and the reply show it: the folder's name, then the file's path inside it. */
export function sourcePath(p) {
  const folder = str(field(p, 'folderName', 'folder')), file = str(field(p, 'filePath', 'file', 'file_path'));
  return folder ? folder + '/' + file : file;
}

/** "{file_path}#{chunk_index} ({heading_path})" as the task writes it. The brackets are left out when a passage has no heading and no
 *  page; a PDF's page is added inside them ("Results > Table 2, page 4"), so the model and the reader can find it. */
export function sourceLabel(p) {
  const idx = field(p, 'chunkIndex', 'chunk', 'chunk_index');
  const head = str(field(p, 'headingPath', 'heading', 'heading_path')).trim();
  const page = field(p, 'page');
  const inner = [head, page != null ? 'page ' + page : ''].filter(Boolean).join(', ');
  return sourcePath(p) + '#' + (idx == null ? 0 : idx) + (inner ? ' (' + inner + ')' : '');
}

/** The context block: "[1] label\ntext" for each passage, best first, a blank line between them. */
export function contextBlock(passages) {
  return (passages || []).map((p, i) => '[' + (i + 1) + '] ' + sourceLabel(p) + '\n' + str(p.text).trim()).join('\n\n');
}

/** The system prompt of the task, with the context and the question filled in. */
export function systemPrompt(passages, question) {
  return SYSTEM_LINES.join('\n') + '\nContext:\n' + contextBlock(passages) + '\n\nQuestion: ' + str(question).trim();
}

/**
 * What one chat turn sends. result: what IngRag.retrieve returned ({passages}); question: her words; budgetChars: the room the chat's
 * model has for what it reads, in characters (the chat's own chatBudget(); 0 or nothing for no limit).
 * Zero passages: {notFound: true} and nothing is to be sent: the chat answers NOT_FOUND without calling the model.
 * Otherwise the passages are kept best first while the system prompt and the question fit the room; the worst go first, at least one
 * stays, and the citations [1] to [n] number only the passages kept. -> {notFound, passages, dropped, system, messages}
 */
export function ground(result, question, budgetChars) {
  const all = (result && Array.isArray(result.passages)) ? result.passages : [];
  const q = str(question).trim();
  if (!all.length || !q) return { notFound: true, passages: [], dropped: 0, system: '', messages: [] };
  let kept = all.slice();
  const room = Number(budgetChars) > 0 ? Number(budgetChars) : Infinity;
  while (kept.length > 1 && systemPrompt(kept, q).length + q.length > room) kept = kept.slice(0, -1);
  const system = systemPrompt(kept, q);
  return { notFound: false, passages: kept, dropped: all.length - kept.length, system, messages: [{ role: 'system', content: system }, { role: 'user', content: q }] };
}

const norm = (s) => str(s).replace(/[“”„«»"'‘’`*_]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
const NF = norm(NOT_FOUND);

/** True when the reply is the NOT_FOUND sentence, with or without quotes, emphasis, a final full stop or a [n] after it. */
export function isNotFound(answer) {
  const a = norm(str(answer).replace(/\[\d[\d\s,;–-]*\]/g, ''));
  return a === NF || a === NF.replace(/\.$/, '');
}

/**
 * True when the reply declares NOT_FOUND: it is the sentence, or the sentence is one of its sentences and it cites no passage it was
 * given (count: how many). Her contract asks the model to "respond exactly" with NOT_FOUND when the context does not hold the answer;
 * small models add an explanation around it (6.63, found by the evaluation). A reply that cites a passage given is an answer, and is
 * checked as one.
 */
export function declaresNotFound(answer, count) {
  if (isNotFound(answer)) return true;
  const ss = sentences(answer);
  if (!ss.some((x) => isNotFound(x.text))) return false;
  return !ss.some((x) => x.cites.some((n) => n >= 1 && n <= count));
}

// [1]  [1, 3]  [1,3]  [1; 3]  [2-4]  [2–4]   (a range is expanded, at most 20 numbers); [1](link) is a Markdown link, not a citation
const CITE = /\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?!\()/g;

/** The citation marks of a text: [{n, start, end, raw}] in order, one entry for each number a mark names. */
export function citations(text) {
  const out = [], s = str(text);
  CITE.lastIndex = 0;
  let m;
  while ((m = CITE.exec(s))) {
    const nums = [];
    for (const part of m[1].split(/\s*[,;]\s*/)) {
      const r = /^(\d+)\s*[–-]\s*(\d+)$/.exec(part);
      if (r) { const a = +r[1], b = +r[2]; if (b >= a && b - a < 20) for (let k = a; k <= b; k++) nums.push(k); else nums.push(a, b); }
      else if (/^\d+$/.test(part)) nums.push(+part);
    }
    for (const n of nums) out.push({ n, start: m.index, end: m.index + m[0].length, raw: m[0] });
  }
  return out;
}

const ONLY_CITES = /^(?:\s*\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\])+[\s.,;:!?)]*$/;

/**
 * The reply cut into sentences: [{text, cites: [n...], lead}]. Code blocks are left out (code is not a claim the folders can carry),
 * headings and table rules too; each line of a list is its own sentence. A run of marks that follows a sentence's full stop
 * ("... two years. [1]") belongs to that sentence. lead: the sentence ends with a colon (it introduces what follows).
 */
export function sentences(text) {
  const body = str(text).replace(/```[\s\S]*?(```|$)/g, '\n').replace(/\r/g, '');
  const out = [];
  for (let line of body.split('\n')) {
    line = line.trim();
    if (!line || /^#{1,6}\s/.test(line) || /^\|?[\s:|-]+\|?$/.test(line) && /-/.test(line)) continue;
    line = line.replace(/^>\s*/, '').replace(/^(?:[-*+•]|\d{1,3}[.)])\s+/, '').replace(/^\|\s*|\s*\|$/g, '');
    if (!line) continue;
    // sentence ends: . ! ? (and a closing quote or bracket) followed by a space and a capital, a digit, a quote or a mark
    const parts = line.split(/(?<=[.!?]["'”’)]?)\s+(?=[\[A-Z0-9"'“‘(À-Þ])/);
    for (let part of parts) {
      part = part.trim(); if (!part) continue;
      const lead = ONLY_CITES.exec(part) ? null : /^((?:\[\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*\]\s*)+)(.*)$/.exec(part);
      if (ONLY_CITES.test(part)) {             // marks alone: they belong to the sentence before
        if (out.length) { out[out.length - 1].text += ' ' + part; out[out.length - 1].cites.push(...citations(part).map((c) => c.n)); }
        continue;
      }
      if (lead && out.length && /[.!?]["'”’)]?$/.test(out[out.length - 1].text)) {   // "... years. [1] The next ..."
        out[out.length - 1].text += ' ' + lead[1].trim();
        out[out.length - 1].cites.push(...citations(lead[1]).map((c) => c.n));
        part = lead[2].trim(); if (!part) continue;
      }
      out.push({ text: part, cites: citations(part).map((c) => c.n), lead: /:\s*(\[[^\]]*\]\s*)*$/.test(part) });
    }
  }
  return out.filter((s) => /[\p{L}\p{N}]/u.test(s.text.replace(/\[[^\]]*\]/g, '')));
}

/**
 * Is every sentence of the reply carried by a citation, and does every citation point to a passage given? count: how many passages
 * the model was given ([1] to [count]). A reply that is the NOT_FOUND sentence needs no citation. A sentence that only introduces what
 * follows (it ends with a colon) is not counted as a claim. -> {ok, notFound, sentences, flagged: [text], badRefs: [n], cited: [n]}
 */
export function checkGrounded(answer, count) {
  if (isNotFound(answer)) return { ok: true, notFound: true, sentences: [], flagged: [], badRefs: [], cited: [] };
  const ss = sentences(answer);
  const flagged = [], bad = new Set(), cited = new Set();
  for (const s of ss) {
    for (const n of s.cites) { if (n >= 1 && n <= count) cited.add(n); else bad.add(n); }
    if (!s.lead && !s.cites.some((n) => n >= 1 && n <= count)) flagged.push(s.text);
  }
  const badRefs = Array.from(bad).sort((a, b) => a - b);
  return { ok: ss.length > 0 && !flagged.length && !badRefs.length, notFound: false, sentences: ss, flagged, badRefs, cited: Array.from(cited).sort((a, b) => a - b) };
}

/** The passages as the reply lists them: [{n, path, label, heading, page, folder, file}]. */
export function sourceList(passages) {
  return (passages || []).map((p, i) => ({
    n: i + 1, path: sourcePath(p), label: sourceLabel(p),
    heading: str(field(p, 'headingPath', 'heading', 'heading_path')), page: field(p, 'page'),
    folder: str(field(p, 'folderName', 'folder')), file: str(field(p, 'filePath', 'file', 'file_path')),
  }));
}

export default { NOT_FOUND, SYSTEM_LINES, systemPrompt, contextBlock, sourceLabel, sourcePath, ground, isNotFound, declaresNotFound, citations, sentences, checkGrounded, sourceList };
