// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The page's side of the desktop app's folder bridge (rag/DESIGN.md section 6), and nothing else: window.pywebview.api.rag_*.
// The app chooses the folder with its own dialog, remembers only the folders chosen, lists their files (the seven types) and
// reads them in pieces of at most 1 MB; the page never sees a path it was not given.
//
//   rag_choose_folder()                -> {ok: true, path, name} | {ok: false, cancelled: true}
//   rag_folders()                      -> [{path, name, added}]
//   rag_forget_folder(path)            -> {ok}
//   rag_list(path)                     -> {ok, files: [{rel, size, mtime}]}   (recursive, the seven types, at most 5,000 files)
//   rag_read(path, rel, offset, length)-> {ok, data (base64), size, eof}      (length at most 1 MB)
//
// Every function rejects with an Error whose message is the app's own when the app answers {ok: false, error}.

export const PIECE = 1048576;       // the most the app returns for one read
export const LIST_CAP = 5000;       // the most files the app lists for one folder

/** window.pywebview.api when the page runs in the desktop app, else null. */
export function api() {
  try { return (typeof window !== 'undefined' && window.pywebview && window.pywebview.api) || null; } catch (e) { return null; }
}

/** True when the app's folder bridge is there (the page runs in the Mac or Windows app and the app has been started with it). */
export function available() {
  const a = api();
  return !!(a && typeof a.rag_list === 'function' && typeof a.rag_read === 'function');
}

/** Resolves true when the bridge is there, waiting up to timeoutMs for the app to announce it (the pywebviewready event). */
export function whenReady(timeoutMs = 4000) {
  if (available()) return Promise.resolve(true);
  if (typeof window === 'undefined') return Promise.resolve(false);
  return new Promise((resolve) => {
    let done = false;
    const finish = () => { if (done) return; done = true; window.removeEventListener('pywebviewready', onReady); clearTimeout(t); resolve(available()); };
    const onReady = () => setTimeout(finish, 0);
    window.addEventListener('pywebviewready', onReady);
    const t = setTimeout(finish, timeoutMs);
  });
}

async function call(name, ...args) {
  const a = api();
  if (!a || typeof a[name] !== 'function') throw bridgeError('the folder bridge is not available (' + name + ')', 'NO_BRIDGE');
  const r = await a[name](...args);
  return r;
}

function bridgeError(message, code) { const e = new Error(message); e.code = code || 'BRIDGE'; return e; }

function must(r, what) {
  if (r && r.ok === false && !r.cancelled) throw bridgeError(r.error || r.message || (what + ' failed'), r.code || 'BRIDGE');
  return r;
}

/** The app's folder dialog. -> {ok: true, path, name} | {ok: false, cancelled: true} */
export async function chooseFolder() { return must(await call('rag_choose_folder'), 'choosing a folder'); }

/** The folders the app remembers: [{path, name, added}]. */
export async function chosenFolders() {
  const r = await call('rag_folders');
  return Array.isArray(r) ? r : (r && Array.isArray(r.folders) ? r.folders : []);
}

/** Makes the app forget a folder (it may no longer be read). -> {ok} */
export async function forgetFolder(path) { return must(await call('rag_forget_folder', path), 'forgetting a folder'); }

/** The files of a folder: [{rel, size, mtime}] (mtime in milliseconds), with .truncated set when the app says it stopped at its limit
 *  (5,000 files, or more directory entries than it looks at). A folder the app cannot list (an unplugged drive, a folder moved away) rejects. */
export async function listFiles(path) {
  const r = must(await call('rag_list', path), 'listing the folder');
  if (!r || !Array.isArray(r.files)) throw bridgeError('the app returned no list of files', 'BRIDGE');
  const files = r.files.map((f) => ({ rel: String(f.rel), size: Number(f.size) || 0, mtime: Number(f.mtime) || 0 }));
  if (r.truncated !== undefined) files.truncated = !!r.truncated;   // the app stopped at its limit: this is not the whole folder
  return files;
}

/** Base64 to bytes (a Uint8Array that owns its buffer, so it can be sent on without a copy). */
export function b64ToBytes(b64) {
  if (!b64) return new Uint8Array(0);
  const bin = atob(b64), n = bin.length, out = new Uint8Array(n);
  for (let i = 0; i < n; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** The file, a piece at a time (1 MB), as an async iterator of Uint8Array. {signal} stops it between pieces. A file that changes
 *  size while it is read rejects (the next run reads it again). */
export async function* readPieces(path, rel, opts = {}) {
  const piece = Math.max(1, Math.min(PIECE, opts.pieceSize || PIECE));
  let offset = 0, size = null;
  for (;;) {
    if (opts.signal && opts.signal.aborted) { const e = new Error('stopped'); e.name = 'AbortError'; throw e; }
    const r = must(await call('rag_read', path, rel, offset, piece), 'reading ' + rel);
    if (!r) throw bridgeError('the app returned nothing for ' + rel, 'BRIDGE');
    const bytes = b64ToBytes(r.data || '');
    const n = bytes.length;      // taken before the piece is handed on: the consumer may transfer its buffer, which leaves the array empty
    if (r.size != null) size = Number(r.size);
    if (n) { offset += n; yield bytes; }
    if (r.eof || !n) break;
  }
  if (size != null && offset !== size) throw bridgeError('the file changed while it was read (' + rel + ')', 'CHANGED');
}
