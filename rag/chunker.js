// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's pure text logic (rag/DESIGN.md section 5): no DOM, no workers, no network. It runs in
// rag/parse.worker.js and imports in Node for the quick tests (rig/t_rag1_parse.js drives the real thing in Chromium).
//
//   1. hashing           sha256Hex (crypto.subtle, or the same function in plain JS), Sha256 (streaming)
//   2. block extraction  blocksFromText / Markdown / Html / Json / Csv / PdfPages: a file's text becomes blocks
//                        {kind: 'heading'|'para'|'code'|'row'|'item', level, text, char_start, char_end, page}
//                        with offsets into the text each function returns (txt, md, csv: the file's own text, so a
//                        citation can mark the exact words; html, json: the extracted text; pdf: the page's own text)
//   3. chunking          chunkDocument(doc, {count, maxTokens}): blocks to chunks. Sizes are MEASURED with the model's
//                        tokenizer through count(texts) -> Promise<number[]>, never guessed. Split on headings, then
//                        paragraphs, then sentences, then words; 10 to 15% of a split section repeats at the start of
//                        the next chunk (whole sentences where that is possible, otherwise the end of a sentence).
//
// count(texts) must return, for each text, the number of tokens the model would receive for it as an embedded input
// (the special tokens included); the chunker works out the fixed overhead itself and checks every chunk's real input
// against the model's limit at the end.

export const VERSION = 1; // bump when the chunks a given file produces would change

// ============================================================================================================== 1. HASHING
const K256 = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);

/** Streaming SHA-256 in plain JS (update() as many times as needed, then digest()). */
export class Sha256 {
  constructor() {
    this.h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    this.buf = new Uint8Array(64); this.fill = 0; this.len = 0; this.w = new Uint32Array(64);
  }
  _block(b, o) {
    const w = this.w, h = this.h;
    for (let i = 0; i < 16; i++, o += 4) w[i] = (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3];
    for (let i = 16; i < 64; i++) {
      const a = w[i - 15], c = w[i - 2];
      const s0 = ((a >>> 7) | (a << 25)) ^ ((a >>> 18) | (a << 14)) ^ (a >>> 3);
      const s1 = ((c >>> 17) | (c << 15)) ^ ((c >>> 19) | (c << 13)) ^ (c >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let a = h[0], b1 = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (let i = 0; i < 64; i++) {
      const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const t1 = (hh + S1 + ((e & f) ^ (~e & g)) + K256[i] + w[i]) | 0;
      const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const t2 = (S0 + ((a & b1) ^ (a & c) ^ (b1 & c))) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b1; b1 = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + b1) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
  }
  update(data) {
    let p = 0; const n = data.length; this.len += n;
    if (this.fill) {
      const take = Math.min(64 - this.fill, n); this.buf.set(data.subarray(0, take), this.fill); this.fill += take; p = take;
      if (this.fill < 64) return this;
      this._block(this.buf, 0); this.fill = 0;
    }
    for (; p + 64 <= n; p += 64) this._block(data, p);
    if (p < n) { this.buf.set(data.subarray(p), 0); this.fill = n - p; }
    return this;
  }
  digest() {
    const bits = this.len * 8, pad = new Uint8Array(((this.fill < 56 ? 56 : 120) - this.fill) + 8);
    pad[0] = 0x80;
    const hi = Math.floor(bits / 4294967296), lo = bits >>> 0, e = pad.length;
    pad[e - 8] = hi >>> 24; pad[e - 7] = hi >>> 16; pad[e - 6] = hi >>> 8; pad[e - 5] = hi;
    pad[e - 4] = lo >>> 24; pad[e - 3] = lo >>> 16; pad[e - 2] = lo >>> 8; pad[e - 1] = lo;
    const keep = this.len; this.update(pad); this.len = keep;
    const out = new Uint8Array(32);
    for (let i = 0; i < 8; i++) { out[4 * i] = this.h[i] >>> 24; out[4 * i + 1] = this.h[i] >>> 16; out[4 * i + 2] = this.h[i] >>> 8; out[4 * i + 3] = this.h[i]; }
    return out;
  }
}

const hex = (u8) => { let s = ''; for (let i = 0; i < u8.length; i++) s += (u8[i] < 16 ? '0' : '') + u8[i].toString(16); return s; };
const utf8 = (s) => new TextEncoder().encode(s);
const bytesOf = (d) => typeof d === 'string' ? utf8(d) : (d instanceof Uint8Array ? d : (ArrayBuffer.isView(d) ? new Uint8Array(d.buffer, d.byteOffset, d.byteLength) : new Uint8Array(d)));

/** SHA-256 (hex) of a string (as UTF-8), a Uint8Array or an ArrayBuffer, in plain JS. */
export function sha256HexJs(data) { return hex(new Sha256().update(bytesOf(data)).digest()); }

/** SHA-256 (hex) of a string (as UTF-8), a Uint8Array or an ArrayBuffer: crypto.subtle where the page has it, else plain JS. */
export async function sha256Hex(data) {
  const b = bytesOf(data);
  const subtle = typeof crypto !== 'undefined' && crypto.subtle;
  if (subtle && typeof subtle.digest === 'function') {
    try { return hex(new Uint8Array(await subtle.digest('SHA-256', b))); } catch (e) { /* not a secure context: use the JS one */ }
  }
  return sha256HexJs(b);
}

// ======================================================================================================= 2. SMALL TEXT TOOLS
const NAMED = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", ndash: '–', mdash: '—', lsquo: '‘', rsquo: '’', sbquo: '‚', ldquo: '“', rdquo: '”',
  bdquo: '„', dagger: '†', Dagger: '‡', bull: '•', hellip: '…', permil: '‰', prime: '′', Prime: '″', lsaquo: '‹', rsaquo: '›',
  euro: '€', trade: '™', larr: '←', rarr: '→', uarr: '↑', darr: '↓', minus: '−', le: '≤', ge: '≥', ne: '≠', infin: '∞',
  OElig: 'Œ', oelig: 'œ', Scaron: 'Š', scaron: 'š', Yuml: 'Ÿ', circ: 'ˆ', tilde: '˜', ensp: ' ', emsp: ' ', thinsp: ' ', zwnj: '', zwj: '', shy: '' };
('nbsp iexcl cent pound curren yen brvbar sect uml copy ordf laquo not shy reg macr deg plusmn sup2 sup3 acute micro para middot cedil sup1 ordm raquo frac14 frac12 frac34 iquest ' +
 'Agrave Aacute Acirc Atilde Auml Aring AElig Ccedil Egrave Eacute Ecirc Euml Igrave Iacute Icirc Iuml ETH Ntilde Ograve Oacute Ocirc Otilde Ouml times Oslash Ugrave Uacute Ucirc Uuml Yacute THORN szlig ' +
 'agrave aacute acirc atilde auml aring aelig ccedil egrave eacute ecirc euml igrave iacute icirc iuml eth ntilde ograve oacute ocirc otilde ouml divide oslash ugrave uacute ucirc uuml yacute thorn yuml')
  .split(' ').forEach((n, i) => { if (!(n in NAMED)) NAMED[n] = String.fromCharCode(160 + i); });
NAMED.nbsp = ' '; NAMED.shy = '';

/** HTML character references (named, decimal, hex) decoded; an unknown name is left as it is. */
export function decodeEntities(s) {
  if (s.indexOf('&') < 0) return s;
  return s.replace(/&(?:#(\d{1,7})|#[xX]([0-9a-fA-F]{1,6})|([A-Za-z][A-Za-z0-9]{1,31}));/g, (m, dec, hx, name) => {
    if (name) return Object.prototype.hasOwnProperty.call(NAMED, name) ? NAMED[name] : m;
    const cp = dec ? parseInt(dec, 10) : parseInt(hx, 16);
    if (!(cp > 0 && cp <= 0x10ffff) || (cp >= 0xd800 && cp <= 0xdfff)) return '�';
    return cp === 160 ? ' ' : String.fromCodePoint(cp);
  });
}

const HAS_WORD = /[\p{L}\p{N}]/u;
const isBlank = (s) => !/\S/.test(s);

/** Spans [start, end) of the runs of non-white-space characters in str.slice(a, b), as offsets into str. */
export function wordSpans(str, a = 0, b = str.length) {
  const out = []; const re = /\S+/g; re.lastIndex = a; let m;
  while ((m = re.exec(str)) && m.index < b) out.push([m.index, Math.min(b, m.index + m[0].length)]);
  return out;
}

// Words that end in a full stop without ending the sentence (titles, citations' short forms, months, Latin).
const ABBR = new Set(('mr mrs ms mx dr prof rev hon fr sr jr st mt ft gen col lt capt sgt cpl maj adm cmdr gov sen rep pres messrs mme mlle esq vs cf ca c fl ed eds vol vols no nos p pp ch chap ' +
  'fig figs sec art esp viz approx dept est inc ltd co corp op cit trans repr jan feb mar apr jun jul aug sep sept oct nov dec mon tue tues wed thu thur thurs fri sat sun al ibid mss e.g i.e a.m p.m u.s u.k ' +
  'bk bks pt pts ser supp app apps ill illus ms. cl esq.').split(' '));
const isLower = (c) => c !== c.toUpperCase() && c === c.toLowerCase();

/** Sentence spans [start, end) of str.slice(a, b), as offsets into str, trimmed. A full stop after a title, an initial, an
 *  abbreviation, or one followed by a lower-case letter does not end a sentence. Rules are plain code (no Intl.Segmenter), so
 *  every engine cuts a file in the same places. */
export function splitSentences(str, a = 0, b = str.length) {
  const s0 = str.slice(a, b), spans = [];
  const re = /([.!?…]+|[。！？]+)([\"'”’)\]»*]*)/g;
  let start = 0, m;
  while (start < s0.length && /\s/.test(s0[start])) start++;
  while ((m = re.exec(s0))) {
    const end = m.index + m[0].length, term = m[1], cjk = /[。！？]/.test(term[0]);
    if (end < s0.length && !cjk && !/\s/.test(s0[end])) continue;
    let k = end; while (k < s0.length && /\s/.test(s0[k])) k++;
    if (k >= s0.length) break;
    const nc = s0[k];
    if (!cjk && isLower(nc)) continue;
    if (term === '.' && !cjk) {
      const w = /([A-Za-z][A-Za-z.]*|\d+)$/.exec(s0.slice(Math.max(start, m.index - 12), m.index));
      if (w) {
        const word = w[1].toLowerCase();
        if (ABBR.has(word) || (w[1].length === 1 && w[1] !== w[1].toLowerCase())) continue;
        if (/^[a-z]\.[a-z]$/.test(word)) continue;
      }
    }
    spans.push([a + start, a + end]); start = k;
  }
  let e = s0.length; while (e > start && /\s/.test(s0[e - 1])) e--;
  if (e > start) spans.push([a + start, a + e]);
  return spans;
}

/** Line spans [start, end) of str.slice(a, b) without the blank ones, trailing white space cut. */
export function lineSpans(str, a = 0, b = str.length) {
  const out = []; let s = a;
  for (let i = a; i <= b; i++) {
    if (i === b || str.charCodeAt(i) === 10 || str.charCodeAt(i) === 13) {
      let e = i; while (e > s && /\s/.test(str[e - 1])) e--;
      let st = s; while (st < e && (str[st] === ' ' || str[st] === '\t')) st++;
      if (e > st) out.push([st, e]);
      if (str.charCodeAt(i) === 13 && str.charCodeAt(i + 1) === 10) i++;
      s = i + 1;
    }
  }
  return out;
}

/** Lines of a text as [{s, e, next}]: start, end (the line break not included), start of the next line. */
function textLines(text) {
  const out = []; let s = 0; const n = text.length;
  for (let i = 0; i < n; i++) {
    const c = text.charCodeAt(i);
    if (c === 10 || c === 13) {
      let nx = i + 1; if (c === 13 && text.charCodeAt(i + 1) === 10) nx++;
      out.push({ s, e: i, next: nx }); s = nx; i = nx - 1;
    }
  }
  if (s < n) out.push({ s, e: n, next: n });
  return out;
}

const block = (kind, text, s, e, level = 0, page = null) => ({ kind, level, text, char_start: s, char_end: e, page });

// ===================================================================================================== 3. PLAIN TEXT (.txt)
/** Paragraphs of a plain text (blank lines between them). A text that has hardly any blank lines is taken line by line. */
export function blocksFromText(text) {
  const lines = textLines(text), blocks = []; let ps = -1, pe = -1, count = 0;
  const flush = () => { if (ps >= 0) { blocks.push(block('para', text.slice(ps, pe), ps, pe)); count++; ps = -1; } };
  for (const L of lines) {
    if (isBlank(text.slice(L.s, L.e))) { flush(); continue; }
    let s = L.s; while (s < L.e && /[ \t]/.test(text[s])) s++;
    let e = L.e; while (e > s && /\s/.test(text[e - 1])) e--;
    if (ps < 0) ps = s; pe = e;
  }
  flush();
  if (lines.length > 12 && blocks.length * 4000 < text.length) { // a log or a list: every line is its own paragraph
    const alt = [];
    for (const L of lines) { const t = text.slice(L.s, L.e); if (isBlank(t)) continue; let s = L.s; while (s < L.e && /[ \t]/.test(text[s])) s++; let e = L.e; while (e > s && /\s/.test(text[e - 1])) e--; alt.push(block('para', text.slice(s, e), s, e)); }
    if (alt.length > blocks.length * 2) return alt;
  }
  return blocks;
}

// ===================================================================================================== 4. MARKDOWN (.md)
function cleanInline(s) {
  return s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/\[([^\]]*)\]\[[^\]]*\]/g, '$1')
    .replace(/<[^>]+>/g, '').replace(/(\*\*|__|\*|_|~~|`)/g, '').replace(/\\([\\`*_{}\[\]()#+\-.!|>~])/g, '$1').replace(/\s+/g, ' ').trim();
}

/** Blocks of a Markdown text: ATX and setext headings, fenced code kept whole, list items, table rows, paragraphs; the text is
 *  the file's own (offsets are exact, and a block's text is text.slice(char_start, char_end) except for a heading's, which is its words). */
export function blocksFromMarkdown(text) {
  const lines = textLines(text), n = lines.length, blocks = [];
  const T = (i) => text.slice(lines[i].s, lines[i].e);
  let para = null; // {s, e, parts}
  const flush = () => { if (para) { blocks.push(block('para', text.slice(para.s, para.e), para.s, para.e)); para = null; } };
  const lead = (L) => { let s = L.s; while (s < L.e && /[ \t]/.test(text[s])) s++; return s; };
  const SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  const ITEM = /^[ \t]*(?:[-+*]|\d{1,9}[.)])[ \t]+\S/;
  const startsBlock = (t) => /^ {0,3}(?:#{1,6}(?:[ \t]|$)|(?:`{3,}|~{3,}))/.test(t) || ITEM.test(t) || /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/.test(t);
  let i = 0;
  if (n && /^---\s*$/.test(T(0))) { // front matter
    for (let j = 1; j < Math.min(n, 200); j++) if (/^(---|\.\.\.)\s*$/.test(T(j))) { i = j + 1; break; }
  }
  while (i < n) {
    const t = T(i), L = lines[i];
    if (isBlank(t)) { flush(); i++; continue; }
    const fence = /^ {0,3}(`{3,}|~{3,})/.exec(t);
    if (fence) {
      flush(); const ch = fence[1][0], len = fence[1].length; let j = i + 1;
      const close = new RegExp('^ {0,3}\\' + ch + '{' + len + ',}\\s*$');
      while (j < n && !close.test(T(j))) j++;
      const last = Math.min(j, n - 1);
      blocks.push(block('code', text.slice(L.s, lines[last].e), L.s, lines[last].e)); i = last + 1; continue;
    }
    const atx = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$/.exec(t);
    if (atx) {
      flush(); const words = cleanInline((atx[2] || '').replace(/[ \t]+#+[ \t]*$/, '').replace(/^#+$/, ''));
      if (words) blocks.push(block('heading', words, L.s, L.e, atx[1].length));
      i++; continue;
    }
    const under = /^ {0,3}(=+|-+)[ \t]*$/.exec(t);
    if (under && para) { // setext heading: the paragraph above is its words
      const words = cleanInline(text.slice(para.s, para.e)); const ps = para.s; para = null;
      if (words) blocks.push(block('heading', words, ps, L.e, under[1][0] === '=' ? 1 : 2));
      i++; continue;
    }
    if (/^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/.test(t)) { flush(); i++; continue; }
    if (t.indexOf('|') >= 0 && i + 1 < n && SEP.test(T(i + 1)) && T(i + 1).indexOf('-') >= 0) { // table
      flush(); blocks.push(block('row', t.trim(), lead(L), L.e));
      let j = i + 2;
      while (j < n && !isBlank(T(j)) && T(j).indexOf('|') >= 0) { const s = lead(lines[j]); blocks.push(block('row', text.slice(s, lines[j].e), s, lines[j].e)); j++; }
      i = j; continue;
    }
    if (ITEM.test(t) && !(para && /^[ \t]*(?!1[.)])\d+[.)]/.test(t))) {
      flush(); const s = lead(L); let e = L.e, j = i + 1;
      while (j < n && !isBlank(T(j)) && !ITEM.test(T(j)) && !startsBlock(T(j))) { e = lines[j].e; j++; }
      blocks.push(block('item', text.slice(s, e), s, e)); i = j; continue;
    }
    if (!para) para = { s: lead(L), e: L.e }; else para.e = L.e;
    i++;
  }
  flush();
  for (const b of blocks) if (b.kind !== 'heading') { let e = b.char_end; while (e > b.char_start && /\s/.test(text[e - 1])) e--; b.char_end = e; b.text = text.slice(b.char_start, e); }
  return blocks;
}

// ======================================================================================================== 5. HTML (.html .htm, .docx via mammoth)
const SKIP = new Set(['script', 'style', 'nav', 'noscript', 'template', 'svg', 'iframe', 'object', 'canvas', 'select', 'textarea']);
const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
const BLOCKISH = new Set(['p', 'div', 'section', 'article', 'main', 'header', 'footer', 'aside', 'blockquote', 'figure', 'figcaption', 'form', 'fieldset', 'address', 'details',
  'summary', 'dl', 'dt', 'dd', 'body', 'html', 'hr', 'caption', 'center', 'tbody', 'thead', 'tfoot', 'legend', 'menu']);
const TAG = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<![^>]*>|<\?[\s\S]*?\?>|<(\/?)([A-Za-z][A-Za-z0-9:-]*)((?:"[^"]*"|'[^']*'|[^'">])*)>/g;

/** Blocks of an HTML text. h1 to h6 make the headings (a heading with the class ing-title, which the docx reader gives a document's
 *  title, has level 0); script, style, nav and the like are dropped; p, div, li, tr ... start a new block; table rows are lines
 *  (cells joined by " | ", and "heading: value" where the first row is a row of th cells). Returns {text, blocks, meta}. */
export function blocksFromHtml(html) {
  const blocks = [], meta = {}; let out = '';
  let cur = '', mode = 'flow', hLevel = 0, inPre = false, liPrefix = null, lastKind = '';
  const lists = []; // {ordered, n}
  let skipName = null, skipDepth = 0, inTitle = false, titleBuf = '';
  let table = null; // {rows: 0, labels: null, cells: [], cellBuf: null, isHead: bool, thCount}
  const emit = (kind, text, level = 0) => {
    if (kind !== 'code') text = text.replace(/[ \t]*\n[ \t]*/g, '\n').replace(/[ \t]+/g, ' ').trim(); else text = text.replace(/^\n+|\s+$/g, '');
    if (!text) return;
    const sep = !out ? '' : ((kind === 'item' && lastKind === 'item') || (kind === 'row' && lastKind === 'row') ? '\n' : '\n\n');
    out += sep; const s = out.length; out += text;
    blocks.push(block(kind, text, s, out.length, level)); lastKind = kind;
  };
  const flush = () => {
    if (mode === 'cell') return;
    const t = cur; cur = '';
    if (mode === 'heading') { emit('heading', t, hLevel); return; }
    if (inPre) { emit('code', t); return; }
    if (liPrefix !== null) { const p = liPrefix; liPrefix = null; if (t.trim()) emit('item', p + t.trim().replace(/\s+/g, ' ')); return; }
    emit('para', t);
  };
  const addText = (raw) => {
    if (skipName) return;
    if (inTitle) { titleBuf += raw; return; }
    let s = decodeEntities(raw);
    if (!inPre) s = s.replace(/[ \t\r\n\f]+/g, ' ');
    if (!s) return;
    if (mode === 'cell') { table.cellBuf += s; return; }
    if (!inPre && /^ /.test(s) && (cur === '' || /[ \n]$/.test(cur))) s = s.slice(1);
    cur += s;
  };
  let pos = 0, m; TAG.lastIndex = 0;
  while ((m = TAG.exec(html))) {
    if (m.index > pos) addText(html.slice(pos, m.index));
    pos = TAG.lastIndex;
    if (m[2] === undefined) continue; // comment, doctype, cdata
    const close = m[1] === '/', name = m[2].toLowerCase(), attrs = m[3] || '';
    if (skipName) {
      if (name === skipName) { if (close) { if (--skipDepth === 0) skipName = null; } else if (!/\/\s*$/.test(attrs)) skipDepth++; }
      continue;
    }
    if (!close && SKIP.has(name)) {
      if (!VOID.has(name) && !/\/\s*$/.test(attrs)) {
        if (name === 'script' || name === 'style' || name === 'textarea') { const re = new RegExp('</' + name + '\\s*>', 'ig'); re.lastIndex = pos; const e = re.exec(html); pos = e ? e.index + e[0].length : html.length; TAG.lastIndex = pos; }
        else { skipName = name; skipDepth = 1; }
      }
      continue;
    }
    if (name === 'title') { if (!close) { inTitle = true; titleBuf = ''; } else { inTitle = false; meta.title = decodeEntities(titleBuf).replace(/\s+/g, ' ').trim(); } continue; }
    if (/hidden/i.test(attrs) && /(^|\s)hidden(\s|=|$)/i.test(attrs) && !close && !VOID.has(name)) { skipName = name; skipDepth = 1; continue; }
    if (name === 'table') {
      flush();
      if (!close) { table = { rows: 0, labels: null, cells: [], cellBuf: '', cellIsTh: false, allTh: true }; }
      else { table = null; if (mode === 'cell') mode = 'flow'; }
      continue;
    }
    if (name === 'tr') {
      if (!table) { flush(); continue; }
      if (!close) { table.cells = []; table.allTh = true; table.any = false; }
      else {
        if (mode === 'cell') { table.cells.push(table.cellBuf.replace(/\s+/g, ' ').trim()); mode = 'flow'; }
        const cells = table.cells; mode = 'flow';
        if (cells.some((c) => c)) {
          if (table.rows === 0 && table.allTh && cells.length > 1) { table.labels = cells; emit('row', cells.join(' | ')); }
          else if (table.labels && table.labels.length === cells.length) emit('row', cells.map((c, i) => (c && table.labels[i] ? table.labels[i] + ': ' + c : c)).filter(Boolean).join(' | '));
          else emit('row', cells.join(' | '));
        }
        table.rows++; table.cells = [];
      }
      continue;
    }
    if (name === 'td' || name === 'th') {
      if (!table) continue;
      if (!close) { if (mode === 'cell') table.cells.push(table.cellBuf.replace(/\s+/g, ' ').trim()); mode = 'cell'; table.cellBuf = ''; table.any = true; if (name === 'td') table.allTh = false; }
      else if (mode === 'cell') { table.cells.push(table.cellBuf.replace(/\s+/g, ' ').trim()); mode = 'flow'; table.cellBuf = ''; }
      continue;
    }
    if (mode === 'cell') { if (BLOCKISH.has(name) || name === 'li' || name === 'br' || name === 'ul' || name === 'ol' || /^h[1-6]$/.test(name)) table.cellBuf += ' '; continue; }
    if (/^h[1-6]$/.test(name)) {
      flush();
      if (!close) { mode = 'heading'; const cls = /class\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i.exec(attrs); hLevel = cls && /(^|\s)ing-title(\s|$)/.test(cls[1] || cls[2] || cls[3] || '') ? 0 : +name[1]; }
      else { mode = 'flow'; }
      continue;
    }
    if (name === 'br') { if (mode === 'cell') table.cellBuf += ' '; else if (mode !== 'heading') cur += '\n'; continue; }
    if (name === 'pre') { flush(); inPre = !close; if (close) inPre = false; continue; }
    if (name === 'ul' || name === 'ol') {
      flush(); if (!close) lists.push({ ordered: name === 'ol', n: 0 }); else lists.pop(); continue;
    }
    if (name === 'li') {
      flush(); if (!close) { const L = lists[lists.length - 1]; if (L) { L.n++; liPrefix = L.ordered ? L.n + '. ' : '- '; } else liPrefix = '- '; }
      continue;
    }
    if (BLOCKISH.has(name)) flush();
  }
  if (pos < html.length) addText(html.slice(pos));
  if (table && mode === 'cell') { table.cells.push(table.cellBuf.replace(/\s+/g, ' ').trim()); mode = 'flow'; if (table.cells.some((c) => c)) emit('row', table.cells.join(' | ')); }
  flush();
  return { text: out, blocks, meta };
}

// ====================================================================================================== 6. JSON (.json)
const jsonScalar = (v) => String(v).replace(/\s*[\r\n]+\s*/g, ' ');
function flatten(v, path, push) {
  if (v === null) push(path ? path + ': null' : 'null');
  else if (Array.isArray(v)) { if (!v.length) push((path || '') + ': []'); else v.forEach((x, i) => flatten(x, path + '[' + i + ']', push)); }
  else if (typeof v === 'object') { const ks = Object.keys(v); if (!ks.length) push((path || '') + ': {}'); else for (const k of ks) flatten(v[k], path ? path + '.' + k : k, push); }
  else push(path ? path + ': ' + jsonScalar(v) : jsonScalar(v));
}
const isComplex = (x) => x !== null && typeof x === 'object' && (Array.isArray(x) ? x.length > 0 : Object.keys(x).length > 0);

/** Blocks of a JSON text (or JSON Lines): every top-level key (a key that holds an array or an object makes a heading of its
 *  own, and each item of an array a block) written as `path: value` lines. Returns {text, blocks} or null when it is not JSON. */
export function blocksFromJson(src) {
  let root;
  try { root = JSON.parse(src); } catch (e) {
    const rows = src.split(/\r?\n/).filter((l) => l.trim());
    try { root = rows.map((l) => JSON.parse(l)); if (!rows.length) return null; } catch (e2) { return null; }
  }
  const parts = []; // {kind, level, lines|text}
  const walk = (v, path, level) => {
    if (Array.isArray(v)) {
      if (!v.length) { parts.push({ kind: 'item', lines: [(path || '') + ': []'] }); return; }
      v.forEach((x, i) => { const lines = []; flatten(x, path + '[' + i + ']', (l) => lines.push(l)); parts.push({ kind: 'item', lines }); });
    } else if (v !== null && typeof v === 'object') {
      const scalars = [], complex = [];
      for (const k of Object.keys(v)) { const p = path ? path + '.' + k : k; if (isComplex(v[k])) complex.push([k, p, v[k]]); else flatten(v[k], p, (l) => scalars.push(l)); }
      if (scalars.length) parts.push({ kind: 'item', lines: scalars });
      for (const [k, p, x] of complex) {
        if (level >= 2) { const lines = []; flatten(x, p, (l) => lines.push(l)); parts.push({ kind: 'item', lines }); continue; }
        parts.push({ kind: 'heading', level: level + 1, text: k }); walk(x, p, level + 1);
      }
    } else { const lines = []; flatten(v, path, (l) => lines.push(l)); parts.push({ kind: 'item', lines }); }
  };
  walk(root, '', 0);
  let text = ''; const blocks = [];
  for (const p of parts) {
    const t = p.kind === 'heading' ? p.text : p.lines.join('\n');
    if (text) text += '\n\n';
    const s = text.length; text += t; blocks.push(block(p.kind, t, s, text.length, p.level || 0));
  }
  return { text, blocks };
}

// ======================================================================================================== 7. CSV (.csv)
/** Rows of a CSV text as [{s, e, cells}] (quoted fields may hold the delimiter and line breaks); s and e are offsets of the raw row. */
export function csvRows(text, delim) {
  const rows = []; const n = text.length; let i = 0;
  while (i < n) {
    const s = i; const cells = []; let cell = '', q = false, any = false;
    for (; i < n; i++) {
      const c = text[i];
      if (q) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c; continue; }
      if (c === '"' && cell === '') { q = true; any = true; continue; }
      if (c === delim) { cells.push(cell); cell = ''; any = true; continue; }
      if (c === '\n' || c === '\r') break;
      cell += c; any = true;
    }
    const e = i; cells.push(cell);
    if (i < n) { if (text[i] === '\r' && text[i + 1] === '\n') i++; i++; }
    if (any || cells.length > 1) rows.push({ s, e, cells });
  }
  return rows;
}

function sniffDelimiter(text) {
  const first = text.slice(0, Math.min(text.length, 4000)).split(/\r?\n/).filter((l) => l.trim()).slice(0, 5);
  let best = ',', bestN = 0;
  for (const d of [',', ';', '\t', '|']) {
    const counts = first.map((l) => { let n = 0, q = false; for (const c of l) { if (c === '"') q = !q; else if (c === d && !q) n++; } return n; });
    const n = Math.min(...counts);
    if (counts.length && n > bestN) { best = d; bestN = n; }
  }
  return best;
}

/** Blocks of a CSV text: the header row becomes the heading (so it is the heading path of every chunk), each row a block;
 *  the text is the file's own. Returns {text, blocks, meta}. */
export function blocksFromCsv(text) {
  const delim = sniffDelimiter(text), rows = csvRows(text, delim), blocks = [];
  const numeric = (c) => c.trim() !== '' && !isNaN(Number(c.trim()));
  let from = 0;
  if (rows.length >= 2 && rows[0].cells.every((c) => c.trim() !== '' && !numeric(c))) {
    const h = rows[0].cells.map((c) => c.trim()).join(' | ');
    blocks.push(block('heading', h.length > 300 ? h.slice(0, 300) : h, rows[0].s, rows[0].e, 1)); from = 1;
  }
  for (let r = from; r < rows.length; r++) {
    let { s, e } = rows[r]; while (e > s && /\s/.test(text[e - 1])) e--;
    if (e > s) blocks.push(block('row', text.slice(s, e), s, e));
  }
  return { text, blocks, meta: { delimiter: delim, header: from === 1, rows: rows.length } };
}

// ================================================================================================================ 8. PDF
const BULLET = /^(?:[•◦▪●‣·*]|[-–—]\s)\s*\S|^\(?\d{1,3}[.)]\s+\S|^\(?[a-z]\)\s+\S/;
const PAGE_NO = /^\s*(?:page\s+)?\d{1,4}(?:\s*(?:of|\/)\s*\d{1,4})?\s*$|^\s*[-–—]\s*\d{1,4}\s*[-–—]\s*$|^\s*[ivxlcdm]{1,7}\s*$/i;

function median(a) { if (!a.length) return 0; const s = a.slice().sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; }
function mode(a) { const m = new Map(); let best = 0, bv = 0; for (const v of a) { const k = Math.round(v * 2) / 2; const c = (m.get(k) || 0) + 1; m.set(k, c); if (c > best) { best = c; bv = k; } } return bv; }

/** Lines of one page from pdf.js text items [{str, x, y, w, h}] (stream order; y grows upwards). */
export function pdfLines(items) {
  const lines = []; let cur = null;
  for (const it of items) {
    if (!it.str || !/\S/.test(it.str)) { if (cur && it.str && /\s/.test(it.str)) cur.pendingSpace = true; continue; }
    const size = it.h || 10;
    if (cur && Math.abs(it.y - cur.y) <= 0.45 * Math.max(size, cur.size)) {
      const gap = it.x - cur.right;
      if (cur.pendingSpace || (gap > 0.14 * size && !/\s$/.test(cur.text) && !/^\s/.test(it.str))) cur.text += ' ';
      cur.text += it.str; cur.right = it.x + it.w; cur.chars += it.str.length; cur.pendingSpace = false;
      if (size > cur.size) cur.size = size;
    } else {
      cur = { text: it.str, x: it.x, y: it.y, size, right: it.x + it.w, chars: it.str.length, pendingSpace: false };
      lines.push(cur);
    }
  }
  for (const l of lines) l.text = l.text.replace(/\s+/g, ' ').trim();
  return lines.filter((l) => l.text);
}

/** Blocks of a PDF from its pages: pages is [{page, height, items}] with items as pdfLines takes them. Page numbers and the lines that
 *  repeat at the top or bottom of the pages are left out; lines in a larger type than the body are headings (the largest type is
 *  level 1); lines that run on are one paragraph, a line that ends in a hyphen and a lower-case word joined. Offsets are into the
 *  text of the block's own page. Returns {text, pages: [text per page], blocks}. */
export function blocksFromPdfPages(pages) {
  const per = pages.map((p) => ({ page: p.page, H: p.height || 792, lines: pdfLines(p.items) }));
  // running heads and page numbers
  const edge = (p, l) => l.y > p.H * 0.92 || l.y < p.H * 0.08;
  const seen = new Map();
  for (const p of per) {
    const keys = new Set(); for (const l of p.lines) if (edge(p, l)) keys.add(l.text.replace(/\d+/g, '#'));
    for (const k of keys) seen.set(k, (seen.get(k) || 0) + 1);
  }
  const rep = Math.max(2, Math.ceil(per.length * 0.5)); // a line at the edge of at least half the pages (and two) is a running head or foot
  for (const p of per) p.lines = p.lines.filter((l) => !(edge(p, l) && (PAGE_NO.test(l.text) || (per.length >= 2 && seen.get(l.text.replace(/\d+/g, '#')) >= rep))));
  // the body type
  const sizes = []; for (const p of per) for (const l of p.lines) for (let i = 0; i < Math.min(l.text.length, 200); i += 10) sizes.push(l.size);
  const body = mode(sizes) || 10;
  const headSizes = new Set();
  const isHead = (l) => l.size >= body * 1.15 && l.text.length <= 160 && !/[,;]$/.test(l.text) && HAS_WORD.test(l.text);
  for (const p of per) for (const l of p.lines) if (isHead(l)) headSizes.add(Math.round(l.size * 2) / 2);
  const order = [...headSizes].sort((a, b) => b - a), levelOf = (sz) => Math.min(4, order.indexOf(Math.round(sz * 2) / 2) + 1);
  const outBlocks = [], pageTexts = [];
  for (const p of per) {
    // paragraphs
    const gaps = []; for (let i = 1; i < p.lines.length; i++) { const d = p.lines[i - 1].y - p.lines[i].y; if (d > 0 && Math.abs(p.lines[i].size - p.lines[i - 1].size) < 0.5) gaps.push(d); }
    const g0 = mode(gaps) || body * 1.3;
    const paras = []; let cur = null;
    for (let i = 0; i < p.lines.length; i++) {
      const l = p.lines[i], prev = p.lines[i - 1];
      if (isHead(l)) {
        const lv = levelOf(l.size);
        if (cur && cur.kind === 'heading' && cur.level === lv && prev && prev.y - l.y < l.size * 1.7) { cur.parts.push(l.text); cur.last = l; continue; }
        cur = { kind: 'heading', level: lv, parts: [l.text], last: l }; paras.push(cur); continue;
      }
      const bullet = BULLET.test(l.text);
      const gap = prev ? prev.y - l.y : 0;
      if (!cur || cur.kind === 'heading' || bullet || gap > g0 * 1.3 || gap < 0 || (prev && Math.abs(prev.size - l.size) > 1.5)) {
        cur = { kind: bullet ? 'item' : 'para', level: 0, parts: [l.text], last: l }; paras.push(cur);
      } else { cur.parts.push(l.text); cur.last = l; }
    }
    let text = ''; const pb = [];
    for (const q of paras) {
      let t = '';
      for (const part of q.parts) {
        if (!t) t = part;
        else if (/[A-Za-zÀ-ɏ]-$/.test(t) && isLower(part[0]) && part[0] !== part[0].toUpperCase()) t = t.slice(0, -1) + part;
        else t += ' ' + part;
      }
      t = t.replace(/\s+/g, ' ').trim(); if (!t) continue;
      const sep = !text ? '' : (q.kind === 'item' && pb.length && pb[pb.length - 1].kind === 'item' ? '\n' : '\n\n');
      text += sep; const s = text.length; text += t;
      pb.push(block(q.kind, t, s, text.length, q.level, p.page));
    }
    pageTexts.push(text); outBlocks.push(...pb);
  }
  return { text: pageTexts.join('\f'), pages: pageTexts, blocks: outBlocks, meta: { bodySize: body } };
}

// ==================================================================================================== 9. FROM A FILE'S TEXT
/** Blocks for a text file (.txt .md .html .htm .json .csv) from its decoded text: {text, blocks, meta, warnings}. */
export function parseTextFile(ext, text) {
  ext = String(ext).toLowerCase().replace(/^\./, '');
  const warnings = [];
  if (ext === 'md' || ext === 'markdown') return { text, blocks: blocksFromMarkdown(text), meta: {}, warnings };
  if (ext === 'html' || ext === 'htm') { const r = blocksFromHtml(text); return { ...r, warnings }; }
  if (ext === 'csv') { const r = blocksFromCsv(text); return { ...r, warnings }; }
  if (ext === 'json') {
    const r = blocksFromJson(text);
    if (r) return { ...r, meta: {}, warnings };
    warnings.push('not valid JSON: read as plain text');
  }
  return { text, blocks: blocksFromText(text), meta: {}, warnings };
}

// ============================================================================================================ 10. CHUNKING
/** The sizes for a model whose input limit is maxTokens: max is the most a chunk's embedded input should hold (450 of 512;
 *  199 of 256), min the least a chunk of a long section holds (300; 132). */
export function limitsFor(maxTokens = 512) {
  const limit = Math.max(48, maxTokens | 0), max = limit >= 512 ? 450 : Math.floor(limit * 0.78);
  return { limit, max, min: Math.floor(max * 2 / 3), overlapLo: 0.10, overlapHi: 0.15 };
}

/** The text a chunk is embedded as: the heading path, a blank line, the text (or the text alone). */
export function embedInput(headingPath, text) { return headingPath ? headingPath + '\n\n' + text : text; }

async function countAll(count, texts, cache, stats) {
  const need = [];
  for (const s of texts) if (!cache.has(s)) { cache.set(s, -1); need.push(s); }
  for (let i = 0; i < need.length;) {
    let j = i, chars = 0;
    while (j < need.length && j - i < 1500 && chars < 600000) { chars += need[j].length; j++; }
    if (j === i) j = i + 1;
    const batch = need.slice(i, j); const res = await count(batch);
    if (!Array.isArray(res) || res.length !== batch.length) throw new Error('count() must return one number for each text');
    stats.calls++; stats.texts += batch.length;
    for (let k = 0; k < batch.length; k++) cache.set(batch[k], res[k]);
    i = j;
  }
  return texts.map((s) => cache.get(s));
}

const sliceOf = (src, s, e) => src.slice(s, e);

/** Splits chunk-sized pieces out of one over-long span (a sentence, a line): at clause ends and words, and at characters for a
 *  "word" that is longer than a chunk. Returns spans [s, e) whose tokens are all at most cap. */
async function cutSpan(src, s, e, cap, ctx) {
  const words = wordSpans(src, s, e); const parts = [];
  const wc = await countAll(ctx.count, words.map(([a, b]) => src.slice(a, b)), ctx.cache, ctx.stats);
  let from = -1, acc = 0, lastClause = -1, clauseAcc = 0;
  const push = (a, b) => parts.push([a, b]);
  for (let i = 0; i < words.length; i++) {
    const t = Math.max(1, wc[i] - ctx.overhead);
    if (t > cap) { // one "word" longer than a chunk: cut it by characters
      if (from >= 0) { push(words[from][0], words[i - 1][1]); from = -1; acc = 0; lastClause = -1; }
      let a = words[i][0]; const b = words[i][1];
      while (a < b) {
        let len = Math.max(1, Math.floor((b - a) * Math.min(1, cap / t) * 0.9));
        for (let tries = 0; tries < 12; tries++) {
          const c = (await countAll(ctx.count, [src.slice(a, Math.min(b, a + len))], ctx.cache, ctx.stats))[0] - ctx.overhead;
          if (c <= cap || len <= 1) break; len = Math.max(1, Math.floor(len * Math.min(0.9, cap / c)));
        }
        push(a, Math.min(b, a + len)); a += len;
      }
      continue;
    }
    if (from < 0) { from = i; acc = t; lastClause = -1; }
    else if (acc + t > cap) {
      let end = i - 1;
      // cut after the last clause end if it is in the later half and what is left over with this word still fits
      if (lastClause >= from && clauseAcc >= cap * 0.5 && acc - clauseAcc + t <= cap) end = lastClause;
      push(words[from][0], words[end][1]);
      from = end + 1; acc = 0; for (let k = from; k <= i; k++) acc += Math.max(1, wc[k] - ctx.overhead); lastClause = -1;
      if (from > i) { from = -1; acc = 0; }
    } else acc += t;
    if (from >= 0 && /[,;:—)]$/.test(src.slice(words[i][0], words[i][1]))) { lastClause = i; clauseAcc = acc; }
  }
  if (from >= 0) push(words[from][0], words[words.length - 1][1]);
  return parts;
}

/** Chunks for a document {text, pages?, blocks}. opts: {count, maxTokens = 512, onStage?, overhead?}. Returns {chunks, stats}; each chunk is
 *  {chunk_index, heading_path, char_start, char_end, page, content_hash, text, input, tokens, overlap: {tokens, chars}}:
 *  text is the verbatim slice of the document's text (of the page's text for a PDF) between char_start and char_end, input is
 *  what is embedded (heading path, blank line, text), tokens its exact count, content_hash the SHA-256 (hex) of input. */
export async function chunkDocument(doc, opts) {
  const t0 = Date.now(), stats = { calls: 0, texts: 0, units: 0, groups: 0, repacked: 0, trimmed: 0, fragments: 0, ms: 0 };
  const L = { ...limitsFor(opts.maxTokens || 512), ...(opts.limits || {}) };
  const stage = opts.onStage || (() => {});
  const cache = new Map();
  const ctx = { count: opts.count, cache, stats, overhead: 0, L };
  if (Number.isInteger(opts.overhead)) ctx.overhead = opts.overhead; // known from an earlier file
  else { // the fixed cost of one embedded input (the special tokens), from two small texts
    const [c1, c2] = await countAll(opts.count, ['a', 'a a'], cache, stats);
    ctx.overhead = Math.max(0, Math.min(8, 2 * c1 - c2));
  }
  const srcOf = (page) => (doc.pages ? doc.pages[(page || 1) - 1] || '' : doc.text);

  // ---- groups: a run of blocks under one heading path (on one page)
  const groups = []; const stack = []; let cur = null;
  for (const b of doc.blocks) {
    if (b.kind === 'heading') {
      while (stack.length && stack[stack.length - 1].level >= b.level) stack.pop();
      stack.push({ level: b.level, text: b.text }); cur = null; continue;
    }
    if (!HAS_WORD.test(b.text)) continue;
    const path = stack.map((h) => h.text).join(' > ');
    if (!cur || cur.path !== path || cur.page !== b.page) { cur = { path, page: b.page, src: srcOf(b.page), blocks: [], shrink: 0 }; groups.push(cur); }
    cur.blocks.push(b);
  }
  if (!groups.length) { // only headings: they are the text
    const hs = doc.blocks.filter((b) => b.kind === 'heading' && HAS_WORD.test(b.text));
    if (hs.length) groups.push({ path: '', page: hs[0].page, src: srcOf(hs[0].page), blocks: hs.map((b) => ({ ...b, kind: 'item' })), shrink: 0 });
  }
  stats.groups = groups.length;

  // ---- units: sentences of a paragraph, lines of a list item or code, a table row whole
  stage('units');
  for (const g of groups) {
    g.units = [];
    for (const b of g.blocks) {
      let spans, kind;
      if (b.kind === 'para') { spans = splitSentences(g.src, b.char_start, b.char_end); kind = 'sentence'; }
      else if (b.kind === 'row') { spans = [[b.char_start, b.char_end]]; kind = 'line'; }
      else { spans = lineSpans(g.src, b.char_start, b.char_end); kind = 'line'; }
      spans.forEach(([s, e], i) => { if (e > s) g.units.push({ s, e, t: 0, para: i === 0, kind }); });
    }
    for (let i = 1; i < g.units.length; i++) { // text between two units that is not white space (a table's separator line, a rule) still belongs to a chunk that holds both
      const a = g.units[i - 1].e, b = g.units[i].s;
      if (b > a && /\S/.test(g.src.slice(a, b))) g.units[i].gap = g.src.slice(a, b);
    }
    stats.units += g.units.length;
  }
  stage('counting');
  { // tokens of every unit, of the gaps between them, and of every heading path
    const texts = []; for (const g of groups) for (const u of g.units) { texts.push(g.src.slice(u.s, u.e)); if (u.gap) texts.push(u.gap); }
    const cs = await countAll(opts.count, texts, cache, stats); let k = 0;
    for (const g of groups) for (const u of g.units) { u.t = Math.max(1, cs[k++] - ctx.overhead); u.g = u.gap ? Math.max(0, cs[k++] - ctx.overhead) : 0; delete u.gap; }
  }
  // the heading path as it is embedded: a path longer than a fifth of the input drops its first parts, then cuts at words
  const maxPrefix = Math.max(8, Math.floor(0.2 * L.max));
  {
    const paths = [...new Set(groups.map((g) => g.path).filter(Boolean))];
    const pc = await countAll(opts.count, paths, cache, stats); const pmap = new Map();
    for (let i = 0; i < paths.length; i++) {
      let p = paths[i], tok = pc[i] - ctx.overhead;
      if (tok > maxPrefix) {
        const parts = p.split(' > ');
        while (parts.length > 1 && tok > maxPrefix) { parts.shift(); p = parts.join(' > '); tok = (await countAll(opts.count, [p], cache, stats))[0] - ctx.overhead; }
        if (tok > maxPrefix) { const w = wordSpans(p); const sp = await cutSpan(p, w[0][0], w[w.length - 1][1], maxPrefix, ctx); p = p.slice(sp[0][0], sp[0][1]); tok = (await countAll(opts.count, [p], cache, stats))[0] - ctx.overhead; }
      }
      pmap.set(paths[i], { p, tok: Math.max(0, tok) });
    }
    for (const g of groups) { const m = g.path ? pmap.get(g.path) : null; g.embedPath = m ? m.p : ''; g.prefixTok = m ? m.tok : 0; }
  }
  // ---- units longer than a chunk can take are cut (a sentence in clauses and words, a line in sentences)
  stage('splitting');
  for (const g of groups) {
    const cap = Math.max(8, Math.floor(0.85 * (L.max - ctx.overhead - g.prefixTok)));
    let changed = false;
    for (let pass = 0; pass < 3; pass++) {
      const next = []; changed = false;
      for (const u of g.units) {
        if (u.t <= cap) { next.push(u); continue; }
        changed = true;
        let parts;
        if (u.kind === 'line') { const ss = splitSentences(g.src, u.s, u.e); parts = ss.length > 1 ? ss : null; }
        if (!parts) parts = await cutSpan(g.src, u.s, u.e, cap, ctx);
        const cs = await countAll(opts.count, parts.map(([a, b]) => g.src.slice(a, b)), cache, stats);
        parts.forEach(([a, b], i) => next.push({ s: a, e: b, t: Math.max(1, cs[i] - ctx.overhead), g: i === 0 ? u.g : 0, para: i === 0 ? u.para : false, kind: 'sentence' }));
      }
      g.units = next; if (!changed) break;
    }
  }

  // ---- packing
  stage('packing');
  const results = [];
  for (const g of groups) results.push(await packGroup(g, ctx));

  // ---- the real input of every chunk, measured; a chunk over the model's limit is packed again smaller
  stage('checking');
  const build = (g, d) => {
    d.text = g.src.slice(d.s, d.e); d.input = embedInput(g.embedPath, d.text);
  };
  results.forEach((r, gi) => r.forEach((d) => build(groups[gi], d)));
  for (let round = 0; round < 6; round++) {
    const inputs = []; results.forEach((r) => r.forEach((d) => inputs.push(d.input)));
    const exact = await countAll(opts.count, inputs, cache, stats); let k = 0, worst = new Map();
    results.forEach((r, gi) => r.forEach((d) => { d.tokens = exact[k++]; if (d.tokens > L.limit) worst.set(gi, Math.max(worst.get(gi) || 0, d.tokens - L.limit)); }));
    if (!worst.size) break;
    for (const [gi, over] of worst) {
      const g = groups[gi]; g.shrink += Math.max(10, over + 10); stats.repacked++;
      results[gi] = await packGroup(g, ctx); results[gi].forEach((d) => build(g, d));
    }
  }
  { // last resort: cut words off the end of a chunk that is still over the limit
    for (let gi = 0; gi < results.length; gi++) for (const d of results[gi]) {
      if (!(d.tokens > L.limit)) continue;
      const g = groups[gi]; const w = wordSpans(g.src, d.s, d.e);
      let lo = 1, hi = w.length;
      while (lo < hi) {
        const mid = (lo + hi + 1) >> 1; const inp = embedInput(g.embedPath, g.src.slice(d.s, w[mid - 1][1]));
        const c = (await countAll(opts.count, [inp], cache, stats))[0];
        if (c <= L.limit) lo = mid; else hi = mid - 1;
      }
      d.e = w[lo - 1][1]; build(g, d); d.tokens = (await countAll(opts.count, [d.input], cache, stats))[0]; d.trimmed = true; stats.trimmed++;
    }
  }
  // ---- output
  const chunks = []; let idx = 0;
  const flat = []; results.forEach((r, gi) => r.forEach((d) => flat.push([groups[gi], d])));
  const hashes = await Promise.all(flat.map(([, d]) => sha256Hex(d.input)));
  flat.forEach(([g, d], i) => {
    chunks.push({ chunk_index: idx++, heading_path: g.path, char_start: d.s, char_end: d.e, page: g.page == null ? null : g.page, content_hash: hashes[i],
      text: d.text, input: d.input, tokens: d.tokens, overlap: { tokens: d.ovTok || 0, chars: d.ovChars || 0 } });
  });
  stats.ms = Date.now() - t0; stats.overhead = ctx.overhead;
  return { chunks, stats };
}

/** Packs one group's units into chunks (drafts {s, e, ovTok, ovChars}). Token counts of a run of units include the text between them
 *  that is not white space (u.g, the gap before unit u), so a chunk's size and its overlap are those of the text it really holds. */
async function packGroup(g, ctx) {
  const L = ctx.L, U = g.units, n = U.length, out = [];
  if (!n) return out;
  const budget = L.max - ctx.overhead - g.prefixTok - g.shrink;
  const hard = L.limit - ctx.overhead - g.prefixTok - 4 - g.shrink;
  const minBody = Math.max(1, L.min - ctx.overhead - g.prefixTok);
  const absorbMax = Math.floor(0.12 * budget);
  const t = U.map((u) => u.t), gp = U.map((u) => u.g || 0), Q = new Array(n + 1); Q[0] = 0;
  for (let i = 0; i < n; i++) Q[i + 1] = Q[i] + t[i] + gp[i];
  const run = (a, b) => (b > a ? Q[b] - Q[a] - gp[a] : 0);   // tokens of the text from unit a to unit b - 1, the gaps between them included
  let i = 0, carry = null;
  while (i < n) {
    const k0 = carry ? carry.k : i, fragTok = carry ? carry.fragTok : 0, hasFrag = fragTok > 0;
    const tokAt = (b) => fragTok + (hasFrag && b > k0 ? gp[k0] : 0) + run(k0, b);   // a chunk from the carried text to unit b - 1
    const step = (j) => ((j > k0 || hasFrag) ? gp[j] : 0) + t[j];
    let j = i, used = tokAt(i);
    while (j < n && used + step(j) <= budget) { used += step(j); j++; }
    if (j === i) {
      if (carry) { carry = null; continue; } // the carried text and the next unit do not fit together: carry nothing
      j = i + 1; // a unit that fills the chunk by itself (cut earlier; here only if the budget was shrunk to nothing)
    }
    const startChar = carry && carry.fragStart != null ? carry.fragStart : U[k0].s, ovTok = tokAt(i);
    const draftOf = (b) => ({ s: startChar, e: U[b - 1].e, ovTok, ovChars: 0 });
    if (j === n) { out.push(draftOf(n)); break; }
    let bmin = i + 1; while (bmin < j && tokAt(bmin) < minBody) bmin++;
    // the overlap with the next chunk, as whole units, for a chunk that ends before unit b
    const whole = (b, tA) => {
      const lo = L.overlapLo * tA, hi = L.overlapHi * tA;
      for (let k = b - 1; k >= k0; k--) { const acc = run(k, b); if (acc >= lo) return acc <= hi ? k : -1; }
      return -1;
    };
    let pick = null, firstValid = null;
    for (let b = j; b >= bmin; b--) {
      const kk = whole(b, tokAt(b));
      if (kk < 0) continue;
      if (U[b].para) { pick = { b, k: kk }; break; }
      if (!firstValid) firstValid = { b, k: kk };
    }
    if (!pick) pick = firstValid;
    let b, nextCarry = null;
    if (pick) { b = pick.b; nextCarry = { k: pick.k, fragStart: null, fragTok: 0 }; }
    else { // no whole-sentence overlap in the window: end at a paragraph boundary if there is one, and carry the end of a sentence
      b = j; for (let q = j; q >= bmin; q--) if (U[q].para) { b = q; break; }
      nextCarry = await fragmentOverlap(g, U, run, gp, k0, b, tokAt(b), ctx);
    }
    if (b < n && run(b, n) <= absorbMax && tokAt(n) <= Math.min(hard, Math.floor(budget * 1.05))) { b = n; nextCarry = null; } // a small tail joins this chunk instead of making a runt
    out.push(draftOf(b));
    if (b >= n) break;
    carry = nextCarry; i = b;
    if (carry && carry.k > i) carry = null;
  }
  // the overlap of each draft, in characters, from its predecessor's end
  for (let q = 1; q < out.length; q++) out[q].ovChars = Math.max(0, out[q - 1].e - out[q].s);
  return out;
}

/** The overlap for a chunk that ends before unit b when no run of whole units is 10 to 15% of it: whole units from the end, then
 *  the end of the unit before them (a sentence, a list item, a row), from a sentence start, a clause start or a word, so that the tokens
 *  carried are inside the window. */
async function fragmentOverlap(g, U, run, gp, k0, b, tA, ctx) {
  const L = ctx.L, lo = L.overlapLo * tA, hi = L.overlapHi * tA, mid = (lo + hi) / 2;
  let k = b;
  while (k > k0 && run(k - 1, b) <= hi) k--;
  const acc = run(k, b);
  if (acc >= lo) return { k, fragStart: null, fragTok: 0 };
  const near = () => { // whole units only: the closer of the two runs that bracket the window
    const withMore = k > k0 ? run(k - 1, b) : Infinity;
    return Math.abs(acc - mid) <= Math.abs(withMore - mid) || k <= k0 ? { k, fragStart: null, fragTok: 0 } : { k: k - 1, fragStart: null, fragTok: 0 };
  };
  if (k <= k0) return acc ? { k, fragStart: null, fragTok: 0 } : null;
  const u = U[k - 1];
  const fixed = k < b ? gp[k] + acc : 0;   // what follows the fragment: the gap before unit k and the whole units from k
  const words = wordSpans(g.src, u.s, u.e); if (words.length < 2) return (await charFragment(g, u, k, fixed, lo, hi, ctx)) || near();
  const wc = await countAll(ctx.count, words.map(([a, e]) => g.src.slice(a, e)), ctx.cache, ctx.stats);
  let ft = 0, best = null;
  for (let w = words.length - 1; w >= 1; w--) { // the fragment starts at word w
    ft += Math.max(1, wc[w] - ctx.overhead);
    const tot = fixed + ft;
    if (tot > hi) break;
    if (tot < lo) continue;
    const before = g.src.slice(words[w - 1][0], words[w - 1][1]);   // the fragment starts after a sentence end if it can, else after a clause end
    const bonus = /[.!?\u2026]["'\u201d\u2019)\]]*$/.test(before) ? 0.5 : (/[,;:\u2014)]$/.test(before) ? 0.25 : 0);
    const score = Math.abs(tot - mid) - bonus * (hi - lo);
    if (!best || score < best.score) best = { score, w, ft };
  }
  if (!best) return (await charFragment(g, u, k, fixed, lo, hi, ctx)) || near();
  ctx.stats.fragments++;
  return { k, fragStart: words[best.w][0], fragTok: best.ft };
}

/** The last resort for an overlap, when no run of whole units and no run that starts at a word is 10 to 15% of the chunk (one "word" of
 *  dozens of tokens: a minified line, a long number, a path): the carried text starts inside the last unit, at the character that puts its
 *  tokens in the window (never between the halves of a surrogate pair). null when no character does. */
async function charFragment(g, u, k, fixed, lo, hi, ctx) {
  const src = g.src;
  const tokOf = async (x) => (await countAll(ctx.count, [src.slice(x, u.e)], ctx.cache, ctx.stats))[0] - ctx.overhead;
  if (u.e - u.s < 2 || (await tokOf(u.s)) + fixed < lo) return null;
  let a = u.s, z = u.e - 1;                    // the largest start whose text still holds lo tokens
  while (a < z) { const m = (a + z + 1) >> 1; if ((await tokOf(m)) + fixed >= lo) a = m; else z = m - 1; }
  let x = a; if (x > u.s && /[\udc00-\udfff]/.test(src[x])) x--;
  const ft = await tokOf(x);
  if (!(ft >= 1 && ft + fixed >= lo && ft + fixed <= hi)) return null;
  ctx.stats.fragments++;
  return { k, fragStart: x, fragTok: ft };
}
