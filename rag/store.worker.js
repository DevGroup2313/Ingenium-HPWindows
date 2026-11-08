// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's store: the ONLY SQLite connection of the page (rag/DESIGN.md sections 3 and 4). A module worker.
// SQLite 3.45.3 with FTS5 and sqlite-vec 0.1.6 (vendor/sqlite/sqlite3.mjs, from sqlite-vec-wasm-demo 0.1.6), kept in the
// origin's private file system (OPFS) by the opfs-sahpool file system, or in memory for tests ('memory' touches no OPFS).
//
// Messages (DESIGN section 3): request {id, op, args}; reply {id, ok: true, result} or {id, ok: false, error}, where error is
// {name, message, code, resultCode, stack}. Events carry no id: {event: 'hello', worker: 'store'} when the script has started.
// Requests run one at a time, in the order they arrive; every op is synchronous SQLite, so a file.replace is never
// interleaved with another request.
//
// Ops: open close info ops compact reset | principal.ensure | folder.add folder.list folder.remove folder.touch | acl.grant
// acl.revoke acl.allowed | file.list file.get file.replace file.delete | emb.lookup emb.put emb.prune | stats |
// debug.rows debug.sql debug.fail debug.migrate debug.wipe (tests only: served only when the worker was started with ?debug=1
// in its address, which store.js does for createStore({debug: true}); the app never does).
//
// Three details that the vendored file system does not do for us:
//  - installOpfsSAHPoolVfs() deletes its whole directory (the database) when it fails to start, and it remembers a failure for
//    the life of the worker. A start can fail only because another window, or the page that has just been reloaded, still holds
//    the files. So before installing it we try the files ourselves and wait for them to be free; if they stay held we report
//    OPFS_LOCKED and never call the installer.
//  - the build does not bind a Float32Array: vectors are bound as the bytes of one (new Uint8Array(f32.buffer)).
//  - vec0 and FTS5 tables are not reached by foreign keys, so every delete of chunks removes their rows by rowid itself.
//  - a database file does not shrink when rows are deleted. A new database is made with auto_vacuum = incremental (a setting of
//    the file, not a table; it can only be chosen before the first table exists), and compact() gives the freed pages back
//    (and the empty vector blocks that sqlite-vec leaves behind, see reclaimEmptyVecBlocks).
//  - crash safety: the vendored file system told SQLite, whatever the truth, that another connection held a lock, so the journal
//    a crash leaves was never rolled back. vendor.json patches it (as SQLite 3.54.1 answers); open() also checks the database
//    with quick_check whenever a journal was left behind, and any damaged database is reported as CORRUPT (main.js then offers
//    to start the folder search again from nothing; the person's files are never touched).
//  - removing leaves nothing behind: secure_delete is on (SQLite overwrites deleted rows with zeros), FTS5 runs with its
//    'secure-delete' option (schema version 2), each vector is overwritten with zeros before its row is deleted (sqlite-vec
//    0.1.6 keeps a deleted vector's bytes in its block), and folder.remove({prune: true}) drops every cached vector no
//    passage uses any more.

import { ftsMatch } from './ftsquery.js';   // phase 2: a question's words, safe for FTS5's MATCH

const DEFAULT_DIMS = 384;
const POOL_NAME = 'ingenium-rag';       // the VFS name, and the OPFS directory '.ingenium-rag' that holds the database files
const POOL_DIR = '.ingenium-rag';
const POOL_CAPACITY = 12;               // files the pool may hold: the database, its journal, spare names for tests
const TABLES = ['meta', 'principals', 'folders', 'folder_acl', 'files', 'chunks', 'chunks_fts', 'chunks_vec', 'emb_cache'];

class StoreError extends Error {
  constructor(code, message, extra) { super(message); this.name = 'StoreError'; this.code = code; if (extra) Object.assign(this, extra); }
}
const bad = (m) => new StoreError('BAD_ARGS', m);

let sqlite3 = null;   // the loaded module
let pool = null;      // the opfs-sahpool utility object, once installed
let db = null;        // the one connection
let conn = null;      // {dbName, vfs, dims} of the open database
let faultAfter = null; // debug.fail: throw inside the next file.replace after this many chunks
let lastOpen = null;   // the arguments of the last successful open, for reset
const DEBUG = (() => { try { return new URL(self.location.href).searchParams.get('debug') === '1'; } catch (_) { return false; } })();

// ------------------------------------------------------------------------------------------------ schema and migrations
// Version 1 is DESIGN section 4, word for word; only the vector length is a parameter (384 unless open() is told otherwise).
function schemaV1(dims) {
  return `
create table meta(key text primary key, value text);
create table principals(id integer primary key, name text not null unique);
create table folders(id integer primary key, name text not null, path text not null unique, source text not null,
  manual_only integer not null default 0, added_at integer not null, last_indexed_at integer);
create table folder_acl(folder_id integer not null references folders(id) on delete cascade,
  principal_id integer not null references principals(id) on delete cascade, primary key(folder_id, principal_id));
create table files(id integer primary key, folder_id integer not null references folders(id) on delete cascade,
  path text not null, size integer not null, mtime integer not null, content_hash text not null, chunk_count integer not null,
  indexed_at integer not null, unique(folder_id, path));
create table chunks(id integer primary key, folder_id integer not null, file_id integer not null references files(id) on delete cascade,
  file_path text not null, chunk_index integer not null, heading_path text not null default '', char_start integer not null,
  char_end integer not null, page integer, content_hash text not null, text text not null);
create index chunks_file on chunks(file_id);
create virtual table chunks_fts using fts5(text, heading_path, folder_id unindexed, tokenize = 'porter unicode61');
create virtual table chunks_vec using vec0(folder_id integer partition key, embedding float[${dims}] distance_metric=cosine);
create table emb_cache(model text not null, hash text not null, vec blob not null, primary key(model, hash));`;
}

// The ordered list. A later version appends one entry {version: n + 1, name, up(db, ctx)}; each runs in its own transaction,
// together with the new meta.schema_version, so a step that fails leaves the database at the version it had.
const MIGRATIONS = [
  { version: 1, name: 'folder search, version 1', up(d, ctx) { d.exec(schemaV1(ctx.dims)); } },
  // FTS5 keeps a deleted row's words in its index as tombstones until a merge; with 'secure-delete' they are taken out at once
  { version: 2, name: 'removing leaves nothing behind', up(d) { d.exec("insert into chunks_fts(chunks_fts, rank) values ('secure-delete', 1)"); } },
];

function currentVersion(d) {
  if (!d.selectValue("select 1 from sqlite_master where type = 'table' and name = 'meta'")) return 0;
  const v = d.selectValue("select value from meta where key = 'schema_version'");
  return v == null ? 0 : parseInt(v, 10);
}

function checkList(list) {
  list.forEach((m, i) => {
    if (!m || m.version !== i + 1 || typeof m.up !== 'function') throw new StoreError('BAD_MIGRATIONS', 'migration ' + (i + 1) + ' is missing or out of order');
  });
}

function migrate(d, list, ctx) {
  checkList(list);
  const from = currentVersion(d), latest = list.length;
  if (from > latest) {
    throw new StoreError('SCHEMA_TOO_NEW', 'this index was made by a newer Ingenium (schema version ' + from + '; this one knows ' + latest + ')', { schemaVersion: from });
  }
  const applied = [];
  for (const m of list) {
    if (m.version <= from) continue;
    txOn(d, () => {
      m.up(d, ctx);
      d.exec({ sql: "insert into meta(key, value) values ('schema_version', ?) on conflict(key) do update set value = excluded.value", bind: [String(m.version)] });
    });
    applied.push(m.version);
  }
  return { from, to: Math.max(from, latest), applied };
}

function txOn(d, fn) {
  d.exec('begin immediate');
  try { const r = fn(); d.exec('commit'); return r; }
  catch (e) { try { d.exec('rollback'); } catch (_) { /* the original error is the one to report */ } throw e; }
}
const tx = (fn) => txOn(db, fn);

// ------------------------------------------------------------------------------------------------ small helpers
const num = (v) => (typeof v === 'bigint' ? Number(v) : v);
function str(v, name) { if (typeof v !== 'string' || v === '') throw bad(name + ' must be a non-empty string'); return v; }
function int(v, name) { if (!Number.isInteger(v)) throw bad(name + ' must be an integer'); return v; }
function fin(v, name) { if (typeof v !== 'number' || !Number.isFinite(v)) throw bad(name + ' must be a number'); return v; }
// retrieval's arguments: how many hits a search gives (1 to 200, 20 when not said), and a chosen set of folders as JSON for json_each
function limitK(v) { if (v == null) return 20; const k = int(v, 'k'); if (k < 1 || k > 200) throw bad('k must be between 1 and 200'); return k; }
function onlyFolders(v) {
  if (v == null) return null;
  if (!Array.isArray(v) || v.length > 10000) throw bad('folderIds must be an array of folder ids');
  return JSON.stringify(v.map((x, i) => int(x, 'folderIds[' + i + ']')));
}
function needOpen() { if (!db) throw new StoreError('NOT_OPEN', 'the store is not open: call open first'); }
function needFolder(id) { if (db.selectValue('select 1 from folders where id = ?', [id]) == null) throw new StoreError('NO_SUCH_FOLDER', 'there is no folder ' + id); }
function needPrincipal(id) { if (db.selectValue('select 1 from principals where id = ?', [id]) == null) throw new StoreError('NO_SUCH_PRINCIPAL', 'there is no principal ' + id); }

const folderRow = (o) => ({ id: num(o.id), name: o.name, path: o.path, source: o.source, manualOnly: !!o.manual_only, addedAt: num(o.added_at), lastIndexedAt: o.last_indexed_at == null ? null : num(o.last_indexed_at) });
const fileRow = (o) => ({ id: num(o.id), path: o.path, size: num(o.size), mtime: num(o.mtime), contentHash: o.content_hash, chunkCount: num(o.chunk_count), indexedAt: num(o.indexed_at) });

// The bytes of an embedding, for binding. The vector must have exactly the index's length and hold only finite numbers.
function vecBytes(v, dims, label) {
  let f;
  if (v instanceof Float32Array) f = v;
  else if (v instanceof ArrayBuffer) { if (v.byteLength % 4) throw bad(label + ': embedding is not whole floats'); f = new Float32Array(v); }
  else if (ArrayBuffer.isView(v) && v.BYTES_PER_ELEMENT === 1) { if (v.byteLength % 4) throw bad(label + ': embedding is not whole floats'); f = new Float32Array(v.buffer.slice(v.byteOffset, v.byteOffset + v.byteLength)); }
  else if (Array.isArray(v)) f = Float32Array.from(v);
  else throw bad(label + ': embedding must be a Float32Array');
  if (f.length !== dims) throw bad(label + ': embedding has ' + f.length + ' values, this index holds ' + dims);
  for (let i = 0; i < f.length; i++) if (!Number.isFinite(f[i])) throw bad(label + ': embedding holds a value that is not a number');
  return new Uint8Array(f.buffer, f.byteOffset, f.byteLength);
}

function normChunk(c, i, dims) {
  const label = 'chunk ' + i;
  if (!c || typeof c !== 'object') throw bad(label + ' must be an object');
  if (typeof c.text !== 'string') throw bad(label + ': text must be a string');
  if (typeof c.content_hash !== 'string' || c.content_hash === '') throw bad(label + ': content_hash must be a non-empty string');
  if (c.heading_path != null && typeof c.heading_path !== 'string') throw bad(label + ': heading_path must be a string');
  if (c.page != null && !Number.isInteger(c.page)) throw bad(label + ': page must be an integer or null');
  return {
    chunk_index: c.chunk_index == null ? i : int(c.chunk_index, label + ': chunk_index'),
    heading_path: c.heading_path || '',
    char_start: int(c.char_start, label + ': char_start'),
    char_end: int(c.char_end, label + ': char_end'),
    page: c.page == null ? null : c.page,
    content_hash: c.content_hash,
    text: c.text,
    vec: vecBytes(c.embedding, dims, label),
  };
}

// Removes the chunk rows that match `where`, and their FTS5 and vec0 rows (which no foreign key reaches), by rowid.
// Each vector is first overwritten with zeros: sqlite-vec 0.1.6 only marks a deleted vector as unused and keeps its bytes.
function removeChunks(where, bind) {
  const ids = db.selectValues('select id from chunks where ' + where, bind);
  if (!ids.length) return 0;
  const zeros = new Uint8Array((conn.dims || DEFAULT_DIMS) * 4);
  const zv = db.prepare('update chunks_vec set embedding = ? where rowid = ?');
  const dv = db.prepare('delete from chunks_vec where rowid = ?'), df = db.prepare('delete from chunks_fts where rowid = ?');
  try { for (const id of ids) { zv.bind([zeros, id]).stepReset(); dv.bind(id).stepReset(); df.bind(id).stepReset(); } }
  finally { zv.finalize(); dv.finalize(); df.finalize(); }
  db.exec({ sql: 'delete from chunks where ' + where, bind });
  return ids.length;
}

// The embedding cache keeps nothing that no chunk uses any more: after chunks go, their hashes go too unless another chunk,
// in any file, still has the same hash (so a file renamed by adding the new name first still reuses its vectors).
function purgeCache(hashes) {
  if (!hashes.length) return 0;
  const set = JSON.stringify(hashes);
  db.exec({
    sql: 'delete from emb_cache where hash in (select value from json_each(?1)) and hash not in (select content_hash from chunks where content_hash in (select value from json_each(?1)))',
    bind: [set],
  });
  return db.changes();
}

function vecDimsOf(d) {
  const sql = d.selectValue("select sql from sqlite_master where name = 'chunks_vec'") || '';
  const m = /embedding\s+float\[(\d+)\]/i.exec(sql);
  return m ? parseInt(m[1], 10) : null;
}

// sqlite-vec 0.1.6 keeps the block of 1,024 vectors of a folder whose vectors are all deleted (1.5 MB for 384 values) and gives
// it to no other folder, so removing a folder would leave its vector storage allocated for good. compact() deletes such empty
// blocks from vec0's own tables. That is touching another library's tables, so it is done only for the pinned version, only if
// the three tables have exactly the columns of 0.1.6, and only if the vector table answers the same count afterwards.
// vec0 numbers a new block with an AUTOINCREMENT in chunks_vec_chunks but gives its vector row "highest rowid + 1" in
// chunks_vec_vector_chunks00; the two agree only while the block with the highest number exists. So the highest block is never
// deleted (if it is empty it waits for the next compact, after a newer block exists), and the two tables are compared afterwards.
const VEC_SHADOWS = {
  chunks_vec_chunks: 'chunk_id,size,sequence_id,partition00,validity,rowids',
  chunks_vec_rowids: 'rowid,id,chunk_id,chunk_offset',
  chunks_vec_vector_chunks00: 'rowid,vectors',
};
function reclaimEmptyVecBlocks() {
  if (db.selectValue('select vec_version()') !== 'v0.1.6') return 0;
  for (const [t, cols] of Object.entries(VEC_SHADOWS)) {
    if (db.selectValues("select name from pragma_table_info('" + t + "') order by cid").join(',') !== cols) return 0;
  }
  return tx(() => {
    const ids = db.selectValues('select chunk_id from chunks_vec_chunks where validity = zeroblob(length(validity)) and chunk_id not in (select chunk_id from chunks_vec_rowids) and chunk_id < (select max(chunk_id) from chunks_vec_chunks)');
    if (!ids.length) return 0;
    const before = num(db.selectValue('select count(*) from chunks_vec'));
    const set = JSON.stringify(ids);
    db.exec({ sql: 'delete from chunks_vec_vector_chunks00 where rowid in (select value from json_each(?))', bind: [set] });
    db.exec({ sql: 'delete from chunks_vec_chunks where chunk_id in (select value from json_each(?))', bind: [set] });
    const apart = num(db.selectValue('select (select count(*) from chunks_vec_chunks where chunk_id not in (select rowid from chunks_vec_vector_chunks00)) + (select count(*) from chunks_vec_vector_chunks00 where rowid not in (select chunk_id from chunks_vec_chunks))'));
    if (apart !== 0 || num(db.selectValue('select count(*) from chunks_vec')) !== before) {
      throw new StoreError('COMPACT_CHECK', 'compact: the vector table changed while empty blocks were released; nothing was changed');
    }
    return ids.length;
  });
}

function collectTransfer(result) {
  const seen = new Set(), list = [];
  const visit = (v) => {
    if (v instanceof Float32Array) {
      if (v.byteOffset === 0 && v.byteLength === v.buffer.byteLength && v.buffer instanceof ArrayBuffer && !seen.has(v.buffer)) { seen.add(v.buffer); list.push(v.buffer); }
    } else if (Array.isArray(v)) v.forEach(visit);
    else if (v && typeof v === 'object' && Object.getPrototypeOf(v) === Object.prototype) Object.values(v).forEach(visit);
  };
  visit(result);
  return list;
}

// SQLITE_CORRUPT (11, with its extended codes such as CORRUPT_VTAB) and SQLITE_NOTADB (26) are reported as CORRUPT.
const isCorrupt = (e) => !!e && ((e.code === 'CORRUPT') || (Number.isInteger(e.resultCode) && [11, 26].includes(e.resultCode & 0xff)) || /SQLITE_(CORRUPT|NOTADB)/.test(String(e.message)));
function serializeError(e) {
  return {
    name: (e && e.name) || 'Error',
    message: String((e && e.message) || e),
    code: isCorrupt(e) ? 'CORRUPT' : ((e && e.code) || null),
    resultCode: e && e.resultCode != null ? e.resultCode : null,
    stack: e && e.stack ? String(e.stack) : null,
  };
}

// ------------------------------------------------------------------------------------------------ opening
async function loadSqlite(vendorBase) {
  if (sqlite3) return sqlite3;
  const mod = await import(vendorBase + 'sqlite/sqlite3.mjs');
  sqlite3 = await mod.default();   // no arguments: a module object passed here aborts the start
  return sqlite3;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isHeld = (e) => e && (e.name === 'NoModificationAllowedError' || /Access Handles cannot be created|another open Access Handle/i.test(String(e.message)));

// Tries every file of the pool directory for a moment; resolves when all are free, rejects with OPFS_LOCKED if some stay held.
async function waitForFreeFiles(waitMs) {
  let dir = await navigator.storage.getDirectory();
  for (const part of POOL_DIR.split('/').filter(Boolean)) {
    try { dir = await dir.getDirectoryHandle(part); } catch (e) { if (e && e.name === 'NotFoundError') return 'new'; throw e; }
  }
  let opaque;
  try { opaque = await dir.getDirectoryHandle('.opaque'); } catch (e) { if (e && e.name === 'NotFoundError') return 'new'; throw e; }
  const t0 = Date.now();
  for (;;) {
    let held = false;
    for await (const [, h] of opaque) {
      if (h.kind !== 'file') continue;
      try { const s = await h.createSyncAccessHandle(); await s.close(); }
      catch (e) { if (isHeld(e)) { held = true; break; } throw e; }
    }
    if (!held) return 'free';
    if (Date.now() - t0 >= waitMs) {
      throw new StoreError('OPFS_LOCKED', 'the folder search index is in use by another window of Ingenium (or the page has only just been reloaded); close the other window and try again');
    }
    await sleep(120);
  }
}

async function ensurePool(a) {
  if (pool) return pool;
  if (!(typeof navigator !== 'undefined' && navigator.storage && navigator.storage.getDirectory) || typeof sqlite3.installOpfsSAHPoolVfs !== 'function') {
    throw new StoreError('OPFS_UNAVAILABLE', 'this web view has no private file system (OPFS) for the folder search index');
  }
  await waitForFreeFiles(a.lockWaitMs == null ? 8000 : a.lockWaitMs);
  try {
    pool = await sqlite3.installOpfsSAHPoolVfs({ name: POOL_NAME, directory: POOL_DIR, initialCapacity: POOL_CAPACITY });
  } catch (e) {
    throw new StoreError('OPFS_UNAVAILABLE', 'the private file system of this web view could not be used for the folder search index: ' + String((e && e.message) || e), { cause: e });
  }
  await pool.reserveMinimumCapacity(POOL_CAPACITY);
  return pool;
}

async function open(a) {
  const dbName = a.dbName == null ? 'rag.sqlite3' : a.dbName;
  const vfs = a.vfs == null ? 'opfs-sahpool' : a.vfs;
  if (typeof dbName !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(dbName)) throw bad('dbName must be letters, digits, dot, dash or underscore');
  if (vfs !== 'opfs-sahpool' && vfs !== 'memory') throw bad("vfs must be 'opfs-sahpool' or 'memory'");
  const wantDims = a.dims == null ? null : a.dims;
  if (wantDims != null && (!Number.isInteger(wantDims) || wantDims < 1 || wantDims > 8192)) throw bad('dims must be a whole number from 1 to 8192');
  if (db) {
    if (conn.dbName === dbName && conn.vfs === vfs && (wantDims == null || wantDims === conn.dims)) return info();
    throw new StoreError('ALREADY_OPEN', 'the store is already open on ' + conn.dbName + ' (' + conn.vfs + '); close it first');
  }
  if (typeof a.vendorBase !== 'string' || !/\/$/.test(a.vendorBase)) throw bad('vendorBase must be the URL of the vendor folder, ending in /');
  await loadSqlite(a.vendorBase);

  let d, leftJournal = false;
  if (vfs === 'memory') {
    d = new sqlite3.oo1.DB(':memory:', 'c');
  } else {
    await ensurePool(a);
    leftJournal = pool.getFileNames().includes('/' + dbName + '-journal');
    d = new pool.OpfsSAHPoolDb('/' + dbName);
  }
  try {
    d.exec('pragma foreign_keys = on');
    d.exec('pragma secure_delete = on');
    if (leftJournal && currentVersion(d) > 0) {
      // a journal was left by a crash or a forced quit: SQLite has rolled it back on this first read; check the result
      const verdict = d.selectValues('pragma quick_check(3)');
      if (!(verdict.length === 1 && verdict[0] === 'ok')) {
        throw new StoreError('CORRUPT', 'the folder search index was damaged (' + verdict.join('; ').slice(0, 200) + ')');
      }
    }
    const fresh = currentVersion(d) === 0;
    if (fresh) d.exec('pragma auto_vacuum = 2');   // incremental; has an effect only while the file has no table
    const existing = fresh ? null : vecDimsOf(d);
    if (existing != null && wantDims != null && existing !== wantDims) {
      throw new StoreError('DIMS_MISMATCH', 'this index holds vectors of ' + existing + ' values; a model of ' + wantDims + ' needs a new index', { have: existing, want: wantDims });
    }
    migrate(d, MIGRATIONS, { dims: wantDims == null ? DEFAULT_DIMS : wantDims });
    db = d;
    conn = { dbName, vfs, dims: vecDimsOf(d) || DEFAULT_DIMS };
    lastOpen = Object.assign({}, a, { dbName, vfs });
  } catch (e) {
    try { d.close(); } catch (_) { /* nothing more to do */ }
    throw e;
  }
  return info();
}

const sizeNow = () => num(db.selectValue('select page_count * page_size from pragma_page_count, pragma_page_size'));

// persisted: whether the browser has promised not to clear this origin's storage (the page asks for that: persist() is not
// available in workers); storage: how much the browser will let this origin keep, and how much it holds now.
async function info() {
  needOpen();
  let persisted = null, storage = null;
  if (conn.vfs === 'opfs-sahpool' && typeof navigator !== 'undefined' && navigator.storage) {
    try { persisted = await navigator.storage.persisted(); } catch (_) { persisted = null; }
    try { const e = await navigator.storage.estimate(); storage = { quota: e.quota, usage: e.usage }; } catch (_) { storage = null; }
  }
  return {
    schemaVersion: currentVersion(db),
    vfs: conn.vfs,
    sqlite: sqlite3.version.libVersion,
    vec: db.selectValue('select vec_version()'),
    dims: conn.dims,
    dbName: conn.dbName,
    bytes: sizeNow(),
    persisted,
    storage,
  };
}

// ------------------------------------------------------------------------------------------------ the ops
const OPS = {
  open,
  info: () => info(),
  ops: () => Object.keys(OPS).filter((o) => DEBUG || !o.startsWith('debug.')),

  // Gives back the space of deleted rows: the empty vector blocks of removed folders are deleted, the pages on the free list are
  // released and the file gets smaller. (An index made by an older build without auto_vacuum is rewritten instead, which needs
  // room for a second copy while it runs.)
  compact() {
    needOpen();
    const before = sizeNow(), mode = num(db.selectValue('pragma auto_vacuum')) === 2 ? 'incremental' : 'vacuum';
    const blocks = reclaimEmptyVecBlocks();
    if (mode === 'vacuum') db.exec('vacuum');
    else {
      // one page is given back for each step of the statement, and db.exec steps a statement that returns rows only when
      // it is given a callback: so it is stepped here until it is done
      const st = db.prepare('pragma incremental_vacuum');
      try { while (st.step()) { /* a page released */ } } finally { st.finalize(); }
    }
    return { before, after: sizeNow(), mode, blocks };
  },
  close() { if (db) { try { db.close(); } finally { db = null; conn = null; } } return { closed: true }; },

  // Starts the folder search index again from nothing (after it was found damaged): the database files are deleted and a new,
  // empty database is opened with the same arguments. The person's own files are never touched.
  async reset(a) {
    const args = Object.assign({}, lastOpen || {}, a || {});
    if (db) { try { db.close(); } catch (_) { /* a damaged file may refuse; it is deleted below */ } db = null; conn = null; }
    const name = args.dbName == null ? 'rag.sqlite3' : args.dbName;
    if (typeof name !== 'string' || !/^[A-Za-z0-9._-]{1,64}$/.test(name)) throw bad('dbName must be letters, digits, dot, dash or underscore');
    if ((args.vfs == null ? 'opfs-sahpool' : args.vfs) === 'opfs-sahpool') {
      if (typeof args.vendorBase !== 'string' || !/\/$/.test(args.vendorBase)) throw bad('vendorBase must be the URL of the vendor folder, ending in /');
      await loadSqlite(args.vendorBase);
      await ensurePool(args);
      for (const f of ['/' + name, '/' + name + '-journal']) { try { pool.unlink(f); } catch (_) { /* not there */ } }
    }
    return open(args);
  },

  'principal.ensure'(a) {
    needOpen();
    const name = str(a.name, 'name');
    db.exec({ sql: 'insert or ignore into principals(name) values (?)', bind: [name] });
    return { id: num(db.selectValue('select id from principals where name = ?', [name])) };
  },

  'folder.add'(a) {
    needOpen();
    const name = str(a.name, 'name'), path = str(a.path, 'path');
    const source = a.source == null ? 'bridge' : str(a.source, 'source');
    return tx(() => {
      const old = db.selectObject('select * from folders where path = ?', [path]);
      if (old) {
        // adding a folder that is already there changes nothing but what the caller says differently
        if (a.manualOnly != null) db.exec({ sql: 'update folders set manual_only = ? where id = ?', bind: [a.manualOnly ? 1 : 0, old.id] });
        if (name !== old.name) db.exec({ sql: 'update folders set name = ? where id = ?', bind: [name, old.id] });
        return Object.assign(folderRow(db.selectObject('select * from folders where id = ?', [old.id])), { created: false });
      }
      db.exec({ sql: 'insert into folders(name, path, source, manual_only, added_at) values (?,?,?,?,?)', bind: [name, path, source, a.manualOnly ? 1 : 0, Date.now()] });
      return Object.assign(folderRow(db.selectObject('select * from folders where path = ?', [path])), { created: true });
    });
  },

  'folder.list'(a) {
    needOpen();
    const sql = 'select f.*, (select count(*) from files x where x.folder_id = f.id) as file_count, ' +
      '(select coalesce(sum(x.chunk_count), 0) from files x where x.folder_id = f.id) as chunk_total from folders f' +
      (a && a.principalId != null ? ' where f.id in (select folder_id from folder_acl where principal_id = ?)' : '') + ' order by f.id';
    return db.selectObjects(sql, a && a.principalId != null ? [int(a.principalId, 'principalId')] : undefined)
      .map((o) => Object.assign(folderRow(o), { fileCount: num(o.file_count), chunkCount: num(o.chunk_total) }));
  },

  'folder.remove'(a) {
    needOpen();
    const folderId = int(a.folderId, 'folderId');
    return tx(() => {
      if (db.selectValue('select 1 from folders where id = ?', [folderId]) == null) return { removed: false, files: 0, chunks: 0 };
      const hashes = db.selectValues('select distinct content_hash from chunks where folder_id = ?', [folderId]);
      const files = num(db.selectValue('select count(*) from files where folder_id = ?', [folderId]));
      const chunks = removeChunks('folder_id = ?', [folderId]);
      db.exec({ sql: 'delete from files where folder_id = ?', bind: [folderId] });
      db.exec({ sql: 'delete from folders where id = ?', bind: [folderId] });   // its folder_acl rows go with it
      purgeCache(hashes);
      // prune: also every cached vector that no passage uses (those a stopped or failed reading had saved); main.js asks
      // for it when no reading is running, since a running reading's saved vectors are in the cache only
      let pruned = 0;
      if (a.prune) { db.exec('delete from emb_cache where hash not in (select content_hash from chunks)'); pruned = db.changes(); }
      return { removed: true, files, chunks, pruned };
    });
  },

  'folder.touch'(a) {
    needOpen();
    const folderId = int(a.folderId, 'folderId'), at = fin(a.indexedAt, 'indexedAt');
    needFolder(folderId);
    db.exec({ sql: 'update folders set last_indexed_at = ? where id = ?', bind: [at, folderId] });
    return folderRow(db.selectObject('select * from folders where id = ?', [folderId]));
  },

  'acl.grant'(a) {
    needOpen();
    const folderId = int(a.folderId, 'folderId'), principalId = int(a.principalId, 'principalId');
    needFolder(folderId); needPrincipal(principalId);
    db.exec({ sql: 'insert or ignore into folder_acl(folder_id, principal_id) values (?,?)', bind: [folderId, principalId] });
    return { folderId, principalId };
  },

  'acl.revoke'(a) {
    needOpen();
    const folderId = int(a.folderId, 'folderId'), principalId = int(a.principalId, 'principalId');
    db.exec({ sql: 'delete from folder_acl where folder_id = ? and principal_id = ?', bind: [folderId, principalId] });
    return { removed: db.changes() };
  },

  'acl.allowed'(a) {
    needOpen();
    return db.selectValues('select folder_id from folder_acl where principal_id = ? order by folder_id', [int(a.principalId, 'principalId')]).map(num);
  },

  // ---- retrieval (phase 2, DESIGN section 9) ----
  // Who may read what is decided by SQL inside each search: a row counts only when its folder is one the principal has in
  // folder_acl, found by a subquery of the same statement, so a passage of a folder that is not theirs is never ranked, never
  // fused and never returned. A chosen set of folders (folderIds: the folders the chat was given) can only narrow that set: it is
  // a second condition of the same query, never a replacement for the first.

  // A principal by name, without making one (retrieval for a name the index does not know finds nothing)
  'principal.find'(a) {
    needOpen();
    const id = db.selectValue('select id from principals where name = ?', [str(a.name, 'name')]);
    return id == null ? null : { id: num(id) };
  },

  // The text search: bm25 over the passages' words and headings (FTS5), best first. The question is never handed to MATCH as it
  // is: ftsMatch() keeps its words only, each quoted, joined with OR. A question with no word gives no hits.
  'search.fts'(a) {
    needOpen();
    const principalId = int(a.principalId, 'principalId'), k = limitK(a.k), only = onlyFolders(a.folderIds);
    if (a.query != null && typeof a.query !== 'string') throw bad('query must be text');
    const match = ftsMatch(a.query || '');
    if (!match) return { match: null, hits: [] };
    const sql = 'select rowid as id, folder_id, bm25(chunks_fts) as score from chunks_fts where chunks_fts match ?1' +
      ' and folder_id in (select folder_id from folder_acl where principal_id = ?2)' +
      (only ? ' and folder_id in (select value from json_each(?4))' : '') +
      ' order by score, rowid limit ?3';
    const hits = db.selectObjects(sql, only ? [match, principalId, k, only] : [match, principalId, k])
      .map((o, i) => ({ id: num(o.id), folderId: num(o.folder_id), rank: i + 1, score: o.score }));
    return { match, hits };
  },

  // The vector search: the k nearest passages by cosine distance (vec0), nearest first. sqlite-vec 0.1.6 runs "folder_id in (...)"
  // on its partition key once for each folder, giving the k nearest of each, and refuses LIMIT beside "k = ?" (both checked in
  // rig/t_rag2_retrieve.js): the k nearest of them all are taken here.
  'search.vec'(a) {
    needOpen();
    const principalId = int(a.principalId, 'principalId'), k = limitK(a.k), only = onlyFolders(a.folderIds);
    const vec = vecBytes(a.embedding, conn.dims, 'the question');
    const sql = 'select rowid as id, folder_id, distance from chunks_vec where embedding match ?1 and k = ?2' +
      ' and folder_id in (select folder_id from folder_acl where principal_id = ?3)' +
      (only ? ' and folder_id in (select value from json_each(?4))' : '');
    const rows = db.selectObjects(sql, only ? [vec, k, principalId, only] : [vec, k, principalId])
      .map((o) => ({ id: num(o.id), folderId: num(o.folder_id), distance: o.distance }))
      .sort((x, y) => x.distance - y.distance || x.id - y.id).slice(0, k);
    return { hits: rows.map((h, i) => Object.assign(h, { rank: i + 1 })) };
  },

  // The passages a fused list names, with what a citation needs, for the principal only: a passage outside the folders they may
  // read is left out here too
  'chunk.get'(a) {
    needOpen();
    const principalId = int(a.principalId, 'principalId');
    if (!Array.isArray(a.ids) || a.ids.length > 1000) throw bad('ids must be an array of at most 1000 passage ids');
    const ids = JSON.stringify(a.ids.map((x, i) => int(x, 'ids[' + i + ']')));
    return db.selectObjects('select c.id, c.folder_id, f.name as folder_name, f.path as folder_path, c.file_id, c.file_path, c.chunk_index,' +
      ' c.heading_path, c.char_start, c.char_end, c.page, c.text from chunks c join folders f on f.id = c.folder_id' +
      ' where c.id in (select value from json_each(?1)) and c.folder_id in (select folder_id from folder_acl where principal_id = ?2)',
    [ids, principalId]).map((o) => ({
      id: num(o.id), folderId: num(o.folder_id), folderName: o.folder_name, folderPath: o.folder_path, fileId: num(o.file_id),
      filePath: o.file_path, chunkIndex: num(o.chunk_index), headingPath: o.heading_path, charStart: num(o.char_start),
      charEnd: num(o.char_end), page: o.page == null ? null : num(o.page), text: o.text }));
  },

  'file.list'(a) {
    needOpen();
    return db.selectObjects('select * from files where folder_id = ? order by path', [int(a.folderId, 'folderId')]).map(fileRow);
  },

  'file.get'(a) {
    needOpen();
    const o = db.selectObject('select * from files where folder_id = ? and path = ?', [int(a.folderId, 'folderId'), str(a.path, 'path')]);
    return o ? fileRow(o) : null;
  },

  // ONE transaction: the old chunks of the file (rows, FTS5 rows, vec0 rows) go, the new ones come, the embedding cache is
  // updated. Everything is checked before the first write; anything that still fails inside leaves the old rows.
  'file.replace'(a) {
    needOpen();
    const folderId = int(a.folderId, 'folderId'), path = str(a.path, 'path');
    const size = fin(a.size, 'size'), mtime = fin(a.mtime, 'mtime'), contentHash = str(a.contentHash, 'contentHash');
    if (!Array.isArray(a.chunks)) throw bad('chunks must be an array');
    const model = a.chunks.length ? str(a.model, 'model') : null;
    const rows = a.chunks.map((c, i) => normChunk(c, i, conn.dims));
    needFolder(folderId);
    return tx(() => {
      const now = Date.now();
      const old = db.selectObject('select id from files where folder_id = ? and path = ?', [folderId, path]);
      let fileId, stale = [];
      if (old) {
        fileId = num(old.id);
        stale = db.selectValues('select distinct content_hash from chunks where file_id = ?', [fileId]);
        removeChunks('file_id = ?', [fileId]);
        db.exec({ sql: 'update files set size = ?, mtime = ?, content_hash = ?, chunk_count = ?, indexed_at = ? where id = ?', bind: [size, mtime, contentHash, rows.length, now, fileId] });
      } else {
        db.exec({ sql: 'insert into files(folder_id, path, size, mtime, content_hash, chunk_count, indexed_at) values (?,?,?,?,?,?,?)', bind: [folderId, path, size, mtime, contentHash, rows.length, now] });
        fileId = num(db.selectValue('select id from files where folder_id = ? and path = ?', [folderId, path]));
      }
      if (rows.length) {
        let next = num(db.selectValue('select coalesce(max(id), 0) from chunks')) + 1;
        const sC = db.prepare('insert into chunks(id, folder_id, file_id, file_path, chunk_index, heading_path, char_start, char_end, page, content_hash, text) values (?,?,?,?,?,?,?,?,?,?,?)');
        const sF = db.prepare('insert into chunks_fts(rowid, text, heading_path, folder_id) values (?,?,?,?)');
        const sV = db.prepare('insert into chunks_vec(rowid, folder_id, embedding) values (?,?,?)');
        const sE = db.prepare('insert or replace into emb_cache(model, hash, vec) values (?,?,?)');
        try {
          rows.forEach((r, i) => {
            if (faultAfter !== null && i === faultAfter) { faultAfter = null; throw new StoreError('INJECTED', 'injected failure (debug.fail) after ' + i + ' chunks'); }
            const id = next++;
            sC.bind([id, folderId, fileId, path, r.chunk_index, r.heading_path, r.char_start, r.char_end, r.page, r.content_hash, r.text]).stepReset();
            sF.bind([id, r.text, r.heading_path, folderId]).stepReset();
            sV.bind([id, folderId, r.vec]).stepReset();
            sE.bind([model, r.content_hash, r.vec]).stepReset();
          });
        } finally { sC.finalize(); sF.finalize(); sV.finalize(); sE.finalize(); }
      }
      if (stale.length) purgeCache(stale);
      return { fileId, chunkCount: rows.length };
    });
  },

  'file.delete'(a) {
    needOpen();
    const folderId = int(a.folderId, 'folderId'), path = str(a.path, 'path');
    return tx(() => {
      const f = db.selectObject('select id from files where folder_id = ? and path = ?', [folderId, path]);
      if (!f) return { deleted: false, chunkCount: 0 };
      const hashes = db.selectValues('select distinct content_hash from chunks where file_id = ?', [f.id]);
      const chunkCount = removeChunks('file_id = ?', [f.id]);
      db.exec({ sql: 'delete from files where id = ?', bind: [f.id] });
      purgeCache(hashes);
      return { deleted: true, chunkCount };
    });
  },

  'emb.lookup'(a) {
    needOpen();
    const model = str(a.model, 'model');
    if (!Array.isArray(a.hashes)) throw bad('hashes must be an array');
    const out = {};
    if (!a.hashes.length) return out;
    for (const [hash, blob] of db.selectArrays('select hash, vec from emb_cache where model = ? and hash in (select value from json_each(?))', [model, JSON.stringify(a.hashes)])) {
      const f = new Float32Array(blob.byteLength / 4);
      new Uint8Array(f.buffer).set(blob);   // its own buffer, so it can travel back without taking others with it
      out[hash] = f;
    }
    return out;
  },

  'emb.put'(a) {
    needOpen();
    const model = str(a.model, 'model');
    if (!Array.isArray(a.items)) throw bad('items must be an array');
    const items = a.items.map((it, i) => {
      if (!it || typeof it.hash !== 'string' || it.hash === '') throw bad('item ' + i + ': hash must be a non-empty string');
      return { hash: it.hash, vec: vecBytes(it.embedding != null ? it.embedding : it.vec, conn.dims, 'item ' + i) };
    });
    tx(() => {
      const s = db.prepare('insert or replace into emb_cache(model, hash, vec) values (?,?,?)');
      try { for (const it of items) s.bind([model, it.hash, it.vec]).stepReset(); } finally { s.finalize(); }
    });
    return { stored: items.length };
  },

  // Drops every cache entry that no chunk uses.
  'emb.prune'() {
    needOpen();
    db.exec('delete from emb_cache where hash not in (select content_hash from chunks)');
    return { removed: db.changes() };
  },

  stats() {
    needOpen();
    const out = {};
    for (const t of TABLES) out[t] = num(db.selectValue('select count(*) from ' + t));
    return out;
  },

  // ---- tests only
  'debug.rows'(a) {
    needOpen();
    const t = a.table, limit = a.limit == null ? 100 : int(a.limit, 'limit');
    const cols = {
      chunks_vec: 'rowid, folder_id, vec_length(embedding) as dims',
      chunks_fts: 'rowid, text, heading_path, folder_id',
      emb_cache: 'model, hash, length(vec) as bytes',
    };
    if (!TABLES.includes(t) && t !== 'sqlite_master') throw bad('debug.rows: unknown table ' + t);
    return db.selectObjects('select ' + (cols[t] || '*') + ' from ' + t + (t === 'sqlite_master' ? ' order by name' : ' order by rowid') + ' limit ?', [limit]);
  },
  'debug.sql'(a) {
    needOpen();
    const s = String(a.sql || '').trim();
    if (!/^select\b/i.test(s) || s.includes(';')) throw bad('debug.sql runs one select');
    return db.selectObjects(s, a.bind);
  },
  'debug.fail'(a) { faultAfter = a && a.after != null ? int(a.after, 'after') : null; return { armed: faultAfter }; },
  'debug.migrate'(a) {
    needOpen();
    if (!Array.isArray(a.migrations)) throw bad('migrations must be an array of {version, sql}');
    const extra = a.migrations.map((m) => ({ version: m.version, name: 'test step', up(d) { d.exec(m.sql); } }));
    return migrate(db, MIGRATIONS.concat(extra), { dims: conn.dims });
  },
  async 'debug.wipe'() {
    if (db) { try { db.close(); } finally { db = null; conn = null; } }
    if (pool) await pool.wipeFiles();
    return { wiped: true };
  },
};

// ------------------------------------------------------------------------------------------------ the message loop
let chain = Promise.resolve();
async function handle(m) {
  const { id, op, args } = m || {};
  try {
    const f = OPS[op];
    if (!Object.prototype.hasOwnProperty.call(OPS, op) || typeof f !== 'function' || (!DEBUG && String(op).startsWith('debug.'))) throw new StoreError('UNKNOWN_OP', 'unknown op ' + op);
    const result = await f(args || {});
    self.postMessage({ id, ok: true, result }, collectTransfer(result));
  } catch (e) {
    try { self.postMessage({ id, ok: false, error: serializeError(e) }); } catch (_) { /* the page is gone */ }
  }
}
self.onmessage = (e) => { chain = chain.then(() => handle(e.data)); };
self.postMessage({ event: 'hello', worker: 'store' });
