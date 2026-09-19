// docs/js/supabase-compat.js
//
// Replaces the Firestore SDK import in every file:
//   import {...} from "https://www.gstatic.com/firebasejs/11.9.0/firebase-firestore.js";
// becomes:
//   import {...} from "./supabase-compat.js";
//
// This module implements the small subset of the Firestore client API this
// app actually uses (collection, doc, getDoc, getDocs, setDoc, updateDoc,
// deleteDoc, addDoc, query, orderBy, onSnapshot, serverTimestamp,
// arrayUnion, arrayRemove) on top of Supabase Postgres tables + Realtime,
// so the rest of the codebase (watchlist.js, position-tracker.js,
// accounts.js, etc.) needs NO further changes beyond its import line.
//
// Reference shape, same as Firestore:
//   collection(db, "users", uid, "positions")        -> a collection ref
//   doc(db, "users", uid, "positions", id)            -> a doc ref
//   doc(db, "users", uid)                              -> the user's own row
//   doc(collectionRef)                                 -> a new doc ref w/ generated id

import { supabase } from "./supabase.js";

// The `users` table's primary key is the user's own id (not a separate
// userId column) — everything else uses "userId" (+ "symbol" for watchlist,
// since that table's key is composite).
const PK = { users: "id", watchlist: "symbol" };
function pkColumn(table) {
  return PK[table] || "id";
}
function onConflictCols(table) {
  return table === "watchlist" ? "userId,symbol" : pkColumn(table);
}

// ── references ──────────────────────────────────────────────────────────
export function collection(_db, ...parts) {
  if (parts[0] === "users" && parts.length >= 3) {
    return { table: parts[2], userId: parts[1] };
  }
  throw new Error(`Unsupported collection() path: ${parts.join("/")}`);
}

export function doc(refOrDb, ...parts) {
  // doc(collectionRef) -> brand-new doc, generated id
  if (parts.length === 0 && refOrDb && refOrDb.table) {
    return { table: refOrDb.table, userId: refOrDb.userId, id: crypto.randomUUID() };
  }
  // doc(db, "users", uid) -> the user's own row
  if (parts[0] === "users" && parts.length === 2) {
    return { table: "users", userId: parts[1], id: parts[1] };
  }
  // doc(db, "users", uid, table, id)
  if (parts[0] === "users" && parts.length === 4) {
    return { table: parts[2], userId: parts[1], id: parts[3] };
  }
  throw new Error(`Unsupported doc() path: ${parts.join("/")}`);
}

export function query(ref, ...mods) {
  const out = { ...ref };
  for (const m of mods) if (m && m.__orderBy) out._order = m;
  return out;
}

export function orderBy(field, dir = "asc") {
  return { __orderBy: true, field, dir };
}

// ── write-value sentinels (serverTimestamp / arrayUnion / arrayRemove) ──
export function serverTimestamp() {
  return { __op: "serverTimestamp" };
}
export function arrayUnion(value) {
  return { __op: "arrayUnion", value };
}
export function arrayRemove(value) {
  return { __op: "arrayRemove", value };
}

async function resolvePayload(table, id, data) {
  const out = { ...data };
  const arrayOps = [];
  for (const [key, val] of Object.entries(out)) {
    if (val && typeof val === "object" && val.__op === "serverTimestamp") {
      out[key] = new Date().toISOString();
    } else if (val && typeof val === "object" && (val.__op === "arrayUnion" || val.__op === "arrayRemove")) {
      arrayOps.push([key, val]);
    }
  }
  if (arrayOps.length) {
    const cols = arrayOps.map(([k]) => k).join(",");
    const { data: row } = await supabase.from(table).select(cols).eq(pkColumn(table), id).maybeSingle();
    for (const [key, op] of arrayOps) {
      const current = Array.isArray(row?.[key]) ? row[key] : [];
      out[key] =
        op.__op === "arrayUnion"
          ? current.includes(op.value) ? current : [...current, op.value]
          : current.filter((v) => v !== op.value);
    }
  }
  return out;
}

// ── reads ───────────────────────────────────────────────────────────────
export async function getDoc(ref) {
  let q = supabase.from(ref.table).select("*").eq(pkColumn(ref.table), ref.id);
  if (ref.table !== "users") q = q.eq("userId", ref.userId);
  const { data, error } = await q.maybeSingle();
  if (error) throw error;
  return { exists: () => !!data, data: () => data || undefined, id: ref.id };
}

function buildSelect(ref) {
  let q = supabase.from(ref.table).select("*").eq("userId", ref.userId);
  if (ref._order) q = q.order(ref._order.field, { ascending: ref._order.dir !== "desc" });
  return q;
}

export async function getDocs(ref) {
  const { data, error } = await buildSelect(ref);
  if (error) throw error;
  const rows = data || [];
  const docs = rows.map((row) => ({ id: row[pkColumn(ref.table)], data: () => row }));
  return { docs, empty: docs.length === 0, forEach: (cb) => docs.forEach(cb) };
}

// ── writes ──────────────────────────────────────────────────────────────
export async function setDoc(ref, data, _opts = {}) {
  const payload = await resolvePayload(ref.table, ref.id, data);
  const row = { [pkColumn(ref.table)]: ref.id, ...payload };
  if (ref.table !== "users") row.userId = ref.userId;
  const { error } = await supabase.from(ref.table).upsert(row, { onConflict: onConflictCols(ref.table) });
  if (error) throw error;
}

export async function updateDoc(ref, partial) {
  const payload = await resolvePayload(ref.table, ref.id, partial);
  let q = supabase.from(ref.table).update(payload).eq(pkColumn(ref.table), ref.id);
  if (ref.table !== "users") q = q.eq("userId", ref.userId);
  const { error } = await q;
  if (error) throw error;
}

export async function deleteDoc(ref) {
  let q = supabase.from(ref.table).delete().eq(pkColumn(ref.table), ref.id);
  if (ref.table !== "users") q = q.eq("userId", ref.userId);
  const { error } = await q;
  if (error) throw error;
}

export async function addDoc(ref, data) {
  const id = crypto.randomUUID();
  const payload = await resolvePayload(ref.table, id, data);
  const row = { id, userId: ref.userId, ...payload };
  const { error } = await supabase.from(ref.table).insert(row);
  if (error) throw error;
  return { id };
}

// ── live subscriptions (replaces Firestore's onSnapshot) ────────────────
// Simple "any change -> refetch everything" strategy rather than true
// incremental diffing. Perfectly adequate at this app's scale and far
// less code/risk than reimplementing Firestore's diff semantics.
export function onSnapshot(ref, onNext, onError) {
  let active = true;

  async function load() {
    try {
      const snap = await getDocs(ref);
      if (active) onNext(snap);
    } catch (err) {
      if (active && onError) onError(err);
    }
  }
  load();

  const channelName = `rt:${ref.table}:${ref.userId}:${Math.random().toString(36).slice(2)}`;
  const channel = supabase
    .channel(channelName)
    .on(
      "postgres_changes",
      { event: "*", schema: "public", table: ref.table, filter: `userId=eq.${ref.userId}` },
      load
    )
    .subscribe();

  return () => {
    active = false;
    supabase.removeChannel(channel);
  };
}