// docs/js/watchlist-lists.js
//
// Shared multiple-watchlist engine used by:
//   • watchlist-star.js — the ☆/★ button + "which list?" picker on every
//     scan dashboard.
//   • watchlist.js — the full Watchlist page (list tabs, rename/delete).
//
// Data model
// ──────────
// Logged OUT (localStorage only):
//   wl_lists = { [listId]: { name, order, symbols: [SYM, …] } }
//   wl_meta  = { [SYM]: { currentPrice, previousClose, change,
//                          changePercent, industryGroup, industry } }
//
// Logged IN (Firestore, synced across devices):
//   users/{uid}/watchlistDefs/{listId}   -> { name, order, createdAt }
//   users/{uid}/watchlist/{symbol}       -> { symbol, currentPrice,
//                          previousClose, change, changePercent,
//                          industryGroup, industry, addedAt,
//                          lists: [listId, …] }
//
// The per-symbol price document (users/{uid}/watchlist/{symbol}) is left
// exactly as it was before multi-list support — scripts/update-prices.js
// (the scheduled server job) refreshes it unmodified via a collectionGroup
// query. All that's new is the `lists` array on that same doc, saying
// which watchlist(s) the symbol belongs to.

import { db, auth } from "./firebase.js";
import {
  doc,
  setDoc,
  updateDoc,
  deleteDoc,
  getDoc,
  getDocs,
  collection,
  onSnapshot,
  arrayUnion,
  arrayRemove,
  serverTimestamp
} from "https://www.gstatic.com/firebasejs/11.9.0/firebase-firestore.js";

const LISTS_KEY = "wl_lists";
const META_KEY = "wl_meta";
const LEGACY_SYMBOLS_KEY = "wl_symbols"; // pre-multi-list format
export const DEFAULT_LIST_ID = "default";
const DEFAULT_LIST_NAME = "My Watchlist";

function uid8() {
  return Math.random().toString(36).slice(2, 10);
}

// ── local storage ──────────────────────────────────────────────────────
function readLocalLists() {
  try {
    const raw = JSON.parse(localStorage.getItem(LISTS_KEY) || "null");
    if (raw && typeof raw === "object") return raw;
  } catch { /* ignore */ }
  return null;
}

function writeLocalLists(lists) {
  try { localStorage.setItem(LISTS_KEY, JSON.stringify(lists)); }
  catch { /* ignore quota / private-mode errors */ }
}

// Migrates the old single-list "wl_symbols" array (if present) into the
// new multi-list shape, once, the first time this module runs.
function migrateLocal() {
  let lists = readLocalLists();
  if (lists) return lists;

  lists = {};
  try {
    const legacy = JSON.parse(localStorage.getItem(LEGACY_SYMBOLS_KEY) || "[]");
    if (Array.isArray(legacy) && legacy.length) {
      lists[DEFAULT_LIST_ID] = { name: DEFAULT_LIST_NAME, order: 0, symbols: legacy };
    }
  } catch { /* ignore */ }

  if (!lists[DEFAULT_LIST_ID]) {
    lists[DEFAULT_LIST_ID] = { name: DEFAULT_LIST_NAME, order: 0, symbols: [] };
  }
  writeLocalLists(lists);
  return lists;
}

export function getLocalMeta() {
  try { return JSON.parse(localStorage.getItem(META_KEY) || "{}"); }
  catch { return {}; }
}

export function setLocalMeta(symbol, meta) {
  try {
    const all = getLocalMeta();
    all[symbol] = { ...all[symbol], ...meta };
    localStorage.setItem(META_KEY, JSON.stringify(all));
  } catch { /* ignore */ }
}

export function getLocalLists() {
  return migrateLocal();
}

export function getLocalListsSorted() {
  const lists = getLocalLists();
  return Object.entries(lists)
    .map(([id, l]) => ({ id, name: l.name, order: l.order ?? 0, symbols: l.symbols || [] }))
    .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name));
}

export function localSymbolLists(symbol) {
  const lists = getLocalLists();
  return Object.entries(lists)
    .filter(([, l]) => (l.symbols || []).includes(symbol))
    .map(([id]) => id);
}

export function createLocalList(name) {
  const lists = getLocalLists();
  const id = uid8();
  const order = Object.keys(lists).length;
  lists[id] = { name: (name || "New Watchlist").trim() || "New Watchlist", order, symbols: [] };
  writeLocalLists(lists);
  return id;
}

export function renameLocalList(id, name) {
  const lists = getLocalLists();
  if (!lists[id]) return;
  lists[id].name = (name || "").trim() || lists[id].name;
  writeLocalLists(lists);
}

export function deleteLocalList(id) {
  const lists = getLocalLists();
  delete lists[id];
  if (!Object.keys(lists).length) {
    lists[DEFAULT_LIST_ID] = { name: DEFAULT_LIST_NAME, order: 0, symbols: [] };
  }
  writeLocalLists(lists);
}

export function addToLocalList(listId, symbol, meta) {
  const lists = getLocalLists();
  if (!lists[listId]) return;
  const set = new Set(lists[listId].symbols || []);
  set.add(symbol);
  lists[listId].symbols = Array.from(set);
  writeLocalLists(lists);
  if (meta) setLocalMeta(symbol, meta);
}

export function removeFromLocalList(listId, symbol) {
  const lists = getLocalLists();
  if (!lists[listId]) return;
  lists[listId].symbols = (lists[listId].symbols || []).filter((s) => s !== symbol);
  writeLocalLists(lists);
}

// ── Firestore ───────────────────────────────────────────────────────────
export async function fetchRemoteLists(uid) {
  const snap = await getDocs(collection(db, "users", uid, "watchlistDefs"));
  const out = [];
  snap.forEach((d) => out.push({ id: d.id, ...d.data() }));
  if (!out.length) {
    // First time this account has used watchlists — seed a default list,
    // and if legacy watchlist/{symbol} docs already exist (pre-multi-list
    // data), fold them all into it so nothing gets orphaned.
    await setDoc(doc(db, "users", uid, "watchlistDefs", DEFAULT_LIST_ID), {
      name: DEFAULT_LIST_NAME,
      order: 0,
      createdAt: serverTimestamp()
    });
    out.push({ id: DEFAULT_LIST_ID, name: DEFAULT_LIST_NAME, order: 0 });

    try {
      const wlSnap = await getDocs(collection(db, "users", uid, "watchlist"));
      const updates = [];
      wlSnap.forEach((d) => {
        const data = d.data();
        if (!Array.isArray(data.lists) || !data.lists.length) {
          updates.push(updateDoc(doc(db, "users", uid, "watchlist", d.id), {
            lists: [DEFAULT_LIST_ID]
          }));
        }
      });
      await Promise.all(updates);
    } catch { /* ignore — non-fatal */ }
  }
  return out.sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.name.localeCompare(b.name));
}

export async function createRemoteList(uid, name, order) {
  const ref = doc(collection(db, "users", uid, "watchlistDefs"));
  await setDoc(ref, {
    name: (name || "New Watchlist").trim() || "New Watchlist",
    order: order ?? 999,
    createdAt: serverTimestamp()
  });
  return ref.id;
}

export async function renameRemoteList(uid, listId, name) {
  await updateDoc(doc(db, "users", uid, "watchlistDefs", listId), {
    name: (name || "").trim()
  });
}

export async function deleteRemoteList(uid, listId) {
  await deleteDoc(doc(db, "users", uid, "watchlistDefs", listId));
  // Pull this listId out of every symbol doc that referenced it; delete the
  // symbol doc entirely once it belongs to no list at all.
  const wlSnap = await getDocs(collection(db, "users", uid, "watchlist"));
  const work = [];
  wlSnap.forEach((d) => {
    const lists = d.data().lists || [];
    if (!lists.includes(listId)) return;
    const remaining = lists.filter((l) => l !== listId);
    work.push(
      remaining.length
        ? updateDoc(d.ref, { lists: arrayRemove(listId) })
        : deleteDoc(d.ref)
    );
  });
  await Promise.all(work);
}

export async function addToRemoteList(uid, listId, symbol, meta) {
  const ref = doc(db, "users", uid, "watchlist", symbol);
  const snap = await getDoc(ref);
  await setDoc(ref, {
    symbol,
    industryGroup: meta?.industryGroup ?? (snap.exists() ? snap.data().industryGroup : "") ?? "",
    industry: meta?.industry ?? (snap.exists() ? snap.data().industry : "") ?? "",
    currentPrice: meta?.currentPrice ?? (snap.exists() ? snap.data().currentPrice : null) ?? null,
    addedAt: snap.exists() ? snap.data().addedAt : Date.now(),
    lists: arrayUnion(listId)
  }, { merge: true });
}

export async function removeFromRemoteList(uid, listId, symbol) {
  const ref = doc(db, "users", uid, "watchlist", symbol);
  const snap = await getDoc(ref);
  if (!snap.exists()) return;
  const remaining = (snap.data().lists || []).filter((l) => l !== listId);
  if (remaining.length) {
    await updateDoc(ref, { lists: arrayRemove(listId) });
  } else {
    await deleteDoc(ref);
  }
}

export function subscribeRemoteListDefs(uid, cb) {
  return onSnapshot(collection(db, "users", uid, "watchlistDefs"), (snap) => {
    const out = [];
    snap.forEach((d) => out.push({ id: d.id, ...d.data() }));
    cb(out.sort((a, b) => (a.order ?? 0) - (b.order ?? 0) || a.name.localeCompare(b.name)));
  });
}

export function subscribeRemoteSymbols(uid, cb) {
  return onSnapshot(collection(db, "users", uid, "watchlist"), (snap) => {
    const out = [];
    snap.forEach((d) => out.push({ id: d.id, ...d.data() }));
    cb(out);
  });
}
