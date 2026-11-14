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
// 6.64: a bracket joined to a word or number before it (strains[7], x[2]) is an index, not a citation (the evaluation found one)
const CITE = /(?<![\p{L}\p{N}_])\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?!\()/gu;

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
    // 6.64: a title or a short form (Mr. Wren, Dr. Hale, St. James, No. 7, Capt. Pryce) does not end a sentence
    const parts = line.split(/(?<=[.!?]["'”’)]?)(?<!\b(?:Mr|Mrs|Ms|Mx|Dr|Prof|Rev|Sr|Jr|St|Capt|Col|Gen|Lt|Sgt|No|Nos|Fig|Figs|Vol|vs|cf|approx|etc|e\.g|i\.e)\.)\s+(?=[\[A-Z0-9"'“‘(À-Þ])/);
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

// ------------------------------------------------------------------------------------------------------------------------------
// 7.07, phase 6: the detail check. Her criterion 2 asks that every claim be supported by the context, and an unsupported claim is a
// blocker. A citation alone does not make a sentence true: the evaluation found sentences that cite a passage but give a detail the
// passage does not have ("Saturday 28th, 1781" for a logbook's "Friday 28th" of 1802; "8 on 1784-02-11" where a manifest has 8 on the
// 9th). So each sentence's exact details are looked for in the passages it cites:
//   - numbers (412, £2,140, 24.5, the 14th; "three" and "3" are the same), dates (1784-02-11 is read as 11 February 1784), weekdays and
//     months, codes (FX-208), and names (capitalised words inside the sentence);
//   - the numbers, dates, weekdays and months of one sentence must stand together in the passage: in one line of a table or a log,
//     or within two sentences of prose; names may be anywhere in the passage (a letter names its writer at the top);
//   - a sentence with no exact detail needs most of its words in the passage.
// What it cannot see: a claim that puts true details in a false relation ("from Wren to Grieve" for a letter from Grieve to Wren).
//   details(text)                     -> {nums, cal, codes, names, words}: what the sentence states exactly
//   supports(passage, text)           -> {ok, missing}: does this passage hold the sentence's details, together where they must be?
//   guard(answer, passages)           -> {text, notFound, ok, kept, repaired, removed}: the reply with every sentence cited by a passage
//                                        that holds its details; a missing or wrong citation is mended when one passage holds them all;
//                                        a sentence no passage supports is taken out (and listed); nothing left gives NOT_FOUND
const NUMWORDS = { zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9, ten: 10, eleven: 11, twelve: 12,
  thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16, seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50,
  sixty: 60, seventy: 70, eighty: 80, ninety: 90, hundred: 100 };
const MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'];
const MON = { jan: 'january', feb: 'february', mar: 'march', apr: 'april', jun: 'june', jul: 'july', aug: 'august', sep: 'september', sept: 'september', oct: 'october', nov: 'november', dec: 'december' };
const DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
const STOP = new Set(('a an and are as at be been but by did do does for from had has have he her his i if in into is it its no not of on or ' +
  'our she so than that the their them then there these they this those to was we were what when where which who whom why will with you your ' +
  'also about after before over under between during per each any all both more most other some such only own same can could should would may ' +
  'might must shall here how out up down off once again very just context passage passages provided according stated states information ' +
  'document documents file files source sources text answer question cited cites mentioned mentions shown shows given gives say says said ' +
  'found find indicates indicated noted notes note specifically based above below following follows').split(' '));
const NAME_SKIP = new Set(('the a an this that these those it its in on at from to for of by with as according based answer note however ' +
  'yes no there here i we you he she they his her their mr mrs ms dr st no capt').split(' '));

/** A text made plain for matching: ISO dates read out, thousands joined, ordinals and number words as digits, lower case. */
function plain(t) {
  return str(t)
    .replace(/\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?!\()/g, ' ')
    .replace(/\b(\d{4})-(\d{2})-(\d{2})\b/g, (m, y, mo, d) => (+mo >= 1 && +mo <= 12 ? ' ' + y + ' ' + MONTHS[+mo - 1] + ' ' + (+d) + ' ' : m))
    .replace(/(\d),(?=\d{3}\b)/g, '$1')
    .replace(/\b(\d+)(?:st|nd|rd|th)\b/gi, '$1')
    .toLowerCase()
    .replace(/\b(sept|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.?(?=\s|$|,)/g, (m, a) => MON[a] || a)
    .replace(/\b([a-z]+)\b/g, (m) => (m in NUMWORDS ? String(NUMWORDS[m]) : m));
}
const numsOf = (t) => (t.match(/\d+(?:\.\d+)?/g) || []).map((x) => String(+x));
const wordsOf = (t) => t.match(/[\p{L}][\p{L}'’-]*/gu) || [];

/** What a sentence states exactly. */
export function details(text) {
  const raw = str(text).replace(/\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?!\()/g, ' ').replace(/[*_`#>]/g, ' ');
  const codes = (raw.match(/\b[A-Za-z]{1,6}-?\d{2,}[A-Za-z0-9-]*\b/g) || []).map((c) => c.toLowerCase());
  const p = plain(raw.replace(/\b[A-Za-z]{1,6}-?\d{2,}[A-Za-z0-9-]*\b/g, ' '));
  const ws = wordsOf(p);
  const cal = Array.from(new Set(ws.filter((w) => MONTHS.includes(w) || DAYS.includes(w))));
  const nums = Array.from(new Set(numsOf(p)));
  // names: capitalised words that do not open the sentence (or a clause), and are not months, weekdays or common words
  const names = new Set();
  const toks = raw.split(/\s+/);
  toks.forEach((w, i) => {
    const clean = w.replace(/^[^\p{L}]+|[^\p{L}]+$/gu, '');
    if (!clean || !/^\p{Lu}/u.test(clean) || clean.length < 2) return;
    const prev = i ? toks[i - 1] : '';
    if (i === 0 || /[.!?:;]["'”’)]?$/.test(prev)) return;            // the first word of a sentence or clause
    const lw = clean.toLowerCase();
    if (NAME_SKIP.has(lw) || MONTHS.includes(lw) || DAYS.includes(lw) || MON[lw]) return;
    if (/^\p{Lu}+$/u.test(clean) && clean.length > 5) return;       // a shouted word, not a name
    names.add(lw.replace(/['’]s$/, ''));
  });
  const words = Array.from(new Set(ws.filter((w) => w.length > 3 && !STOP.has(w) && !/^\d/.test(w))));
  return { nums, cal, codes: Array.from(new Set(codes)), names: Array.from(names), words };
}

/** The passage cut into the places where details must stand together: the lines of a table or a log, else pairs of sentences. */
function units(text) {
  const t = str(text).replace(/\r/g, '');
  const lines = t.split('\n').map((l) => l.trim()).filter(Boolean);
  const tabular = lines.length >= 4 && lines.filter((l) => l.length < 260 && /[,\t|;]/.test(l)).length >= lines.length * 0.6;
  if (tabular) return lines.map((l, i) => (i && lines[0] !== l ? lines[0] + ' ' : '') + l);   // each row, with the header row's names
  const ss = t.replace(/\s+/g, ' ').split(/(?<=[.!?;])\s+(?=[\p{Lu}\d"'“(])/u);
  const out = [];
  for (let i = 0; i < ss.length; i++) out.push(ss[i] + ' ' + (ss[i + 1] || ''));
  // a log written one entry to a line, without full stops: each line and the one after it
  if (lines.length >= 4 && ss.length <= 2) for (let i = 0; i < lines.length; i++) out.push(lines[i] + ' ' + (lines[i + 1] || ''));
  return out;
}

/** Does this passage hold the sentence's details? -> {ok, missing: [what it lacks], share: how much of the sentence's wording it has} */
export function supports(passage, text, det) {
  const d = det || details(text);
  const ptext = str(passage && passage.text != null ? passage.text : passage);
  const P = plain(ptext), pw = new Set(wordsOf(P)), pn = new Set(numsOf(P)), praw = ptext.toLowerCase();
  const missing = [];
  for (const c of d.codes) if (!praw.includes(c)) missing.push(c);
  for (const n of d.names) if (!pw.has(n) && !praw.includes(n)) missing.push(n);
  for (const n of d.nums) if (!pn.has(n)) missing.push(n);
  for (const c of d.cal) if (!pw.has(c)) missing.push(c);
  const share = d.words.length ? d.words.filter((w) => pw.has(w) || pw.has(w.replace(/s$/, '')) || pw.has(w + 's')).length / d.words.length : 1;
  if (missing.length) return { ok: false, missing, share };
  // the numbers, dates, weekdays and months must stand together
  const together = d.nums.concat(d.cal);
  if (together.length > 1) {
    const hit = units(ptext).some((u) => { const U = plain(u), uw = new Set(wordsOf(U)), un = new Set(numsOf(U)); return d.nums.every((n) => un.has(n)) && d.cal.every((c) => uw.has(c)); });
    if (!hit) return { ok: false, missing: ['together: ' + together.join(' ')], share };
  }
  const exact = d.codes.length + d.names.length + d.nums.length + d.cal.length;
  if (!exact && d.words.length >= 2 && share < 0.6) return { ok: false, missing: ['wording ' + Math.round(share * 100) + '%'], share };
  return { ok: true, missing: [], share };
}

const META = /^(?:this|that|these|the (?:answer|information|figure|detail|fact)s?)\b[^.]*\b(?:is|are|was|were|can be)\s+(?:stated|found|given|shown|mentioned|listed|recorded|noted|written|confirmed)\b/i;
const SPLIT = /(?<=[.!?]["'”’)]?)(?<!\b(?:Mr|Mrs|Ms|Mx|Dr|Prof|Rev|Sr|Jr|St|Capt|Col|Gen|Lt|Sgt|No|Nos|Fig|Figs|Vol|vs|cf|approx|etc|e\.g|i\.e)\.)\s+(?=[\[A-Z0-9"'“‘(À-Þ])/;

/**
 * The reply as it may be shown: every sentence cited by a passage that holds its details. passages: what the model was given, [1] first.
 * -> {text, notFound, ok, kept: n, repaired: [{text, from: [n], to: n}], removed: [{text, missing}]}
 * A sentence that introduces what follows (it ends with a colon) is kept as it is; so are code blocks and headings. A sentence that
 * only points at a passage ("This is stated in [1].") is kept when its passage is one given, and it is not counted as a claim.
 */
export function guard(answer, passages) {
  const ps = Array.isArray(passages) ? passages : [];
  const count = ps.length;
  if (!count || declaresNotFound(answer, count)) return { text: NOT_FOUND, notFound: true, ok: true, kept: 0, repaired: [], removed: [] };
  const repaired = [], removed = [];
  let kept = 0;
  const judge = (sent) => {
    const body = sent.replace(/\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?!\()/g, '').trim();
    if (!/[\p{L}\p{N}]/u.test(body)) return sent;
    if (/:\s*$/.test(body)) return sent;                                  // it introduces what follows
    const cites = citations(sent).map((c) => c.n).filter((n) => n >= 1 && n <= count);
    const d = details(body);
    const exact = d.codes.length + d.names.length + d.nums.length + d.cal.length;
    if (META.test(body) && !exact) { if (cites.length) return sent; removed.push({ text: sent.trim(), missing: ['names no passage'] }); return ''; }
    if (cites.length && cites.some((n) => supports(ps[n - 1], body, d).ok)) { kept++; return sent; }
    // the passage that holds every detail, and most of the wording, mends the citation
    let best = 0, bestShare = -1;
    ps.forEach((p, i) => { const s = supports(p, body, d); if (s.ok && s.share > bestShare) { best = i + 1; bestShare = s.share; } });
    if (best && (exact || bestShare >= 0.6)) {
      const stripped = sent.replace(/\s*\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?!\()/g, '');
      const m = /^(.*?)([.!?]["'”’)]?)?\s*$/s.exec(stripped);
      repaired.push({ text: sent.trim(), from: cites, to: best });
      kept++;
      return m[1] + ' [' + best + ']' + (m[2] || '');
    }
    const why = cites.length ? supports(ps[cites[0] - 1], body, d).missing : ['no citation, and no passage holds it'];
    removed.push({ text: sent.trim(), missing: why });
    return '';
  };
  const out = [];
  let code = false;
  for (const line0 of str(answer).replace(/\r/g, '').split('\n')) {
    if (/^\s*(```|~~~)/.test(line0)) { code = !code; out.push(line0); continue; }
    if (code || !line0.trim() || /^\s*#{1,6}\s/.test(line0) || /^\s*\|?[\s:|-]+\|?\s*$/.test(line0) && /-/.test(line0)) { out.push(line0); continue; }
    const m = /^(\s*(?:>\s*)?(?:(?:[-*+•]|\d{1,3}[.)])\s+)?)(.*)$/.exec(line0);
    const lead = m[1], rest = m[2];
    // marks alone on a line belong to the sentence before (as sentences() reads them): join them to it before judging
    if (/^(?:\s*\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\])+[\s.,;:!?)]*$/.test(rest) && out.length) {
      let k = out.length - 1; while (k > 0 && !out[k].trim()) k--;
      if (out[k] && out[k].trim()) { out[k] = out[k].replace(/\s*$/, '') + ' ' + rest.trim(); out.splice(k + 1); continue; }
    }
    out.push(lead + '\u0000' + rest);
  }
  // judge each line's sentences (a run of marks after a full stop belongs to the sentence before it)
  const final = [];
  code = false;
  for (const line of out) {
    if (/^\s*(```|~~~)/.test(line)) { code = !code; final.push(line); continue; }
    if (code || line.indexOf('\u0000') < 0) { final.push(line); continue; }
    const [lead, rest] = line.split('\u0000');
    const parts = rest.split(SPLIT);
    const merged = [];
    for (const part of parts) {
      const lm = /^((?:\[\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*\]\s*)+)(.*)$/s.exec(part);
      if (lm && merged.length) { merged[merged.length - 1] += ' ' + lm[1].trim(); if (lm[2].trim()) merged.push(lm[2]); }
      else merged.push(part);
    }
    const keptParts = merged.map(judge).filter((x) => x && x.trim());
    if (keptParts.length) final.push(lead + keptParts.join(' '));
  }
  // lines left with only an opening (a colon line whose list was taken out) or a heading with nothing under it are dropped
  let text = final.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  const g = checkGrounded(text, count);
  const claims = g.sentences.filter((x) => !x.lead && !(META.test(x.text.replace(/\[[^\]]*\]/g, '').trim()))).length;
  if (!text || !kept || !claims) return { text: NOT_FOUND, notFound: true, ok: true, kept: 0, repaired, removed };
  return { text, notFound: false, ok: g.ok, kept, repaired, removed };
}

/** The passages as the reply lists them: [{n, path, label, heading, page, folder, file}]. */
export function sourceList(passages) {
  return (passages || []).map((p, i) => ({
    n: i + 1, path: sourcePath(p), label: sourceLabel(p),
    heading: str(field(p, 'headingPath', 'heading', 'heading_path')), page: field(p, 'page'),
    folder: str(field(p, 'folderName', 'folder')), file: str(field(p, 'filePath', 'file', 'file_path')),
  }));
}

export default { NOT_FOUND, SYSTEM_LINES, systemPrompt, contextBlock, sourceLabel, sourcePath, ground, isNotFound, declaresNotFound, citations, sentences, checkGrounded, details, supports, guard, sourceList };
