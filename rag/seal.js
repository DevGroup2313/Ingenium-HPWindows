// INGENIUM  |  Copyright (c) 2026 Yannie D. Forest. All rights reserved.
// NO LICENCE IS GRANTED. See LICENCE.txt.
//
// The folder search's index, sealed (Ingenium 7.59). Her approval (20 November 2026), of the recommendation to seal the index at the
// cost measured on a prototype; her choice (21 November 2026): "One index per profile", each sealed with that profile's own key.
//
// SQLite's opfs-sahpool file system (vendor/sqlite/sqlite3.mjs) reads and writes every pool file through FileSystemSyncAccessHandle, past
// the pool's own 4,096-byte header (HEADER_OFFSET_DATA). This part takes over the handle's read, write, getSize and truncate, in the
// store worker alone, so that a file's data is kept sealed:
//   a 64-byte head: "INGRAGS1", the key's id (8 bytes), the plain size (8 bytes, little-endian), a salt (16 bytes);
//   then sectors of 4,096 plain bytes, each kept as a 24-byte nonce, the sealed bytes and a 16-byte tag: XChaCha20-Poly1305
//   (@noble/ciphers 2.4.0, vendor/noble), the file's salt and the sector's number bound in as additional data.
// The key is the profile's own, derived from its data key by modules/r197.js (IngLock.ragKey: HKDF-SHA-256, info "ingenium-rag 1"),
// handed to the worker when the index opens; it is kept in this worker's memory only. A plain file is sealed in place, from its last
// sector to its first (a sealed sector lies at or after the plain bytes it holds), so a stop midway loses nothing that was not already
// written twice; a sealed file is opened again in place from its first sector to its last. Opened sectors are kept in this worker's
// memory (at most 192 MiB, the least recently used let go first), so a search after the first reads no sector twice.
// Measured on the prototype (20,000 passages, 121 MB, headless Chromium): building about three times as long as plain; the first
// vector search of a session about 3.5 s, later ones 37 to 74 ms, against 0.8 to 1.4 s for the plain index.

const HDR = 4096, META = 64, L = 4096, NONCE = 24, TAG = 16, P = L + NONCE + TAG;
const MAGIC = new Uint8Array([0x49, 0x4e, 0x47, 0x52, 0x41, 0x47, 0x53, 0x31]);   // INGRAGS1
const K = { key: null, kid: null, convert: false, unseal: false };
let ST = new WeakMap();
const SEEN = new Set();          // the handles whose data this part has read or written (WeakRefs), for openAll()
let AEAD = null;                 // xchacha20poly1305, once loaded
const proto = globalThis.FileSystemSyncAccessHandle && FileSystemSyncAccessHandle.prototype;
const nRead = proto && proto.read, nWrite = proto && proto.write, nSize = proto && proto.getSize, nTrunc = proto && proto.truncate;
export const stats = { opened: 0, sealed: 0, converted: 0, unsealed: 0, cached: 0 };

const CACHE = new Map(); let BUDGET = 48 * 1024;      // opened sectors: 192 MiB at most
const ids = new WeakMap(); let nextId = 1;
function cid(sah, i) { let n = ids.get(sah); if (!n) { n = nextId++; ids.set(sah, n); } return n * 4294967296 + i; }
function cget(sah, i) { const k = cid(sah, i), v = CACHE.get(k); if (v) { CACHE.delete(k); CACHE.set(k, v); stats.cached++; } return v; }
function cput(sah, i, v) { const k = cid(sah, i); CACHE.delete(k); CACHE.set(k, v); while (CACHE.size > BUDGET) CACHE.delete(CACHE.keys().next().value); }
function cdrop(sah, from) { const n = ids.get(sah); if (!n) return; for (const k of [...CACHE.keys()]) if (Math.floor(k / 4294967296) === n && k % 4294967296 >= from) CACHE.delete(k); }

function u8(b) { return b instanceof Uint8Array ? b : ArrayBuffer.isView(b) ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength) : new Uint8Array(b); }
// SQLite's pool turns an error thrown in a read or a write into an I/O failure and keeps the error itself; so the last one is kept here
// too, for the store worker to report its code (LOCKED, OTHER_KEY, SEAL_BROKEN) and not a bare I/O failure
let LAST = null;
function coded(code, msg) { const e = new Error(msg); e.code = code; LAST = e; return e; }
export function takeError() { const e = LAST; LAST = null; return e; }
function locked() { return coded('LOCKED', 'the folder search index is sealed, and its key is not here (sign in first)'); }
function same(a, b) { if (a.length !== b.length) return false; for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false; return true; }
function ad(s, i) { const a = new Uint8Array(24); a.set(s.salt, 0); const dv = new DataView(a.buffer); dv.setUint32(16, i % 4294967296, true); dv.setUint32(20, Math.floor(i / 4294967296), true); return a; }
function phys(sah) { return Math.max(0, Math.floor((nSize.call(sah) - HDR - META) / P)); }
function seen(sah) { if (!sah.__ingSeen) { sah.__ingSeen = 1; SEEN.add(new WeakRef(sah)); } }
function writeMeta(sah, s) {
  const m = new Uint8Array(META); m.set(MAGIC, 0); m.set(s.kid, 8);
  const dv = new DataView(m.buffer); dv.setUint32(16, s.size % 4294967296, true); dv.setUint32(20, Math.floor(s.size / 4294967296), true); m.set(s.salt, 24);
  nWrite.call(sah, m, { at: HDR });
}
function sealSector(sah, s, i, plain) {
  const nonce = crypto.getRandomValues(new Uint8Array(NONCE));
  const ct = AEAD(K.key, nonce, ad(s, i)).encrypt(plain);
  const raw = new Uint8Array(P); raw.set(nonce, 0); raw.set(ct, NONCE);
  nWrite.call(sah, raw, { at: HDR + META + i * P }); stats.sealed++;
  cput(sah, i, plain.slice ? plain.slice() : new Uint8Array(plain));
}
function openSector(sah, s, i) {
  const c = cget(sah, i); if (c) return c.slice();
  if (i >= phys(sah)) return new Uint8Array(L);
  if (!K.key) throw locked();
  const raw = new Uint8Array(P); nRead.call(sah, raw, { at: HDR + META + i * P });
  stats.opened++;
  let plain;
  try { plain = AEAD(K.key, raw.subarray(0, NONCE), ad(s, i)).decrypt(raw.subarray(NONCE)); }
  catch (e) { throw coded('SEAL_BROKEN', 'a sector of the folder search index does not open (changed, or sealed with another key)'); }
  cput(sah, i, plain.slice());
  return plain;
}
function kidOk(s) { if (!K.kid || !same(s.kid, K.kid)) throw coded('OTHER_KEY', 'the folder search index was sealed with another key'); }

function sealInPlace(sah) {
  const size = nSize.call(sah) - HDR, n = Math.ceil(size / L);
  const s = { sealed: true, kid: K.kid, size, salt: crypto.getRandomValues(new Uint8Array(16)) };
  for (let i = n - 1; i >= 0; i--) {
    const plain = new Uint8Array(L); nRead.call(sah, plain, { at: HDR + i * L });
    if ((i + 1) * L > size) plain.fill(0, size - i * L);
    sealSector(sah, s, i, plain);
  }
  writeMeta(sah, s); stats.converted++;
  return s;
}
function unsealInPlace(sah, s) {
  const n = Math.ceil(s.size / L);
  for (let i = 0; i < n; i++) { const plain = openSector(sah, s, i); nWrite.call(sah, plain, { at: HDR + i * L }); }
  nTrunc.call(sah, HDR + s.size); cdrop(sah, 0); stats.unsealed++;
  return { plain: true };
}

function state(sah) {
  seen(sah);
  let s = ST.get(sah);
  if (!s) {
    const size = nSize.call(sah);
    if (size <= HDR) s = { empty: true };
    else {
      const m = new Uint8Array(META); nRead.call(sah, m, { at: HDR });
      if (same(m.subarray(0, 8), MAGIC)) { const dv = new DataView(m.buffer); s = { sealed: true, kid: m.slice(8, 16), size: dv.getUint32(16, true) + dv.getUint32(20, true) * 4294967296, salt: m.slice(24, 40) }; }
      else s = { plain: true };
    }
    if (s.sealed && K.key && K.unseal) { kidOk(s); s = unsealInPlace(sah, s); }
    else if (s.plain && K.key && K.convert && !K.unseal) s = sealInPlace(sah);
    ST.set(sah, s);
  }
  if (s.sealed && K.key) kidOk(s);
  return s;
}

// ---------------------------------------------------------------------------------------------------- what the store worker calls
export async function load(vendorBase) {
  if (AEAD) return true;
  const m = await import(vendorBase + 'noble/chacha.js');
  AEAD = m.xchacha20poly1305;
  return typeof AEAD === 'function';
}
export function ready() { return !!AEAD; }
// key and kid: the profile's own (bytes, or base64 and hex); convert: seal plain files met from now on; unseal: open sealed ones in place
export function setKey(key, kid, opts) {
  const b = (x) => (typeof x === 'string' ? Uint8Array.from(atob(x), (c) => c.charCodeAt(0)) : u8(x).slice());
  const h = (x) => (typeof x === 'string' ? Uint8Array.from(x.match(/../g).map((p) => parseInt(p, 16))) : u8(x).slice());
  K.key = key ? b(key) : null; K.kid = kid ? h(kid) : null;
  if (K.key && K.key.length !== 32) throw coded('BAD_KEY', 'the folder search key must be 32 bytes');
  K.convert = !!(opts && opts.convert); K.unseal = !!(opts && opts.unseal);
}
export function keyed() { return !!K.key; }
// every file met is judged afresh at its next read or write (after the split, when the key arrives)
export function forget() { ST = new WeakMap(); CACHE.clear(); }
// the state of each file this part has met: how many sealed, plain and empty
export function census() { const c = { sealed: 0, plain: 0, empty: 0 }; for (const w of SEEN) { const h = w.deref(); if (!h) continue; const s = ST.get(h); if (s) c[s.sealed ? 'sealed' : s.plain ? 'plain' : 'empty']++; } return c; }
// turning the sign-in off: every sealed file this worker has met is opened again in place, with the key it still holds
export function openAll() {
  if (!K.key) throw locked();
  K.unseal = true; K.convert = false; let n = 0;
  for (const w of SEEN) { const h = w.deref(); if (!h) continue; const s = ST.get(h); if (s && s.sealed) { kidOk(s); ST.set(h, unsealInPlace(h, s)); n++; } }
  return n;
}
// sealing every plain file this worker has met (when the sign-in is turned on with the index open)
export function sealAll() {
  if (!K.key) throw locked();
  K.convert = true; K.unseal = false; let n = 0;
  for (const w of SEEN) { const h = w.deref(); if (!h) continue; const s = ST.get(h); if (s && s.plain && nSize.call(h) > HDR) { ST.set(h, sealInPlace(h)); n++; } }
  return n;
}
export function setCache(sectors) { BUDGET = Math.max(0, sectors | 0); while (CACHE.size > BUDGET) CACHE.delete(CACHE.keys().next().value); }

export function install() {
  if (!proto || proto.__ingSealed) return !!proto;
  proto.__ingSealed = true;
  proto.read = function (buf, opt) {
    const at = Number(opt && opt.at || 0), b = u8(buf);
    if (at + b.length <= HDR) return nRead.call(this, buf, opt);
    const s = state(this);
    if (!s.sealed) return nRead.call(this, buf, opt);
    if (!K.key) throw locked();
    const off = at - HDR, end = Math.min(s.size, off + b.length);
    if (end <= off) return 0;
    for (let i = Math.floor(off / L); i * L < end; i++) {
      const plain = openSector(this, s, i), a = Math.max(off, i * L), z = Math.min(end, (i + 1) * L);
      b.set(plain.subarray(a - i * L, z - i * L), a - off);
    }
    return end - off;
  };
  proto.write = function (buf, opt) {
    const at = Number(opt && opt.at || 0), b = u8(buf);
    if (at + b.length <= HDR) return nWrite.call(this, buf, opt);
    let s = state(this);
    if (s.empty) { s = K.key && K.convert && !K.unseal ? { sealed: true, kid: K.kid, size: 0, salt: crypto.getRandomValues(new Uint8Array(16)) } : { plain: true }; if (s.sealed) writeMeta(this, s); ST.set(this, s); }
    if (!s.sealed) return nWrite.call(this, buf, opt);
    if (!K.key) throw locked();
    const off = at - HDR, end = off + b.length, have = phys(this);
    for (let i = have; i < Math.floor(off / L); i++) sealSector(this, s, i, new Uint8Array(L));
    for (let i = Math.floor(off / L); i * L < end; i++) {
      const a = Math.max(off, i * L), z = Math.min(end, (i + 1) * L);
      let plain;
      if (a === i * L && z === (i + 1) * L) plain = b.subarray(a - off, z - off);
      else {
        plain = openSector(this, s, i);
        if ((i + 1) * L > s.size) plain.fill(0, Math.max(0, s.size - i * L));
        plain.set(b.subarray(a - off, z - off), a - i * L);
      }
      sealSector(this, s, i, plain);
    }
    if (end > s.size) { s.size = end; writeMeta(this, s); }
    return b.length;
  };
  proto.getSize = function () { const s = state(this); return s.sealed ? HDR + s.size : nSize.call(this); };
  proto.truncate = function (n) {
    n = Number(n);
    if (n <= HDR) { ST.set(this, { empty: true }); cdrop(this, 0); return nTrunc.call(this, n); }
    const s = state(this);
    if (!s.sealed) return nTrunc.call(this, n);
    if (!K.key) throw locked();
    const size = n - HDR;
    if (size < s.size) { nTrunc.call(this, HDR + META + Math.ceil(size / L) * P); cdrop(this, Math.ceil(size / L)); }
    else if (size > s.size && s.size % L) { const i = Math.floor(s.size / L), plain = openSector(this, s, i); plain.fill(0, s.size - i * L); sealSector(this, s, i, plain); }
    s.size = size; writeMeta(this, s);
  };
  return true;
}
