// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The words of a question, made safe for an FTS5 MATCH (rag/DESIGN.md section 9). Used by the store worker (search.fts), which
// never hands a person's text to MATCH as it is: FTS5 reads its own query language there (AND, OR, NOT, NEAR(...), "phrases",
// prefix*, ^first, column:filters, {columns}:, -, +, parentheses), and a question such as `what is "NEAR(x" -- or a: b*` would be
// read as that language, fail, or search another column.
//
// What is done (FTS5's query syntax, sqlite.org/fts5.html section 3, for the SQLite 3.45.3 the folder search carries):
//   - the words are taken out of the text: runs of letters, digits and the underscore in any script (\p{L}\p{N}_), the characters
//     a bareword may hold; everything else (quotes, brackets, colons, asterisks, carets, dashes, plus signs) separates words;
//   - each word becomes a quoted string ("word"): inside quotes FTS5 reads no operator, keyword or column name, and a word holds no
//     quote to escape (the doubling below is kept for safety if the word rule ever changes);
//   - the words are joined with OR, so a passage matches when it holds any of them and bm25 ranks those holding more, rarer ones
//     higher (a question of eight words rarely has all eight in one passage);
//   - the same word twice counts once (compared without case), at most MAX_TERMS words are kept, and a word longer than MAX_LEN
//     characters is left out (no passage of the index holds one, and it would only cost time).
// A question with no word in it gives null: the caller makes no text search then (the vector search still runs).

export const MAX_TERMS = 32;
export const MAX_LEN = 64;

export function ftsWords(text) {
  if (typeof text !== 'string' || !text) return [];
  const seen = new Set(), out = [];
  for (const m of text.normalize('NFC').matchAll(/[\p{L}\p{N}_]+/gu)) {
    const w = m[0];
    if (w.length > MAX_LEN) continue;
    const key = w.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key); out.push(w);
    if (out.length >= MAX_TERMS) break;
  }
  return out;
}

export function ftsMatch(text) {
  const words = ftsWords(text);
  if (!words.length) return null;
  return words.map((w) => '"' + w.replace(/"/g, '""') + '"').join(' OR ');
}
